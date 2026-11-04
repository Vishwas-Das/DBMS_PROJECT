"""Signed external chain-head anchors: round trip, tamper detection, chain cross-check."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from lae.audit.anchor import (
    ANCHOR_FIELDS,
    AnchorVerification,
    SignedChainHead,
    export_chain_head,
    verify_anchor,
)
from lae.audit.canonical import canonical_json
from lae.audit.evidence import ChainAnchor
from lae.audit.signing import Signer
from lae.contracts.types import AuditEventRecord
from lae.errors import ValidationError

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 4, 9, 30, tzinfo=UTC)


@pytest.fixture
def events(vectors: dict[str, Any]) -> list[dict[str, Any]]:
    evs: list[dict[str, Any]] = vectors["chain"]["events"]
    return evs


@pytest.fixture
def head(events: list[dict[str, Any]]) -> ChainAnchor:
    return ChainAnchor(events[-1]["seq"], events[-1]["event_hash"])


@pytest.fixture
def signed(head: ChainAnchor, test_signer: Signer) -> SignedChainHead:
    return export_chain_head(head, test_signer, T0)


def _records(events: list[dict[str, Any]]) -> list[AuditEventRecord]:
    return [
        AuditEventRecord(
            e["seq"], e["event_type"], "x", None, "t", T0,
            e["payload_canonical"], e["prev_hash"], e["event_hash"],
        )
        for e in events
    ]  # fmt: skip


def _resign(anchor: dict[str, Any], signer: Signer) -> str:
    return canonical_json(
        {"anchor": anchor, "signature": signer.sign(canonical_json(anchor))}
    ).decode()


def test_document_shape(signed: SignedChainHead, head: ChainAnchor, test_signer: Signer) -> None:
    assert set(signed.anchor) == ANCHOR_FIELDS
    assert dict(signed.anchor) == {
        "anchor_version": "1",
        "seq": head.seq,
        "event_hash": head.event_hash,
        "exported_at": "2026-11-04T09:30:00.000000Z",
        "key_id": test_signer.key_id,
    }
    assert signed.anchor_canonical == canonical_json(dict(signed.anchor)).decode()
    export = json.loads(signed.export_json())
    assert set(export) == {"anchor", "signature"}
    assert signed.export_json().encode() == canonical_json(export)


def test_deterministic_with_fixed_key(head: ChainAnchor, test_signer: Signer) -> None:
    a = export_chain_head(head, test_signer, T0)
    b = export_chain_head(head, test_signer, T0.astimezone(timezone(timedelta(hours=5))))
    assert a.export_json() == b.export_json()  # canonical UTC timestamps


def test_round_trip(signed: SignedChainHead, test_signer: Signer, head: ChainAnchor) -> None:
    r = verify_anchor(signed.export_json(), test_signer.public_key_pem)
    assert r == AnchorVerification(True, True, None, head.seq, head.event_hash, ())


def test_round_trip_with_chain(
    signed: SignedChainHead, test_signer: Signer, events: list[dict[str, Any]]
) -> None:
    pem = test_signer.public_key_pem
    r = verify_anchor(signed.export_json(), pem, json.dumps(events))
    assert r.valid and r.chain_ok, r.errors
    r = verify_anchor(signed.export_json(), pem, _records(events))
    assert r.valid and r.chain_ok, r.errors


def test_from_audit_event_record(test_signer: Signer, events: list[dict[str, Any]]) -> None:
    rec = _records(events)[0]
    s = export_chain_head(rec, test_signer, T0)
    assert (s.anchor["seq"], s.anchor["event_hash"]) == (rec.seq, rec.event_hash)
    assert verify_anchor(s.export_json(), test_signer.public_key_pem, _records(events)).valid


def test_rewritten_chain_is_detected(
    signed: SignedChainHead, test_signer: Signer, events: list[dict[str, Any]]
) -> None:
    """An attacker rewrites the chain consistently: the anchored head no longer exists."""
    from lae.audit.hashchain import next_hash

    forged = [dict(e) for e in events]
    prev = forged[0]["prev_hash"]
    for e in forged:
        e["payload_canonical"] = e["payload_canonical"].replace("}", ',"x":1}', 1)
        e["prev_hash"] = prev
        e["event_hash"] = next_hash(prev, e["seq"], e["event_type"], e["payload_canonical"])
        prev = e["event_hash"]
    r = verify_anchor(signed.export_json(), test_signer.public_key_pem, json.dumps(forged))
    assert not r.valid and r.chain_ok is False
    assert r.signature_ok
    assert "anchored event not found in the audit chain" in r.errors


def test_tampered_chain_is_detected(
    signed: SignedChainHead, test_signer: Signer, events: list[dict[str, Any]]
) -> None:
    bad = [dict(e) for e in events]
    bad[0]["payload_canonical"] += " "
    r = verify_anchor(signed.export_json(), test_signer.public_key_pem, json.dumps(bad))
    assert not r.valid and r.chain_ok is False
    assert any("audit chain invalid" in e for e in r.errors)


@pytest.mark.parametrize("chain", ["[]", "{}", "not json", "[1.5]", '[{"seq": 1}]'])
def test_bad_chain_input(signed: SignedChainHead, test_signer: Signer, chain: str) -> None:
    r = verify_anchor(signed.export_json(), test_signer.public_key_pem, chain)
    assert not r.valid and r.chain_ok is False and r.errors


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seq", 1),
        ("event_hash", "0" * 64),
        ("exported_at", "2030-01-01T00:00:00Z"),
        ("key_id", "0" * 16),
        ("anchor_version", "2"),
    ],
)
def test_field_tamper_breaks_signature(
    signed: SignedChainHead, test_signer: Signer, field: str, value: object
) -> None:
    export = json.loads(signed.export_json())
    export["anchor"][field] = value
    r = verify_anchor(json.dumps(export), test_signer.public_key_pem)
    assert not r.valid and not r.signature_ok


def test_signature_bit_flip(signed: SignedChainHead, test_signer: Signer) -> None:
    export = json.loads(signed.export_json())
    sig = bytearray(base64.b64decode(export["signature"]))
    sig[0] ^= 0x01
    export["signature"] = base64.b64encode(bytes(sig)).decode()
    r = verify_anchor(json.dumps(export), test_signer.public_key_pem)
    assert not r.valid and not r.signature_ok and "signature is invalid" in r.errors


def test_wrong_public_key(signed: SignedChainHead) -> None:
    r = verify_anchor(signed.export_json(), Signer.generate().public_key_pem)
    assert not r.valid
    assert "key_id does not match the public key" in r.errors


def test_unusable_public_key(signed: SignedChainHead) -> None:
    r = verify_anchor(signed.export_json(), "not a pem")
    assert not r.valid and "public key could not be loaded" in r.errors


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(extra=1),
        lambda a: a.pop("key_id"),
        lambda a: a.update(seq=0),
        lambda a: a.update(seq=True),
        lambda a: a.update(event_hash="A" * 64),
        lambda a: a.update(exported_at="2026-11-04T09:30:00"),  # naive
        lambda a: a.update(exported_at="yesterday"),
        lambda a: a.update(exported_at=5),
    ],
)
def test_structurally_bad_anchor_even_if_signed(
    signed: SignedChainHead, test_signer: Signer, mutate: Any
) -> None:
    """A key holder signing a malformed document still does not get a valid anchor."""
    anchor = json.loads(signed.anchor_canonical)
    mutate(anchor)
    r = verify_anchor(_resign(anchor, test_signer), test_signer.public_key_pem)
    assert r.signature_ok and not r.valid and r.errors


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        '{"anchor": {}}',
        '{"anchor": {}, "signature": "x", "more": 1}',
        '{"anchor": [], "signature": "x"}',
        '{"anchor": {"seq": 1.0}, "signature": "x"}',
        '{"anchor": {"seq": NaN}, "signature": "x"}',
        '{"anchor": {"seq": 1, "seq": 2}, "signature": "x"}',
    ],
)
def test_malformed_files_never_raise(text: str, test_signer: Signer) -> None:
    r = verify_anchor(text, test_signer.public_key_pem, "[]")
    assert not r.valid and not r.signature_ok and r.chain_ok is False
    assert r.seq is None or isinstance(r.seq, int)


@pytest.mark.parametrize(
    ("seq", "event_hash", "at"),
    [
        (0, "a" * 64, T0),
        (True, "a" * 64, T0),
        (1, "A" * 64, T0),
        (1, "a" * 63, T0),
        (1, "a" * 64, datetime(2026, 1, 1)),
    ],
)
def test_export_rejects_bad_input(
    test_signer: Signer, seq: Any, event_hash: str, at: datetime
) -> None:
    with pytest.raises(ValidationError):
        export_chain_head(ChainAnchor(seq, event_hash), test_signer, at)
