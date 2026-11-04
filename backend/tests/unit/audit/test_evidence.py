from __future__ import annotations

import base64
import dataclasses
import json
import random
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from lae.audit.canonical import canonical_json
from lae.audit.evidence import (
    BUNDLE_FIELDS,
    BUNDLE_VERSION,
    ChainAnchor,
    EvidenceBundle,
    build_evidence,
    verify_evidence_offline,
)
from lae.audit.merkle import EMPTY_ROOT, MerkleTree
from lae.audit.signing import Signer
from lae.contracts.enums import ExecutionMode, ExecutionStatus, VerificationStatus
from lae.contracts.types import CheckResult, VerificationReport
from lae.errors import CanonicalizationError, ValidationError

pytestmark = pytest.mark.unit

EvidenceArgs = dict[str, Any]


def export_dict(bundle: EvidenceBundle) -> dict[str, Any]:
    d: dict[str, Any] = json.loads(bundle.export_json())
    return d


def resign(bundle_obj: dict[str, Any], signer: Signer) -> str:
    """Re-sign a modified bundle dict (simulates an attacker who holds a key)."""
    return json.dumps({"bundle": bundle_obj, "signature": signer.sign(canonical_json(bundle_obj))})


# ------------------------------------------------------------------ round trip


def test_round_trip_full(bundle: EvidenceBundle, test_signer: Signer, chain_json: str) -> None:
    r = verify_evidence_offline(
        bundle.export_json(), test_signer.public_key_pem, bundle.leaves_canonical, chain_json
    )
    assert r.valid, r.errors
    assert r.signature_ok and r.merkle_ok is True and r.chain_ok is True and r.errors == ()


def test_round_trip_signature_only(bundle: EvidenceBundle, test_signer: Signer) -> None:
    r = verify_evidence_offline(bundle.export_json(), test_signer.public_key_pem)
    assert r.valid and r.merkle_ok is None and r.chain_ok is None


def test_pretty_printed_export_still_verifies(bundle: EvidenceBundle, test_signer: Signer) -> None:
    pretty = json.dumps(export_dict(bundle), indent=4, ensure_ascii=True)
    assert verify_evidence_offline(pretty, test_signer.public_key_pem).valid


def test_bundle_shape(bundle: EvidenceBundle, evidence_args: EvidenceArgs) -> None:
    b = bundle.bundle
    assert set(b) == BUNDLE_FIELDS
    assert b["bundle_version"] == BUNDLE_VERSION == "1"
    assert set(b["execution"]) == {"run_id", "mode", "committed_at", "step_counts"}
    assert set(b["verification"]) == {"status", "checks"}
    assert set(b["chain_anchor"]) == {"seq", "event_hash"}
    assert b["leaf_count"] == bundle.leaf_count == len(evidence_args["leaves"])
    assert b["merkle_root"] == bundle.merkle_root == MerkleTree.build(bundle.leaves).root
    assert canonical_json(dict(b)).decode() == bundle.bundle_canonical
    assert b["signing_key_id"] == bundle.signing_key_id == evidence_args["signer"].key_id
    assert b["execution"]["step_counts"] == {"APPLIED": 2, "FAILED": 0, "SKIPPED": 1}
    assert bundle.export_json() == canonical_json(export_dict(bundle)).decode()


def test_leaf_and_check_order_do_not_matter(evidence_args: EvidenceArgs) -> None:
    a = build_evidence(**evidence_args)
    shuffled = dict(evidence_args)
    leaves = list(evidence_args["leaves"])
    random.Random(7).shuffle(leaves)
    shuffled["leaves"] = leaves
    v: VerificationReport = evidence_args["verification"]
    shuffled["verification"] = dataclasses.replace(v, checks=tuple(reversed(v.checks)))
    b = build_evidence(**shuffled)
    assert a.bundle_canonical == b.bundle_canonical and a.signature == b.signature


def test_empty_leaves(evidence_args: EvidenceArgs, test_signer: Signer) -> None:
    eb = build_evidence(**{**evidence_args, "leaves": []})
    assert eb.merkle_root == EMPTY_ROOT and eb.leaf_count == 0 and eb.leaves_canonical == "[]"
    assert verify_evidence_offline(eb.export_json(), test_signer.public_key_pem, "[]").valid


