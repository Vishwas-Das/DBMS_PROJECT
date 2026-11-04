"""Synthetic generator: determinism, idempotent reload, ground-truth sanity, hard cases."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from psycopg import sql
from sqlalchemy import Connection, Engine, text

from lae.contracts.types import TableRef
from lae.db._sql import driver_connection

pytestmark = pytest.mark.integration

APP_RELATIONS = (
    "customer",
    "customer_address",
    "loyalty_account",
    "product",
    "purchase_order",
    "order_line",
    "shipment",
    "payment",
    "support_ticket",
    "ticket_message",
    "document",
    "document_embedding",
    "customer_profile_cache",
    "marketing_event_log",
    "customer_ltv",
    "daily_revenue",
    "customer_segment",
)
PK = {
    "customer": ("id",),
    "customer_address": ("id",),
    "loyalty_account": ("customer_id",),
    "product": ("id",),
    "purchase_order": ("id",),
    "order_line": ("order_id", "line_no"),
    "shipment": ("id",),
    "payment": ("id",),
    "support_ticket": ("id",),
    "ticket_message": ("id",),
    "document": ("id",),
    "document_embedding": ("id",),
    "customer_profile_cache": ("customer_id",),
    "customer_ltv": ("customer_id",),
    "customer_segment": ("customer_id",),
}


def checksums(engine: Engine) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    with engine.connect() as conn:
        raw = driver_connection(conn)
        with raw.cursor() as cur:
            cur.execute("SET LOCAL TimeZone = 'UTC'")
            for rel in APP_RELATIONS:
                cur.execute(
                    sql.SQL(
                        "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text "
                        "COLLATE \"C\"), '')) FROM {} t"
                    ).format(sql.Identifier("app", rel))
                )
                n, digest = cur.fetchone()  # type: ignore[misc]
                out[rel] = (int(n), str(digest))
        conn.rollback()
    return out


def lae_config(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        rows: list[Any] = []
        for q in (
            "SELECT name, schema_name, table_name, key_columns, identifier_columns "
            "FROM lae.subject_root ORDER BY name",
            "SELECT schema_name, table_name, action, legal_basis FROM lae.retention_policy "
            "ORDER BY 1, 2",
            "SELECT schema_name, table_name, column_name, category, erasure_strategy, source, "
            "is_confirmed, use_for_residual_scan FROM lae.pii_classification ORDER BY 1, 2, 3",
            "SELECT name, kind, schema_name, table_name, definition_sql, refresh_strategy, "
            "source_key_mapping, is_inspectable FROM lae.derived_artifact ORDER BY name",
        ):
            rows.append([tuple(r) for r in conn.execute(text(q))])
    return rows


def test_in_memory_generation_is_deterministic(generator: ModuleType) -> None:
    _, a = generator.generate("small", 42)
    _, b = generator.generate("small", 42)
    _, c = generator.generate("small", 43)
    assert a.dumps() == b.dumps()
    assert a.dumps() != c.dumps()


def test_same_seed_same_rows_different_seed_different_rows(
    migrated_db: Engine, generator: ModuleType, mark_dataset_dirty: Any
) -> None:
    mark_dataset_dirty()
    t1 = generator.seed_database(migrated_db, "tiny", 11)
    c1, cfg1 = checksums(migrated_db), lae_config(migrated_db)
    t2 = generator.seed_database(migrated_db, "tiny", 11)  # idempotent reload
    c2, cfg2 = checksums(migrated_db), lae_config(migrated_db)
    t3 = generator.seed_database(migrated_db, "tiny", 12)
    c3 = checksums(migrated_db)
    mark_dataset_dirty()
    assert c1 == c2 and cfg1 == cfg2
    assert t1.dumps() == t2.dumps()
    assert c1 != c3 and t1.dumps() != t3.dumps()
    assert {k: v[0] for k, v in c1.items() if k in t1.meta["row_counts"]} == t1.meta["row_counts"]


def _exists(conn: Connection, table: str, key: list[Any]) -> bool:
    ref = TableRef.parse(table)
    cols = PK[ref.name]
    raw = driver_connection(conn)
    with raw.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE ({}) = ({})").format(
                sql.Identifier(ref.schema, ref.name),
                sql.SQL(", ").join(map(sql.Identifier, cols)),
                sql.SQL(", ").join(sql.Placeholder() * len(cols)),
            ),
            key,
        )
        row = cur.fetchone()
        return bool(row and row[0] == 1)


def test_every_ground_truth_key_exists(seeded_db: tuple[Engine, Any], app_conn: Connection) -> None:
    _, truth = seeded_db
    checked = 0
    for subject in truth.subjects.values():
        for category in ("owned", "retained", "shared", "referencing_other", "derived_rows"):
            for entry in subject[category]:
                assert _exists(app_conn, entry[0], entry[1]), (category, entry[0])
                checked += 1
    assert checked > 100


def test_hidden_copy_counts_match_database(
    seeded_db: tuple[Engine, Any], app_conn: Connection
) -> None:
    _, truth = seeded_db
    for cid, subject in truth.subjects.items():
        email = app_conn.execute(
            text("SELECT email FROM app.customer WHERE id = :i"), {"i": cid}
        ).scalar_one()
        for _table, _column, norm, count in subject["hidden_copies"]:
            if norm == "EXACT":
                q = "SELECT count(*) FROM app.marketing_event_log WHERE email = :e"
            else:
                q = (
                    "SELECT count(*) FROM app.marketing_event_log WHERE translate(btrim(email, "
                    "E' \\t\\n\\r\\f\\x0B'), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    "'abcdefghijklmnopqrstuvwxyz') = :e"
                )
            assert app_conn.execute(text(q), {"e": email}).scalar_one() == count


def test_ground_truth_is_not_just_fk_closure_but_agrees_with_it(
    seeded_db: tuple[Engine, Any], app_conn: Connection
) -> None:
    """Cross-check a few bookkeeping facts against the data (independent derivations)."""
    _, truth = seeded_db
    for cid, subject in truth.subjects.items():
        orders = {k[0] for t, k in subject["owned"] if t == "app.purchase_order"}
        db_orders = {
            r[0]
            for r in app_conn.execute(
                text("SELECT id FROM app.purchase_order WHERE customer_id = :c"), {"c": cid}
            )
        }
        assert orders == db_orders
        referred = {k[0] for _t, k in subject["referencing_other"]}
        db_referred = {
            r[0]
            for r in app_conn.execute(
                text("SELECT id FROM app.customer WHERE referrer_id = :c"), {"c": cid}
            )
        }
        assert referred == db_referred


def test_hard_cases_for_primary_subject(seeded_db: tuple[Engine, Any]) -> None:
    _, truth = seeded_db
    known = truth.meta["known_subjects"]
    p, s = known["primary"], known["secondary"]
    subj = truth.subject(p)
    tables = {t for t, _ in subj["owned"]}
    assert {
        "app.customer",
        "app.customer_address",
        "app.loyalty_account",
        "app.purchase_order",
        "app.order_line",
        "app.shipment",
        "app.payment",
        "app.support_ticket",
        "app.ticket_message",
        "app.document",
    } <= tables
    assert "app.product" not in tables  # upstream, never the subject's data
    assert {k[0] for _t, k in subj["referencing_other"]} >= set(known["referred_by_primary"])
    assert ["app.customer", [known["primary_referrer"]]] not in subj["referencing_other"]
    others = {other for _t, _k, other in subj["shared"]}
    assert s in others and len(subj["shared"]) >= 2
    derived = {t for t, _ in subj["derived_rows"]}
    assert derived == {
        "app.customer_ltv",
        "app.customer_segment",
        "app.customer_profile_cache",
        "app.document_embedding",
    }
    hidden = {norm: n for _t, _c, norm, n in subj["hidden_copies"]}
    assert hidden == {"EXACT": 3, "LOWER_TRIM": 5}
    retained_actions = {t: a for t, _k, a in subj["retained"]}
    assert retained_actions == {
        "app.purchase_order": "ANONYMIZE",
        "app.order_line": "RETAIN",
        "app.payment": "RETAIN",
        "app.shipment": "RETAIN",
    }
    assert subj["aggregates"] == ["app.daily_revenue"]


def test_seeded_lae_configuration(seeded_db: tuple[Engine, Any], app_conn: Connection) -> None:
    rows = app_conn.execute(
        text(
            "SELECT name, kind, refresh_strategy, is_inspectable, source_key_mapping "
            "FROM lae.derived_artifact ORDER BY name"
        )
    ).all()
    by_name = {r[0]: r[1:] for r in rows}
    assert by_name["customer_ltv"][:2] == ("MATERIALIZED_VIEW", "REFRESH_MATVIEW")
    assert by_name["daily_revenue"][:2] == ("MATERIALIZED_VIEW", "REFRESH_MATVIEW")
    assert by_name["customer_segment"][:2] == ("DERIVED_TABLE", "RECOMPUTE_SQL")
    assert by_name["document_embedding"][3] == [
        {
            "source": "app.document",
            "source_columns": ["id"],
            "target_columns": ["source_document_id"],
        }
    ]
    assert by_name["customer_profile_cache"][:2] == ("CACHE", "INVALIDATE_BY_KEY")
    assert by_name["crm_export_files"][:3] == ("EXTERNAL", "EXTERNAL_TASK", False)
    policies = dict(
        app_conn.execute(text("SELECT table_name, action FROM lae.retention_policy")).all()
    )
    assert policies == {
        "purchase_order": "ANONYMIZE",
        "order_line": "RETAIN",
        "payment": "RETAIN",
        "shipment": "RETAIN",
    }
    root = app_conn.execute(
        text("SELECT key_columns, identifier_columns FROM lae.subject_root WHERE name='customer'")
    ).one()
    assert tuple(root) == (["id"], ["email", "phone"])
    n_pii = app_conn.execute(
        text("SELECT count(*) FROM lae.pii_classification WHERE is_confirmed")
    ).scalar_one()
    assert n_pii >= 8


def test_only_synthetic_identifiers(seeded_db: tuple[Engine, Any], app_conn: Connection) -> None:
    bad = app_conn.execute(
        text(
            "SELECT count(*) FROM app.customer WHERE email NOT LIKE '%@example.test' "
            "OR (phone IS NOT NULL AND phone NOT LIKE '+1-555-%')"
        )
    ).scalar_one()
    assert bad == 0


def test_no_float_columns_except_vector(
    seeded_db: tuple[Engine, Any], app_conn: Connection
) -> None:
    rows = app_conn.execute(
        text(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'app' AND a.attnum > 0 AND NOT a.attisdropped "
            "AND c.relkind IN ('r', 'm') "
            "AND format_type(a.atttypid, a.atttypmod) IN ('real', 'double precision', 'numeric')"
        )
    ).all()
    assert rows == []


def test_scale_presets_and_validation(generator: ModuleType) -> None:
    assert generator.PRESETS["default"].customers == 1000
    assert generator.PRESETS["default"].orders == 3000
    assert generator.PRESETS["default"].tickets == 1000
    assert generator.PRESETS["default"].documents == 500
    assert generator.PRESETS["default"].embeddings == 500
    assert generator.PRESETS["tiny"].customers == 20
    assert generator.PRESETS["small"].customers == 100
    assert generator.PRESETS["large"].customers == 10_000
    with pytest.raises(ValueError):
        generator.ScaleParams(customers=5)
    with pytest.raises(ValueError):
        generator.resolve_scale("huge")
    _, truth = generator.generate(generator.ScaleParams(customers=30, orders=10), 1)
    assert truth.meta["scale_name"] == "custom"
    assert generator.GroundTruth.from_json(truth.to_json()).dumps() == truth.dumps()


def test_cli_writes_ground_truth(
    migrated_url: str, generator: ModuleType, tmp_path: Any, mark_dataset_dirty: Any
) -> None:
    mark_dataset_dirty()
    out = tmp_path / "gt.json"
    assert (
        generator.main(["--scale", "tiny", "--seed", "3", "--url", migrated_url, "--out", str(out)])
        == 0
    )
    loaded = generator.GroundTruth.from_json(__import__("json").loads(out.read_text("utf-8")))
    assert loaded.meta["seed"] == 3
    assert set(loaded.subjects) == set(range(1, 21))
