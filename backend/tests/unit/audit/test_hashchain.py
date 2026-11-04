from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lae.audit.canonical import canonical_json
from lae.audit.hashchain import GENESIS_HASH, HashChain, next_hash, verify_chain
from lae.contracts.types import AuditEventRecord

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 4, tzinfo=UTC)


def make_chain(n: int, *, start_prev: str = GENESIS_HASH, seq0: int = 1) -> list[AuditEventRecord]:
    events: list[AuditEventRecord] = []
    prev = start_prev
    for i in range(n):
        seq = seq0 + i
        payload = canonical_json({"i": i, "note": "Zoë"}).decode()
        h = next_hash(prev, seq, "PLAN_CREATED", payload)
        events.append(
            AuditEventRecord(seq, "PLAN_CREATED", "plan", None, "tester", T0, payload, prev, h)
        )
        prev = h
    return events


def test_formula_matches_independent_computation() -> None:
    payload = '{"a":1}'
    expected = hashlib.sha256(
        bytes(32)
        + hashlib.sha256(payload.encode()).digest()
        + (7).to_bytes(8, "big")
        + b"REQUEST_RECEIVED"
    ).hexdigest()
    assert next_hash(GENESIS_HASH, 7, "REQUEST_RECEIVED", payload) == expected
    assert HashChain.next_hash(GENESIS_HASH, 7, "REQUEST_RECEIVED", payload) == expected
    assert HashChain.genesis() == GENESIS_HASH == "0" * 64