# ------------------------------------------------------------------ build validation


def _with(args: EvidenceArgs, **changes: Any) -> EvidenceArgs:
    return {**args, **changes}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: _with(a, plan=dataclasses.replace(a["plan"], plan_hash="xyz")),
        lambda a: _with(a, plan=dataclasses.replace(a["plan"], record_set_hash="AB" * 32)),
        lambda a: _with(a, plan=dataclasses.replace(a["plan"], schema_fingerprint="")),
        lambda a: _with(a, chain_anchor=ChainAnchor(0, "ab" * 32)),
        lambda a: _with(a, chain_anchor=ChainAnchor(True, "ab" * 32)),  # type: ignore[arg-type]
        lambda a: _with(a, chain_anchor=ChainAnchor(3, "nothex")),
        lambda a: _with(a, issued_at=datetime(2026, 1, 1)),
        lambda a: _with(
            a,
            execution=dataclasses.replace(a["execution"], committed_at=datetime(2026, 1, 1)),
        ),
        lambda a: _with(
            a,
            execution=dataclasses.replace(
                a["execution"],
                result=dataclasses.replace(a["execution"].result, mode=ExecutionMode.DRY_RUN),
            ),
        ),
        lambda a: _with(
            a,
            execution=dataclasses.replace(
                a["execution"],
                result=dataclasses.replace(
                    a["execution"].result, status=ExecutionStatus.ROLLED_BACK
                ),
            ),
        ),
        lambda a: _with(
            a,
            verification=dataclasses.replace(a["verification"], status=VerificationStatus.RUNNING),
        ),
        lambda a: _with(
            a, leaves=[dataclasses.replace(a["leaves"][0], key_hmac="alice@example.com")]
        ),
    ],
)
def test_build_rejects_invalid_inputs(
    evidence_args: EvidenceArgs, mutate: Callable[[EvidenceArgs], EvidenceArgs]
) -> None:
    with pytest.raises(ValidationError):
        build_evidence(**mutate(evidence_args))


def test_build_rejects_floats_in_checks(evidence_args: EvidenceArgs) -> None:
    v: VerificationReport = evidence_args["verification"]
    c: CheckResult = v.checks[0]
    bad = dataclasses.replace(v, checks=(dataclasses.replace(c, observed={"ratio": 0.5}),))
    with pytest.raises(CanonicalizationError):
        build_evidence(**_with(evidence_args, verification=bad))


def test_failed_verification_can_still_be_evidenced(
    evidence_args: EvidenceArgs, test_signer: Signer
) -> None:
    v = dataclasses.replace(evidence_args["verification"], status=VerificationStatus.FAILED)
    eb = build_evidence(**_with(evidence_args, verification=v))
    assert eb.bundle["verification"]["status"] == "FAILED"
    assert verify_evidence_offline(eb.export_json(), test_signer.public_key_pem).valid


# ------------------------------------------------------------------ tamper: bundle


BUNDLE_TAMPERS: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("plan_hash", lambda b: b.__setitem__("plan_hash", "00" * 32)),
    ("plan_id", lambda b: b.__setitem__("plan_id", "00000000-0000-4000-8000-000000000000")),
    ("snapshot_fingerprint", lambda b: b.__setitem__("snapshot_fingerprint", "11" * 32)),
    ("record_set_hash", lambda b: b.__setitem__("record_set_hash", "22" * 32)),
    ("merkle_root", lambda b: b.__setitem__("merkle_root", "33" * 32)),
    ("leaf_count", lambda b: b.__setitem__("leaf_count", b["leaf_count"] + 1)),
    ("issued_at", lambda b: b.__setitem__("issued_at", "2030-01-01T00:00:00.000000Z")),
    ("signing_key_id", lambda b: b.__setitem__("signing_key_id", "0" * 16)),
    ("bundle_version", lambda b: b.__setitem__("bundle_version", "2")),
    ("mode", lambda b: b["execution"].__setitem__("mode", "DRY_RUN")),
    ("run_id", lambda b: b["execution"].__setitem__("run_id", "x")),
    ("step_counts", lambda b: b["execution"]["step_counts"].__setitem__("FAILED", 1)),
    ("status", lambda b: b["verification"].__setitem__("status", "FAILED")),
    ("check_passed", lambda b: b["verification"]["checks"][0].__setitem__("passed", False)),
    (
        "check_observed",
        lambda b: b["verification"]["checks"][1]["observed"].__setitem__("count", 1),
    ),
    ("check_removed", lambda b: b["verification"]["checks"].pop()),
    ("anchor_seq", lambda b: b["chain_anchor"].__setitem__("seq", 4)),
    ("anchor_hash", lambda b: b["chain_anchor"].__setitem__("event_hash", "44" * 32)),
    ("extra_field", lambda b: b.__setitem__("note", "hi")),
    ("removed_field", lambda b: b.pop("issued_at")),
]


