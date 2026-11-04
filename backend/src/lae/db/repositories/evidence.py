"""EvidenceRepo (evidence bundles) and SigningKeyRepo (public signing keys)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from lae.db.models import EvidenceBundle, SigningKey
from lae.db.records import EvidenceRecord, SigningKeyRecord
from lae.db.repositories._base import Repo
from lae.errors import ValidationError


def _evidence(m: EvidenceBundle) -> EvidenceRecord:
    return EvidenceRecord(
        id=m.id,
        plan_id=m.plan_id,
        verification_run_id=m.verification_run_id,
        merkle_root=m.merkle_root,
        leaf_count=m.leaf_count,
        leaves_canonical=m.leaves_canonical,
        chain_anchor_seq=m.chain_anchor_seq,
        chain_anchor_hash=m.chain_anchor_hash,
        bundle_canonical=m.bundle_canonical,
        signature=m.signature,
        signing_key_id=m.signing_key_id,
        issued_at=m.issued_at,
    )


def _key(m: SigningKey) -> SigningKeyRecord:
    return SigningKeyRecord(m.key_id, m.algorithm, m.public_key_pem, m.created_at, m.retired_at)


class EvidenceRepo(Repo):
    """Methods:
    save(*, plan_id, verification_run_id, merkle_root, leaf_count, leaves_canonical,
         chain_anchor_seq, chain_anchor_hash, bundle_canonical, signature, signing_key_id,
         issued_at) -> EvidenceRecord                (one bundle per plan)
    get(evidence_id) -> EvidenceRecord
    for_plan(plan_id) -> EvidenceRecord | None
    """

    def save(
        self,
        *,
        plan_id: UUID,
        verification_run_id: UUID,
        merkle_root: str,
        leaf_count: int,
        leaves_canonical: str,
        chain_anchor_seq: int,
        chain_anchor_hash: str,
        bundle_canonical: str,
        signature: str,
        signing_key_id: str,
        issued_at: datetime,
    ) -> EvidenceRecord:
        m = EvidenceBundle(
            plan_id=plan_id,
            verification_run_id=verification_run_id,
            merkle_root=merkle_root,
            leaf_count=leaf_count,
            leaves_canonical=leaves_canonical,
            chain_anchor_seq=chain_anchor_seq,
            chain_anchor_hash=chain_anchor_hash,
            bundle_canonical=bundle_canonical,
            signature=signature,
            signing_key_id=signing_key_id,
            issued_at=issued_at,
        )
        self.session.add(m)
        self.session.flush()
        return _evidence(m)

    def get(self, evidence_id: UUID) -> EvidenceRecord:
        return _evidence(self._get(EvidenceBundle, evidence_id))

    def for_plan(self, plan_id: UUID) -> EvidenceRecord | None:
        m = self.session.scalars(
            select(EvidenceBundle).where(EvidenceBundle.plan_id == plan_id)
        ).first()
        return None if m is None else _evidence(m)


class SigningKeyRepo(Repo):
    """Methods:
    register(key_id, public_key_pem) -> SigningKeyRecord   (idempotent for the same PEM)
    get(key_id) -> SigningKeyRecord
    list_keys() -> list[SigningKeyRecord]                       (oldest first)
    active() -> SigningKeyRecord | None                    (newest non-retired key)
    retire(key_id, retired_at) -> SigningKeyRecord
    """

    def register(self, key_id: str, public_key_pem: str) -> SigningKeyRecord:
        existing = self.session.get(SigningKey, key_id)
        if existing is not None:
            if existing.public_key_pem.strip() != public_key_pem.strip():
                raise ValidationError(
                    "signing key id already registered with a different public key",
                    details={"key_id": key_id},
                )
            return _key(existing)
        m = SigningKey(key_id=key_id, algorithm="Ed25519", public_key_pem=public_key_pem)
        self.session.add(m)
        self.session.flush()
        self.session.refresh(m)
        return _key(m)

    def get(self, key_id: str) -> SigningKeyRecord:
        return _key(self._get(SigningKey, key_id))

    def list_keys(self) -> list[SigningKeyRecord]:
        q = select(SigningKey).order_by(SigningKey.created_at, SigningKey.key_id)
        return [_key(m) for m in self.session.scalars(q)]

    def active(self) -> SigningKeyRecord | None:
        q = (
            select(SigningKey)
            .where(SigningKey.retired_at.is_(None))
            .order_by(SigningKey.created_at.desc(), SigningKey.key_id)
            .limit(1)
        )
        m = self.session.scalars(q).first()
        return None if m is None else _key(m)

    def retire(self, key_id: str, retired_at: datetime) -> SigningKeyRecord:
        m = self._get(SigningKey, key_id)
        m.retired_at = retired_at
        self.session.flush()
        return _key(m)
