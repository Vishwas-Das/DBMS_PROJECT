from __future__ import annotations

import hashlib
import hmac
import pickle

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lae.audit.hmac_keys import HmacKeyring, normalize
from lae.contracts.enums import Normalization
from lae.contracts.types import RecordKey, TableRef
from lae.errors import ConfigurationError, ValidationError

pytestmark = pytest.mark.unit

KEY = bytes(range(32))


def _ref(text: str) -> str:
    return hmac.new(KEY, text.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.fixture
def ring() -> HmacKeyring:
    return HmacKeyring(KEY)


@pytest.mark.parametrize("key", [b"", b"x" * 31])
def test_short_key_rejected(key: bytes) -> None:
    with pytest.raises(ConfigurationError):
        HmacKeyring(key)


def test_non_bytes_key_rejected() -> None:
    with pytest.raises(ConfigurationError):
        HmacKeyring("k" * 40)  # type: ignore[arg-type]


def test_exact(ring: HmacKeyring) -> None:
    assert ring.digest("Alice@Example.com") == _ref("Alice@Example.com")
    assert ring.digest(" a ", Normalization.EXACT) == _ref(" a ")


def test_lower_trim(ring: HmacKeyring) -> None:
    d = ring.digest("  Alice@Example.COM\t\n", Normalization.LOWER_TRIM)
    assert d == _ref("alice@example.com")
    assert d == ring.digest("alice@example.com", Normalization.LOWER_TRIM)


def test_digits_only(ring: HmacKeyring) -> None:
    assert ring.digest("+1 (555) 010-9999", Normalization.DIGITS_ONLY) == _ref("15550109999")
    # non-ASCII digits are dropped (matches SQL [^0-9])
    assert normalize(chr(0x661) + chr(0x662) + "3", Normalization.DIGITS_ONLY) == "3"


def test_int_uses_decimal_string(ring: HmacKeyring) -> None:
    assert ring.digest(42) == _ref("42") == ring.digest("42")
    assert ring.digest(-7, Normalization.DIGITS_ONLY) == _ref("7")


@pytest.mark.parametrize("bad", [True, 1.5, None, b"x"])
def test_bad_value_types(ring: HmacKeyring, bad: object) -> None:
    with pytest.raises(ValidationError):
        ring.digest(bad)  # type: ignore[arg-type]


def test_key_digest_is_hmac_of_canonical_record_key(ring: HmacKeyring) -> None:
    rk = RecordKey(TableRef("app", "customer"), ("customer_id",), (42,))
    expected = hmac.new(
        KEY,
        b'{"columns":["customer_id"],"is_predicate":false,"table":"app.customer","values":[42]}',
        hashlib.sha256,
    ).hexdigest()
    assert ring.key_digest(rk) == expected
    assert ring.key_digest(rk) != ring.key_digest(
        RecordKey(TableRef("app", "customer"), ("customer_id",), ("42",))
    )


def test_different_keys_differ() -> None:
    assert HmacKeyring(KEY).digest("x") != HmacKeyring(b"\xff" * 32).digest("x")


def test_matches(ring: HmacKeyring) -> None:
    d = ring.digest("bob", Normalization.LOWER_TRIM)
    assert ring.matches(" BOB ", d, Normalization.LOWER_TRIM)
    assert ring.matches(" BOB ", d.upper(), Normalization.LOWER_TRIM)
    assert not ring.matches("alice", d, Normalization.LOWER_TRIM)
    assert not ring.matches("bob", "é" * 64)


def test_key_never_exposed(ring: HmacKeyring) -> None:
    assert KEY.hex() not in repr(ring)
    assert "redacted" in str(ring)
    with pytest.raises(TypeError):
        pickle.dumps(ring)
    assert not hasattr(ring, "__dict__")


@given(st.text())
def test_lower_trim_digest_matches_reference(text: str) -> None:
    once = normalize(text, Normalization.LOWER_TRIM)
    assert HmacKeyring(KEY).digest(text, Normalization.LOWER_TRIM) == _ref(once)
