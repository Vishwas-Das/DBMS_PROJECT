from __future__ import annotations

import dataclasses
import random

import pytest

from lae.contracts.enums import OnDeleteAction, TableKind
from lae.contracts.types import ColumnInfo
from lae.discovery import build_catalog, catalog_fingerprint, read_catalog, schema_drift
from lae.errors import CatalogError

pytestmark = pytest.mark.unit


def test_read_catalog_uses_reader_and_sorts(kit):
    tables = kit.demo_tables()
    shuffled = tables[:]
    random.Random(1).shuffle(shuffled)
    reader = kit.FakeCatalogReader(shuffled, kit.demo_fks())
    snap = read_catalog(reader, ["app"])
    assert list(snap.tables) == sorted(t.ref for t in tables)
    assert snap.schemas == ("app",)
    assert len(snap.fingerprint) == 64
    assert [c[0] for c in reader.calls] == ["list_tables", "list_foreign_keys"]


def test_fingerprint_is_order_independent(kit):
    tables, fks = kit.demo_tables(), kit.demo_fks()
    a = catalog_fingerprint(tables, fks, ["app"])
    b = catalog_fingerprint(list(reversed(tables)), list(reversed(fks)), ["app", "app"])
    assert a == b
    # golden-ish stability: same input twice gives the same value
    assert catalog_fingerprint(tables, fks, ["app"]) == a


def test_fingerprint_ignores_row_estimates_and_definition_whitespace(kit):
    tables = kit.demo_tables()
    base = catalog_fingerprint(tables, [], ["app"])
    estimated = [dataclasses.replace(t, estimated_rows=12345) for t in tables]
    assert catalog_fingerprint(estimated, [], ["app"]) == base
    padded = [
        dataclasses.replace(t, definition_sql=f"\n  {t.definition_sql}  \n")
        if t.definition_sql
        else t
        for t in tables
    ]
    assert catalog_fingerprint(padded, [], ["app"]) == base


@pytest.mark.parametrize(
    "mutate",
    [
        "type",
        "nullability",
        "pk",
        "definition",
        "kind",
        "add_column",
        "rename_column",
    ],
)
def test_fingerprint_changes_on_schema_change(kit, mutate):
    tables = kit.demo_tables()
    base = catalog_fingerprint(tables, [], ["app"])
    idx = next(i for i, t in enumerate(tables) if t.ref.name == "customer_ltv")
    tgt = tables[idx]
    col0 = tgt.columns[0]
    if mutate == "type":
        new = dataclasses.replace(
            tgt, columns=(dataclasses.replace(col0, data_type="text"), *tgt.columns[1:])
        )
    elif mutate == "nullability":
        new = dataclasses.replace(
            tgt,
            columns=(dataclasses.replace(col0, is_nullable=not col0.is_nullable), *tgt.columns[1:]),
        )
    elif mutate == "pk":
        new = dataclasses.replace(tgt, primary_key=("customer_id",))
    elif mutate == "definition":
        new = dataclasses.replace(tgt, definition_sql="SELECT 1")
    elif mutate == "kind":
        new = dataclasses.replace(tgt, kind=TableKind.VIEW)
    elif mutate == "add_column":
        new = dataclasses.replace(tgt, columns=(*tgt.columns, ColumnInfo("x", 99, "int", True)))
    else:
        new = dataclasses.replace(
            tgt, columns=(dataclasses.replace(col0, name="cust"), *tgt.columns[1:])
        )
    changed = [*tables[:idx], new, *tables[idx + 1 :]]
    assert catalog_fingerprint(changed, [], ["app"]) != base


def test_fingerprint_changes_on_fk_change(kit):
    tables, fks = kit.demo_tables(), kit.demo_fks()
    base = catalog_fingerprint(tables, fks, ["app"])
    changed = [dataclasses.replace(fks[0], on_delete=OnDeleteAction.CASCADE), *fks[1:]]
    assert catalog_fingerprint(tables, changed, ["app"]) != base
    assert catalog_fingerprint(tables, fks[1:], ["app"]) != base
    reordered = [dataclasses.replace(fks[6], child_columns=("line_no", "order_id")), *fks[:6]]
    assert catalog_fingerprint(tables, [*reordered, *fks[7:]], ["app"]) != base


def test_validation_errors(kit):
    good = kit.table("a", [("id", "int")], ("id",))
    with pytest.raises(CatalogError):
        build_catalog([good], [], [])
    with pytest.raises(CatalogError) as e:
        build_catalog([kit.table("a", [("id", "int")], ("id",), schema="other")], [], ["app"])
    assert e.value.details == {"table": "other.a"}
    with pytest.raises(CatalogError):
        build_catalog([good, good], [], ["app"])
    with pytest.raises(CatalogError):
        build_catalog([kit.table("b", [("id", "int"), ("id", "text")])], [], ["app"])
    with pytest.raises(CatalogError):
        build_catalog([kit.table("c", [("id", "int")], ("nope",))], [], ["app"])


def test_schema_drift_detects_added_removed_changed(kit):
    tables = kit.demo_tables()
    old = build_catalog(tables, kit.demo_fks(), ["app"])
    same = build_catalog(list(reversed(tables)), kit.demo_fks(), ["app"])
    none = schema_drift(old, same)
    assert not none.has_drift
    assert none == schema_drift(same, old)

    by_name = {t.ref.name: t for t in tables}
    changed_payment = dataclasses.replace(
        by_name["payment"],
        columns=(*by_name["payment"].columns, ColumnInfo("card_last4", 9, "text", True)),
    )
    new_tables = [t for t in tables if t.ref.name not in ("product", "payment")] + [
        changed_payment,
        kit.table("coupon", [("id", "int")], ("id",)),
    ]
    new = build_catalog(new_tables, kit.demo_fks()[:5], ["app"])
    drift = schema_drift(old, new)
    assert drift.has_drift and drift.fingerprint_changed
    assert drift.added_tables == (kit.t("coupon"),)
    assert drift.removed_tables == (kit.t("product"),)
    assert drift.changed_tables == (kit.t("payment"),)


def test_schema_drift_fk_only_change_is_visible_via_fingerprint(kit):
    tables = kit.demo_tables()
    old = build_catalog(tables, kit.demo_fks(), ["app"])
    new = build_catalog(tables, kit.demo_fks()[1:], ["app"])
    drift = schema_drift(old, new)
    assert drift.changed_tables == () and drift.added_tables == () and drift.removed_tables == ()
    assert drift.fingerprint_changed and drift.has_drift
