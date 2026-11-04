"""Inferred (undeclared) relationships (architecture.md §6, last paragraph).

Heuristic: a child column named ``<p>_id`` (or ``email`` / ``<p>_email``), a type compatible with
a candidate parent column, and a sampled value containment of at least `threshold`. The result is
always ``origin=INFERRED``, ``status=SUGGESTED`` with a ``confidence`` < 1. Suggested edges are
never traversed until a human confirms them. Only counts are recorded; sampled values never
leave the reader call and are never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from lae.contracts.enums import (
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    TableKind,
    Traceability,
    Transform,
)
from lae.contracts.ports import CatalogReader
from lae.contracts.types import (
    CatalogSnapshot,
    ColumnInfo,
    ColumnRef,
    RelationshipEdge,
    TableInfo,
    TableRef,
)
from lae.discovery._ids import NS_INFERRED, stable_id

__all__ = ["candidate_pairs", "infer_relationships"]

_log = logging.getLogger(__name__)
_MAX_CONFIDENCE = Decimal("0.999")

_INTEGER = ("smallint", "integer", "bigint", "int", "serial", "bigserial")
_TEXT = ("text", "character varying", "varchar", "character", "char", "citext")


def _type_family(data_type: str) -> str:
    d = data_type.lower()
    if d.startswith(_INTEGER):
        return "integer"
    if d.startswith(_TEXT):
        return "text"
    if d.startswith("uuid"):
        return "uuid"
    if d.startswith(("numeric", "decimal")):
        return "numeric"
    return d


@dataclass(frozen=True, slots=True, order=True)
class _Candidate:
    child: ColumnRef
    parent: ColumnRef
    pattern: str
    weight: Decimal


def _parents_named(stem: str, tables: list[TableInfo]) -> list[tuple[TableInfo, Decimal]]:
    """Tables matching `stem` exactly (weight 1), by plural (0.98) or by a suffix of it (0.95)."""
    out: list[tuple[TableInfo, Decimal]] = []
    parts = stem.split("_")
    suffixes = ["_".join(parts[i:]) for i in range(1, len(parts))]
    for t in tables:
        n = t.ref.name.lower()
        if n == stem:
            out.append((t, Decimal(1)))
        elif n in (stem + "s", stem + "es"):
            out.append((t, Decimal("0.98")))
        elif n in suffixes or any(n in (s + "s", s + "es") for s in suffixes):
            out.append((t, Decimal("0.95")))
    return out


def candidate_pairs(
    catalog: CatalogSnapshot, exclude: Iterable[RelationshipEdge] = ()
) -> list[_Candidate]:
    """Name/type candidates (child column, parent column), sorted. No data is read."""
    tables = [catalog.tables[r] for r in sorted(catalog.tables)]
    base = [t for t in tables if t.kind is TableKind.TABLE]
    covered: set[tuple[ColumnRef, ColumnRef]] = set()
    declared_children: set[ColumnRef] = set()
    for e in exclude:
        for sc, tc in zip(e.source_columns, e.target_columns, strict=True):
            covered.add((ColumnRef(e.target, tc), ColumnRef(e.source, sc)))
            if e.origin is EdgeOrigin.DECLARED_FK:
                declared_children.add(ColumnRef(e.target, tc))
    out: set[_Candidate] = set()
    for child in base:
        for col in child.columns:
            cref = ColumnRef(child.ref, col.name)
            if cref in declared_children:
                continue
            name = col.name.lower()
            found: list[tuple[ColumnInfo, TableInfo, str, Decimal]] = []
            if name.endswith("_id") and len(name) > 3:
                for parent, weight in _parents_named(name[:-3], base):
                    if len(parent.primary_key) == 1:
                        found.append(
                            (parent.column(parent.primary_key[0]), parent, "name_id", weight)
                        )
            if name == "email" or name.endswith("_email"):
                stem = name[: -len("_email")] if name != "email" else ""
                for parent in base:
                    if parent.ref == child.ref or not parent.has_column("email"):
                        continue
                    if not parent.primary_key:
                        continue
                    named = stem and parent.ref.name.lower() in (stem, stem + "s")
                    weight = Decimal("0.95") if named else Decimal("0.9")
                    found.append((parent.column("email"), parent, "email", weight))
            for pcol, parent, pattern, weight in found:
                if parent.ref == child.ref and pcol.name == col.name:
                    continue
                if _type_family(pcol.data_type) != _type_family(col.data_type):
                    continue
                pref = ColumnRef(parent.ref, pcol.name)
                if (cref, pref) in covered:
                    continue
                out.add(_Candidate(cref, pref, pattern, weight))
    return sorted(out)


def _edge(
    cand: _Candidate, level: LineageLevel, confidence: float, sampled: int, contained: int
) -> RelationshipEdge:
    source: TableRef = cand.parent.table
    target: TableRef = cand.child.table
    return RelationshipEdge(
        edge_id=stable_id(
            NS_INFERRED,
            EdgeOrigin.INFERRED.value,
            cand.parent.qualified(),
            cand.child.qualified(),
            level.value,
        ),
        level=level,
        origin=EdgeOrigin.INFERRED,
        source=source,
        target=target,
        source_columns=(cand.parent.column,),
        target_columns=(cand.child.column,),
        transform=Transform.FOREIGN_KEY,
        traceability=Traceability.KEY_PRESERVING,
        status=EdgeStatus.SUGGESTED,
        confidence=confidence,
        details={
            "evidence": "name_pattern+sampled_containment",
            "pattern": cand.pattern,
            "sampled": sampled,
            "contained": contained,
        },
    )


def infer_relationships(
    catalog: CatalogSnapshot,
    reader: CatalogReader,
    *,
    threshold: float = 0.95,
    sample: int = 1000,
    exclude: Iterable[RelationshipEdge] = (),
) -> list[RelationshipEdge]:
    """SUGGESTED edges (TABLE + one COLUMN edge each) for candidates whose sampled containment
    ratio is >= `threshold`. `exclude` holds known edges (declared FKs etc.) to skip.

    confidence = containment ratio x name weight, rounded down to 3 decimals, capped at 0.999.
    """
    limit = Decimal(str(threshold))
    edges: list[RelationshipEdge] = []
    for cand in candidate_pairs(catalog, exclude):
        values = reader.sample_values(cand.child, sample)
        if not values:
            continue
        contained = reader.count_contained(cand.child, cand.parent, values)
        ratio = Decimal(contained) / Decimal(len(values))
        if ratio < limit:
            continue
        conf = min(ratio * cand.weight, _MAX_CONFIDENCE).quantize(
            Decimal("0.001"), rounding=ROUND_DOWN
        )
        confidence = float(conf)
        for level in (LineageLevel.TABLE, LineageLevel.COLUMN):
            edges.append(_edge(cand, level, confidence, len(values), contained))
    _log.info("inferred relationships: %d suggested edges", len(edges))
    return edges