@pytest.mark.parametrize(("name", "tamper"), BUNDLE_TAMPERS, ids=[t[0] for t in BUNDLE_TAMPERS])
def test_any_bundle_change_breaks_signature(
    bundle: EvidenceBundle,
    test_signer: Signer,
    name: str,
    tamper: Callable[[dict[str, Any]], None],
) -> None:
    exp = export_dict(bundle)
    tamper(exp["bundle"])
    r = verify_evidence_offline(json.dumps(exp), test_signer.public_key_pem)
    assert not r.valid and not r.signature_ok
    assert "signature is invalid" in r.errors


@pytest.mark.parametrize("name", ["extra_field", "removed_field", "bundle_version", "leaf_count"])
def test_structural_errors_even_if_resigned(
    bundle: EvidenceBundle, test_signer: Signer, name: str
) -> None:
    exp = export_dict(bundle)
    dict(BUNDLE_TAMPERS)[name](exp["bundle"])
    r = verify_evidence_offline(
        resign(exp["bundle"], test_signer), test_signer.public_key_pem, bundle.leaves_canonical
    )
    assert r.signature_ok and not r.valid and r.errors


def test_flipped_signature_bit(bundle: EvidenceBundle, test_signer: Signer) -> None:
    exp = export_dict(bundle)
    raw = bytearray(base64.b64decode(exp["signature"]))
    raw[10] ^= 0x80
    exp["signature"] = base64.b64encode(raw).decode()
    assert not verify_evidence_offline(json.dumps(exp), test_signer.public_key_pem).valid


def test_wrong_public_key(bundle: EvidenceBundle) -> None:
    other = Signer.generate()
    r = verify_evidence_offline(bundle.export_json(), other.public_key_pem)
    assert not r.valid and not r.signature_ok
    assert "signing_key_id does not match the public key" in r.errors


def test_resigned_by_attacker_key_detected_by_key_id(
    bundle: EvidenceBundle, test_signer: Signer
) -> None:
    attacker = Signer.generate()
    exp = export_dict(bundle)
    exp["bundle"]["plan_hash"] = "00" * 32
    r = verify_evidence_offline(resign(exp["bundle"], attacker), attacker.public_key_pem)
    # the signature is valid for the attacker key, but the bundle names the real key id
    assert r.signature_ok and not r.valid
    # and against the real key it fails outright
    assert not verify_evidence_offline(
        resign(exp["bundle"], attacker), test_signer.public_key_pem
    ).signature_ok


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"bundle": {}}',
        '{"bundle": [], "signature": "x"}',
        '{"bundle": {}, "signature": 5}',
        '{"bundle": {"a": 1.5}, "signature": "x"}',
        '{"bundle": {"a": NaN}, "signature": "x"}',
        '{"bundle": {}, "signature": "x", "signature": "y"}',
        '{"bundle": {}, "signature": "x", "extra": 1}',
    ],
)
def test_malformed_bundle_file_never_raises(text: str, test_signer: Signer) -> None:
    r = verify_evidence_offline(text, test_signer.public_key_pem, "[]", "[]")
    assert not r.valid and r.errors
    assert r.merkle_ok is not True and r.chain_ok is not True


def test_float_or_duplicate_inside_signed_bundle_rejected(
    bundle: EvidenceBundle, test_signer: Signer
) -> None:
    text = bundle.export_json().replace('"leaf_count":5', '"leaf_count":5.0')
    assert not verify_evidence_offline(text, test_signer.public_key_pem).valid
    dup = bundle.export_json().replace('"leaf_count":5', '"leaf_count":5,"leaf_count":6')
    assert not verify_evidence_offline(dup, test_signer.public_key_pem).valid


