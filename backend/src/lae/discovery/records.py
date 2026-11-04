"""Record-level dependency discovery (architecture.md §8).

Records come only from queries executed through `TargetReader`; table- and column-level edges
only say *where* to look. Traversal is downstream only, over CONFIRMED TABLE edges only.

Roles:
- SUBJECT_ROOT: the subject's row.
- REFERENCING_OTHER_SUBJECT: any other row of the subject-root table that was reached (for
  example customer 77 whose ``referrer_id`` is 42). Never traversed further.
- SHARED: after the BFS, a greatest fixpoint: a reached row is SHARED if one of its non-null
  references into a subject-reachable table points at a row outside the subject's set
  (SUBJECT_ROOT + OWNED), or if none of its parents is in that set (descendants reached only
  through SHARED rows).
- OWNED: everything else.

Raw key values live only in local variables of these functions. They are never logged, never
put into exception details and never returned except inside the `RecordGraph` (which keeps
them in memory only) — residual probes return HMACs only.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from lae.audit.hmac_keys import HmacKeyring, normalize
from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    Normalization,
    PiiCategory,
    RecordRole,
    Traceability,
)
from lae.contracts.ports import TargetReader
from lae.contracts.types import (
    ColumnRef,
    DiscoveryLimits,
    KeyTuple,
    KeyValue,
    PolicySet,
    RecordKey,
    RelationshipEdge,
    ResidualProbe,
    SubjectRoot,
    TableRef,
    key_sort_key,
)
from lae.errors import DiscoveryLimitExceeded, SubjectNotFoundError, ValidationError
from lae.lineage import CONFIRMED_ONLY, LineageGraph, RecordDiscoveryResult, RecordGraph

__all__ = [
    "capture_residual_probes",
    "discover_records",
    "probe_normalizations",
    "record_set_fingerprint",
]

_log = logging.getLogger(__name__)

_SEVERITY = {
    Traceability.KEY_PRESERVING: 0,
    Traceability.AGGREGATE: 1,
    Traceability.OPAQUE: 2,
}
_OWNED_ROLES = frozenset({RecordRole.SUBJECT_ROOT, RecordRole.OWNED})


def _key_value(v: Any) -> KeyValue:
    """Normalize a database value into a KeyValue (int | str). Never echoes the value."""
    if isinstance(v, bool):
        raise ValidationError("boolean key values are not supported")
    if isinstance(v, int | str):
        return v
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, Decimal) and v == v.to_integral_value():
        return int(v)
    return str(v)


def _project(row: Mapping[str, Any], columns: Sequence[str]) -> KeyTuple | None:
    """Values of `columns` in `row`, or None if any is NULL (NULL never matches)."""
    values: list[KeyValue] = []
    for c in columns:
        v = row.get(c)
        if v is None:
            return None
        values.append(_key_value(v))
    return tuple(values)


def _chunks(values: Sequence[KeyTuple], size: int) -> Iterable[Sequence[KeyTuple]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _is_traversable(e: RelationshipEdge) -> bool:
    return (
        e.level is LineageLevel.TABLE
        and e.traceability is Traceability.KEY_PRESERVING
        and bool(e.source_columns)
    )


@dataclass(frozen=True, slots=True)
class _RefSpec:
    """A reference from rows of `table` (via `target_columns`) to one row of `source`."""

    source: TableRef
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]


class _Discovery:
    """Mutable state of one discovery run (never shared between runs)."""

    def __init__(
        self,
        graph: LineageGraph,
        root: SubjectRoot,
        reader: TargetReader,
        limits: DiscoveryLimits,
    ) -> None:
        self.graph = graph
        self.catalog = graph.catalog
        self.root = root
        self.reader = reader
        self.limits = limits
        self.rows: dict[RecordKey, list[dict[str, Any]]] = {}
        self.depth: dict[RecordKey, int] = {}
        self.links: list[tuple[RecordKey, RecordKey, UUID]] = []
        self.link_set: set[tuple[RecordKey, RecordKey, UUID]] = set()
        self.parents: dict[RecordKey, set[RecordKey]] = defaultdict(set)
        self.affected: dict[TableRef, Traceability] = {}
        self.warnings: list[str] = []
        self.edge_cache: dict[tuple[UUID, KeyTuple], list[RecordKey]] = {}
        self.reachable = graph.reachable_from(root.table) | {root.table}
        self.downstream: dict[TableRef, list[RelationshipEdge]] = {}
        self.refs: dict[TableRef, list[_RefSpec]] = defaultdict(list)
        self.needed: dict[TableRef, tuple[str, ...]] = {}
        self._prepare()

    # ------------------------------------------------------------------ setup

    def _prepare(self) -> None:
        needed: dict[TableRef, set[str]] = defaultdict(set)
        for table in sorted(self.reachable):
            edges = self.graph.downstream(table, CONFIRMED_ONLY)
            self.downstream[table] = edges
            for e in edges:
                if not _is_traversable(e):
                    continue
                needed[table].update(e.source_columns)
                needed[e.target].update(e.target_columns)
                src_pk = self.catalog.tables[e.source].primary_key
                if e.origin is EdgeOrigin.DECLARED_FK or (
                    src_pk and tuple(e.source_columns) == tuple(src_pk)
                ):
                    spec = _RefSpec(e.source, e.source_columns, e.target_columns)
                    if spec not in self.refs[e.target]:
                        self.refs[e.target].append(spec)
        suggested = [
            e
            for e in self.graph.edges()
            if e.level is LineageLevel.TABLE
            and e.status is not EdgeStatus.CONFIRMED
            and e.source in self.reachable
        ]
        if suggested:
            self.warnings.append(
                f"{len(suggested)} unconfirmed table edge(s) from subject-reachable tables "
                "were not traversed"
            )
        for table in self.catalog.tables:
            info = self.catalog.tables[table]
            self.needed[table] = tuple(sorted(set(info.primary_key) | needed.get(table, set())))
            self.refs[table].sort(key=lambda s: (s.source, s.source_columns, s.target_columns))

    def _mark(self, table: TableRef, traceability: Traceability) -> None:
        old = self.affected.get(table)
        if old is None or _SEVERITY[traceability] > _SEVERITY[old]:
            self.affected[table] = traceability

    def _record_key(
        self, table: TableRef, row: Mapping[str, Any], edge: RelationshipEdge
    ) -> RecordKey | None:
        pk = self.catalog.tables[table].primary_key
        if pk:
            values = _project(row, pk)
            if values is None:
                return None
            return RecordKey(table, tuple(pk), values)
        values = _project(row, edge.target_columns)
        if values is None:  # pragma: no cover - IN (...) never matches NULL
            return None
        return RecordKey(table, tuple(edge.target_columns), values, is_predicate=True)

    def _link(self, parent: RecordKey, child: RecordKey, edge_id: UUID) -> None:
        triple = (parent, child, edge_id)
        if triple not in self.link_set:
            self.link_set.add(triple)
            self.links.append(triple)
            self.parents[child].add(parent)

    # ------------------------------------------------------------------ BFS

    def load_root(self, subject_key: KeyTuple) -> RecordKey:
        table = self.root.table
        info = self.catalog.table(table)
        if not info.primary_key or tuple(self.root.key_columns) != tuple(info.primary_key):
            raise ValidationError(
                "subject root key columns must equal the table's primary key",
                details={"table": table.qualified()},
            )
        if len(subject_key) != len(self.root.key_columns):
            raise ValidationError(
                "subject key has the wrong number of values",
                details={"table": table.qualified(), "expected": len(self.root.key_columns)},
            )
        key_values = tuple(_key_value(v) for v in subject_key)
        rows = self.reader.select_by_columns(
            table, self.root.key_columns, [key_values], self.needed[table]
        )
        if not rows:
            raise SubjectNotFoundError(
                "subject not found in root table", details={"table": table.qualified()}
            )
        key = RecordKey(table, tuple(self.root.key_columns), key_values)
        self.rows[key] = [dict(rows[0])]
        self.depth[key] = 0
        return key

    def _limit(self, name: str, value: int, table: TableRef) -> DiscoveryLimitExceeded:
        return DiscoveryLimitExceeded(
            f"discovery limit {name} exceeded",
            details={"limit": name, "value": value, "table": table.qualified()},
        )

    def _follow(
        self, edge: RelationshipEdge, parents: list[RecordKey], depth: int
    ) -> list[RecordKey]:
        """Fetch children of `parents` over `edge`; return newly discovered traversable keys."""
        by_values: dict[KeyTuple, list[RecordKey]] = defaultdict(list)
        for p in parents:
            for row in self.rows[p]:
                values = _project(row, edge.source_columns)
                if values is not None and p not in by_values[values]:
                    by_values[values].append(p)
        pending = sorted(
            (v for v in by_values if (edge.edge_id, v) not in self.edge_cache), key=key_sort_key
        )
        target = edge.target
        select = tuple(sorted(set(self.needed[target]) | set(edge.target_columns)))
        for batch in _chunks(pending, self.limits.batch_size):
            for v in batch:
                self.edge_cache[(edge.edge_id, v)] = []
            fetched = self.reader.select_by_columns(target, edge.target_columns, batch, select)
            for row in fetched:
                tv = _project(row, edge.target_columns)
                if tv is None or (edge.edge_id, tv) not in self.edge_cache:
                    continue
                child = self._record_key(target, row, edge)
                if child is None:
                    continue
                cached = self.edge_cache[(edge.edge_id, tv)]
                if child not in cached:
                    cached.append(child)
                stored = self.rows.setdefault(child, [])
                plain = dict(row)
                if plain not in stored:
                    stored.append(plain)
        new: list[RecordKey] = []
        for values in sorted(by_values, key=key_sort_key):
            for child in self.edge_cache[(edge.edge_id, values)]:
                is_new = child not in self.depth
                if is_new:
                    if depth + 1 > self.limits.max_depth:
                        raise self._limit("max_depth", self.limits.max_depth, target)
                    self.depth[child] = depth + 1
                    if len(self.depth) > self.limits.max_records:
                        raise self._limit("max_records", self.limits.max_records, target)
                    if target != self.root.table:
                        new.append(child)
                for p in by_values[values]:
                    self._link(p, child, edge.edge_id)
        is_artifact_edge = edge.artifact_id is not None or edge.origin in (
            EdgeOrigin.SQL_LINEAGE,
            EdgeOrigin.REGISTERED_MAPPING,
        )
        if is_artifact_edge and any(self.edge_cache[(edge.edge_id, v)] for v in by_values):
            self._mark(target, Traceability.KEY_PRESERVING)
        return new

    def run(self, root_key: RecordKey) -> None:
        frontier = [root_key]
        depth = 0
        while frontier:
            by_table: dict[TableRef, list[RecordKey]] = defaultdict(list)
            for k in frontier:
                by_table[k.table].append(k)
            nxt: list[RecordKey] = []
            for table in sorted(by_table):
                parents = sorted(by_table[table], key=RecordKey.sort_key)
                for edge in self.downstream.get(table, []):
                    if _is_traversable(edge):
                        nxt.extend(self._follow(edge, parents, depth))
                    elif edge.traceability is Traceability.KEY_PRESERVING:
                        self.warnings.append(
                            f"edge {edge.edge_id} is KEY_PRESERVING without key columns; "
                            "treated as OPAQUE"
                        )
                        self._mark(edge.target, Traceability.OPAQUE)
                    else:
                        self._mark(edge.target, edge.traceability)
            frontier = sorted(set(nxt), key=RecordKey.sort_key)
            depth += 1

    # ------------------------------------------------------------------ roles

    def roles(self, root_key: RecordKey) -> dict[RecordKey, RecordRole]:
        roles: dict[RecordKey, RecordRole] = {}
        for k in self.depth:
            if k == root_key:
                roles[k] = RecordRole.SUBJECT_ROOT
            elif k.table == self.root.table:
                roles[k] = RecordRole.REFERENCING_OTHER_SUBJECT
            else:
                roles[k] = RecordRole.OWNED
        ordered = sorted(self.depth, key=RecordKey.sort_key)
        referenced: dict[TableRef, set[tuple[str, ...]]] = defaultdict(set)
        for specs in self.refs.values():
            for spec in specs:
                referenced[spec.source].add(spec.source_columns)
        changed = True
        while changed:
            changed = False
            index: dict[tuple[TableRef, tuple[str, ...]], set[KeyTuple]] = defaultdict(set)
            for k in ordered:
                if roles[k] not in _OWNED_ROLES:
                    continue
                for spec_cols in referenced.get(k.table, ()):
                    for row in self.rows[k]:
                        v = _project(row, spec_cols)
                        if v is not None:
                            index[(k.table, spec_cols)].add(v)
            for k in ordered:
                if roles[k] is not RecordRole.OWNED:
                    continue
                if self._is_shared(k, roles, index):
                    roles[k] = RecordRole.SHARED
                    changed = True
        return roles

    def _is_shared(
        self,
        key: RecordKey,
        roles: Mapping[RecordKey, RecordRole],
        index: Mapping[tuple[TableRef, tuple[str, ...]], set[KeyTuple]],
    ) -> bool:
        if not any(roles[p] in _OWNED_ROLES for p in self.parents[key]):
            return True
        for spec in self.refs.get(key.table, []):
            if spec.source not in self.reachable:
                continue
            for row in self.rows[key]:
                v = _project(row, spec.target_columns)
                if v is not None and v not in index.get((spec.source, spec.source_columns), ()):
                    return True
        return False


def discover_records(
    graph: LineageGraph,
    root: SubjectRoot,
    subject_key: KeyTuple,
    reader: TargetReader,
    limits: DiscoveryLimits,
    policies: PolicySet | None = None,
) -> RecordDiscoveryResult:
    """BFS downstream from the subject's root row over CONFIRMED TABLE edges.

    `policies` is accepted for contract compatibility; roles do not depend on retention policy
    (the planner applies it). Raises SubjectNotFoundError if the root row does not exist and
    DiscoveryLimitExceeded if max_depth / max_records would be exceeded.
    """
    del policies
    run = _Discovery(graph, root, reader, limits)
    root_key = run.load_root(subject_key)
    run.run(root_key)
    roles = run.roles(root_key)

    rg = RecordGraph()
    rg.add_root(root_key)
    for parent, child, edge_id in run.links:
        role = roles[child]
        if role is RecordRole.SUBJECT_ROOT:
            role = RecordRole.OWNED  # add_dependency keeps the stronger SUBJECT_ROOT role
        rg.add_dependency(parent, child, edge_id, role)

    no_pk = sorted({k.table for k in run.depth if k.is_predicate})
    for t in no_pk:
        run.warnings.append(f"{t.qualified()} has no primary key; rows are addressed by predicate")
    counts: dict[RecordRole, int] = defaultdict(int)
    for r in roles.values():
        counts[r] += 1
    _log.info(
        "record discovery: %d records (%s), %d affected artifacts",
        len(roles),
        ", ".join(f"{r.value}={counts[r]}" for r in sorted(counts)),
        len(run.affected),
    )
    return RecordDiscoveryResult(
        record_graph=rg,
        affected_artifacts=run.affected,
        warnings=tuple(sorted(set(run.warnings))),
    )


def record_set_fingerprint(
    graph: LineageGraph,
    root: SubjectRoot,
    subject_key: KeyTuple,
    reader: TargetReader,
    limits: DiscoveryLimits,
) -> str:
    """Re-run discovery and return the record-set hash (used by the executor drift check)."""
    return discover_records(graph, root, subject_key, reader, limits).record_graph.record_set_hash()


# ---------------------------------------------------------------------- residual probes

_EMAIL_HINTS = ("email", "e_mail", "mail")
_PHONE_HINTS = ("phone", "mobile", "tel", "fax", "msisdn")


def probe_normalizations(column: str, value: str | int) -> tuple[Normalization, ...]:
    """EXACT always; LOWER_TRIM for e-mail-like, DIGITS_ONLY for phone-like columns/values."""
    name = column.lower()
    out = [Normalization.EXACT]
    is_email = any(h in name for h in _EMAIL_HINTS) or (isinstance(value, str) and "@" in value)
    is_phone = any(h in name for h in _PHONE_HINTS)
    if is_email:
        out.append(Normalization.LOWER_TRIM)
    if is_phone:
        out.append(Normalization.DIGITS_ONLY)
    return tuple(out)


def _probe_columns(root: SubjectRoot, policies: PolicySet) -> dict[TableRef, tuple[str, ...]]:
    wanted: dict[TableRef, set[str]] = defaultdict(set)
    wanted[root.table].update(root.identifier_columns)
    for ref, pii in policies.pii.items():
        if not pii.is_confirmed:
            continue
        if pii.category is PiiCategory.DIRECT_IDENTIFIER or pii.use_for_residual_scan:
            wanted[ref.table].add(ref.column)
    return {t: tuple(sorted(c)) for t, c in wanted.items() if c}


def capture_residual_probes(
    root: SubjectRoot,
    subject_key: KeyTuple,
    record_graph: RecordGraph,
    reader: TargetReader,
    policies: PolicySet,
    keyring: HmacKeyring,
    *,
    batch_size: int = 1_000,
) -> list[ResidualProbe]:
    """HMAC digests of the subject's identifier values (root identifier columns plus confirmed
    DIRECT_IDENTIFIER / residual-scan columns of the root and OWNED rows).

    Each value is read once, normalized, digested and dropped. Only `ResidualProbe`s (column,
    normalization, HMAC) leave this function; values are never logged. Sorted and unique.
    """
    columns = _probe_columns(root, policies)
    targets: dict[TableRef, list[RecordKey]] = defaultdict(list)
    for key, attrs in record_graph.nodes():
        if attrs.role in _OWNED_ROLES and key.table in columns:
            targets[key.table].append(key)
    root_key = tuple(_key_value(v) for v in subject_key)
    if not any(k.table == root.table and k.values == root_key for k in targets[root.table]):
        targets[root.table].append(RecordKey(root.table, tuple(root.key_columns), root_key))

    probes: set[ResidualProbe] = set()
    for table in sorted(targets):
        wanted = columns.get(table, ())
        if not wanted:
            continue
        groups: dict[tuple[tuple[str, ...], bool], list[KeyTuple]] = defaultdict(list)
        for k in targets[table]:
            if k.values not in groups[(k.columns, k.is_predicate)]:
                groups[(k.columns, k.is_predicate)].append(k.values)
        found_any = False
        for (key_cols, _is_pred), values in sorted(groups.items()):
            ordered = sorted(values, key=key_sort_key)
            for batch in _chunks(ordered, batch_size):
                for row in reader.select_by_columns(table, key_cols, batch, wanted):
                    found_any = True
                    for col in wanted:
                        raw = row.get(col)
                        if raw is None:
                            continue
                        value: str | int = (
                            raw
                            if isinstance(raw, int | str) and not isinstance(raw, bool)
                            else str(raw)
                        )
                        for norm in probe_normalizations(col, value):
                            if not normalize(value, norm).strip():
                                continue
                            digest = keyring.digest(value, norm)
                            probes.add(ResidualProbe(ColumnRef(table, col), norm, digest))
        if table == root.table and not found_any:
            raise SubjectNotFoundError(
                "subject not found in root table", details={"table": table.qualified()}
            )
    return sorted(probes, key=lambda p: (p.origin, p.normalization.value, p.value_hmac))
