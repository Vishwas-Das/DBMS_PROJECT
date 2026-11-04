from __future__ import annotations

import logging
import re

import pytest

from lae.audit.hmac_keys import HmacKeyring
from lae.contracts.enums import ErasureStrategy, Normalization, PiiCategory
from lae.contracts.types import ColumnRef, DiscoveryLimits, PiiColumn, PolicySet
from lae.discovery import capture_residual_probes, discover_records, probe_normalizations
from lae.errors import SubjectNotFoundError

pytestmark = pytest.mark.unit

KEY = b"k" * 32
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RAW_VALUES = (
    "Alice.Subject@Example.ORG",
    "alice.subject@example.org",
    "+1 (555) 010-4242",
    "15550104242",
    "ALICE.SUBJECT@example.org",
    "alice.alt@example.org",
    "bob@example.org",
    "alice.third@example.org",
)


def _policies(kit, confirmed=True):
    def pii(table, col, cat=PiiCategory.DIRECT_IDENTIFIER, ok=confirmed, scan=False):
        ref = ColumnRef(kit.t(table), col)
        return ref, PiiColumn(ref, cat, ErasureStrategy.NULLIFY, ok, scan)

    return PolicySet(
        pii=dict(
            [
                pii("support_ticket", "contact_email"),
                pii("ticket_message", "author_email"),
                pii("customer", "full_name", ok=False),  # unconfirmed: ignored
                pii("document", "title", PiiCategory.QUASI_IDENTIFIER),  # not direct, no scan
                pii("document", "body", PiiCategory.SENSITIVE, scan=True),  # opted into scan
            ]
        )
    )


def _probes(demo, policies, caplog=None):
    keyring = HmacKeyring(KEY)
    rg = discover_records(
        demo.graph, demo.root, (42,), demo.reader(), DiscoveryLimits()
    ).record_graph
    return capture_residual_probes(demo.root, (42,), rg, demo.reader(), policies, keyring), rg


def test_probes_contain_only_hmacs(kit, demo, caplog):
    caplog.set_level(logging.DEBUG)
    probes, _ = _probes(demo, _policies(kit))
    assert probes
    for p in probes:
        assert HEX64.match(p.value_hmac)
    dump = repr(probes) + caplog.text
    for raw in RAW_VALUES:
        assert raw not in dump


def test_expected_digests_and_normalizations(kit, demo):
    ring = HmacKeyring(KEY)
    probes, _ = _probes(demo, _policies(kit))
    got = {(p.origin.qualified(), p.normalization, p.value_hmac) for p in probes}
    email = " Alice.Subject@Example.ORG "
    phone = "+1 (555) 010-4242"
    expected_subset = {
        ("app.customer.email", Normalization.EXACT, ring.digest(email, Normalization.EXACT)),
        (
            "app.customer.email",
            Normalization.LOWER_TRIM,
            ring.digest("alice.subject@example.org", Normalization.EXACT),
        ),
        ("app.customer.phone", Normalization.EXACT, ring.digest(phone, Normalization.EXACT)),
        (
            "app.customer.phone",
            Normalization.DIGITS_ONLY,
            ring.digest("15550104242", Normalization.EXACT),
        ),
        (
            "app.support_ticket.contact_email",
            Normalization.LOWER_TRIM,
            ring.digest("alice.subject@example.org", Normalization.EXACT),
        ),
        (
            "app.ticket_message.author_email",
            Normalization.EXACT,
            ring.digest("alice.alt@example.org", Normalization.EXACT),
        ),
        ("app.document.body", Normalization.EXACT, ring.digest("Alice", Normalization.EXACT)),
    }
    assert expected_subset <= got
    # SHARED message 102 (by 42 on 77's ticket) and 101 (by 77) are excluded
    for shared_value in ("alice.third@example.org", "bob@example.org"):
        assert ring.digest(shared_value, Normalization.EXACT) not in {p.value_hmac for p in probes}
    origins = {p.origin.qualified() for p in probes}
    assert "app.customer.full_name" not in origins  # unconfirmed
    assert "app.document.title" not in origins  # quasi-identifier, not opted in
    assert probes == sorted(probes, key=lambda p: (p.origin, p.normalization.value, p.value_hmac))
    assert len(set(probes)) == len(probes)


def test_root_only_probes_without_policies(kit, demo):
    probes, _ = _probes(demo, PolicySet())
    assert {p.origin.qualified() for p in probes} == {"app.customer.email", "app.customer.phone"}


def test_unconfirmed_policies_add_nothing(kit, demo):
    probes, _ = _probes(demo, _policies(kit, confirmed=False))
    assert {p.origin.table.name for p in probes} == {"customer"}


def test_empty_normalized_values_are_skipped(kit, demo):
    for row in demo.data[kit.t("customer")]:
        if row["id"] == 42:
            row["phone"] = "n/a"
            row["email"] = None
    probes, _ = _probes(demo, PolicySet())
    assert {(p.origin.column, p.normalization) for p in probes} == {("phone", Normalization.EXACT)}


def test_missing_subject_raises(kit, demo):
    from lae.lineage import RecordGraph

    with pytest.raises(SubjectNotFoundError):
        capture_residual_probes(
            demo.root, (4242,), RecordGraph(), demo.reader(), PolicySet(), HmacKeyring(KEY)
        )


def test_batching_of_probe_reads(kit, demo):
    rg = discover_records(
        demo.graph, demo.root, (42,), demo.reader(), DiscoveryLimits()
    ).record_graph
    reader = demo.reader()
    capture_residual_probes(
        demo.root, (42,), rg, reader, _policies(kit), HmacKeyring(KEY), batch_size=1
    )
    assert all(c["n"] <= 1 for c in reader.calls)


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("email", "a@b.c", (Normalization.EXACT, Normalization.LOWER_TRIM)),
        ("contact", "a@b.c", (Normalization.EXACT, Normalization.LOWER_TRIM)),
        ("mobile_no", "+44 1", (Normalization.EXACT, Normalization.DIGITS_ONLY)),
        ("full_name", "Alice", (Normalization.EXACT,)),
        ("customer_number", 42, (Normalization.EXACT,)),
    ],
)
def test_probe_normalizations(column, value, expected):
    assert probe_normalizations(column, value) == expected
