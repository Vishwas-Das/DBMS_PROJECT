"""SQL transformation lineage with SQLGlot (architecture.md §7).

`extract_sql_lineage` is pure (no I/O) and never raises for bad or unsupported SQL: such SQL
yields ``Traceability.OPAQUE`` plus warnings. The only exception is `UnknownTableError` for an
artifact *target* missing from the catalog.

Pipeline:
1. ``sqlglot.parse_one(sql, read="postgres")``; CREATE ... AS / INSERT ... SELECT are unwrapped.
2. Construct checks (recursive CTE, LATERAL / set-returning functions, unknown functions,
   tables outside the catalog). Any hit makes the artifact OPAQUE.
3. ``optimizer.qualify`` against a schema built from the catalog (expands ``*``, resolves
   aliases, qualifies columns).
4. ``sqlglot.lineage`` for every output column; each source-column -> output-column path is
   classified IDENTITY / EXPRESSION / AGGREGATE. Columns that appear only in JOIN ... ON,
   WHERE, HAVING or GROUP BY become JOIN_KEY / FILTER (control lineage).
5. Per source table: KEY_PRESERVING if every column of one of its candidate keys (its primary
   key, or a caller-supplied extra key) reaches an output column through an IDENTITY path that
   is grouped wherever it crosses an aggregating scope; otherwise AGGREGATE if every value path
   is aggregated; otherwise OPAQUE (values flow without a surviving key).
6. Registered key mappings override inference for their source table (REGISTERED_MAPPING,
   KEY_PRESERVING).

Warnings never contain SQL text or literals (a definition may embed constants), only table and
column names, function names and exception class names.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope
from sqlglot.schema import MappingSchema

from lae.contracts.enums import (
    ArtifactKind,
    EdgeOrigin,
    EdgeStatus,
    LineageLevel,
    Traceability,
    Transform,
)
from lae.contracts.types import (
    ArtifactDefinition,
    CatalogSnapshot,
    RelationshipEdge,
    SqlLineageResult,
    TableInfo,
    TableRef,
)
from lae.discovery._ids import NS_SQL, cols, stable_id

__all__ = [
    "DIALECT",
    "SQLGLOT_VERSION",
    "extract_artifacts_lineage",
    "extract_sql_lineage",
    "sql_edge_id",
]

_log = logging.getLogger(__name__)

DIALECT = "postgres"
SQLGLOT_VERSION: str = str(getattr(sqlglot, "__version__", "unknown"))
_MAX_PATHS = 5_000

# PostgreSQL functions that SQLGlot 30.29 parses as exp.Anonymous but that are known,
# deterministic, and read nothing except their arguments.
_KNOWN_SCALAR_FUNCTIONS = frozenset(
    {
        "age",
        "array_length",
        "array_to_string",
        "btrim",
        "cardinality",
        "date_part",
        "format",
        "initcap",
        "json_build_array",
        "json_build_object",
        "jsonb_build_array",
        "jsonb_build_object",
        "jsonb_extract_path_text",
        "make_date",
        "row_to_json",
        "to_json",
        "to_jsonb",
        "to_number",
    }
)
# Aggregates that SQLGlot 30.29 parses as exp.Anonymous (not exp.AggFunc).
_KNOWN_AGG_FUNCTIONS = frozenset(
    {"bit_and", "bit_or", "every", "json_object_agg", "jsonb_agg", "jsonb_object_agg", "xmlagg"}
)
_SET_RETURNING = (exp.Lateral, exp.Unnest, exp.Explode, exp.ExplodingGenerateSeries)


def sql_edge_id(
    artifact_id: UUID,
    origin: EdgeOrigin,
    level: LineageLevel,
    transform: Transform,
    source: TableRef,
    source_columns: tuple[str, ...],
    target: TableRef,
    target_columns: tuple[str, ...],
) -> UUID:
    return stable_id(
        NS_SQL,
        artifact_id,
        origin.value,
        level.value,
        transform.value,
        source.qualified(),
        cols(source_columns),
        target.qualified(),
        cols(target_columns),
    )


# ---------------------------------------------------------------------- results


class _Opaque(Exception):
    """Internal signal: the SQL cannot be analysed further (reason is PII-free)."""


@dataclass(slots=True)
class _Path:
    source: TableRef
    column: str
    output: str
    transform: Transform
    aggregated: bool  # passes an aggregating scope or an aggregate expression
    key_identity: bool  # IDENTITY and grouped wherever it crosses an aggregating scope


@dataclass(slots=True)
class _Analysis:
    sources: list[TableRef] = field(default_factory=list)
    paths: list[_Path] = field(default_factory=list)
    control: dict[tuple[TableRef, str], Transform] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    opaque_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- helpers


def _strip(e: exp.Expr) -> exp.Expr:
    """Remove aliases, casts and parentheses around an expression."""
    while isinstance(e, exp.Alias | exp.Cast | exp.TryCast | exp.Paren):
        e = e.this
    return e


def _is_identity(e: exp.Expr) -> bool:
    return isinstance(_strip(e), exp.Column)


def _is_agg_node(n: exp.Expr) -> bool:
    if isinstance(n, exp.AggFunc):
        return True
    return isinstance(n, exp.Anonymous) and n.name.lower() in _KNOWN_AGG_FUNCTIONS


def _contains_agg(e: exp.Expr) -> bool:
    """Any aggregate (window aggregates and scalar sub-queries included)."""
    return any(_is_agg_node(n) for n in e.walk())


def _direct_agg(e: exp.Expr) -> bool:
    """An aggregate that collapses rows of *this* scope (not windows, not sub-queries)."""

    def prune(n: exp.Expr) -> bool:
        return n is not e and isinstance(n, exp.Window | exp.Subquery | exp.Query)

    return any(_is_agg_node(n) for n in e.walk(prune=prune) if not isinstance(n, exp.Window))


def _group_exprs(select: exp.Select) -> tuple[bool, list[exp.Expr]]:
    """(is_aggregating, grouping expressions usable for the identity check)."""
    group = select.args.get("group")
    exprs: list[exp.Expr] = []
    aggregating = False
    if group is not None:
        aggregating = True
        if not any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
            exprs.extend(_strip(g) for g in group.expressions)
    distinct = select.args.get("distinct")
    if distinct is not None:
        aggregating = True
        on = distinct.args.get("on")
        if on is not None:
            exprs.extend(_strip(g) for g in on.expressions)
        else:
            exprs.extend(_strip(p) for p in select.expressions)
    if select.args.get("having") is not None or any(_direct_agg(p) for p in select.expressions):
        aggregating = True
    return aggregating, exprs


def _table_ref(t: exp.Table) -> TableRef | None:
    if not t.name or not t.db or t.args.get("catalog"):
        return None
    return TableRef(t.db, t.name)


def _catalog_schema(catalog: CatalogSnapshot) -> MappingSchema:
    mapping: dict[str, object] = {}
    for ref, info in catalog.tables.items():
        tables = mapping.setdefault(ref.schema, {})
        assert isinstance(tables, dict)
        tables[ref.name] = {
            c.name: c.data_type for c in sorted(info.columns, key=lambda c: c.ordinal)
        }
    return MappingSchema(mapping, dialect=DIALECT, normalize=False)


def _text_scan(sql: str, catalog: CatalogSnapshot, target: TableRef) -> list[TableRef]:
    """Conservative fallback when the SQL cannot be parsed: catalog tables named in the text."""
    lowered = sql.lower()
    names: dict[str, list[TableRef]] = {}
    for ref in catalog.tables:
        names.setdefault(ref.name.lower(), []).append(ref)
    found: set[TableRef] = set()
    for ref in catalog.tables:
        if ref == target:
            continue
        qualified = (
            rf'(?<![\w$])"?{re.escape(ref.schema.lower())}"?\s*\.\s*"?'
            rf'{re.escape(ref.name.lower())}"?(?![\w$])'
        )
        bare = rf'(?<![\w$.])"?{re.escape(ref.name.lower())}"?(?![\w$])'
        if re.search(qualified, lowered) or (
            len(names[ref.name.lower()]) == 1 and re.search(bare, lowered)
        ):
            found.add(ref)
    return sorted(found)


def _unwrap_statement(tree: exp.Expr) -> exp.Query:
    if isinstance(tree, exp.Create | exp.Insert):
        inner = tree.expression
        if isinstance(inner, exp.Query):
            return inner
        raise _Opaque(f"{type(tree).__name__} statement without a query body")
    if isinstance(tree, exp.Query):
        return tree
    if isinstance(tree, exp.Block):
        raise _Opaque("multiple statements are not supported")
    if isinstance(tree, exp.Command):
        raise _Opaque("unsupported or dynamic SQL statement")
    raise _Opaque(f"unsupported statement type {type(tree).__name__}")


def _construct_checks(query: exp.Query, reasons: list[str]) -> None:
    for w in query.find_all(exp.With):
        if w.args.get("recursive"):
            reasons.append("recursive CTE (not supported)")
            break
    for node in query.find_all(*_SET_RETURNING):
        reasons.append(f"set-returning function or LATERAL ({type(node).__name__})")
        break
    for t in query.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            reasons.append("table function in FROM")
            break
    unknown = sorted(
        {
            a.name.lower()
            for a in query.find_all(exp.Anonymous)
            if a.name.lower() not in _KNOWN_SCALAR_FUNCTIONS | _KNOWN_AGG_FUNCTIONS
        }
    )
    for name in unknown:
        reasons.append(f"unknown function {name}()")


def _resolve_tables(
    query: exp.Query, catalog: CatalogSnapshot, reasons: list[str]
) -> list[TableRef]:
    """Fill in missing schemas from the catalog; return the catalog tables referenced."""
    cte_names = {c.alias_or_name for c in query.find_all(exp.CTE)}
    by_name: dict[str, list[TableRef]] = {}
    for ref in catalog.tables:
        by_name.setdefault(ref.name, []).append(ref)
    found: set[TableRef] = set()
    for t in list(query.find_all(exp.Table)):
        if not isinstance(t.this, exp.Identifier):
            continue
        if not t.db:
            if t.name in cte_names:
                continue
            candidates = sorted(by_name.get(t.name, []))
            preferred = [c for s in catalog.schemas for c in candidates if c.schema == s]
            if not preferred:
                reasons.append(f"references table {t.name} outside the catalog")
                continue
            t.set("db", exp.to_identifier(preferred[0].schema, quoted=True))
        resolved = _table_ref(t)
        if resolved is None or not catalog.has_table(resolved):
            name = ".".join(p for p in (t.catalog, t.db, t.name) if p)
            reasons.append(f"references table {name} outside the catalog")
            continue
        found.add(resolved)
    return sorted(found)


def _collect_paths(root: Node) -> list[list[Node]]:
    out: list[list[Node]] = []
    stack: list[tuple[Node, list[Node]]] = [(root, [])]
    while stack:
        node, prefix = stack.pop()
        path = [*prefix, node]
        if not node.downstream:
            out.append(path)
            if len(out) > _MAX_PATHS:
                raise _Opaque("lineage too complex to analyse")
            continue
        for child in reversed(node.downstream):
            stack.append((child, path))
    return out


def _leaf_column(node: Node) -> str:
    col = exp.maybe_parse(node.name, into=exp.Column, dialect=DIALECT)
    return str(col.name)


def _classify_path(
    path: list[Node], output: str, group_cache: dict[int, tuple[bool, list[exp.Expr]]]
) -> _Path | None:
    leaf = path[-1]
    if not isinstance(leaf.expression, exp.Table):
        if leaf.expression.find(exp.Column) is None:
            return None  # constant / COUNT(*) projection without column sources
        raise _Opaque("lineage reached a non-table source")
    ref = _table_ref(leaf.expression)
    if ref is None:
        raise _Opaque("lineage reached an unqualified table")
    column = _leaf_column(leaf)
    if column == "*":
        raise _Opaque("SELECT * over a table that cannot be expanded")
    transform = Transform.IDENTITY
    aggregated = False
    grouped = True
    for node in path[:-1]:
        if isinstance(node.source, exp.SetOperation):
            continue  # placeholder node; branches carry the real projections
        expr = node.expression
        if _contains_agg(expr):
            transform = Transform.AGGREGATE
        elif not _is_identity(expr) and transform is Transform.IDENTITY:
            transform = Transform.EXPRESSION
        if isinstance(node.source, exp.Select):
            key = id(node.source)
            if key not in group_cache:
                group_cache[key] = _group_exprs(node.source)
            aggregating, exprs = group_cache[key]
            if aggregating:
                aggregated = True
                if _strip(expr) not in exprs:
                    grouped = False
    if transform is Transform.AGGREGATE:
        aggregated = True
    return _Path(
        source=ref,
        column=column,
        output=output,
        transform=transform,
        aggregated=aggregated,
        key_identity=transform is Transform.IDENTITY and grouped,
    )


def _owning_scope(node: exp.Expr, scopes: Mapping[int, Scope]) -> Scope | None:
    cur: exp.Expr | None = node
    while cur is not None:
        sc = scopes.get(id(cur))
        if sc is not None:
            return sc
        cur = cur.parent
    return None


def _resolve_column(
    column: exp.Column,
    scopes: Mapping[int, Scope],
    catalog: CatalogSnapshot,
    seen: set[int],
) -> set[tuple[TableRef, str]]:
    if id(column) in seen:
        return set()
    seen.add(id(column))
    scope = _owning_scope(column, scopes)
    if scope is None:
        return set()
    src = scope.sources.get(column.table)
    if isinstance(src, exp.Table):
        ref = _table_ref(src)
        if ref is not None and catalog.has_table(ref):
            return {(ref, column.name)}
        return set()
    if isinstance(src, Scope):
        return _resolve_output(src.expression, column.name, None, scopes, catalog, seen)
    return set()


def _resolve_output(
    query: exp.Expr,
    name: str,
    position: int | None,
    scopes: Mapping[int, Scope],
    catalog: CatalogSnapshot,
    seen: set[int],
) -> set[tuple[TableRef, str]]:
    if isinstance(query, exp.SetOperation):
        if position is None:
            names = list(query.named_selects)
            if name not in names:
                return set()
            position = names.index(name)
        out: set[tuple[TableRef, str]] = set()
        for branch in (query.left, query.right):
            out |= _resolve_output(branch, name, position, scopes, catalog, seen)
        return out
    if not isinstance(query, exp.Select):
        return set()
    projections = query.expressions
    chosen: exp.Expr | None = None
    if position is not None:
        chosen = projections[position] if position < len(projections) else None
    else:
        chosen = next((p for p in projections if p.alias_or_name == name), None)
    if chosen is None:
        return set()
    out = set()
    for c in chosen.find_all(exp.Column):
        out |= _resolve_column(c, scopes, catalog, seen)
    return out


def _control_lineage(
    query: exp.Query, catalog: CatalogSnapshot
) -> dict[tuple[TableRef, str], Transform]:
    scopes = {id(s.expression): s for s in traverse_scope(query)}
    found: dict[tuple[TableRef, str], Transform] = {}

    def note(clause: exp.Expr | None, transform: Transform) -> None:
        if clause is None:
            return
        for c in clause.find_all(exp.Column):
            for key in _resolve_column(c, scopes, catalog, set()):
                if found.get(key) is not Transform.JOIN_KEY:
                    found[key] = transform

    for select in query.find_all(exp.Select):
        for join in select.args.get("joins") or []:
            note(join.args.get("on"), Transform.JOIN_KEY)
        note(select.args.get("where"), Transform.FILTER)
        note(select.args.get("having"), Transform.FILTER)
        note(select.args.get("group"), Transform.FILTER)
    return found


def _analyse(sql: str, catalog: CatalogSnapshot, target: TableRef) -> _Analysis:
    result = _Analysis()
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except SqlglotError as exc:
        result.opaque_reasons.append(f"SQL could not be parsed ({type(exc).__name__})")
        result.sources = _text_scan(sql, catalog, target)
        return result
    try:
        query = _unwrap_statement(tree)
    except _Opaque as exc:
        result.opaque_reasons.append(str(exc))
        result.sources = _text_scan(sql, catalog, target)
        return result
    query = normalize_identifiers(query, dialect=DIALECT)
    result.sources = [s for s in _resolve_tables(query, catalog, result.opaque_reasons)]
    _construct_checks(query, result.opaque_reasons)
    if result.opaque_reasons:
        return result
    schema = _catalog_schema(catalog)
    try:
        qualified = qualify(
            query, schema=schema, dialect=DIALECT, validate_qualify_columns=True, identify=True
        )
        scope = build_scope(qualified)
        if scope is None:
            raise _Opaque("no query scope could be built")
        nodes = lineage(
            None, qualified, schema=schema, dialect=DIALECT, scope=scope, trim_selects=False
        )
        assert isinstance(nodes, dict)
        group_cache: dict[int, tuple[bool, list[exp.Expr]]] = {}
        result.outputs = list(qualified.named_selects)
        for output in result.outputs:
            for path in _collect_paths(nodes[output]):
                classified = _classify_path(path, output, group_cache)
                if classified is not None:
                    result.paths.append(classified)
        result.control = _control_lineage(qualified, catalog)
    except _Opaque as exc:
        result.opaque_reasons.append(str(exc))
    except SqlglotError as exc:
        result.opaque_reasons.append(f"SQL could not be qualified ({type(exc).__name__})")
    return result


# ---------------------------------------------------------------------- edge building


def _table_edge(
    artifact: ArtifactDefinition,
    source: TableRef,
    traceability: Traceability,
    origin: EdgeOrigin = EdgeOrigin.SQL_LINEAGE,
    source_columns: tuple[str, ...] = (),
    target_columns: tuple[str, ...] = (),
    details: Mapping[str, Any] | None = None,
) -> RelationshipEdge:
    transform = {
        Traceability.KEY_PRESERVING: Transform.IDENTITY,
        Traceability.AGGREGATE: Transform.AGGREGATE,
        Traceability.OPAQUE: Transform.EXPRESSION,
    }[traceability]
    return RelationshipEdge(
        edge_id=sql_edge_id(
            artifact.artifact_id,
            origin,
            LineageLevel.TABLE,
            transform,
            source,
            source_columns,
            artifact.target,
            target_columns,
        ),
        level=LineageLevel.TABLE,
        origin=origin,
        source=source,
        target=artifact.target,
        source_columns=source_columns,
        target_columns=target_columns,
        transform=transform,
        traceability=traceability,
        status=EdgeStatus.CONFIRMED,
        artifact_id=artifact.artifact_id,
        details=dict(details or {}),
    )


def _column_edge(
    artifact: ArtifactDefinition,
    source: TableRef,
    source_column: str,
    target_column: str,
    transform: Transform,
    traceability: Traceability,
    origin: EdgeOrigin,
    details: Mapping[str, Any],
) -> RelationshipEdge:
    return RelationshipEdge(
        edge_id=sql_edge_id(
            artifact.artifact_id,
            origin,
            LineageLevel.COLUMN,
            transform,
            source,
            (source_column,),
            artifact.target,
            (target_column,),
        ),
        level=LineageLevel.COLUMN,
        origin=origin,
        source=source,
        target=artifact.target,
        source_columns=(source_column,),
        target_columns=(target_column,),
        transform=transform,
        traceability=traceability,
        status=EdgeStatus.CONFIRMED,
        artifact_id=artifact.artifact_id,
        details=dict(details),
    )


def _mapping_edges(
    artifact: ArtifactDefinition,
    catalog: CatalogSnapshot,
    target_info: TableInfo,
    warnings: list[str],
) -> tuple[list[RelationshipEdge], set[TableRef], bool]:
    """Edges for registered key mappings: (edges, mapped sources, all mappings valid)."""
    edges: list[RelationshipEdge] = []
    mapped: set[TableRef] = set()
    valid = True
    for m in sorted(artifact.key_mappings, key=lambda m: (m.source, m.source_columns)):
        problem: str | None = None
        if not m.source_columns or len(m.source_columns) != len(m.target_columns):
            problem = "mismatched column lists"
        elif not catalog.has_table(m.source):
            problem = "source table outside the catalog"
        elif not all(catalog.tables[m.source].has_column(c) for c in m.source_columns):
            problem = "unknown source column"
        elif not all(target_info.has_column(c) for c in m.target_columns):
            problem = "unknown target column"
        if problem is not None:
            warnings.append(f"key mapping from {m.source.qualified()} ignored: {problem}")
            valid = False
            continue
        mapped.add(m.source)
        details = {"evidence": "registered_mapping"}
        edges.append(
            _table_edge(
                artifact,
                m.source,
                Traceability.KEY_PRESERVING,
                EdgeOrigin.REGISTERED_MAPPING,
                tuple(m.source_columns),
                tuple(m.target_columns),
                details,
            )
        )
        for sc, tc in zip(m.source_columns, m.target_columns, strict=True):
            edges.append(
                _column_edge(
                    artifact,
                    m.source,
                    sc,
                    tc,
                    Transform.IDENTITY,
                    Traceability.KEY_PRESERVING,
                    EdgeOrigin.REGISTERED_MAPPING,
                    details,
                )
            )
    return edges, mapped, valid


def _candidate_keys(
    source: TableRef,
    catalog: CatalogSnapshot,
    extra_keys: Mapping[TableRef, Iterable[tuple[str, ...]]],
) -> list[tuple[str, ...]]:
    keys: list[tuple[str, ...]] = []
    pk = catalog.tables[source].primary_key
    if pk:
        keys.append(tuple(pk))
    for k in sorted({tuple(k) for k in extra_keys.get(source, ()) if k}):
        if k not in keys:
            keys.append(k)
    return keys


def _inferred_edges(
    artifact: ArtifactDefinition,
    catalog: CatalogSnapshot,
    target_info: TableInfo,
    analysis: _Analysis,
    extra_keys: Mapping[TableRef, Iterable[tuple[str, ...]]],
    skip_sources: set[TableRef],
    warnings: list[str],
) -> list[RelationshipEdge]:
    target = artifact.target
    sources = [s for s in analysis.sources if s != target and s not in skip_sources]
    if target in analysis.sources:
        warnings.append("artifact reads from its own target table; self edge ignored")
    if analysis.opaque_reasons:
        base = {"evidence": "sqlglot", "sqlglot_version": SQLGLOT_VERSION, "opaque": True}
        return [_table_edge(artifact, s, Traceability.OPAQUE, details=base) for s in sources]

    order = {name: i for i, name in enumerate(analysis.outputs)}
    missing = sorted(
        {p.output for p in analysis.paths if not target_info.has_column(p.output)}
        | {o for o in analysis.outputs if not target_info.has_column(o)}
    )
    for o in missing:
        warnings.append(f"output column {o} is not a column of {target.qualified()}")
    present = [o for o in analysis.outputs if target_info.has_column(o)]

    edges: list[RelationshipEdge] = []
    for source in sources:
        paths = [p for p in analysis.paths if p.source == source]
        identity: dict[str, list[str]] = {}
        for p in paths:
            if p.key_identity and target_info.has_column(p.output):
                identity.setdefault(p.column, []).append(p.output)
        mapping: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        for key in _candidate_keys(source, catalog, extra_keys):
            if all(k in identity for k in key):
                outs = tuple(min(identity[k], key=lambda o: order[o]) for k in key)
                mapping = (key, outs)
                break
        details: dict[str, Any] = {"evidence": "sqlglot", "sqlglot_version": SQLGLOT_VERSION}
        if mapping is not None:
            edges.append(
                _table_edge(
                    artifact,
                    source,
                    Traceability.KEY_PRESERVING,
                    source_columns=mapping[0],
                    target_columns=mapping[1],
                    details={**details, "key_mapping": "inferred"},
                )
            )
            traceability = Traceability.KEY_PRESERVING
        elif any(not p.aggregated for p in paths):
            warnings.append(
                f"values of {source.qualified()} reach {target.qualified()} without a "
                "surviving key column"
            )
            edges.append(_table_edge(artifact, source, Traceability.OPAQUE, details=details))
            traceability = Traceability.OPAQUE
        else:
            edges.append(_table_edge(artifact, source, Traceability.AGGREGATE, details=details))
            traceability = Traceability.AGGREGATE

        value_pairs: set[tuple[str, str, Transform]] = {
            (p.column, p.output, p.transform) for p in paths if target_info.has_column(p.output)
        }
        for col, out, transform in sorted(value_pairs, key=lambda v: (v[0], order[v[1]], v[2])):
            edges.append(
                _column_edge(
                    artifact,
                    source,
                    col,
                    out,
                    transform,
                    traceability,
                    EdgeOrigin.SQL_LINEAGE,
                    {"evidence": "sqlglot", "clause": "SELECT"},
                )
            )
        value_columns = {p.column for p in paths}
        control = sorted(
            (col, tr)
            for (src, col), tr in analysis.control.items()
            if src == source and col not in value_columns
        )
        for col, transform in control:
            clause = "JOIN" if transform is Transform.JOIN_KEY else "WHERE/HAVING/GROUP BY"
            for out in present:
                edges.append(
                    _column_edge(
                        artifact,
                        source,
                        col,
                        out,
                        transform,
                        traceability,
                        EdgeOrigin.SQL_LINEAGE,
                        {"evidence": "sqlglot", "clause": clause},
                    )
                )
    return edges


def _combine(edges: Sequence[RelationshipEdge]) -> Traceability:
    table_edges = [e for e in edges if e.level is LineageLevel.TABLE]
    kinds = {e.traceability for e in table_edges}
    if Traceability.OPAQUE in kinds or not table_edges:
        return Traceability.OPAQUE
    if Traceability.KEY_PRESERVING in kinds:
        return Traceability.KEY_PRESERVING
    return Traceability.AGGREGATE


def _sorted_edges(edges: Iterable[RelationshipEdge]) -> tuple[RelationshipEdge, ...]:
    unique = {e.edge_id: e for e in edges}
    return tuple(
        sorted(
            unique.values(),
            key=lambda e: (
                e.level is not LineageLevel.TABLE,
                e.source,
                e.source_columns,
                e.target_columns,
                e.transform.value,
                str(e.edge_id),
            ),
        )
    )


def extract_sql_lineage(
    artifact: ArtifactDefinition,
    catalog: CatalogSnapshot,
    *,
    extra_keys: Mapping[TableRef, Iterable[tuple[str, ...]]] | None = None,
) -> SqlLineageResult:
    """Table- and column-level lineage of one artifact. Pure; never raises for bad SQL.

    `extra_keys` adds candidate keys per source table beyond its primary key (for example the
    key columns of an upstream materialized view, which PostgreSQL cannot declare as a PK).
    Raises UnknownTableError only if a non-EXTERNAL artifact's target is not in the catalog.
    """
    if artifact.kind is ArtifactKind.EXTERNAL:
        return SqlLineageResult(
            artifact.artifact_id,
            (),
            Traceability.OPAQUE,
            ("external artifact is not inspectable",),
        )
    target_info = catalog.table(artifact.target)  # UnknownTableError for an unknown target
    warnings: list[str] = []
    mapping_edges, mapped, mappings_valid = _mapping_edges(artifact, catalog, target_info, warnings)
    sql = (artifact.definition_sql or "").strip()
    inferred: list[RelationshipEdge] = []
    if sql:
        try:
            analysis = _analyse(sql, catalog, artifact.target)
        except Exception as exc:  # defensive: SQLGlot internals must never escape
            _log.warning("SQL lineage analysis failed internally: %s", type(exc).__name__)
            analysis = _Analysis(
                sources=_text_scan(sql, catalog, artifact.target),
                opaque_reasons=[f"internal analysis error ({type(exc).__name__})"],
            )
        warnings.extend(analysis.opaque_reasons)
        inferred = _inferred_edges(
            artifact, catalog, target_info, analysis, extra_keys or {}, mapped, warnings
        )
        if analysis.opaque_reasons and not inferred and not mapping_edges:
            warnings.append("no source table could be identified")
    elif not mapping_edges:
        warnings.append("no definition SQL and no key mapping")
    edges = _sorted_edges([*mapping_edges, *inferred])
    traceability = _combine(edges)
    if not mappings_valid:
        traceability = Traceability.OPAQUE
    if not edges and sql and "no source table could be identified" not in warnings:
        warnings.append("no source table could be identified")
    deduped = tuple(dict.fromkeys(warnings))
    if traceability is Traceability.OPAQUE:
        _log.info("artifact %s is OPAQUE (%d warnings)", artifact.target.qualified(), len(deduped))
    return SqlLineageResult(artifact.artifact_id, edges, traceability, deduped)


def extract_artifacts_lineage(
    artifacts: Iterable[ArtifactDefinition],
    catalog: CatalogSnapshot,
    *,
    extra_keys: Mapping[TableRef, Iterable[tuple[str, ...]]] | None = None,
) -> list[SqlLineageResult]:
    """Lineage of several artifacts, propagating keys along artifact-on-artifact chains.

    The target columns of every KEY_PRESERVING edge into an artifact become candidate keys of
    that artifact for its downstream artifacts (e.g. customer_ltv.customer_id for
    customer_segment). Iterates to a fixpoint; results are sorted by (target, artifact_id).
    """
    items = sorted(artifacts, key=lambda a: (a.target, str(a.artifact_id)))
    keys: dict[TableRef, set[tuple[str, ...]]] = {
        t: {tuple(k) for k in ks} for t, ks in (extra_keys or {}).items()
    }
    results: list[SqlLineageResult] = []
    for _ in range(len(items) + 1):
        results = [extract_sql_lineage(a, catalog, extra_keys=keys) for a in items]
        new_keys = {t: set(ks) for t, ks in keys.items()}
        for r in results:
            for e in r.edges:
                if (
                    e.level is LineageLevel.TABLE
                    and e.traceability is Traceability.KEY_PRESERVING
                    and e.target_columns
                ):
                    new_keys.setdefault(e.target, set()).add(e.target_columns)
        if new_keys == keys:
            break
        keys = new_keys
    return results
