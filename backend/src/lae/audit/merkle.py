"""Merkle tree over evidence leaves with RFC 6962-style domain separation (architecture.md §13).

    leaf_hash = SHA-256(0x00 || canonical_json(leaf.as_canonical()))
    node_hash = SHA-256(0x01 || left || right)

The tree is built level by level, pairing nodes left to right; an odd last node is promoted to
the next level unchanged. (This yields the same root as RFC 6962's MTH.) The root of an empty
tree is SHA-256(b""). Leaves are used in the order given; callers that need determinism sort
them first (``build_evidence`` does).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from lae.audit.canonical import canonical_json
from lae.audit.hashchain import is_hex64
from lae.contracts.types import EvidenceLeaf

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
EMPTY_ROOT = hashlib.sha256(b"").hexdigest()

Side = Literal["L", "R"]


def leaf_hash(leaf: EvidenceLeaf) -> str:
    return hashlib.sha256(LEAF_PREFIX + canonical_json(leaf.as_canonical())).hexdigest()


def node_hash(left: str, right: str) -> str:
    return hashlib.sha256(NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


@dataclass(frozen=True, slots=True)
class ProofStep:
    """One sibling on the path to the root. ``side`` is where the *sibling* sits: ``"L"`` means
    ``parent = node_hash(sibling, current)``, ``"R"`` means ``parent = node_hash(current,
    sibling)``."""

    hash: str
    side: Side

    def as_dict(self) -> dict[str, Any]:
        return {"hash": self.hash, "side": self.side}


@dataclass(frozen=True, slots=True)
class MerkleProof:
    leaf_index: int
    leaf_hash: str
    siblings: tuple[ProofStep, ...]
    root: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "leaf_index": self.leaf_index,
            "leaf_hash": self.leaf_hash,
            "siblings": [s.as_dict() for s in self.siblings],
            "root": self.root,
        }


@dataclass(frozen=True, slots=True)
class MerkleTree:
    """Immutable tree. ``levels[0]`` are the leaf hashes, ``levels[-1] == (root,)``."""

    levels: tuple[tuple[str, ...], ...]

    @classmethod
    def build(cls, leaves: Sequence[EvidenceLeaf]) -> MerkleTree:
        return cls.from_leaf_hashes([leaf_hash(leaf) for leaf in leaves])

    @classmethod
    def from_leaf_hashes(cls, hashes: Sequence[str]) -> MerkleTree:
        level = tuple(hashes)
        if not level:
            return cls(levels=((),))
        levels = [level]
        while len(level) > 1:
            nxt = [node_hash(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
            if len(level) % 2:
                nxt.append(level[-1])  # odd last node promoted unchanged
            level = tuple(nxt)
            levels.append(level)
        return cls(levels=tuple(levels))

    @property
    def leaf_hashes(self) -> tuple[str, ...]:
        return self.levels[0]

    @property
    def size(self) -> int:
        return len(self.levels[0])

    @property
    def root(self) -> str:
        return self.levels[-1][0] if self.levels[0] else EMPTY_ROOT

    def proof(self, i: int) -> MerkleProof:
        """Inclusion proof for leaf ``i``. Raises ``ValueError`` if ``i`` is out of range."""
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < self.size:
            raise ValueError(f"leaf index {i!r} out of range for tree of size {self.size}")
        steps: list[ProofStep] = []
        idx = i
        for level in self.levels[:-1]:
            if idx % 2 == 1:
                steps.append(ProofStep(level[idx - 1], "L"))
            elif idx + 1 < len(level):
                steps.append(ProofStep(level[idx + 1], "R"))
            # else: promoted, no sibling at this level
            idx //= 2
        return MerkleProof(i, self.levels[0][i], tuple(steps), self.root)


def root_of(leaves: Sequence[EvidenceLeaf]) -> str:
    return MerkleTree.build(leaves).root


def verify_proof(leaf: EvidenceLeaf, proof: MerkleProof, root: str) -> bool:
    """True iff ``leaf`` hashes to ``proof.leaf_hash`` and the path reproduces ``root``
    (which must also equal ``proof.root``). Never raises."""
    try:
        current = leaf_hash(leaf)
        if not hmac.compare_digest(current, proof.leaf_hash):
            return False
        for step in proof.siblings:
            if not is_hex64(step.hash):
                return False
            if step.side == "L":
                current = node_hash(step.hash, current)
            elif step.side == "R":
                current = node_hash(current, step.hash)
            else:
                return False
        return hmac.compare_digest(current, root) and hmac.compare_digest(root, proof.root)
    except Exception:
        return False