@pytest.mark.parametrize(
    "args",
    [
        ("0" * 63, 1, "X", "{}"),
        ("Z" * 64, 1, "X", "{}"),
        ("A" * 64, 1, "X", "{}"),
        (GENESIS_HASH, -1, "X", "{}"),
        (GENESIS_HASH, 2**64, "X", "{}"),
        (GENESIS_HASH, True, "X", "{}"),
        (GENESIS_HASH, 1, "", "{}"),
        (GENESIS_HASH, 1, "X", b"{}"),
    ],
)
def test_next_hash_rejects_malformed(args: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        next_hash(*args)  # type: ignore[arg-type]


def test_valid_chain() -> None:
    events = make_chain(5)
    r = verify_chain(events)
    assert r.valid and r.first_invalid_seq is None and r.reason is None
    assert r.checked_events == 5
    assert r.head_seq == 5 and r.head_hash == events[-1].event_hash


def test_empty_chain_is_valid() -> None:
    r = verify_chain([])
    assert r.valid and r.checked_events == 0 and r.head_seq == 0 and r.head_hash == GENESIS_HASH


def test_accepts_generator_and_seq_gaps() -> None:
    events = make_chain(3)
    # gaps are legitimate (identity values consumed by rollbacks) as long as links hold
    gapped: list[AuditEventRecord] = []
    prev = GENESIS_HASH
    for ev, seq in zip(events, (1, 4, 9), strict=True):
        h = next_hash(prev, seq, ev.event_type, ev.payload_canonical)
        gapped.append(dataclasses.replace(ev, seq=seq, prev_hash=prev, event_hash=h))
        prev = h

    def gen() -> Iterator[AuditEventRecord]:
        yield from gapped

    assert verify_chain(gen()).valid


def test_payload_tamper_detected_at_exact_seq() -> None:
    events = make_chain(6)
    events[3] = dataclasses.replace(events[3], payload_canonical='{"i":999}')
    r = verify_chain(events)
    assert not r.valid
    assert r.first_invalid_seq == 4
    assert r.reason == "event_hash_mismatch"
    assert r.head_seq == 3 and r.head_hash == events[2].event_hash
    assert r.checked_events == 4


def test_event_type_tamper_detected() -> None:
    events = make_chain(3)
    events[1] = dataclasses.replace(events[1], event_type="PLAN_APPROVED")
    assert verify_chain(events).first_invalid_seq == 2


def test_recomputed_hash_but_broken_link_detected() -> None:
    # attacker rewrites event 3 and recomputes its hash: event 4's prev link breaks
    events = make_chain(5)
    forged_payload = '{"forged":true}'
    forged_hash = next_hash(events[2].prev_hash, 3, "PLAN_CREATED", forged_payload)
    events[2] = dataclasses.replace(
        events[2], payload_canonical=forged_payload, event_hash=forged_hash
    )
    r = verify_chain(events)
    assert not r.valid and r.first_invalid_seq == 4 and r.reason == "prev_hash_mismatch"


def test_deleted_event_detected() -> None:
    events = make_chain(5)
    del events[2]
    r = verify_chain(events)
    assert r.first_invalid_seq == 4 and r.reason == "prev_hash_mismatch"


def test_reordered_events_detected() -> None:
    events = make_chain(5)
    events[1], events[2] = events[2], events[1]
    r = verify_chain(events)
    assert not r.valid
    assert r.first_invalid_seq == 3


def test_non_increasing_seq_detected() -> None:
    events = make_chain(3)
    # keep hashes self-consistent but repeat seq 2
    prev = events[1].event_hash
    h = next_hash(prev, 2, "PLAN_CREATED", "{}")
    events[2] = AuditEventRecord(2, "PLAN_CREATED", "plan", None, "t", T0, "{}", prev, h)
    r = verify_chain(events)
    assert not r.valid and r.first_invalid_seq == 2 and r.reason == "seq_not_increasing"


def test_seq_tamper_detected() -> None:
    events = make_chain(3)
    events[2] = dataclasses.replace(events[2], seq=10)
    r = verify_chain(events)
    assert r.first_invalid_seq == 10 and r.reason == "event_hash_mismatch"


def test_wrong_genesis_detected() -> None:
    events = make_chain(2, start_prev="ab" * 32)
    assert verify_chain(events).reason == "prev_hash_mismatch"
    assert verify_chain(events, expected_start_prev="ab" * 32).valid


def test_segment_verification() -> None:
    full = make_chain(6)
    seg = full[3:]
    assert verify_chain(seg, expected_start_prev=full[2].event_hash).valid


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: dataclasses.replace(e, event_hash="X" * 64),
        lambda e: dataclasses.replace(e, event_hash=e.event_hash.upper()),
        lambda e: dataclasses.replace(e, prev_hash=None),
        lambda e: dataclasses.replace(e, seq="2"),
        lambda e: dataclasses.replace(e, seq=True),
        lambda e: dataclasses.replace(e, payload_canonical=None),
        lambda e: dataclasses.replace(e, event_type=""),
    ],
)
def test_malformed_events_never_raise(mutate: object) -> None:
    events = make_chain(3)
    events[1] = mutate(events[1])  # type: ignore[operator]
    r = verify_chain(events)
    assert not r.valid
    assert r.first_invalid_seq in (2, -1)


def test_garbage_inputs_never_raise() -> None:
    assert not verify_chain(None).valid  # type: ignore[arg-type]
    assert verify_chain([object()]).first_invalid_seq == -1  # type: ignore[list-item]

    def boom() -> Iterator[AuditEventRecord]:
        yield make_chain(1)[0]
        raise RuntimeError("db went away")

    r = verify_chain(boom())
    assert not r.valid and r.reason == "iteration_error" and r.head_seq == 1
    assert not verify_chain(make_chain(1), expected_start_prev="é" * 64).valid


@given(n=st.integers(min_value=1, max_value=12), data=st.data())
def test_any_single_payload_tamper_detected_at_that_seq(n: int, data: st.DataObject) -> None:
    events = make_chain(n)
    i = data.draw(st.integers(min_value=0, max_value=n - 1))
    events[i] = dataclasses.replace(events[i], payload_canonical=events[i].payload_canonical + " ")
    r = verify_chain(events)
    assert not r.valid and r.first_invalid_seq == events[i].seq