def test_unloadable_public_key(bundle: EvidenceBundle) -> None:
    r = verify_evidence_offline(bundle.export_json(), "garbage")
    assert not r.valid and "public key could not be loaded" in r.errors


# ------------------------------------------------------------------ tamper: leaves


def _leaves(bundle: EvidenceBundle) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = json.loads(bundle.leaves_canonical)
    return out


LEAF_TAMPERS: list[tuple[str, Callable[[list[dict[str, Any]]], None]]] = [
    ("drop", lambda ls: ls.pop()),
    ("duplicate", lambda ls: ls.append(dict(ls[0]))),
    ("swap", lambda ls: ls.__setitem__(slice(0, 2), [ls[1], ls[0]])),
    ("flip_passed", lambda ls: ls[0].__setitem__("passed", not ls[0]["passed"])),
    ("change_table", lambda ls: ls[2].__setitem__("table", "app.other")),
    ("change_hmac", lambda ls: ls[1].__setitem__("key_hmac", "00" * 32)),
    ("change_action", lambda ls: ls[0].__setitem__("action", "MANUAL_REVIEW")),
    (
        "unsorted_checks",
        lambda ls: ls[0].__setitem__("check_types", ["ROW_ABSENT", "CASCADE_COMPLETE"]),
    ),
    ("bad_check", lambda ls: ls[0].__setitem__("check_types", ["NOPE"])),
    ("bad_action", lambda ls: ls[0].__setitem__("action", "NOPE")),
    ("extra_key", lambda ls: ls[0].__setitem__("value", "alice")),
    ("wrong_type", lambda ls: ls[0].__setitem__("passed", "yes")),
    ("not_object", lambda ls: ls.__setitem__(0, 5)),
]


@pytest.mark.parametrize(("name", "tamper"), LEAF_TAMPERS, ids=[t[0] for t in LEAF_TAMPERS])
def test_leaf_tampering_fails_merkle(
    bundle: EvidenceBundle,
    test_signer: Signer,
    name: str,
    tamper: Callable[[list[dict[str, Any]]], None],
) -> None:
    leaves = _leaves(bundle)
    tamper(leaves)
    r = verify_evidence_offline(
        bundle.export_json(), test_signer.public_key_pem, json.dumps(leaves)
    )
    assert r.signature_ok and r.merkle_ok is False and not r.valid and r.errors


@pytest.mark.parametrize("text", ["", "{}", "[1.5]", "nope"])
def test_malformed_leaves_file(bundle: EvidenceBundle, test_signer: Signer, text: str) -> None:
    r = verify_evidence_offline(bundle.export_json(), test_signer.public_key_pem, text)
    assert r.merkle_ok is False and not r.valid


def test_leaf_count_mismatch_with_matching_root(
    bundle: EvidenceBundle, test_signer: Signer
) -> None:
    exp = export_dict(bundle)
    exp["bundle"]["leaf_count"] = 99
    r = verify_evidence_offline(
        resign(exp["bundle"], test_signer), test_signer.public_key_pem, bundle.leaves_canonical
    )
    assert r.merkle_ok is False
    assert "leaf_count does not match the number of leaves" in r.errors


# ------------------------------------------------------------------ tamper: chain


