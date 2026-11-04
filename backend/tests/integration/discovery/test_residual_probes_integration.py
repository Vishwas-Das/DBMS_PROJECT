"""capture_residual_probes on the real DB: HMACs only, plausible counts, and they find the copies.

Seeded configuration (db/fixtures/generator.py): subject root identifier columns (email, phone);
confirmed DIRECT_IDENTIFIER columns customer.{full_name, email, phone} and
purchase_order.shipping_name; use_for_residual_scan on customer.email / customer.phone.
Expected probes per subject (computed independently with SQL below):
EXACT = each non-NULL identifier value per origin column (shipping_name: distinct values of the
subject's orders), LOWER_TRIM = the e-mail, DIGITS_ONLY = the phone (if any).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pytest
from sqlalchemy import text

from lae.audit.hmac_keys import HmacKeyring
from lae.contracts.enums import Normalization, RecordRole
from lae.contracts.types import ColumnRef, DiscoveryLimits, TableRef
from lae.discovery import capture_residual_probes, discover_records

pytestmark = pytest.mark.integration

APP = "app"
TEST_HMAC_KEY = bytes(range(32))  # test-only key; never a real secret
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def t(name: str) -> TableRef:
    return TableRef(APP, name)


def _probes(pipeline: Any, subject: int) -> tuple[list[Any], Any]:
    result = discover_records(
        pipeline.graph, pipeline.root, (subject,), pipeline.reader, DiscoveryLimits()
    )
    probes = capture_residual_probes(
        pipeline.root,
        (subject,),
        result.record_graph,
        pipeline.reader,
        pipeline.policies,
        HmacKeyring(TEST_HMAC_KEY),
    )
    return probes, result


def _identifiers(pipeline: Any, subject: int) -> tuple[dict[str, Any], list[str]]:
    row = (
        pipeline.conn.execute(
            text("SELECT full_name, email, phone FROM app.customer WHERE id = :id"),
            {"id": subject},
        )
        .mappings()
        .one()
    )
    names = pipeline.conn.execute(
        text(
            "SELECT DISTINCT shipping_name FROM app.purchase_order "
            "WHERE customer_id = :id AND shipping_name IS NOT NULL"
        ),
        {"id": subject},
    ).scalars()
    return dict(row), sorted(names)


def _expected(pipeline: Any, subject: int) -> set[tuple[ColumnRef, Normalization, str]]:
    keyring = HmacKeyring(TEST_HMAC_KEY)
    ident, ship_names = _identifiers(pipeline, subject)
    out: set[tuple[ColumnRef, Normalization, str]] = set()
    for col in ("full_name", "email", "phone"):
        value = ident[col]
        if value is None:
            continue
        ref = ColumnRef(t("customer"), col)
        norms = [Normalization.EXACT]
        if col == "email":
            norms.append(Normalization.LOWER_TRIM)
        if col == "phone":
            norms.append(Normalization.DIGITS_ONLY)
        out |= {(ref, n, keyring.digest(value, n)) for n in norms}
    for name in ship_names:
        ref = ColumnRef(t("purchase_order"), "shipping_name")
        out.add((ref, Normalization.EXACT, keyring.digest(name, Normalization.EXACT)))
    return out


def test_primary_probes_are_hmacs_only(pipeline: Any) -> None:
    primary = pipeline.truth.meta["known_subjects"]["primary"]
    probes, _ = _probes(pipeline, primary)
    ident, ship_names = _identifiers(pipeline, primary)
    assert probes == sorted(probes, key=lambda p: (p.origin, p.normalization.value, p.value_hmac))
    assert len(set(probes)) == len(probes)
    for p in probes:
        assert HEX64.match(p.value_hmac)
    dump = repr(probes)
    for raw in [*(v for v in ident.values() if v), *ship_names, str(primary)]:
        if len(raw) > 2:
            assert raw not in dump
            assert raw.lower() not in dump.lower()
    digits = re.sub(r"[^0-9]", "", ident["phone"])
    assert digits not in dump
    counts = Counter(p.normalization for p in probes)
    # email, full_name, phone, shipping_name (all 3 orders carry the same name) / email / phone
    assert counts == {
        Normalization.EXACT: 4,
        Normalization.LOWER_TRIM: 1,
        Normalization.DIGITS_ONLY: 1,
    }
    assert {(p.origin, p.normalization, p.value_hmac) for p in probes} == _expected(
        pipeline, primary
    )


def test_probe_counts_plausible_for_every_tiny_subject(pipeline: Any) -> None:
    customers = pipeline.truth.meta["scale"]["customers"]
    totals: Counter[Normalization] = Counter()
    for c in range(1, customers + 1):
        probes, _ = _probes(pipeline, c)
        got = {(p.origin, p.normalization, p.value_hmac) for p in probes}
        assert got == _expected(pipeline, c), c
        counts = Counter(p.normalization for p in probes)
        assert counts[Normalization.LOWER_TRIM] == 1  # every customer has an e-mail
        assert counts[Normalization.DIGITS_ONLY] <= 1
        assert 2 <= counts[Normalization.EXACT] <= 4
        totals.update(counts)
    assert totals[Normalization.LOWER_TRIM] == customers


def test_probes_find_the_hidden_copies_fk_discovery_missed(pipeline: Any) -> None:
    """The residual scan closes the recall gap: probe digests match marketing_event_log."""
    primary = pipeline.truth.meta["known_subjects"]["primary"]
    probes, result = _probes(pipeline, primary)
    assert all(k.table != t("marketing_event_log") for k, _ in result.record_graph.nodes())
    hidden = {h[2]: h[3] for h in pipeline.truth.subject(primary)["hidden_copies"]}
    assert hidden == {"EXACT": 3, "LOWER_TRIM": 5}
    target = ColumnRef(t("marketing_event_log"), "email")
    email_ref = ColumnRef(t("customer"), "email")
    for norm in (Normalization.EXACT, Normalization.LOWER_TRIM):
        digests = [
            p.value_hmac for p in probes if p.origin == email_ref and p.normalization is norm
        ]
        assert len(digests) == 1
        n = pipeline.reader.hmac_match_count(target, digests, norm, TEST_HMAC_KEY)
        assert n == hidden[norm.value]
    # the root row itself still holds the e-mail exactly once (nothing was erased here)
    exact = [
        p.value_hmac
        for p in probes
        if p.origin == email_ref and p.normalization is Normalization.EXACT
    ]
    assert (
        pipeline.reader.hmac_match_count(email_ref, exact, Normalization.EXACT, TEST_HMAC_KEY) == 1
    )


def test_probes_ignore_shared_and_other_subject_rows(pipeline: Any) -> None:
    """Only SUBJECT_ROOT + OWNED rows are read: the referred customers' e-mails are not probed."""
    known = pipeline.truth.meta["known_subjects"]
    probes, result = _probes(pipeline, known["primary"])
    roles = {k.values: a.role for k, a in result.record_graph.nodes() if k.table == t("customer")}
    keyring = HmacKeyring(TEST_HMAC_KEY)
    digests = {p.value_hmac for p in probes}
    for other in known["referred_by_primary"]:
        assert roles[(other,)] is RecordRole.REFERENCING_OTHER_SUBJECT
        ident, _ = _identifiers(pipeline, other)
        assert keyring.digest(ident["email"], Normalization.EXACT) not in digests
