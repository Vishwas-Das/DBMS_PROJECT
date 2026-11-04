from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lae.audit.canonical import canonical_hash, canonical_json, sha256_hex
from lae.contracts.enums import PlanAction
from lae.errors import CanonicalizationError

pytestmark = pytest.mark.unit


def test_keys_sorted_and_no_whitespace() -> None:
    assert canonical_json({"b": 1, "a": {"d": 2, "c": [1, 2]}}) == b'{"a":{"c":[1,2],"d":2},"b":1}'


def test_insertion_order_does_not_matter() -> None:
    assert canonical_json({"x": 1, "y": 2}) == canonical_json({"y": 2, "x": 1})


def test_unicode_is_raw_utf8_not_escaped() -> None:
    out = canonical_json({"n": "Zoë 数据"})
    assert out == '{"n":"Zoë 数据"}'.encode()
    assert b"\\u" not in out


def test_control_characters_are_escaped() -> None:
    assert canonical_json("a\nb\u0001") == b'"a\\nb\\u0001"'


@pytest.mark.parametrize("bad", [1.0, float("nan"), float("inf"), {"x": [0.5]}])
def test_floats_rejected(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(bad)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json({"t": datetime(2026, 1, 1)})


@pytest.mark.parametrize("key", [1, None, ("a",), True])
def test_non_string_keys_rejected(key: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json({key: 1})


def test_unsupported_types_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json({"s": {1, 2}})
    with pytest.raises(CanonicalizationError):
        canonical_json(b"bytes")
    with pytest.raises(CanonicalizationError):
        canonical_json(Decimal("NaN"))


def test_typed_values() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    obj = {
        "u": UUID("A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11"),
        "t": datetime(2026, 11, 4, 15, 30, tzinfo=ist),
        "d": Decimal("12.50"),
        "e": PlanAction.DELETE_ROWS,
        "tup": (1, "a", None, False),
    }
    assert canonical_json(obj) == (
        b'{"d":"12.50","e":"DELETE_ROWS","t":"2026-11-04T10:00:00.000000Z",'
        b'"tup":[1,"a",null,false],"u":"a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"}'
    )


def test_datetime_same_instant_same_bytes() -> None:
    a = datetime(2026, 1, 1, 12, tzinfo=UTC)
    b = a.astimezone(timezone(timedelta(hours=-7)))
    assert canonical_json(a) == canonical_json(b)


def test_hash_helpers() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert canonical_hash({"a": 1}) == sha256_hex(b'{"a":1}')


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)


@given(json_values)
def test_roundtrip_is_stable(value: object) -> None:
    once = canonical_json(value)
    assert canonical_json(json.loads(once)) == once


def test_plain_enum_uses_value() -> None:
    from enum import Enum

    class Level(Enum):
        HIGH = 3

    assert canonical_json({"l": Level.HIGH}) == b'{"l":3}'
