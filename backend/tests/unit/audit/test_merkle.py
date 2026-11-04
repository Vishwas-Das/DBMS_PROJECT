from __future__ import annotations

import dataclasses
import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lae.audit.canonical import canonical_json
from lae.audit.merkle import (
    EMPTY_ROOT,
    MerkleProof,
    MerkleTree,
    ProofStep,
    leaf_hash,
    root_of,
    verify_proof,
)
from lae.contracts.enums import CheckType, PlanAction
from lae.contracts.types import EvidenceLeaf

pytestmark = pytest.mark.unit


def make_leaves(n: int) -> list[EvidenceLeaf]:
    return [
        EvidenceLeaf(
            table=f"app.t{i % 3}",
            key_hmac=hashlib.sha256(f"k{i}".encode()).hexdigest(),
            action=PlanAction.DELETE_ROWS,
            check_types=(CheckType.ROW_ABSENT,),
            passed=i % 5 != 4,
        )
        for i in range(n)
    ]


def rfc6962_mth(hashes: list[bytes]) -> bytes:
    """Independent reference: RFC 6962 §2.1 MTH over pre-computed leaf hashes."""
    n = len(hashes)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return hashes[0]
    k = 1
    while k * 2 < n:
        k *= 2
    return hashlib.sha256(b"\x01" + rfc6962_mth(hashes[:k]) + rfc6962_mth(hashes[k:])).digest()


def flip_bit(hex_str: str, bit: int = 0) -> str:
    b = bytearray(bytes.fromhex(hex_str))
    b[bit // 8] ^= 1 << (bit % 8)
    return b.hex()


def test_leaf_hash_domain_separation() -> None:
    leaf = make_leaves(1)[0]
    assert (
        leaf_hash(leaf) == hashlib.sha256(b"\x00" + canonical_json(leaf.as_canonical())).hexdigest()
    )


def test_empty_tree() -> None:
    t = MerkleTree.build([])
    assert t.root == EMPTY_ROOT == hashlib.sha256(b"").hexdigest()
    assert t.size == 0 and t.leaf_hashes == ()
    with pytest.raises(ValueError):
        t.proof(0)


def test_single_leaf_root_is_leaf_hash() -> None:
    leaves = make_leaves(1)
    t = MerkleTree.build(leaves)
    assert t.root == leaf_hash(leaves[0])
    p = t.proof(0)
    assert p.siblings == ()
    assert verify_proof(leaves[0], p, t.root)


def test_three_leaves_promotes_odd_node() -> None:
    leaves = make_leaves(3)
    h = [bytes.fromhex(leaf_hash(x)) for x in leaves]
    n01 = hashlib.sha256(b"\x01" + h[0] + h[1]).digest()
    assert MerkleTree.build(leaves).root == hashlib.sha256(b"\x01" + n01 + h[2]).hexdigest()


@pytest.mark.parametrize("n", range(18))
def test_all_proofs_for_sizes_0_to_17(n: int) -> None:
    leaves = make_leaves(n)
    tree = MerkleTree.build(leaves)
    ref = rfc6962_mth([bytes.fromhex(h) for h in tree.leaf_hashes]).hex()
    assert tree.root == ref == root_of(leaves)
    for i, leaf in enumerate(leaves):
        proof = tree.proof(i)
        assert proof.leaf_index == i and proof.root == tree.root
        assert verify_proof(leaf, proof, tree.root)
        # flipped bit in the root, the leaf hash or any sibling fails
        assert not verify_proof(leaf, proof, flip_bit(tree.root))
        assert not verify_proof(
            leaf, dataclasses.replace(proof, leaf_hash=flip_bit(proof.leaf_hash)), tree.root
        )
        for j, step in enumerate(proof.siblings):
            bad = list(proof.siblings)
            bad[j] = ProofStep(flip_bit(step.hash, 255), step.side)
            assert not verify_proof(
                leaf, dataclasses.replace(proof, siblings=tuple(bad)), tree.root
            )
            bad[j] = ProofStep(step.hash, "R" if step.side == "L" else "L")
            assert not verify_proof(
                leaf, dataclasses.replace(proof, siblings=tuple(bad)), tree.root
            )
        # a different leaf content fails
        tampered = dataclasses.replace(leaf, passed=not leaf.passed)
        assert not verify_proof(tampered, proof, tree.root)
    with pytest.raises(ValueError):
        tree.proof(n)
    with pytest.raises(ValueError):
        tree.proof(-1)


@pytest.mark.parametrize("n", range(2, 18))
def test_swapped_leaves_change_root_and_fail_proofs(n: int) -> None:
    leaves = make_leaves(n)
    tree = MerkleTree.build(leaves)
    swapped = list(leaves)
    swapped[0], swapped[-1] = swapped[-1], swapped[0]
    assert MerkleTree.build(swapped).root != tree.root
    # proof for index 0 does not verify the leaf now at index 0 (the old last leaf)
    assert not verify_proof(swapped[0], tree.proof(0), tree.root)


def test_proof_rejects_malformed_steps() -> None:
    leaves = make_leaves(4)
    tree = MerkleTree.build(leaves)
    p = tree.proof(1)
    bad_side = (ProofStep(p.siblings[0].hash, "X"), *p.siblings[1:])  # type: ignore[arg-type]
    assert not verify_proof(leaves[1], dataclasses.replace(p, siblings=bad_side), tree.root)
    short = (ProofStep(p.siblings[0].hash[:-2], "L"), *p.siblings[1:])
    assert not verify_proof(leaves[1], dataclasses.replace(p, siblings=short), tree.root)
    upper = (ProofStep(p.siblings[0].hash.upper(), "L"), *p.siblings[1:])
    assert not verify_proof(leaves[1], dataclasses.replace(p, siblings=upper), tree.root)
    # root argument must agree with proof.root
    other = MerkleTree.build(make_leaves(5)).root
    assert not verify_proof(leaves[1], dataclasses.replace(p, root=other), tree.root)
    assert not verify_proof(leaves[1], p, "é" * 64)
    assert not verify_proof(leaves[1], None, tree.root)  # type: ignore[arg-type]


def test_proof_as_dict() -> None:
    tree = MerkleTree.build(make_leaves(3))
    d = tree.proof(2).as_dict()
    assert d["leaf_index"] == 2 and d["root"] == tree.root
    assert d["siblings"] == [{"hash": tree.levels[1][0], "side": "L"}]
    assert isinstance(tree.proof(0), MerkleProof)


def test_check_type_order_does_not_change_leaf_hash() -> None:
    a = EvidenceLeaf(
        "app.t",
        "ab" * 32,
        PlanAction.ANONYMIZE_ROWS,
        (CheckType.ROW_ABSENT, CheckType.COLUMNS_ANONYMIZED),
        True,
    )
    b = dataclasses.replace(a, check_types=(CheckType.COLUMNS_ANONYMIZED, CheckType.ROW_ABSENT))
    assert leaf_hash(a) == leaf_hash(b)


@given(st.lists(st.binary(min_size=32, max_size=32), max_size=40))
def test_root_matches_rfc6962_reference(hashes: list[bytes]) -> None:
    assert MerkleTree.from_leaf_hashes([h.hex() for h in hashes]).root == rfc6962_mth(hashes).hex()
