"""Policy-aware erasure planner (architecture.md §10, module_contracts.md §7.1, CR lead-1).

`build_plan` turns a record-level discovery result into an ordered, explainable, hashed
`ErasurePlan`. It never raises for policy conflicts: every problem becomes a `PlanConflict`,
and any unresolved BLOCKING conflict makes the plan blocked.

Step phases (lower runs first):

1. NULLIFY_REFERENCE / ANONYMIZE_ROWS: retained and foreign rows are detached first.
2. DELETE_ROWS: children before parents (`order_deletions`).
3. EXPECT_CASCADE: checks that the database cascaded the expected rows.
4. DELETE_DERIVED_BY_KEY / INVALIDATE_CACHE, in `LineageGraph.artifact_order`.
5. RECOMPUTE_TABLE / REFRESH_MATVIEW, in `LineageGraph.artifact_order`.
6. RETAIN_ROWS, SCHEDULE_EXTERNAL, MANUAL_REVIEW, UNRESOLVED (no DML).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lae.contracts.enums import (
    ArtifactKind,
    ConflictCode,
    ErasureStrategy,
    LineageLevel,
    OnDeleteAction,
    PlanAction,
    RecordRole,
    RefreshStrategy,
    RetentionAction,
    Severity,
    TableKind,
    Traceability,
)
from lae.contracts.types import (
    ArtifactDefinition,
    ColumnRef,
    ErasurePlan,
    KeyTuple,
    PiiColumn,
    PlanConflict,
    PlanItem,
    PolicySet,
    RecordKey,
    RelationshipEdge,
    TableRef,
    key_sort_key,
)
from lae.errors import GraphIntegrityError
from lae.lineage import LineageGraph, RecordDiscoveryResult
from lae.planner.hashing import plan_with_hash
from lae.planner.ordering import order_deletions
from lae.planner.text import definition_token, edge_labels, join_columns, rows

__all__ = ["build_plan", "detect_conflicts", "pseudonym_columns"]

logger = logging.getLogger(__name__)

_SUBJECT_ROLES = frozenset({RecordRole.SUBJECT_ROOT, RecordRole.OWNED})
_RESTRICTING = frozenset({OnDeleteAction.RESTRICT, OnDeleteAction.NO_ACTION})
_BREAKABLE = frozenset(
    {OnDeleteAction.RESTRICT, OnDeleteAction.NO_ACTION, OnDeleteAction.SET_DEFAULT}
)
_LAST_ORDER: dict[PlanAction, int] = {
    PlanAction.RETAIN_ROWS: 0,
    PlanAction.SCHEDULE_EXTERNAL: 1,
    PlanAction.MANUAL_REVIEW: 2,
    PlanAction.UNRESOLVED: 3,
}
_REFRESH_ORDER: dict[PlanAction, int] = {
    PlanAction.DELETE_DERIVED_BY_KEY: 0,
    PlanAction.INVALIDATE_CACHE: 1,
    PlanAction.RECOMPUTE_TABLE: 2,
    PlanAction.REFRESH_MATVIEW: 3,
}


def _sk(k: RecordKey) -> tuple[Any, ...]:
    return k.sort_key()


@dataclass(slots=True)
class _Draft:
    """A plan item before step numbering. `deps` holds indices of other drafts."""

    phase: int
    rank: tuple[Any, ...]
    action: PlanAction
    table: TableRef
    level: LineageLevel
    reason: str
    keys: tuple[RecordKey, ...] = ()
    key_columns: tuple[str, ...] = ()
    record_keys: tuple[KeyTuple, ...] | None = None  # set when keys are projected (external)
    target_columns: tuple[str, ...] = ()
    expected: int | None = None
    edge_ids: set[UUID] = field(default_factory=set)
    deps: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _Detach:
    """One reference from a non-deleted row to a subject row that must be nullified."""

    row: RecordKey
    edge: RelationshipEdge
    purpose: str  # "other_subject" | "retained" | "shared" | "break_cycle"


@dataclass(slots=True)
class _ArtifactNeed:
    key_action: PlanAction | None = None
    key_rows: list[RecordKey] = field(default_factory=list)
    table_action: PlanAction | None = None
    why: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    edge_ids: set[UUID] = field(default_factory=set)


class _Planner:
    def __init__(
        self,
        request_id: UUID,
        discovery: RecordDiscoveryResult,
        lineage: LineageGraph,
        policies: PolicySet,
        artifacts: Sequence[ArtifactDefinition],
        external: Sequence[ArtifactDefinition],
        predicate_row_counts: Mapping[RecordKey, int],
    ) -> None:
        self.request_id = request_id
        self.discovery = discovery
        self.lineage = lineage
        self.catalog = lineage.catalog
        self.policies = policies
        self.predicate_counts = predicate_row_counts
        self.rg = discovery.record_graph
        self.roles: dict[RecordKey, RecordRole] = {}
        self.via: dict[RecordKey, tuple[UUID, ...]] = {}
        for k, a in self.rg.nodes():
            if not self.catalog.has_table(k.table):
                raise GraphIntegrityError(
                    "record graph references a table that is not in the catalog",
                    code="UNKNOWN_RECORD_TABLE",
                    details={"table": k.table.qualified()},
                )
            self.roles[k] = a.role
            self.via[k] = a.via_edge_ids
        self.parents: dict[RecordKey, list[tuple[RecordKey, UUID]]] = defaultdict(list)
        self.children: dict[RecordKey, list[tuple[RecordKey, UUID]]] = defaultdict(list)
        for p, c, eid in self.rg.edges():
            e = self.lineage.edge(eid)
            if e.target != c.table or e.source != p.table:
                raise GraphIntegrityError(
                    "record edge does not match its lineage edge",
                    code="EDGE_MISMATCH",
                    details={"edge_id": str(eid), "table": c.table.qualified()},
                )
            self.parents[c].append((p, eid))
            self.children[p].append((c, eid))

        self.art_by_table: dict[TableRef, ArtifactDefinition] = {}
        ext: dict[UUID, ArtifactDefinition] = {}
        for art in (*artifacts, *external):
            if art.kind is ArtifactKind.EXTERNAL:
                ext[art.artifact_id] = art
            else:
                self.art_by_table.setdefault(art.target, art)
        self.external = sorted(ext.values(), key=lambda a: (a.name, str(a.artifact_id)))

        self.drafts: list[_Draft] = []
        self.conflicts: dict[tuple[Any, ...], PlanConflict] = {}
        # classification
        self.subject_rows: set[RecordKey] = set()
        self.removal: set[RecordKey] = set()
        self.anonymize: set[RecordKey] = set()
        self.retain: set[RecordKey] = set()
        self.referencing: set[RecordKey] = set()
        self.shared: set[RecordKey] = set()
        self.artifact_rows: dict[TableRef, list[RecordKey]] = defaultdict(list)
        self.explicit: set[RecordKey] = set()
        self.expect: set[RecordKey] = set()
        self.cascade_parents: dict[RecordKey, list[tuple[RecordKey, UUID]]] = {}
        self.triggers: dict[RecordKey, frozenset[RecordKey]] = {}
        # record -> draft indices
        self.delete_draft: dict[RecordKey, int] = {}
        self.detach_draft: dict[tuple[RecordKey, UUID], int] = {}
        self.row_drafts: dict[RecordKey, set[int]] = defaultdict(set)

    # ------------------------------------------------------------------ helpers

    def conflict(self, code: ConflictCode, severity: Severity, table: TableRef, msg: str) -> None:
        self.conflicts.setdefault((code, table, msg), PlanConflict(code, severity, table, msg))

    def count(self, keys: Iterable[RecordKey]) -> int:
        return sum(self.predicate_counts.get(k, 1) if k.is_predicate else 1 for k in keys)

    def fk_nullable(self, e: RelationshipEdge) -> bool:
        info = self.catalog.table(e.target)
        return bool(e.target_columns) and all(info.column(c).is_nullable for c in e.target_columns)

    def add(self, d: _Draft) -> int:
        self.drafts.append(d)
        idx = len(self.drafts) - 1
        for k in d.keys:
            self.row_drafts[k].add(idx)
        return idx

    def is_artifact_table(self, t: TableRef) -> bool:
        if t in self.art_by_table:
            return True
        return self.catalog.table(t).kind in (TableKind.VIEW, TableKind.MATERIALIZED_VIEW)

    def via_edges(self, keys: Iterable[RecordKey]) -> list[RelationshipEdge]:
        ids = sorted({eid for k in keys for eid in self.via[k]}, key=str)
        return [self.lineage.edge(eid) for eid in ids]

    # ------------------------------------------------------------------ classification

    def classify(self) -> None:
        root_tables = {k.table for k, r in self.roles.items() if r is RecordRole.SUBJECT_ROOT}
        for k in sorted(self.roles, key=_sk):
            role = self.roles[k]
            if self.is_artifact_table(k.table):
                self.artifact_rows[k.table].append(k)
                continue
            if role is RecordRole.OWNED and k.table in root_tables:
                # Root-table protection: only the subject row itself may be deleted.
                role = RecordRole.REFERENCING_OTHER_SUBJECT
                self.roles[k] = role
            if role in _SUBJECT_ROLES:
                self.subject_rows.add(k)
                action = self.policies.action_for(k.table)
                if action is RetentionAction.DELETE:
                    self.removal.add(k)
                elif action is RetentionAction.ANONYMIZE:
                    self.anonymize.add(k)
                else:
                    self.retain.add(k)
            elif role is RecordRole.REFERENCING_OTHER_SUBJECT:
                self.referencing.add(k)
            else:
                self.shared.add(k)

    def split_cascades(self) -> None:
        for r in self.removal:
            self.cascade_parents[r] = [
                (p, eid)
                for p, eid in self.parents[r]
                if p in self.removal
                and p != r
                and self.lineage.edge(eid).on_delete is OnDeleteAction.CASCADE
            ]
        expect = {r for r in self.removal if self.cascade_parents[r]}
        explicit = self.removal - expect
        covered = set(explicit)
        while True:
            changed = True
            while changed:
                changed = False
                for r in sorted(expect - covered, key=_sk):
                    if any(p in covered for p, _ in self.cascade_parents[r]):
                        covered.add(r)
                        changed = True
            missing = expect - covered
            if not missing:
                break
            # A pure cascade cycle: nothing outside it is deleted explicitly. Delete one row.
            first = min(missing, key=_sk)
            expect.discard(first)
            explicit.add(first)
            covered.add(first)
        self.expect, self.explicit = expect, explicit
        for r in sorted(self.removal, key=_sk):
            self._triggers(r, set())

    def _triggers(self, r: RecordKey, visiting: set[RecordKey]) -> frozenset[RecordKey]:
        if r in self.triggers:
            return self.triggers[r]
        if r in self.explicit:
            self.triggers[r] = frozenset({r})
            return self.triggers[r]
        visiting.add(r)
        out: set[RecordKey] = set()
        for p, _ in self.cascade_parents[r]:
            if p not in visiting:
                out |= self._triggers(p, visiting)
        visiting.discard(r)
        self.triggers[r] = frozenset(out)
        return self.triggers[r]

    # ------------------------------------------------------------------ detaching

    def plan_detaches(self) -> list[_Detach]:
        out: list[_Detach] = []
        for kind, rowset in (
            ("other_subject", self.referencing),
            ("shared", self.shared),
            ("retained", self.anonymize | self.retain),
        ):
            for row in sorted(rowset, key=_sk):
                for p, eid in sorted(self.parents[row], key=lambda x: (_sk(x[0]), str(x[1]))):
                    e = self.lineage.edge(eid)
                    if kind == "other_subject":
                        if p not in self.subject_rows:
                            continue
                    elif p not in self.removal or e.on_delete is None:
                        continue
                    if self.fk_nullable(e):
                        out.append(_Detach(row, e, kind))
                    else:
                        self._detach_conflict(kind, row, e, removed=p in self.removal)
        return out

    def _detach_conflict(
        self, kind: str, row: RecordKey, e: RelationshipEdge, removed: bool
    ) -> None:
        t, label, cols = row.table, edge_labels([e]), join_columns(e.target_columns)
        od = e.on_delete
        if kind == "other_subject":
            if removed and od is OnDeleteAction.CASCADE:
                self.conflict(
                    ConflictCode.CASCADE_DESTROYS_RETAINED,
                    Severity.BLOCKING,
                    t,
                    f"{t}: rows of other subjects reference rows being deleted through NOT NULL "
                    f"column(s) {cols} via {label}; the cascade would delete them",
                )
            else:
                self.conflict(
                    ConflictCode.NOT_NULL_REFERENCE,
                    Severity.BLOCKING,
                    t,
                    f"{t}: rows of other subjects reference the subject through NOT NULL "
                    f"column(s) {cols} via {label}; the reference cannot be nullified",
                )
            return
        if od is OnDeleteAction.CASCADE:
            if kind == "shared":
                return  # reported in the SHARED_RECORD message; a human decides
            self.conflict(
                ConflictCode.CASCADE_DESTROYS_RETAINED,
                Severity.BLOCKING,
                t,
                f"{t}: rows retained by policy {self.policies.action_for(t).value} reference rows "
                f"being deleted through NOT NULL column(s) {cols} via {label}; the cascade would "
                f"destroy them",
            )
        elif od in _RESTRICTING:
            who = (
                "shared rows"
                if kind == "shared"
                else (f"rows retained by policy {self.policies.action_for(t).value}")
            )
            self.conflict(
                ConflictCode.RESTRICT_FROM_RETAINED,
                Severity.BLOCKING,
                t,
                f"{t}: {who} reference rows being deleted through NOT NULL column(s) {cols} via "
                f"{label}; the delete would be rejected",
            )
        else:
            self.conflict(
                ConflictCode.NOT_NULL_REFERENCE,
                Severity.BLOCKING,
                t,
                f"{t}: column(s) {cols} referencing rows being deleted via {label} are NOT NULL "
                f"and cannot be set to NULL",
            )

    # ------------------------------------------------------------------ base drafts

    def draft_nullify(self, detaches: Sequence[_Detach]) -> None:
        groups: dict[tuple[Any, ...], list[_Detach]] = defaultdict(list)
        for d in detaches:
            key = (d.row.table, d.row.columns, d.row.is_predicate, d.edge.target_columns, d.purpose)
            groups[key].append(d)
        why = {
            "other_subject": "rows belong to other subjects and reference the subject",
            "shared": "shared rows reference rows being deleted",
            "retained": "rows retained by policy RETAIN reference rows being deleted",
            "break_cycle": "breaks a reference cycle among rows being deleted",
        }
        for (table, key_cols, _pred, cols, purpose), ds in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], kv[0][3], kv[0][4])
        ):
            keys = tuple(sorted({d.row for d in ds}, key=_sk))
            edges = [d.edge for d in ds]
            n = self.count(keys)
            idx = self.add(
                _Draft(
                    phase=1,
                    rank=(table, 0, cols, purpose, key_cols),
                    action=PlanAction.NULLIFY_REFERENCE,
                    table=table,
                    level=LineageLevel.RECORD,
                    keys=keys,
                    key_columns=key_cols,
                    target_columns=cols,
                    expected=n,
                    edge_ids={e.edge_id for e in edges},
                    reason=(
                        f"NULLIFY_REFERENCE {join_columns(cols)} on {rows(n)} of {table}: "
                        f"{why[purpose]} via {edge_labels(edges)}"
                    ),
                )
            )
            for d in ds:
                self.detach_draft.setdefault((d.row, d.edge.edge_id), idx)

    def pii_targets(self, table: TableRef) -> tuple[list[PiiColumn], list[PiiColumn]]:
        """(confirmed PII columns that can be scrubbed, unconfirmed PII columns) of `table`."""
        info = self.catalog.table(table)
        scrub: list[PiiColumn] = []
        unconfirmed: list[PiiColumn] = []
        remaining: list[str] = []
        for ref in sorted(self.policies.pii):
            pc = self.policies.pii[ref]
            if ref.table != table or not info.has_column(ref.column):
                continue
            if not pc.is_confirmed:
                unconfirmed.append(pc)
            elif ref.column in info.primary_key or (
                pc.strategy is ErasureStrategy.NULLIFY and not info.column(ref.column).is_nullable
            ):
                remaining.append(ref.column)
            else:
                scrub.append(pc)
        if remaining:
            self.conflict(
                ConflictCode.RETAINED_WITH_PII,
                Severity.WARNING,
                table,
                f"{table}: PII column(s) {join_columns(remaining)} of anonymized rows cannot be "
                f"scrubbed (primary key or NOT NULL) and keep their values",
            )
        if any(p.strategy is ErasureStrategy.PSEUDONYMIZE for p in scrub):
            cols = [p.column.column for p in scrub if p.strategy is ErasureStrategy.PSEUDONYMIZE]
            self.conflict(
                ConflictCode.RETAINED_WITH_PII,
                Severity.WARNING,
                table,
                f"{table}: column(s) {join_columns(cols)} are pseudonymized, not anonymized; "
                f"retained rows may still be re-identifiable",
            )
        if unconfirmed and table in {k.table for k in self.anonymize}:
            self.conflict(
                ConflictCode.UNCONFIRMED_PII,
                Severity.WARNING,
                table,
                f"{table}: suggested PII column(s) "
                f"{join_columns(p.column.column for p in unconfirmed)} are unconfirmed and will "
                f"not be anonymized",
            )
        return scrub, unconfirmed

    def draft_anonymize(self, detaches: Sequence[_Detach]) -> None:
        fk_cols: dict[RecordKey, dict[str, RelationshipEdge]] = defaultdict(dict)
        for d in detaches:
            if d.row in self.anonymize:
                for c in d.edge.target_columns:
                    fk_cols[d.row].setdefault(c, d.edge)
        by_table: dict[TableRef, list[RecordKey]] = defaultdict(list)
        for r in sorted(self.anonymize, key=_sk):
            by_table[r.table].append(r)
        for table in sorted(by_table):
            scrub, _ = self.pii_targets(table)
            strategies = {p.column.column: p.strategy for p in scrub}
            groups: dict[tuple[Any, ...], list[RecordKey]] = defaultdict(list)
            for r in by_table[table]:
                cols = tuple(sorted(set(strategies) | set(fk_cols[r])))
                groups[(r.columns, r.is_predicate, cols)].append(r)
            for (key_cols, _pred, cols), keys in sorted(groups.items()):
                n = self.count(keys)
                if not cols:
                    self.draft_retain(
                        keys, f"policy ANONYMIZE, but {table} has no confirmed PII column to scrub"
                    )
                    continue
                fks = {c: e for r in keys for c, e in fk_cols[r].items()}
                parts = []
                for c in cols:
                    if c in fks:
                        parts.append(f"{c} (NULLIFY: references rows being deleted)")
                    else:
                        parts.append(f"{c} ({strategies[c].value})")
                edges = self.via_edges(keys) + list(fks.values())
                idx = self.add(
                    _Draft(
                        phase=1,
                        rank=(table, 1, cols, "", key_cols),
                        action=PlanAction.ANONYMIZE_ROWS,
                        table=table,
                        level=LineageLevel.RECORD,
                        keys=tuple(keys),
                        key_columns=key_cols,
                        target_columns=cols,
                        expected=n,
                        edge_ids={e.edge_id for e in edges},
                        reason=(
                            f"ANONYMIZE {rows(n)} of {table}: retention policy ANONYMIZE; "
                            f"columns {', '.join(parts)}"
                        ),
                    )
                )
                for r in keys:
                    for e in fk_cols[r].values():
                        self.detach_draft.setdefault((r, e.edge_id), idx)

    def draft_retain(self, keys: Sequence[RecordKey], why: str) -> int:
        table = keys[0].table
        n = self.count(keys)
        idx = self.add(
            _Draft(
                phase=6,
                rank=(_LAST_ORDER[PlanAction.RETAIN_ROWS], table, keys[0].columns),
                action=PlanAction.RETAIN_ROWS,
                table=table,
                level=LineageLevel.RECORD,
                keys=tuple(keys),
                key_columns=keys[0].columns,
                expected=n,
                edge_ids={e.edge_id for e in self.via_edges(keys)},
                reason=f"RETAIN {rows(n)} of {table}: {why}; rows are kept and not modified",
            )
        )
        d = self.drafts[idx]
        for k in keys:
            d.deps |= {i for (row, _), i in self.detach_draft.items() if row == k}
        return idx

    def draft_retained_rows(self) -> None:
        by: dict[tuple[Any, ...], list[RecordKey]] = defaultdict(list)
        for r in sorted(self.retain, key=_sk):
            by[(r.table, r.columns, r.is_predicate)].append(r)
        for (table, _c, _p), keys in sorted(by.items()):
            self.draft_retain(keys, "retention policy RETAIN (legal hold)")
            pii = [ref.column for ref in sorted(self.policies.pii) if ref.table == table]
            if pii:
                self.conflict(
                    ConflictCode.RETAINED_WITH_PII,
                    Severity.WARNING,
                    table,
                    f"{table}: rows retained under legal hold keep PII column(s) "
                    f"{join_columns(pii)}",
                )

    def draft_shared(self) -> None:
        by: dict[tuple[Any, ...], list[RecordKey]] = defaultdict(list)
        for r in sorted(self.shared, key=_sk):
            by[(r.table, r.columns, r.is_predicate)].append(r)
        for (table, key_cols, _p), keys in sorted(by.items()):
            self.draft_manual(table, key_cols, keys)

    def draft_manual(
        self, table: TableRef, key_cols: tuple[str, ...], keys: list[RecordKey]
    ) -> None:
        n = self.count(keys)
        edges = self.via_edges(keys)
        cascades = any(
            p in self.removal and self.lineage.edge(eid).on_delete is OnDeleteAction.CASCADE
            for k in keys
            for p, eid in self.parents[k]
            if (k, eid) not in self.detach_draft
        )
        note = "; deleting their parent rows cascades to them" if cascades else ""
        self.add(
            _Draft(
                phase=6,
                rank=(_LAST_ORDER[PlanAction.MANUAL_REVIEW], table, key_cols),
                action=PlanAction.MANUAL_REVIEW,
                table=table,
                level=LineageLevel.RECORD,
                keys=tuple(keys),
                key_columns=key_cols,
                expected=n,
                edge_ids={e.edge_id for e in edges},
                reason=(
                    f"MANUAL_REVIEW {rows(n)} of {table}: shared with another subject "
                    f"(reached via {edge_labels(edges)}){note}"
                ),
            )
        )
        self.conflict(
            ConflictCode.SHARED_RECORD,
            Severity.BLOCKING,
            table,
            f"{table}: {rows(n)} shared with another subject; a human must decide{note}",
        )

    def delete_constraints(self) -> list[tuple[RecordKey, RecordKey, UUID]]:
        """(u, v, edge_id): v must be removed before u is deleted (both are removed rows)."""
        constraints: list[tuple[RecordKey, RecordKey, UUID]] = []
        for u, v, eid in self.rg.edges():
            if u not in self.removal or v not in self.removal or u == v:
                continue
            e = self.lineage.edge(eid)
            if e.on_delete is None:
                continue
            if e.on_delete is OnDeleteAction.CASCADE and v in self.expect:
                continue  # trigger edge: v disappears with u
            if e.on_delete is OnDeleteAction.SET_NULL and self.fk_nullable(e):
                continue  # the database nulls the reference; any order works
            constraints.append((u, v, eid))
        return constraints

    def plan_deletes(self) -> None:
        def nullable(eid: UUID) -> bool:
            e = self.lineage.edge(eid)
            return e.on_delete in _BREAKABLE and self.fk_nullable(e)

        constraints = self.delete_constraints()
        ordered = order_deletions(self.explicit, self.triggers, constraints, nullable)
        promote = {v for _, v, _ in constraints if v in self.expect}
        if ordered.cycle is not None and promote:
            # Relying on a cascade can force an impossible order (the cascading row must be gone
            # before another parent is deleted). Delete those rows explicitly and retry.
            self.expect -= promote
            self.explicit |= promote
            self.triggers = {}
            for r in sorted(self.removal, key=_sk):
                self._triggers(r, set())
            constraints = self.delete_constraints()
            ordered = order_deletions(self.explicit, self.triggers, constraints, nullable)
        if ordered.cycle is not None:
            tables = ordered.cycle.details.get("tables", [])
            first = TableRef.parse(tables[0]) if tables else min(r.table for r in self.explicit)
            self.conflict(
                ConflictCode.UNBREAKABLE_CYCLE,
                Severity.BLOCKING,
                first,
                f"rows of {', '.join(tables)} reference each other in a cycle "
                f"({ordered.cycle.details.get('record_count', 0)} rows) with no nullable "
                f"reference to break it",
            )
        self.draft_nullify(
            [_Detach(v, self.lineage.edge(eid), "break_cycle") for _, v, eid in ordered.breaks]
        )

        for pos, batch in enumerate(ordered.batches):
            parts: dict[tuple[Any, ...], list[RecordKey]] = defaultdict(list)
            for r in batch:
                parts[(r.columns, r.is_predicate)].append(r)
            for (key_cols, pred), keys in sorted(parts.items()):
                self.draft_delete(pos, key_cols, pred, keys)

        # depends_on among delete batches
        for earlier, later in ordered.before:
            a, b = self.delete_draft[later], self.delete_draft[earlier]
            if a != b:
                self.drafts[a].deps.add(b)
        for (row, eid), idx in self.detach_draft.items():
            for p, peid in self.parents[row]:
                if peid == eid:
                    for trig in self.triggers.get(p, frozenset()):
                        self.drafts[self.delete_draft[trig]].deps.add(idx)
        self.draft_expect()

    def draft_delete(
        self, pos: int, key_cols: tuple[str, ...], pred: bool, keys: list[RecordKey]
    ) -> None:
        table = keys[0].table
        n = self.count(keys)
        roots = [k for k in keys if self.roles[k] is RecordRole.SUBJECT_ROOT]
        edges = self.via_edges(k for k in keys if k not in roots)
        why: list[str] = []
        if roots:
            why.append("subject root")
        if edges:
            why.append(f"owned by subject via {edge_labels(edges)}")
        if pred:
            why.append(
                f"matched by predicate on ({join_columns(key_cols)}) because {table} has no "
                f"primary key"
            )
        idx = self.add(
            _Draft(
                phase=2,
                rank=(pos, key_cols, pred),
                action=PlanAction.DELETE_ROWS,
                table=table,
                level=LineageLevel.RECORD,
                keys=tuple(keys),
                key_columns=key_cols,
                expected=n,
                edge_ids={e.edge_id for e in edges},
                reason=f"DELETE {rows(n)} of {table}: {'; '.join(why)}",
            )
        )
        for k in keys:
            self.delete_draft[k] = idx

    def draft_expect(self) -> None:
        by: dict[tuple[Any, ...], list[RecordKey]] = defaultdict(list)
        for r in sorted(self.expect, key=_sk):
            by[(r.table, r.columns, r.is_predicate)].append(r)
        for (table, key_cols, pred), keys in sorted(by.items()):
            n = self.count(keys)
            edges = [self.lineage.edge(eid) for k in keys for _, eid in self.cascade_parents[k]]
            parents = sorted({e.source.qualified() for e in edges})
            deps = {self.delete_draft[t] for k in keys for t in self.triggers[k]}
            suffix = f"; matched by predicate on ({join_columns(key_cols)})" if pred else ""
            self.add(
                _Draft(
                    phase=3,
                    rank=(table, key_cols, pred),
                    action=PlanAction.EXPECT_CASCADE,
                    table=table,
                    level=LineageLevel.RECORD,
                    keys=tuple(keys),
                    key_columns=key_cols,
                    expected=n,
                    edge_ids={e.edge_id for e in edges},
                    deps=deps,
                    reason=(
                        f"EXPECT_CASCADE {rows(n)} of {table}: removed by {edge_labels(edges)} "
                        f"when the referenced rows of {', '.join(parents)} are deleted{suffix}"
                    ),
                )
            )

    # ------------------------------------------------------------------ artifacts

    def _maintain_action(self, t: TableRef) -> PlanAction | None:
        """REFRESH_MATVIEW / RECOMPUTE_TABLE if `t` can be rebuilt as a whole, else None."""
        art = self.art_by_table.get(t)
        kind = self.catalog.table(t).kind
        if kind is TableKind.MATERIALIZED_VIEW or (
            art and art.kind is ArtifactKind.MATERIALIZED_VIEW
        ):
            return PlanAction.REFRESH_MATVIEW
        if art is not None and art.refresh_strategy is RefreshStrategy.RECOMPUTE_SQL:
            return PlanAction.RECOMPUTE_TABLE if art.definition_sql else None
        return None

    def _is_view(self, t: TableRef) -> bool:
        art = self.art_by_table.get(t)
        return self.catalog.table(t).kind is TableKind.VIEW or (
            art is not None and art.kind is ArtifactKind.VIEW
        )

    def plan_artifacts(self) -> None:
        needs: dict[TableRef, _ArtifactNeed] = defaultdict(_ArtifactNeed)
        for t in sorted(self.artifact_rows):
            keys = self.artifact_rows[t]
            if self._is_view(t):
                continue  # views store nothing; they reflect the base tables
            need = needs[t]
            need.edge_ids |= {e.edge_id for e in self.via_edges(keys)}
            maint = self._maintain_action(t)
            art = self.art_by_table.get(t)
            if maint is not None:
                need.table_action = maint
                need.why.append(f"it holds {rows(self.count(keys))} derived from the subject")
                continue
            key_action: PlanAction | None = None
            if art is not None and (
                art.kind is ArtifactKind.CACHE
                or art.refresh_strategy is RefreshStrategy.INVALIDATE_BY_KEY
            ):
                key_action = PlanAction.INVALIDATE_CACHE
            elif art is not None and art.refresh_strategy is RefreshStrategy.DELETE_BY_KEY:
                key_action = PlanAction.DELETE_DERIVED_BY_KEY
            if key_action is None:
                need.unresolved.append(
                    f"it holds {rows(self.count(keys))} derived from the subject but has no "
                    f"usable refresh strategy"
                )
                continue
            owned = [k for k in keys if self.roles[k] in _SUBJECT_ROLES]
            other = [k for k in keys if self.roles[k] not in _SUBJECT_ROLES]
            if other:
                self.draft_manual(t, other[0].columns, other)
            if owned:
                need.key_action = key_action
                need.key_rows = owned

        for t, trace in self.discovery.affected_artifacts.items():
            if not self.catalog.has_table(t):
                self.draft_unresolved(t, [f"affected artifact {t} is not in the catalog"], set())
                continue
            if self._is_view(t):
                continue
            need = needs[t]
            need.edge_ids |= {e.edge_id for e in self.lineage.upstream(t)}
            if trace is Traceability.OPAQUE:
                need.unresolved.append("its lineage is OPAQUE, so affected rows are unknown")
                continue
            maint = self._maintain_action(t)
            if maint is not None:
                need.table_action = maint
                need.why.append(f"{trace.value} lineage from data of the subject")
            elif trace is Traceability.AGGREGATE:
                need.unresolved.append(
                    "it aggregates data of the subject but cannot be refreshed or recomputed"
                )

        # Propagate: artifacts downstream of a rebuilt artifact or of a modified base table.
        modified = {d.table for d in self.drafts if d.phase in (1, 2, 3)}
        sources = sorted(modified | {t for t, n in needs.items() if n.table_action or n.key_action})
        propagated: set[TableRef] = set()
        for src in sources:
            for t in sorted(self.lineage.reachable_from(src)):
                if t == src or self._is_view(t):
                    continue
                maint = self._maintain_action(t)
                if maint is None:
                    continue
                need = needs[t]
                if need.table_action is None:
                    need.table_action = maint
                    propagated.add(t)
                if t in propagated:
                    need.why.append(f"it is derived from {src}, which this plan changes")
                need.edge_ids |= {e.edge_id for e in self.lineage.upstream(t)}

        derived = sorted(t for t, n in needs.items() if n.key_action or n.table_action)
        try:
            order = self.lineage.artifact_order(derived)
        except GraphIntegrityError as exc:
            order = derived
            self.conflict(
                ConflictCode.UNBREAKABLE_CYCLE,
                Severity.BLOCKING,
                derived[0],
                f"derived artifacts {', '.join(exc.details.get('tables', []))} depend on each "
                f"other cyclically; no refresh order exists",
            )
        pos = {t: i for i, t in enumerate(order)}
        for t in sorted(needs):
            need = needs[t]
            if need.key_action is not None:
                self.draft_key_derived(t, need, pos[t])
            if need.table_action is not None:
                self.draft_rebuild(t, need, pos[t])
            if need.unresolved:
                self.draft_unresolved(t, need.unresolved, need.edge_ids)

    def draft_key_derived(self, t: TableRef, need: _ArtifactNeed, pos: int) -> None:
        assert need.key_action is not None
        art = self.art_by_table[t]
        groups: dict[tuple[Any, ...], list[RecordKey]] = defaultdict(list)
        for k in need.key_rows:
            groups[(k.columns, k.is_predicate)].append(k)
        for (key_cols, _p), keys in sorted(groups.items()):
            n = self.count(keys)
            edges = self.via_edges(keys)
            verb = (
                "DELETE_DERIVED_BY_KEY"
                if need.key_action is PlanAction.DELETE_DERIVED_BY_KEY
                else "INVALIDATE_CACHE"
            )
            self.add(
                _Draft(
                    phase=4,
                    rank=(pos, _REFRESH_ORDER[need.key_action], key_cols),
                    action=need.key_action,
                    table=t,
                    level=LineageLevel.RECORD,
                    keys=tuple(keys),
                    key_columns=key_cols,
                    expected=n,
                    edge_ids={e.edge_id for e in edges},
                    reason=(
                        f"{verb} {rows(n)} of {t}: derived artifact {art.name} "
                        f"({art.kind.value}) keyed by the subject via {edge_labels(edges)}"
                    ),
                )
            )

    def draft_rebuild(self, t: TableRef, need: _ArtifactNeed, pos: int) -> None:
        action = need.table_action
        assert action is not None
        why = "; ".join(dict.fromkeys(need.why))
        if action is PlanAction.RECOMPUTE_TABLE:
            sql = self.art_by_table[t].definition_sql
            assert sql is not None
            reason = f"RECOMPUTE_TABLE {t}: {why}; {definition_token(sql)}"
        else:
            reason = f"REFRESH_MATVIEW {t}: {why}"
        self.add(
            _Draft(
                phase=5,
                rank=(pos, _REFRESH_ORDER[action]),
                action=action,
                table=t,
                level=LineageLevel.TABLE,
                edge_ids=set(need.edge_ids),
                reason=reason,
            )
        )

    def draft_unresolved(self, t: TableRef, why: Sequence[str], edge_ids: set[UUID]) -> None:
        text = "; ".join(dict.fromkeys(why))
        self.add(
            _Draft(
                phase=6,
                rank=(_LAST_ORDER[PlanAction.UNRESOLVED], t, text),
                action=PlanAction.UNRESOLVED,
                table=t,
                level=LineageLevel.TABLE,
                edge_ids=set(edge_ids),
                reason=f"UNRESOLVED {t}: {text}",
            )
        )
        self.conflict(
            ConflictCode.OPAQUE_ARTIFACT,
            Severity.BLOCKING,
            t,
            f"{t}: {text}; it cannot be erased or verified automatically",
        )

    def plan_external(self) -> None:
        for art in self.external:
            for km in art.key_mappings:
                src_rows = [
                    k
                    for k in sorted(self.subject_rows, key=_sk)
                    if k.table == km.source and not k.is_predicate
                ]
                if not src_rows:
                    continue
                projectable = all(c in src_rows[0].columns for c in km.source_columns)
                values: tuple[KeyTuple, ...] = ()
                if projectable and km.source_columns:
                    idx = [src_rows[0].columns.index(c) for c in km.source_columns]
                    values = tuple(
                        sorted(
                            {tuple(k.values[i] for i in idx) for k in src_rows}, key=key_sort_key
                        )
                    )
                level = LineageLevel.RECORD if values else LineageLevel.TABLE
                mapping = f"{km.source}({join_columns(km.source_columns)})"
                if art.refresh_strategy is RefreshStrategy.EXTERNAL_TASK:
                    self.add(
                        _Draft(
                            phase=6,
                            rank=(_LAST_ORDER[PlanAction.SCHEDULE_EXTERNAL], art.target, mapping),
                            action=PlanAction.SCHEDULE_EXTERNAL,
                            table=art.target,
                            level=level,
                            key_columns=km.target_columns if values else (),
                            record_keys=values,
                            expected=len(values) if values else None,
                            reason=(
                                f"SCHEDULE_EXTERNAL {art.name} ({art.target}): external artifact "
                                f"keyed by {mapping} for {rows(len(src_rows))} of the subject; "
                                f"handled outside the database and not verifiable in it"
                            ),
                        )
                    )
                    self.conflict(
                        ConflictCode.EXTERNAL_ARTIFACT,
                        Severity.WARNING,
                        art.target,
                        f"{art.target}: external artifact {art.name} is erased by a scheduled "
                        f"task and cannot be verified in the database",
                    )
                else:
                    self.add(
                        _Draft(
                            phase=6,
                            rank=(_LAST_ORDER[PlanAction.UNRESOLVED], art.target, mapping),
                            action=PlanAction.UNRESOLVED,
                            table=art.target,
                            level=level,
                            key_columns=km.target_columns if values else (),
                            record_keys=values,
                            expected=len(values) if values else None,
                            reason=(
                                f"UNRESOLVED {art.name} ({art.target}): external artifact keyed by "
                                f"{mapping} for {rows(len(src_rows))} of the subject has no "
                                f"erasure handler"
                            ),
                        )
                    )
                    self.conflict(
                        ConflictCode.OPAQUE_ARTIFACT,
                        Severity.BLOCKING,
                        art.target,
                        f"{art.target}: external artifact {art.name} has no erasure handler and "
                        f"is not inspectable",
                    )

    def predicate_warnings(self) -> None:
        tables: dict[TableRef, list[RecordKey]] = defaultdict(list)
        for d in self.drafts:
            for k in d.keys:
                if k.is_predicate:
                    tables[k.table].append(k)
        for t in sorted(tables):
            keys = set(tables[t])
            cols = sorted({c for k in keys for c in k.columns})
            self.conflict(
                ConflictCode.NO_PRIMARY_KEY,
                Severity.WARNING,
                t,
                f"{t} has no primary key; {rows(self.count(keys))} matched by predicate on "
                f"({join_columns(cols)}), so per-row evidence is weaker",
            )

    # ------------------------------------------------------------------ assembly

    def derived_deps(self) -> None:
        reach: dict[TableRef, set[TableRef]] = {}
        for i, d in enumerate(self.drafts):
            if d.phase not in (4, 5):
                continue
            for j, other in enumerate(self.drafts):
                if (
                    j == i
                    or other.phase > d.phase
                    or (other.phase == d.phase and other.rank >= d.rank)
                ):
                    continue
                if other.phase in (4, 5) and other.table == d.table:
                    d.deps.add(j)
                    continue
                if other.table not in reach:
                    reach[other.table] = (
                        self.lineage.reachable_from(other.table)
                        if self.catalog.has_table(other.table)
                        else set()
                    )
                if d.table in reach[other.table]:
                    d.deps.add(j)
        for d in self.drafts:
            if d.action is PlanAction.SCHEDULE_EXTERNAL:
                src = {
                    km.source for a in self.external for km in a.key_mappings if a.target == d.table
                }
                d.deps |= {j for j, o in enumerate(self.drafts) if o.phase <= 3 and o.table in src}

    def assemble(self, plan_id: UUID) -> ErasurePlan:
        order = sorted(
            range(len(self.drafts)),
            key=lambda i: (self.drafts[i].phase, self.drafts[i].rank, self.drafts[i].reason),
        )
        step_of = {idx: n + 1 for n, idx in enumerate(order)}
        items: list[PlanItem] = []
        for idx in order:
            d = self.drafts[idx]
            step = step_of[idx]
            record_keys = (
                d.record_keys if d.record_keys is not None else tuple(k.values for k in d.keys)
            )
            items.append(
                PlanItem(
                    step_order=step,
                    action=d.action,
                    table=d.table,
                    lineage_level=d.level,
                    key_columns=d.key_columns,
                    record_keys=record_keys,
                    target_columns=d.target_columns,
                    expected_row_count=d.expected,
                    depends_on=tuple(sorted({step_of[j] for j in d.deps if step_of[j] < step})),
                    reason_edge_ids=tuple(sorted(d.edge_ids, key=str)),
                    reason=d.reason,
                )
            )
        severity_rank = {Severity.BLOCKING: 0, Severity.WARNING: 1}
        conflicts = tuple(
            sorted(
                self.conflicts.values(),
                key=lambda c: (severity_rank[c.severity], c.code.value, c.table, c.message),
            )
        )
        plan = ErasurePlan(
            plan_id=plan_id,
            request_id=self.request_id,
            schema_fingerprint=self.catalog.fingerprint,
            record_set_hash=self.rg.record_set_hash(),
            items=tuple(items),
            conflicts=conflicts,
            plan_hash="",
        )
        return plan_with_hash(plan)

    def run(self, plan_id: UUID) -> ErasurePlan:
        self.classify()
        self.split_cascades()
        detaches = self.plan_detaches()
        self.draft_nullify([d for d in detaches if d.row not in self.anonymize])
        self.draft_anonymize(detaches)
        self.plan_deletes()
        self.plan_artifacts()
        self.draft_retained_rows()
        self.draft_shared()
        self.plan_external()
        self.predicate_warnings()
        self.derived_deps()
        plan = self.assemble(plan_id)
        logger.info(
            "built plan %s: %d items, %d conflicts (%d blocking)",
            plan_id,
            len(plan.items),
            len(plan.conflicts),
            sum(1 for c in plan.conflicts if c.severity is Severity.BLOCKING),
        )
        return plan


def build_plan(
    request_id: UUID,
    discovery: RecordDiscoveryResult,
    lineage: LineageGraph,
    policies: PolicySet,
    artifacts: Sequence[ArtifactDefinition],
    external: Sequence[ArtifactDefinition] = (),
    *,
    plan_id: UUID,
    predicate_row_counts: Mapping[RecordKey, int] | None = None,
) -> ErasurePlan:
    """Build the ordered, hashed erasure plan for one discovery result.

    `external` lists EXTERNAL artifacts (they may also be passed in `artifacts`).
    `predicate_row_counts` gives the number of rows each predicate key (table without a PK)
    matches; without it a predicate counts as one row (CR erasure-engine-engineer-1 §1).

    Never raises for policy conflicts. Raises GraphIntegrityError only if the inputs are
    internally inconsistent (a record edge that does not match its lineage edge, a record in
    a table that is not in the catalog, an unknown edge id).
    """
    planner = _Planner(
        request_id,
        discovery,
        lineage,
        policies,
        artifacts,
        external,
        predicate_row_counts or {},
    )
    return planner.run(plan_id)


def detect_conflicts(
    discovery: RecordDiscoveryResult,
    lineage: LineageGraph,
    policies: PolicySet,
    artifacts: Sequence[ArtifactDefinition],
    external: Sequence[ArtifactDefinition] = (),
) -> list[PlanConflict]:
    """The conflicts `build_plan` would report for these inputs."""
    nil = UUID(int=0)
    return list(
        build_plan(nil, discovery, lineage, policies, artifacts, external, plan_id=nil).conflicts
    )


def pseudonym_columns(
    plan: ErasurePlan, policies: PolicySet, lineage: LineageGraph
) -> frozenset[ColumnRef]:
    """Columns the executor must PSEUDONYMIZE (all other ANONYMIZE targets are set to NULL).

    A target column is pseudonymized if its confirmed PII strategy is PSEUDONYMIZE and it is not
    a foreign-key column of one of the item's reason edges (a pseudonym would break the FK).
    """
    out: set[ColumnRef] = set()
    for item in plan.items:
        if item.action is not PlanAction.ANONYMIZE_ROWS:
            continue
        fk_cols = {
            c
            for eid in item.reason_edge_ids
            for e in (lineage.edge(eid),)
            if e.on_delete is not None and e.target == item.table
            for c in e.target_columns
        }
        for col in item.target_columns:
            ref = ColumnRef(item.table, col)
            pc = policies.pii.get(ref)
            if (
                pc is not None
                and pc.is_confirmed
                and pc.strategy is ErasureStrategy.PSEUDONYMIZE
                and col not in fk_cols
            ):
                out.add(ref)
    return frozenset(out)
