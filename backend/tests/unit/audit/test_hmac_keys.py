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


# ------------------------------------------------------------------ LOWER_TRIM == SQL twin
# Decided rule (CR verification-crypto-engineer-1 §5): SQL
#   translate(btrim(col, E' \t\n\r\f\x0B'),
#             'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
# The model below re-implements btrim/translate character by character, independently of
# str.strip/str.translate, as PostgreSQL defines them.

_SQL_BTRIM_SET = frozenset({" ", "\t", "\n", "\r", "\f", "\x0b"})
_SQL_FROM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SQL_TO = "abcdefghijklmnopqrstuvwxyz"


def _sql_lower_trim(value: str) -> str:
    start, end = 0, len(value)
    while start < end and value[start] in _SQL_BTRIM_SET:
        start += 1
    while end > start and value[end - 1] in _SQL_BTRIM_SET:
        end -= 1
    out = []
    for ch in value[start:end]:
        pos = _SQL_FROM.find(ch)
        out.append(_SQL_TO[pos] if pos >= 0 else ch)
    return "".join(out)


NBSP, EM_SPACE, IDEO_SPACE, NEL = chr(0x00A0), chr(0x2003), chr(0x3000), chr(0x0085)
E_ACUTE_UP, E_UML, SHARP_S = chr(0x00C9), chr(0x00EB), chr(0x00DF)
I_DOT_UP, KELVIN, FW_A, FW_B = chr(0x0130), chr(0x212A), chr(0xFF21), chr(0xFF22)
FS, US, VT = chr(0x1C), chr(0x1F), chr(0x0B)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Alice@Example.COM \t", "alice@example.com"),
        ("\t\n\r\f" + VT + "Bob" + VT + "\f\r\n\t ", "bob"),
        (NBSP + "Alice" + NBSP, NBSP + "alice" + NBSP),  # NBSP is not stripped
        (EM_SPACE + "X" + IDEO_SPACE, EM_SPACE + "x" + IDEO_SPACE),  # Unicode spaces stay
        (FS + "A" + US, FS + "a" + US),  # str.strip() would remove these separators
        (NEL + "A" + NEL, NEL + "a" + NEL),  # NEL stays
        (E_ACUTE_UP + "COLE", E_ACUTE_UP + "cole"),  # 'E-acute' is not lowered
        ("Zo" + E_UML, "zo" + E_UML),
        ("STRASSE " + SHARP_S, "strasse " + SHARP_S),
        (I_DOT_UP + "STANBUL", I_DOT_UP + "stanbul"),  # Python lower() would give 'i' + U+0307
        (KELVIN + "elvin", KELVIN + "elvin"),  # Python lower() would give 'k'
        (FW_A + FW_B, FW_A + FW_B),  # fullwidth letters stay
        ("vivian" + VT, "vivian"),  # VT stripped; the letter 'v' is NOT (E'\v' pitfall)
        ("vvv", "vvv"),
        ("  a  b  ", "a  b"),  # inner whitespace kept
        ("", ""),
        (" \t ", ""),
    ],
)
def test_lower_trim_matches_sql_semantics(raw: str, expected: str) -> None:
    assert _sql_lower_trim(raw) == expected
    assert normalize(raw, Normalization.LOWER_TRIM) == expected


def test_lower_trim_strip_set_is_exactly_ascii_whitespace() -> None:
    from lae.audit.hmac_keys import LOWER_TRIM_STRIP_CHARS

    assert sorted(LOWER_TRIM_STRIP_CHARS) == sorted(_SQL_BTRIM_SET)
    assert len(LOWER_TRIM_STRIP_CHARS) == 6
    assert "v" not in LOWER_TRIM_STRIP_CHARS


@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=40))
def test_lower_trim_equals_sql_model(text: str) -> None:
    assert normalize(text, Normalization.LOWER_TRIM) == _sql_lower_trim(text)


@given(st.text(alphabet=" \t\n\r\f" + VT + "AaZzv" + NBSP + E_ACUTE_UP + KELVIN, max_size=20))
def test_lower_trim_equals_sql_model_on_tricky_alphabet(text: str) -> None:
    assert normalize(text, Normalization.LOWER_TRIM) == _sql_lower_trim(text)


@given(st.text())
def test_lower_trim_digest_matches_reference(text: str) -> None:
    once = normalize(text, Normalization.LOWER_TRIM)
    assert HmacKeyring(KEY).digest(text, Normalization.LOWER_TRIM) == _ref(once)
