"""Signed external chain-head anchors (architecture.md §13, threat model).

A DB superuser can rewrite the whole audit chain consistently. Periodically exporting the chain
head as a signed document *outside* the database makes such a rewrite detectable: the anchored
``(seq, event_hash)`` must still be part of any later, valid chain export.

Anchor document (canonical JSON, ``anchor_version`` "1")::

    {"anchor_version": "1", "seq": n, "event_hash": "<hex64>",
     "exported_at": "<iso Z>", "key_id": "<hex16>"}

Export file: canonical ``{"anchor": {...}, "signature": "<base64 Ed25519 over
canonical_json(anchor)>"}``. Any other top-level key is rejected. The anchor holds no personal
data. Pure: no DB, no network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from lae.audit.canonical import canonical_json
from lae.audit.evidence import ChainAnchor, _InputError, _parse_events, _strict_loads
from lae.audit.hashchain import is_hex64, verify_chain
from lae.audit.signing import Signer, public_key_id, verify_signature
from lae.contracts.types import AuditEventRecord
from lae.errors import SignatureError, ValidationError

__all__ = [
    "ANCHOR_FIELDS",
    "ANCHOR_VERSION",
    "AnchorVerification",
    "SignedChainHead",
    "export_chain_head",
    "verify_anchor",
]

ANCHOR_VERSION = "1"
ANCHOR_FIELDS = frozenset({"anchor_version", "seq", "event_hash", "exported_at", "key_id"})
_EXPORT_FIELDS = frozenset({"anchor", "signature"})


@dataclass(frozen=True, slots=True)
class SignedChainHead:
    anchor: Mapping[str, Any]
    anchor_canonical: str
    signature: str

    def export_json(self) -> str:
        """The export file: canonical ``{"anchor": ..., "signature": ...}``."""
        return canonical_json(
            {"anchor": json.loads(self.anchor_canonical), "signature": self.signature}
        ).decode("utf-8")


@dataclass(frozen=True, slots=True)
class AnchorVerification:
    valid: bool
    signature_ok: bool
    chain_ok: bool | None
    seq: int | None
    event_hash: str | None
    errors: tuple[str, ...]


def export_chain_head(
    head: ChainAnchor | AuditEventRecord, signer: Signer, exported_at: datetime
) -> SignedChainHead:
    """Sign the chain head ``(seq, event_hash)``. Raises ``ValidationError`` on bad input."""
    seq, event_hash = head.seq, head.event_hash
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValidationError("anchor seq must be a positive int")
    if not is_hex64(event_hash):
        raise ValidationError("anchor event_hash must be 64 lowercase hex characters")
    if not isinstance(exported_at, datetime) or exported_at.utcoffset() is None:
        raise ValidationError("exported_at must be a timezone-aware datetime")
    anchor = {
        "anchor_version": ANCHOR_VERSION,
        "seq": seq,
        "event_hash": event_hash,
        "exported_at": exported_at,
        "key_id": signer.key_id,
    }
    canonical = canonical_json(anchor)
    return SignedChainHead(
        anchor=MappingProxyType(json.loads(canonical)),
        anchor_canonical=canonical.decode("utf-8"),
        signature=signer.sign(canonical),
    )


def _anchor_errors(anchor: Mapping[str, Any], public_key_pem: str) -> list[str]:
    errors: list[str] = []
    if set(anchor) != ANCHOR_FIELDS:
        errors.append("anchor fields mismatch")
    if anchor.get("anchor_version") != ANCHOR_VERSION:
        errors.append("unsupported anchor_version")
    seq = anchor.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        errors.append("anchor seq is not a positive int")
    if not is_hex64(anchor.get("event_hash")):
        errors.append("anchor event_hash is not 64-hex")
    exported_at = anchor.get("exported_at")
    try:
        if (
            not isinstance(exported_at, str)
            or datetime.fromisoformat(exported_at).utcoffset() is None
        ):
            raise ValueError
    except ValueError:
        errors.append("anchor exported_at is not an aware ISO-8601 timestamp")
    try:
        if anchor.get("key_id") != public_key_id(public_key_pem):
            errors.append("key_id does not match the public key")
    except SignatureError:
        pass  # reported by the signature step
    return errors


def verify_anchor(
    anchor_json: str,
    public_key_pem: str,
    chain: str | Sequence[AuditEventRecord] | None = None,
) -> AnchorVerification:
    """Verify an exported chain-head anchor. Never raises; no DB, no network.

    Checks the Ed25519 signature and the document structure. If ``chain`` (a chain export JSON
    string, or ``AuditEventRecord`` objects in ``seq`` order) is given, it must be a valid chain
    segment that contains the anchored ``(seq, event_hash)`` (``chain_ok``).
    """
    errors: list[str] = []
    signature_ok = False
    chain_ok: bool | None = None
    anchor: dict[str, Any] | None = None
    try:
        export = _strict_loads(anchor_json, "anchor file")
        if not isinstance(export, dict) or set(export) != _EXPORT_FIELDS:
            raise _InputError("anchor file: expected an object with keys 'anchor', 'signature'")
        if not isinstance(export["anchor"], dict) or not isinstance(export["signature"], str):
            raise _InputError("anchor file: 'anchor' must be an object, 'signature' a string")
        anchor = export["anchor"]
        try:
            signature_ok = verify_signature(
                public_key_pem, canonical_json(anchor), export["signature"]
            )
            if not signature_ok:
                errors.append("signature is invalid")
        except SignatureError:
            errors.append("public key could not be loaded")
        errors.extend(_anchor_errors(anchor, public_key_pem))
    except _InputError as exc:
        errors.append(str(exc))
    except Exception as exc:  # pragma: no cover - defensive: the verifier must never raise
        errors.append(f"anchor could not be processed ({type(exc).__name__})")

    seq = anchor.get("seq") if anchor is not None else None
    event_hash = anchor.get("event_hash") if anchor is not None else None
    if chain is not None:
        chain_ok = False
        try:
            events = _parse_events(chain) if isinstance(chain, str) else list(chain)
            if not events:
                raise _InputError("chain: export is empty")
            result = verify_chain(events, expected_start_prev=events[0].prev_hash)
            if not result.valid:
                errors.append(
                    f"audit chain invalid at seq {result.first_invalid_seq} ({result.reason})"
                )
            elif not any(e.seq == seq and e.event_hash == event_hash for e in events):
                errors.append("anchored event not found in the audit chain")
            else:
                chain_ok = True
        except _InputError as exc:
            errors.append(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"chain could not be processed ({type(exc).__name__})")

    valid = signature_ok and not errors and chain_ok is not False
    return AnchorVerification(
        valid,
        signature_ok,
        chain_ok,
        seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        event_hash if is_hex64(event_hash) else None,
        tuple(errors),
    )
