"""Envelope cross-check of exported audit events (offline evidence + anchor verifiers).

The DB writer stores ``entity_type`` / ``entity_id`` / ``actor`` / ``occurred_at`` both as
columns and inside the hashed ``payload_canonical`` envelope. The columns are not hashed, so the
offline verifiers compare them with the envelope and report the first mismatching ``seq``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest

from lae.audit.anchor import export_chain_head, verify_anchor
from lae.audit.canonical import canonical_json
from lae.audit.evidence import (
    ChainAnchor,
    EvidenceBundle,
    build_evidence,
    envelope_mismatch,
    first_envelope_mismatch,
    verify_evidence_offline,
)
from lae.audit.hashchain import GENESIS_HASH, next_hash
from lae.audit.signing import Signer
from lae.contracts.types import AuditEventRecord

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 4, 9, 30, 0, 123456, tzinfo=UTC)
ENTITY = UUID("00000000-0000-4000-8000-0000000000e1")


def _envelope_payload(i: int, occurred_at: datetime) -> str:
    return canonical_json(
        {
            "envelope": {
                "entity_type": "erasure_plan",
                "entity_id": ENTITY,
                "actor": f"actor-{i}",
                "occurred_at": occurred_at,
            },
            "data": {"n": i},
        }
    ).decode()


def make_events(n: int = 4) -> list[dict[str, Any]]:
    """An export in the AuditRepo.export_chain format (occurred_at without forced µs)."""
    prev = GENESIS_HASH
    out: list[dict[str, Any]] = []
    for i in range(n):
        seq = 10 + 3 * i  # gaps are legal
        at = T0 + timedelta(seconds=i) if i != 1 else T0.replace(microsecond=0)
        payload = _envelope_payload(i, at)
        h = next_hash(prev, seq, "PLAN_CREATED", payload)
        out.append(
            {
                "seq": seq,
                "event_type": "PLAN_CREATED",
                "entity_type": "erasure_plan",
                "entity_id": str(ENTITY),
                "actor": f"actor-{i}",
                "occurred_at": at.isoformat().replace("+00:00", "Z"),
                "payload_canonical": payload,
                "prev_hash": prev,
                "event_hash": h,
            }
        )
        prev = h
    return out


def records(events: list[dict[str, Any]]) -> list[AuditEventRecord]:
    return [
        AuditEventRecord(
            e["seq"],
            e["event_type"],
            e["entity_type"],
            UUID(e["entity_id"]),
            e["actor"],
            datetime.fromisoformat(e["occurred_at"]),
            e["payload_canonical"],
            e["prev_hash"],
            e["event_hash"],
        )
        for e in events
    ]


@pytest.fixture
def events() -> list[dict[str, Any]]:
    return make_events()


@pytest.fixture
def anchored_bundle(
    evidence_args: dict[str, Any], events: list[dict[str, Any]], test_signer: Signer
) -> EvidenceBundle:
    args = dict(evidence_args)
    args["chain_anchor"] = ChainAnchor(events[-1]["seq"], events[-1]["event_hash"])
    return build_evidence(**args)


def _verify(bundle: EvidenceBundle, signer: Signer, events: list[dict[str, Any]]) -> Any:
    return verify_evidence_offline(
        bundle.export_json(), signer.public_key_pem, chain_json=json.dumps(events)
    )


def test_consistent_export_verifies(
    anchored_bundle: EvidenceBundle, test_signer: Signer, events: list[dict[str, Any]]
) -> None:
    r = _verify(anchored_bundle, test_signer, events)
    assert r.valid and r.chain_ok is True, r.errors
    assert first_envelope_mismatch(records(events)) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", "mallory"),
        ("entity_type", "erasure_request"),
        ("entity_id", "00000000-0000-4000-8000-0000000000e2"),
        ("entity_id", None),
        ("occurred_at", "2020-01-01T00:00:00Z"),
        ("occurred_at", "2026-11-04T09:30:01"),  # naive: never equal to an aware instant
        ("occurred_at", None),
    ],
)
def test_rewritten_column_fails_at_its_seq(
    anchored_bundle: EvidenceBundle,
    test_signer: Signer,
    events: list[dict[str, Any]],
    field: str,
    value: object,
) -> None:
    events[2][field] = value
    r = _verify(anchored_bundle, test_signer, events)
    assert r.signature_ok and r.chain_ok is False and not r.valid
    assert r.errors == (f"audit chain invalid at seq {events[2]['seq']} (envelope_mismatch)",)


def test_equivalent_spellings_are_not_mismatches(events: list[dict[str, Any]]) -> None:
    e = dict(events[0])
    e["entity_id"] = str(ENTITY).upper()
    e["occurred_at"] = (T0.astimezone(timezone(timedelta(hours=5)))).isoformat()
    assert not envelope_mismatch(e, e["payload_canonical"])


def test_earliest_failure_wins(
    anchored_bundle: EvidenceBundle, test_signer: Signer, events: list[dict[str, Any]]
) -> None:
    events[3]["actor"] = "mallory"  # envelope mismatch later
    events[1]["payload_canonical"] = events[1]["payload_canonical"].replace('"n":1', '"n":7')
    r = _verify(anchored_bundle, test_signer, events)
    assert r.errors == (f"audit chain invalid at seq {events[1]['seq']} (event_hash_mismatch)",)

    events = make_events()
    events[1]["actor"] = "mallory"  # envelope mismatch first
    events[3]["payload_canonical"] = "{}"
    r = _verify(anchored_bundle, test_signer, events)
    assert r.errors == (f"audit chain invalid at seq {events[1]['seq']} (envelope_mismatch)",)


def test_missing_columns_and_envelope_free_payloads_are_not_compared(
    events: list[dict[str, Any]],
) -> None:
    e = {k: v for k, v in events[0].items() if k not in ("actor", "occurred_at")}
    assert not envelope_mismatch(e, e["payload_canonical"])
    assert not envelope_mismatch({"actor": "x"}, '{"data":{}}')
    assert not envelope_mismatch({"actor": "x"}, '{"envelope":"flat"}')
    assert not envelope_mismatch({"actor": "x"}, "[1,2]")
    assert not envelope_mismatch({"actor": "x"}, "not json")


def test_anchor_verifier_checks_envelopes_for_text_and_records(
    events: list[dict[str, Any]], test_signer: Signer
) -> None:
    signed = export_chain_head(
        ChainAnchor(events[-1]["seq"], events[-1]["event_hash"]), test_signer, T0
    )
    pem = test_signer.public_key_pem
    assert verify_anchor(signed.export_json(), pem, json.dumps(events)).valid
    assert verify_anchor(signed.export_json(), pem, records(events)).valid

    events[0]["actor"] = "mallory"
    want = (f"audit chain invalid at seq {events[0]['seq']} (envelope_mismatch)",)
    by_text = verify_anchor(signed.export_json(), pem, json.dumps(events))
    by_records = verify_anchor(signed.export_json(), pem, records(events))
    assert by_text.chain_ok is False and by_text.errors == want
    assert by_records.chain_ok is False and by_records.errors == want


def test_anchor_not_found_message_is_unchanged(
    events: list[dict[str, Any]], test_signer: Signer
) -> None:
    signed = export_chain_head(ChainAnchor(999, "ab" * 32), test_signer, T0)
    r = verify_anchor(signed.export_json(), test_signer.public_key_pem, json.dumps(events))
    assert r.errors == ("anchored event not found in the audit chain",)
