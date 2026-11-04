"""Append-only audit hash chain (architecture.md §13).

    event_hash = SHA-256( bytes.fromhex(prev_hash)
                          || SHA-256(payload_canonical as UTF-8)
                          || seq as unsigned 64-bit big-endian
                          || event_type as UTF-8 )

The genesis ``prev_hash`` is 64 ASCII zeros. Everything here is pure: no DB, no I/O.

Note: only ``seq``, ``event_type``, ``payload_canonical`` and ``prev_hash`` are bound by the
hash. ``entity_type``, ``entity_id``, ``actor`` and ``occurred_at`` are *not* covered unless the
writer also puts them inside ``payload_canonical`` (see change request
verification-crypto-engineer-1).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass

from lae.contracts.types import AuditEventRecord

GENESIS_HASH = "0" * 64
_HEX = frozenset("0123456789abcdef")
_MAX_SEQ = 2**64 - 1


def is_hex64(value: object) -> bool:
    """True iff ``value`` is a 64-char lowercase hex string (the stored ``char(64)`` form)."""
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def next_hash(prev_hash: str, seq: int, event_type: str, payload_canonical: str) -> str:
    """Compute ``event_hash`` for one event. Raises ``ValueError`` on malformed input."""
    if not is_hex64(prev_hash):
        raise ValueError("prev_hash must be 64 lowercase hex characters")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= _MAX_SEQ:
        raise ValueError("seq must be an int in [0, 2**64)")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("event_type must be a non-empty str")
    if not isinstance(payload_canonical, str):
        raise ValueError("payload_canonical must be str")
    h = hashlib.sha256()
    h.update(bytes.fromhex(prev_hash))
    h.update(hashlib.sha256(payload_canonical.encode("utf-8")).digest())
    h.update(seq.to_bytes(8, "big"))
    h.update(event_type.encode("utf-8"))
    return h.hexdigest()


class HashChain:
    """Namespace matching module_contracts.md §8.2 (``HashChain.genesis()`` / ``.next_hash``)."""

    GENESIS = GENESIS_HASH

    @staticmethod
    def genesis() -> str:
        return GENESIS_HASH

    @staticmethod
    def next_hash(prev_hash: str, seq: int, event_type: str, payload_canonical: str) -> str:
        return next_hash(prev_hash, seq, event_type, payload_canonical)


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """Result of :func:`verify_chain`.

    - ``valid``: every inspected event verified.
    - ``checked_events``: events inspected, *including* the first invalid one (verification
      stops there).
    - ``head_seq`` / ``head_hash``: the last event that verified. If none did, ``head_seq`` is 0
      and ``head_hash`` is ``expected_start_prev``.
    - ``first_invalid_seq``: ``seq`` of the first failing event (``None`` when valid). If the
      failing record has no usable integer seq, it is ``-1``.
    - ``reason``: short machine-readable reason (``None`` when valid). Never contains payloads.
    """

    valid: bool
    checked_events: int
    head_seq: int
    head_hash: str
    first_invalid_seq: int | None
    reason: str | None = None


def verify_chain(
    events: Iterable[AuditEventRecord], expected_start_prev: str = GENESIS_HASH
) -> ChainVerification:
    """Recompute every hash in ``events`` (which must be in the order they were appended).

    Detects: a recomputed hash that differs from ``event_hash`` (payload, type or seq tampered),
    a ``prev_hash`` that does not equal the previous event's ``event_hash`` (event removed,
    inserted or reordered), and a ``seq`` that is not strictly increasing. Gaps in ``seq`` are
    allowed (PostgreSQL identity values are consumed by rolled-back transactions); a deleted
    event is still caught by the broken ``prev_hash`` link.

    Pure and total: it never raises, whatever it is given.
    """
    prev = expected_start_prev
    head_seq = 0
    head_hash = expected_start_prev
    last_seq: int | None = None
    checked = 0

    def fail(seq: object, reason: str) -> ChainVerification:
        bad = seq if isinstance(seq, int) and not isinstance(seq, bool) else -1
        return ChainVerification(False, checked, head_seq, head_hash, bad, reason)

    try:
        iterator = iter(events)
    except TypeError:
        return ChainVerification(False, 0, 0, head_hash, -1, "not_iterable")

    while True:
        try:
            ev = next(iterator)
        except StopIteration:
            break
        except Exception:
            return fail(None, "iteration_error")
        checked += 1
        seq = getattr(ev, "seq", None)
        try:
            if isinstance(seq, bool) or not isinstance(seq, int):
                return fail(seq, "bad_seq")
            if last_seq is not None and seq <= last_seq:
                return fail(seq, "seq_not_increasing")
            stored_prev = ev.prev_hash
            stored_hash = ev.event_hash
            if not is_hex64(stored_prev) or not hmac.compare_digest(stored_prev, prev):
                return fail(seq, "prev_hash_mismatch")
            recomputed = next_hash(stored_prev, seq, ev.event_type, ev.payload_canonical)
            if not is_hex64(stored_hash) or not hmac.compare_digest(recomputed, stored_hash):
                return fail(seq, "event_hash_mismatch")
        except Exception:
            return fail(seq, "malformed_event")
        prev = stored_hash
        last_seq = seq
        head_seq = seq
        head_hash = stored_hash

    return ChainVerification(True, checked, head_seq, head_hash, None, None)
