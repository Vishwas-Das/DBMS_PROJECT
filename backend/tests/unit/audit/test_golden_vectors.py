"""Golden vectors: fixed inputs -> fixed bytes, hashes and signatures.

vectors/vectors.json and vectors/test_signing_key.pem must never change without a documented
reason (CLAUDE.md). A failure here means the wire format of the audit evidence changed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from lae.audit.canonical import canonical_json
from lae.audit.evidence import EvidenceBundle, build_evidence, verify_evidence_offline
from lae.audit.hashchain import GENESIS_HASH, next_hash, verify_chain
from lae.audit.hmac_keys import HmacKeyring
from lae.audit.merkle import EMPTY_ROOT, MerkleTree, verify_proof
from lae.audit.signing import Signer, verify_signature
from lae.contracts.enums import Normalization
from lae.contracts.types import AuditEventRecord, EvidenceLeaf, RecordKey, TableRef

pytestmark = pytest.mark.unit

V = dict[str, Any]
EvidenceArgs = dict[str, Any]


def test_canonical_vectors(vectors: V) -> None:
    assert len(vectors["canonical"]) >= 5
    for case in vectors["canonical"]:
        assert canonical_json(case["input"]).hex() == case["canonical_hex"], case["name"]


def test_hmac_vectors(vectors: V) -> None:
    ring = HmacKeyring(bytes.fromhex(vectors["hmac_key_hex"]))
    for case in vectors["hmac"]:
        got = ring.digest(case["value"], Normalization(case["normalization"]))
        assert got == case["digest"], case
    kd = vectors["key_digest"]
    rk = RecordKey(
        TableRef.parse(kd["table"]), tuple(kd["columns"]), tuple(kd["values"]), kd["is_predicate"]
    )
    assert ring.key_digest(rk) == kd["digest"]


def test_chain_vectors(vectors: V) -> None:
    assert vectors["chain"]["genesis"] == GENESIS_HASH
    records = []
    prev = GENESIS_HASH
    for e in vectors["chain"]["events"]:
        assert e["prev_hash"] == prev
        assert next_hash(prev, e["seq"], e["event_type"], e["payload_canonical"]) == e["event_hash"]
        prev = e["event_hash"]
        records.append(
            AuditEventRecord(
                e["seq"],
                e["event_type"],
                "x",
                None,
                "t",
                datetime(2026, 1, 1, tzinfo=UTC),
                e["payload_canonical"],
                e["prev_hash"],
                e["event_hash"],
            )
        )
    r = verify_chain(records)
    assert r.valid and r.head_hash == vectors["chain"]["events"][-1]["event_hash"]
    assert r.head_hash == "6b1557ccd55bec034956be1d04e4c0f981bdc261909cb8aacf610fd1402d4d1f"


def test_merkle_vectors(
    vectors: V, leaf_from_dict: Callable[[dict[str, Any]], EvidenceLeaf]
) -> None:
    m = vectors["merkle"]
    leaves = [leaf_from_dict(d) for d in m["leaves"]]
    tree = MerkleTree.build(leaves)
    assert list(tree.leaf_hashes) == m["leaf_hashes"]
    assert tree.root == m["root"]
    assert tree.root == "7fdee4cec8ad57e4b17c21e24e35c9c8ae26d67a3ab85bf38868a03fd6d33c8e"
    proof = tree.proof(m["proof_index"])
    assert proof.as_dict() == m["proof"]
    assert verify_proof(leaves[m["proof_index"]], proof, m["root"])
    assert MerkleTree.build([]).root == vectors["merkle_empty_root"] == EMPTY_ROOT


def test_signature_vectors(vectors: V, test_signer: Signer) -> None:
    s = vectors["signature"]
    assert test_signer.key_id == s["key_id"] == "4c2fada436d2148b"
    assert test_signer.public_key_pem == s["public_key_pem"]
    msg = bytes.fromhex(s["message_hex"])
    assert test_signer.sign(msg) == s["signature_b64"]  # Ed25519 is deterministic
    assert verify_signature(s["public_key_pem"], msg, s["signature_b64"])


def test_evidence_vectors(vectors: V, evidence_args: EvidenceArgs) -> None:
    ev = vectors["evidence"]
    eb: EvidenceBundle = build_evidence(**evidence_args)
    assert eb.bundle_canonical == ev["bundle_canonical"]
    assert hashlib.sha256(eb.bundle_canonical.encode()).hexdigest() == ev["bundle_sha256"]
    assert ev["bundle_sha256"] == "8c2244b9d4dbd435f39f3910a9c147f73349e39397154b269654ddbfc34b402f"
    assert eb.signature == ev["signature_b64"]
    assert eb.merkle_root == ev["merkle_root"]
    assert eb.leaves_canonical == ev["leaves_canonical"]
    assert hashlib.sha256(eb.export_json().encode()).hexdigest() == ev["export_sha256"]


def test_committed_vector_verifies_offline(vectors: V) -> None:
    ev = vectors["evidence"]
    export = json.dumps(
        {"bundle": json.loads(ev["bundle_canonical"]), "signature": ev["signature_b64"]}
    )
    r = verify_evidence_offline(
        export,
        vectors["signature"]["public_key_pem"],
        ev["leaves_canonical"],
        json.dumps(vectors["chain"]["events"]),
    )
    assert r.valid, r.errors