def _events(chain_json: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = json.loads(chain_json)
    return out


CHAIN_TAMPERS: list[tuple[str, Callable[[list[dict[str, Any]]], Any]]] = [
    ("payload", lambda es: es[1].__setitem__("payload_canonical", '{"items":4}')),
    ("reorder", lambda es: es.__setitem__(slice(0, 2), [es[1], es[0]])),
    ("drop_middle", lambda es: es.pop(1)),
    ("drop_anchor", lambda es: es.pop()),
    ("anchor_hash", lambda es: es[-1].__setitem__("event_hash", "55" * 32)),
    ("event_type", lambda es: es[0].__setitem__("event_type", "PLAN_CREATED")),
    ("seq", lambda es: es[2].__setitem__("seq", 6)),
    ("prev_link", lambda es: es[2].__setitem__("prev_hash", "66" * 32)),
    ("empty", lambda es: es.clear()),
    ("missing_field", lambda es: es[0].pop("event_hash")),
    ("bad_seq_type", lambda es: es[0].__setitem__("seq", "1")),
    ("bad_entity_id", lambda es: es[0].__setitem__("entity_id", "not-a-uuid")),
    ("bad_occurred_at", lambda es: es[0].__setitem__("occurred_at", "yesterday")),
    ("not_object", lambda es: es.__setitem__(0, [])),
]


@pytest.mark.parametrize(("name", "tamper"), CHAIN_TAMPERS, ids=[t[0] for t in CHAIN_TAMPERS])
def test_chain_tampering_fails(
    bundle: EvidenceBundle,
    test_signer: Signer,
    chain_json: str,
    name: str,
    tamper: Callable[[list[dict[str, Any]]], Any],
) -> None:
    events = _events(chain_json)
    tamper(events)
    r = verify_evidence_offline(
        bundle.export_json(), test_signer.public_key_pem, chain_json=json.dumps(events)
    )
    assert r.signature_ok and r.chain_ok is False and not r.valid and r.errors


def test_chain_with_optional_fields(
    bundle: EvidenceBundle, test_signer: Signer, chain_json: str
) -> None:
    events = _events(chain_json)
    for e in events:
        e.update(
            entity_type="plan",
            entity_id="00000000-0000-4000-8000-0000000000a1",
            actor="tester",
            occurred_at="2026-11-04T10:00:00.000000Z",
        )
    r = verify_evidence_offline(
        bundle.export_json(), test_signer.public_key_pem, chain_json=json.dumps(events)
    )
    assert r.valid and r.chain_ok is True


def test_chain_segment_without_genesis(
    bundle: EvidenceBundle, test_signer: Signer, chain_json: str
) -> None:
    events = _events(chain_json)[1:]  # segment still contains the anchor
    r = verify_evidence_offline(
        bundle.export_json(), test_signer.public_key_pem, chain_json=json.dumps(events)
    )
    assert r.valid and r.chain_ok is True


def test_chain_not_a_list(bundle: EvidenceBundle, test_signer: Signer) -> None:
    r = verify_evidence_offline(bundle.export_json(), test_signer.public_key_pem, chain_json="{}")
    assert r.chain_ok is False and not r.valid


def test_malformed_anchor_resigned(
    bundle: EvidenceBundle, test_signer: Signer, chain_json: str
) -> None:
    exp = export_dict(bundle)
    exp["bundle"]["chain_anchor"] = "seq5"
    r = verify_evidence_offline(
        resign(exp["bundle"], test_signer), test_signer.public_key_pem, chain_json=chain_json
    )
    assert r.chain_ok is False and "chain_anchor is malformed" in r.errors


def test_no_raw_values_in_bundle(bundle: EvidenceBundle) -> None:
    text = bundle.export_json() + bundle.leaves_canonical
    for raw in ("alice", "@", "Zoë"):
        assert raw not in text


def test_export_is_offline_only() -> None:
    import lae.audit.evidence as ev

    # The offline verifier module must not depend on DB/network libraries.
    src = Path(ev.__file__).read_text(encoding="utf-8")
    for banned in ("sqlalchemy", "psycopg", "socket", "urllib", "requests", "httpx", "lae.db"):
        assert f"import {banned}" not in src and f"from {banned}" not in src


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("merkle_root", "XYZ", "merkle_root is not 64-hex"),
        ("leaf_count", -1, "leaf_count is not a non-negative int"),
        ("leaf_count", True, "leaf_count is not a non-negative int"),
    ],
)
def test_resigned_bad_fields_reported(
    bundle: EvidenceBundle, test_signer: Signer, field: str, value: object, error: str
) -> None:
    exp = export_dict(bundle)
    exp["bundle"][field] = value
    r = verify_evidence_offline(resign(exp["bundle"], test_signer), test_signer.public_key_pem)
    assert r.signature_ok and not r.valid and error in r.errors


def test_chain_anchor_from_event(chain_json: str) -> None:
    from datetime import UTC

    from lae.contracts.types import AuditEventRecord

    e = _events(chain_json)[-1]
    rec = AuditEventRecord(
        e["seq"],
        e["event_type"],
        "plan",
        None,
        "t",
        datetime(2026, 1, 1, tzinfo=UTC),
        e["payload_canonical"],
        e["prev_hash"],
        e["event_hash"],
    )
    assert ChainAnchor.from_event(rec) == ChainAnchor(e["seq"], e["event_hash"])
