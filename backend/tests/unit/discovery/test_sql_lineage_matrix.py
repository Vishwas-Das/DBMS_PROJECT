"""Classification matrix for extract_sql_lineage (architecture.md §7).

Each case declares the artifact traceability, the traceability of each source TABLE edge, the
KEY_PRESERVING key mapping where one exists, and selected column-level transforms. The target
table ``app.art`` is created per case with the expected output columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from lae.contracts.enums import (
    ArtifactKind,
    EdgeOrigin,
    LineageLevel,
    RefreshStrategy,
    Traceability,
    Transform,
)
from lae.contracts.types import ArtifactDefinition, TableRef
from lae.discovery import build_catalog, extract_sql_lineage

pytestmark = pytest.mark.unit

KP = Traceability.KEY_PRESERVING
AGG = Traceability.AGGREGATE
OPQ = Traceability.OPAQUE
ID_ = Transform.IDENTITY
EXPR = Transform.EXPRESSION
AGGT = Transform.AGGREGATE
FIL = Transform.FILTER
JK = Transform.JOIN_KEY

CUSTOMER_COLS = ("id", "email", "full_name", "phone", "referrer_id")


@dataclass(frozen=True)
class Case:
    id: str
    sql: str
    out: tuple[str, ...]
    trace: Traceability
    sources: dict[str, Traceability]
    kp: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=dict)
    cols: dict[tuple[str, str, str], Transform] = field(default_factory=dict)
    extra_keys: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    warning: str | None = None


CASES = [
    # ---------------------------------------------------------------- KEY_PRESERVING
    Case(
        "plain_columns",
        "SELECT id, email FROM app.customer",
        ("id", "email"),
        KP,
        {"customer": KP},
        kp={"customer": (("id",), ("id",))},
        cols={("customer", "id", "id"): ID_, ("customer", "email", "email"): ID_},
    ),
    Case(
        "aliases",
        "SELECT c.id AS cid, c.email AS mail FROM app.customer AS c",
        ("cid", "mail"),
        KP,
        {"customer": KP},
        kp={"customer": (("id",), ("cid",))},
        cols={("customer", "email", "mail"): ID_},
    ),
    Case(
        "casts_are_identity",
        "SELECT id::text AS cid, CAST(email AS varchar(200)) AS mail FROM app.customer",
        ("cid", "mail"),
        KP,
        {"customer": KP},
        kp={"customer": (("id",), ("cid",))},
        cols={("customer", "id", "cid"): ID_, ("customer", "email", "mail"): ID_},
    ),
    Case(
        "expressions",
        "SELECT id, lower(email) AS e, concat(full_name, '!') AS n FROM app.customer",
        ("id", "e", "n"),
        KP,
        {"customer": KP},
        cols={("customer", "email", "e"): EXPR, ("customer", "full_name", "n"): EXPR},
    ),
    Case(
        "case_when",
        "SELECT id, CASE WHEN phone IS NULL THEN 'none' ELSE 'some' END AS has_phone "
        "FROM app.customer",
        ("id", "has_phone"),
        KP,
        {"customer": KP},
        cols={("customer", "phone", "has_phone"): EXPR},
    ),
    Case(
        "coalesce",
        "SELECT id, COALESCE(phone, email) AS contact FROM app.customer",
        ("id", "contact"),
        KP,
        {"customer": KP},
        cols={("customer", "phone", "contact"): EXPR, ("customer", "email", "contact"): EXPR},
    ),
    Case(
        "select_star",
        "SELECT * FROM app.customer",
        CUSTOMER_COLS,
        KP,
        {"customer": KP},
        kp={"customer": (("id",), ("id",))},
        cols={("customer", c, c): ID_ for c in CUSTOMER_COLS},
    ),
    Case(
        "qualified_star",
        "SELECT c.* FROM app.customer c",
        CUSTOMER_COLS,
        KP,
        {"customer": KP},
        cols={("customer", "phone", "phone"): ID_},
    ),
    Case(
        "unqualified_table_resolved_from_catalog",
        "SELECT id, email FROM customer",
        ("id", "email"),
        KP,
        {"customer": KP},
    ),
    Case(
        "where_filter",
        "SELECT id FROM app.customer WHERE email LIKE '%@example.org' AND phone IS NOT NULL",
        ("id",),
        KP,
        {"customer": KP},
        cols={("customer", "email", "id"): FIL, ("customer", "phone", "id"): FIL},
    ),
    Case(
        "inner_join_both_keys",
        "SELECT o.id AS order_id, c.id AS customer_id, c.email FROM app.purchase_order o "
        "JOIN app.customer c ON c.id = o.customer_id",
        ("order_id", "customer_id", "email"),
        KP,
        {"customer": KP, "purchase_order": KP},
        kp={"customer": (("id",), ("customer_id",)), "purchase_order": (("id",), ("order_id",))},
        cols={("purchase_order", "customer_id", "email"): JK},
    ),
    Case(
        "left_join",
        "SELECT c.id AS customer_id, o.id AS order_id, o.total_cents FROM app.customer c "
        "LEFT JOIN app.purchase_order o ON o.customer_id = c.id",
        ("customer_id", "order_id", "total_cents"),
        KP,
        {"customer": KP, "purchase_order": KP},
        cols={("purchase_order", "customer_id", "order_id"): JK},
    ),
    Case(
        "composite_key",
        "SELECT ol.order_id, ol.line_no, ol.qty * 2 AS double_qty FROM app.order_line ol",
        ("order_id", "line_no", "double_qty"),
        KP,
        {"order_line": KP},
        kp={"order_line": (("order_id", "line_no"), ("order_id", "line_no"))},
        cols={("order_line", "qty", "double_qty"): EXPR},
    ),
    Case(
        "group_by_pk_with_ordinal",
        "SELECT id, count(*) AS n FROM app.customer GROUP BY 1",
        ("id", "n"),
        KP,
        {"customer": KP},
    ),
    Case(
        "demo_customer_ltv",
        """SELECT c.id AS customer_id, count(DISTINCT o.id) AS order_count,
                  COALESCE(sum(p.amount_cents), 0)::bigint AS total_spent
           FROM app.customer c
           LEFT JOIN app.purchase_order o ON o.customer_id = c.id
           LEFT JOIN app.payment p ON p.order_id = o.id
           GROUP BY c.id""",
        ("customer_id", "order_count", "total_spent"),
        KP,
        {"customer": KP, "purchase_order": AGG, "payment": AGG},
        kp={"customer": (("id",), ("customer_id",))},
        cols={
            ("customer", "id", "customer_id"): ID_,
            ("payment", "amount_cents", "total_spent"): AGGT,
            ("purchase_order", "id", "order_count"): AGGT,
            ("purchase_order", "customer_id", "customer_id"): JK,
            ("payment", "order_id", "total_spent"): JK,
        },
    ),
    Case(
        "having",
        "SELECT c.id, count(o.id) AS n FROM app.customer c JOIN app.purchase_order o "
        "ON o.customer_id = c.id GROUP BY c.id HAVING sum(o.total_cents) > 100",
        ("id", "n"),
        KP,
        {"customer": KP, "purchase_order": AGG},
        cols={("purchase_order", "total_cents", "n"): FIL, ("purchase_order", "id", "n"): AGGT},
    ),
    Case(
        "window_aggregate",
        "SELECT id, sum(total_cents) OVER (PARTITION BY customer_id) AS customer_total "
        "FROM app.purchase_order",
        ("id", "customer_total"),
        KP,
        {"purchase_order": KP},
        cols={("purchase_order", "total_cents", "customer_total"): AGGT},
    ),
    Case(
        "window_row_number",
        "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM app.customer",
        ("id", "rn"),
        KP,
        {"customer": KP},
        cols={("customer", "id", "rn"): EXPR},
    ),
    Case(
        "cte_key_preserving",
        "WITH recent AS (SELECT id, customer_id FROM app.purchase_order WHERE status = 'open') "
        "SELECT r.id AS order_id, r.customer_id FROM recent r",
        ("order_id", "customer_id"),
        KP,
        {"purchase_order": KP},
        kp={"purchase_order": (("id",), ("order_id",))},
        cols={("purchase_order", "status", "order_id"): FIL},
    ),
    Case(
        "cte_aggregate_joined_to_key",
        "WITH t AS (SELECT customer_id, sum(total_cents) AS s FROM app.purchase_order "
        "GROUP BY customer_id) SELECT c.id AS customer_id, t.s AS total "
        "FROM app.customer c JOIN t ON t.customer_id = c.id",
        ("customer_id", "total"),
        KP,
        {"customer": KP, "purchase_order": AGG},
        cols={
            ("purchase_order", "total_cents", "total"): AGGT,
            ("purchase_order", "customer_id", "total"): JK,
        },
    ),
    Case(
        "scalar_subquery",
        "SELECT c.id, (SELECT max(o.total_cents) FROM app.purchase_order o "
        "WHERE o.customer_id = c.id) AS biggest FROM app.customer c",
        ("id", "biggest"),
        KP,
        {"customer": KP, "purchase_order": AGG},
        cols={
            ("purchase_order", "total_cents", "biggest"): AGGT,
            ("purchase_order", "customer_id", "biggest"): FIL,
        },
    ),
    Case(
        "in_subquery_is_control_lineage",
        "SELECT c.id, c.email FROM app.customer c "
        "WHERE c.id IN (SELECT customer_id FROM app.purchase_order WHERE total_cents > 10)",
        ("id", "email"),
        KP,
        {"customer": KP, "purchase_order": AGG},
        cols={
            ("purchase_order", "customer_id", "email"): FIL,
            ("purchase_order", "total_cents", "id"): FIL,
        },
    ),
    Case(
        "exists_subquery",
        "SELECT c.id FROM app.customer c WHERE EXISTS "
        "(SELECT 1 FROM app.support_ticket s WHERE s.customer_id = c.id)",
        ("id",),
        KP,
        {"customer": KP, "support_ticket": AGG},
        cols={("support_ticket", "customer_id", "id"): FIL},
    ),
    Case(
        "union_all_keys_on_both_sides",
        "SELECT id AS k, 'customer' AS kind FROM app.customer "
        "UNION ALL SELECT id, 'product' FROM app.product",
        ("k", "kind"),
        KP,
        {"customer": KP, "product": KP},
        kp={"customer": (("id",), ("k",)), "product": (("id",), ("k",))},
    ),
    Case(
        "union_in_derived_table_with_filter",
        "SELECT u.k FROM (SELECT id AS k, email AS e FROM app.customer "
        "UNION ALL SELECT id, name FROM app.product) u WHERE u.e IS NOT NULL",
        ("k",),
        KP,
        {"customer": KP, "product": KP},
        cols={("customer", "email", "k"): FIL, ("product", "name", "k"): FIL},
    ),
    Case(
        "known_anonymous_function",
        "SELECT id, jsonb_build_object('e', email) AS doc FROM app.customer",
        ("id", "doc"),
        KP,
        {"customer": KP},
        cols={("customer", "email", "doc"): EXPR},
    ),
    Case(
        "ctas_body",
        "CREATE TABLE app.art AS SELECT id, email FROM app.customer",
        ("id", "email"),
        KP,
        {"customer": KP},
    ),
    Case(
        "insert_select_body",
        "INSERT INTO app.art SELECT id FROM app.customer WHERE email IS NOT NULL",
        ("id",),
        KP,
        {"customer": KP},
    ),
    Case(
        "fk_column_as_extra_key",
        "SELECT customer_id, sum(total_cents) AS total FROM app.purchase_order "
        "GROUP BY customer_id",
        ("customer_id", "total"),
        KP,
        {"purchase_order": KP},
        kp={"purchase_order": (("customer_id",), ("customer_id",))},
        extra_keys={"purchase_order": [("customer_id",)]},
    ),
    # ---------------------------------------------------------------- AGGREGATE
    Case(
        "demo_daily_revenue",
        """SELECT date_trunc('day', p.paid_at)::date AS day, sum(p.amount_cents) AS revenue_cents,
                  count(*) AS payment_count
           FROM app.payment p GROUP BY 1""",
        ("day", "revenue_cents", "payment_count"),
        AGG,
        {"payment": AGG},
        cols={
            ("payment", "paid_at", "day"): EXPR,
            ("payment", "amount_cents", "revenue_cents"): AGGT,
        },
    ),
    Case(
        "group_by_non_key",
        "SELECT customer_id, count(*) AS n, sum(total_cents) AS total "
        "FROM app.purchase_order GROUP BY customer_id",
        ("customer_id", "n", "total"),
        AGG,
        {"purchase_order": AGG},
        cols={("purchase_order", "customer_id", "customer_id"): ID_},
    ),
    Case(
        "global_aggregate",
        "SELECT count(*) AS n, max(id) AS max_id FROM app.customer",
        ("n", "max_id"),
        AGG,
        {"customer": AGG},
        cols={("customer", "id", "max_id"): AGGT},
    ),
    Case(
        "derived_table_aggregate",
        "SELECT t.cid AS customer_id, t.n FROM (SELECT customer_id AS cid, count(*) AS n "
        "FROM app.purchase_order GROUP BY customer_id) t WHERE t.n > 1",
        ("customer_id", "n"),
        AGG,
        {"purchase_order": AGG},
    ),
    Case(
        "distinct_is_grouping",
        "SELECT DISTINCT customer_id FROM app.purchase_order",
        ("customer_id",),
        AGG,
        {"purchase_order": AGG},
    ),
    Case(
        "anonymous_aggregate",
        "SELECT jsonb_agg(email) AS emails FROM app.customer",
        ("emails",),
        AGG,
        {"customer": AGG},
        cols={("customer", "email", "emails"): AGGT},
    ),
    Case(
        "rollup_never_key_preserving",
        "SELECT customer_id, sum(total_cents) AS total FROM app.purchase_order "
        "GROUP BY ROLLUP (customer_id)",
        ("customer_id", "total"),
        AGG,
        {"purchase_order": AGG},
        extra_keys={"purchase_order": [("customer_id",)]},
    ),
    Case(
        "distinct_on_is_conservative",
        "SELECT DISTINCT ON (customer_id) customer_id, id FROM app.purchase_order "
        "ORDER BY customer_id, id DESC",
        ("customer_id", "id"),
        AGG,
        {"purchase_order": AGG},
    ),
    # ---------------------------------------------------------------- OPAQUE
    Case(
        "create_without_query_body",
        "CREATE TABLE app.art (id int)",
        ("id",),
        OPQ,
        {},
        warning="without a query body",
    ),
    Case(
        "unknown_unqualified_table",
        "SELECT id FROM ghost",
        ("id",),
        OPQ,
        {},
        warning="references table ghost outside the catalog",
    ),
    Case(
        "no_surviving_key",
        "SELECT email FROM app.customer",
        ("email",),
        OPQ,
        {"customer": OPQ},
        warning="without a surviving key",
    ),
    Case(
        "join_loses_one_key",
        "SELECT o.id AS order_id, c.email FROM app.purchase_order o "
        "JOIN app.customer c ON c.id = o.customer_id",
        ("order_id", "email"),
        OPQ,
        {"customer": OPQ, "purchase_order": KP},
    ),
    Case(
        "union_without_key_on_one_side",
        "SELECT id AS customer_id, email FROM app.customer "
        "UNION SELECT customer_id, contact_email FROM app.support_ticket",
        ("customer_id", "email"),
        OPQ,
        {"customer": KP, "support_ticket": OPQ},
    ),
    Case(
        "using_join_coalesce_quirk",
        "SELECT * FROM app.loyalty_account JOIN app.customer_profile_cache USING (customer_id)",
        ("customer_id", "points", "payload"),
        OPQ,
        {"loyalty_account": OPQ, "customer_profile_cache": OPQ},
    ),
    Case(
        "unknown_function",
        "SELECT id, my_mask(email) AS e FROM app.customer",
        ("id", "e"),
        OPQ,
        {"customer": OPQ},
        warning="unknown function my_mask()",
    ),
    Case(
        "lateral_set_returning",
        "SELECT c.id, u.x FROM app.customer c, LATERAL unnest(ARRAY[c.email]) AS u(x)",
        ("id", "x"),
        OPQ,
        {"customer": OPQ},
        warning="set-returning",
    ),
    Case(
        "generate_series_in_from",
        "SELECT g AS n FROM generate_series(1, 3) AS g",
        ("n",),
        OPQ,
        {},
    ),
    Case(
        "recursive_cte",
        "WITH RECURSIVE r AS (SELECT id, referrer_id FROM app.customer WHERE id = 1 "
        "UNION ALL SELECT c.id, c.referrer_id FROM app.customer c JOIN r ON c.referrer_id = r.id) "
        "SELECT id FROM r",
        ("id",),
        OPQ,
        {"customer": OPQ},
        warning="recursive CTE",
    ),
    Case(
        "unknown_table",
        "SELECT id FROM app.ghost",
        ("id",),
        OPQ,
        {},
        warning="outside the catalog",
    ),
    Case(
        "unknown_schema_table_mixed",
        "SELECT c.id FROM app.customer c JOIN other.thing x ON x.id = c.id",
        ("id",),
        OPQ,
        {"customer": OPQ},
        warning="other.thing",
    ),
    Case(
        "unknown_column",
        "SELECT nope FROM app.customer",
        ("nope",),
        OPQ,
        {"customer": OPQ},
        warning="could not be qualified",
    ),
    Case(
        "parse_error_text_scan",
        "SELECT id FROM app.customer WHERE (",
        ("id",),
        OPQ,
        {"customer": OPQ},
        warning="could not be parsed",
    ),
    Case(
        "tokenizer_error",
        "SELECT 'unterminated FROM app.payment",
        ("id",),
        OPQ,
        {"payment": OPQ},
        warning="could not be parsed",
    ),
    Case(
        "multiple_statements",
        "SELECT id FROM app.customer; SELECT id FROM app.product",
        ("id",),
        OPQ,
        {"customer": OPQ, "product": OPQ},
        warning="multiple statements",
    ),
    Case(
        "dynamic_sql",
        "DO $$ BEGIN EXECUTE 'SELECT 1'; END $$",
        ("id",),
        OPQ,
        {},
        warning="dynamic SQL",
    ),
    Case(
        "update_statement",
        "UPDATE app.customer SET email = NULL",
        ("id",),
        OPQ,
        {"customer": OPQ},
        warning="unsupported statement type Update",
    ),
    Case(
        "not_sql_at_all",
        "this is not sql",
        ("id",),
        OPQ,
        {},
    ),
]

assert len(CASES) >= 25


def _run(kit, case: Case) -> Any:
    art_cols = [(c, "text") for c in case.out]
    tables = [*kit.demo_tables(), kit.table("art", art_cols)]
    catalog = build_catalog(tables, [], ["app"])
    artifact = ArtifactDefinition(
        kit.uid(case.id),
        case.id,
        ArtifactKind.MATERIALIZED_VIEW,
        kit.t("art"),
        case.sql,
        RefreshStrategy.REFRESH_MATVIEW,
    )
    extra = {kit.t(k): v for k, v in case.extra_keys.items()}
    return extract_sql_lineage(artifact, catalog, extra_keys=extra), artifact


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_classification_matrix(kit, case: Case):
    result, artifact = _run(kit, case)
    assert result.artifact_id == artifact.artifact_id
    assert result.traceability is case.trace, result.warnings

    table_edges = [e for e in result.edges if e.level is LineageLevel.TABLE]
    got = {e.source.name: e.traceability for e in table_edges}
    assert got == case.sources
    for e in result.edges:
        assert e.target == TableRef("app", "art")
        assert e.origin is EdgeOrigin.SQL_LINEAGE
        assert e.artifact_id == artifact.artifact_id
        if e.level is LineageLevel.COLUMN:
            assert len(e.source_columns) == len(e.target_columns) == 1
    for e in table_edges:
        if e.traceability is KP:
            assert e.source_columns and len(e.source_columns) == len(e.target_columns)
            assert e.transform is ID_
        else:
            assert e.source_columns == () == e.target_columns
    for src, mapping in case.kp.items():
        edge = next(e for e in table_edges if e.source.name == src)
        assert (edge.source_columns, edge.target_columns) == mapping
    col_edges = {
        (e.source.name, e.source_columns[0], e.target_columns[0]): e.transform
        for e in result.edges
        if e.level is LineageLevel.COLUMN
    }
    for key, transform in case.cols.items():
        assert col_edges.get(key) is transform, (key, col_edges)
    if result.traceability is OPQ:
        assert result.warnings
    if table_edges and all(e.details.get("opaque") for e in table_edges):
        # SQL that could not be analysed yields TABLE edges only (no invented column lineage)
        assert not [e for e in result.edges if e.level is LineageLevel.COLUMN]
    if case.warning:
        assert any(case.warning in w for w in result.warnings), result.warnings


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_matrix_is_deterministic(kit, case: Case):
    a, _ = _run(kit, case)
    b, _ = _run(kit, case)
    assert a == b
    assert [e.edge_id for e in a.edges] == [e.edge_id for e in b.edges]
    assert len({e.edge_id for e in a.edges}) == len(a.edges)


def test_warnings_never_echo_sql_literals(kit):
    secret = "alice.subject@example.org"
    for sql in (
        f"SELECT email FROM app.customer WHERE email = '{secret}'",
        f"SELECT id FROM app.customer WHERE email = '{secret}' AND (",
        f"SELECT my_udf('{secret}') AS id FROM app.customer",
        f"SELECT '{secret}",
    ):
        result, _ = _run(kit, Case("lit", sql, ("id", "email"), OPQ, {}))
        assert result.traceability is OPQ
        assert all(secret not in w for w in result.warnings)
        assert all(secret not in str(dict(e.details)) for e in result.edges)
