"""Keyed HMAC-SHA256 digests for identifiers (architecture.md §13, "No PII in the audit").

Raw subject keys, record keys and identifier values never leave the target schema. Everywhere
else (audit payloads, evidence leaves, residual probes, logs) they appear only as
``HMAC-SHA256(LAE_HMAC_KEY, ...)`` hex digests produced here.

The key is held privately and is never logged, repr'd or included in exception messages.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import NoReturn

from lae.audit.canonical import canonical_json
from lae.contracts.enums import Normalization
from lae.contracts.types import RecordKey
from lae.errors import ConfigurationError, ValidationError

MIN_KEY_BYTES = 32
_ASCII_DIGITS = frozenset("0123456789")


def normalize(value: str | int, normalization: Normalization = Normalization.EXACT) -> str:
    """Return the text that is fed to the HMAC for ``value``.

    - ints (not bools) are rendered as their decimal string first;
    - ``EXACT``: unchanged;
    - ``LOWER_TRIM``: ``value.strip().lower()`` (Python semantics: strips all Unicode whitespace);
    - ``DIGITS_ONLY``: keeps only the ASCII digits ``0-9`` (so ``"+1 (555) 010-9999"`` becomes
      ``"15550109999"``). Non-ASCII digit characters are dropped, matching SQL ``[^0-9]``.
    """
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValidationError(f"HMAC input must be str or int, got {type(value).__name__}")
    text = str(value) if isinstance(value, int) else value
    if normalization is Normalization.EXACT:
        return text
    if normalization is Normalization.LOWER_TRIM:
        return text.strip().lower()
    if normalization is Normalization.DIGITS_ONLY:
        return "".join(ch for ch in text if ch in _ASCII_DIGITS)
    raise ValidationError(f"unsupported normalization {normalization!r}")  # pragma: no cover


class HmacKeyring:
    """HMAC-SHA256 with a single secret key (``LAE_HMAC_KEY``, at least 32 bytes)."""

    __slots__ = ("__key",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes | bytearray):
            raise ConfigurationError("HMAC key must be bytes")
        if len(key) < MIN_KEY_BYTES:
            raise ConfigurationError(
                f"HMAC key must be at least {MIN_KEY_BYTES} bytes",
                details={"min_bytes": MIN_KEY_BYTES},
            )
        self.__key = bytes(key)

    def __repr__(self) -> str:
        return "HmacKeyring(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("HmacKeyring must not be pickled")

    def digest_bytes(self, data: bytes) -> str:
        """Hex HMAC-SHA256 over raw bytes."""
        return hmac.new(self.__key, data, hashlib.sha256).hexdigest()

    def digest(self, value: str | int, normalization: Normalization = Normalization.EXACT) -> str:
        """Hex HMAC-SHA256 over the UTF-8 of ``normalize(value, normalization)``."""
        return self.digest_bytes(normalize(value, normalization).encode("utf-8"))

    def key_digest(self, key: RecordKey) -> str:
        """Hex HMAC-SHA256 over ``canonical_json(key.as_canonical())``."""
        return self.digest_bytes(canonical_json(key.as_canonical()))

    def matches(
        self,
        value: str | int,
        expected_hex: str,
        normalization: Normalization = Normalization.EXACT,
    ) -> bool:
        """Constant-time comparison of ``digest(value)`` against ``expected_hex``."""
        if not expected_hex.isascii():
            return False
        return hmac.compare_digest(self.digest(value, normalization), expected_hex.lower())
