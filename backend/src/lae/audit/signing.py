"""Ed25519 signing of evidence (architecture.md §13). Uses only the ``cryptography`` library.

- Private keys are PEM / PKCS#8, unencrypted (research prototype; see risks R10).
- ``key_id`` = first 16 hex chars of SHA-256 over the raw 32-byte public key.
- Signatures are standard base64 (with padding) of the 64-byte Ed25519 signature.

Key material is never logged, repr'd or put in exception messages.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
from typing import NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from lae.errors import SignatureError

ALGORITHM = "Ed25519"
_SIGNATURE_BYTES = 64


def key_id_for(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def load_public_key(public_key_pem: str | bytes) -> Ed25519PublicKey:
    """Parse an Ed25519 SubjectPublicKeyInfo PEM. Raises ``SignatureError`` if unusable."""
    data = public_key_pem.encode("ascii") if isinstance(public_key_pem, str) else public_key_pem
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SignatureError("public key could not be loaded") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError("public key is not an Ed25519 key")
    return key


def public_key_id(public_key_pem: str | bytes) -> str:
    """``key_id`` of a public key PEM. Raises ``SignatureError`` if unusable."""
    return key_id_for(load_public_key(public_key_pem))


class Signer:
    """Holds an Ed25519 private key. Construct via :meth:`generate` or :meth:`load`."""

    __slots__ = ("__private",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise SignatureError("signer requires an Ed25519 private key")
        self.__private = private_key

    def __repr__(self) -> str:
        return f"Signer(key_id={self.key_id!r})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("Signer must not be pickled")

    @classmethod
    def generate(cls) -> Signer:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_pem(cls, pem: bytes) -> Signer:
        try:
            key = serialization.load_pem_private_key(pem, password=None)
        except (ValueError, TypeError) as exc:
            raise SignatureError("signing key could not be loaded") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SignatureError("signing key is not an Ed25519 key")
        return cls(key)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Signer:
        """Load an unencrypted PKCS#8 PEM private key from ``path``."""
        try:
            pem = Path(path).read_bytes()
        except OSError as exc:
            raise SignatureError("signing key file could not be read") from exc
        return cls.from_pem(pem)

    def private_key_pem(self) -> bytes:
        return self.__private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def save(self, path: str | os.PathLike[str]) -> None:
        """Write the private key as PKCS#8 PEM. Refuses to overwrite an existing file.

        The file is created exclusively with mode 0600 (best effort on Windows)."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(os.fspath(path), flags, 0o600)
        except FileExistsError as exc:
            raise SignatureError("refusing to overwrite an existing key file") from exc
        except OSError as exc:
            raise SignatureError("signing key file could not be created") from exc
        with os.fdopen(fd, "wb") as fh:
            fh.write(self.private_key_pem())

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.__private.public_key()

    @property
    def public_key_pem(self) -> str:
        return self.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")

    @property
    def key_id(self) -> str:
        return key_id_for(self.public_key)

    def sign(self, data: bytes) -> str:
        """Base64 Ed25519 signature over ``data``. Ed25519 is deterministic."""
        if not isinstance(data, bytes | bytearray):
            raise SignatureError("sign() requires bytes")
        return base64.b64encode(self.__private.sign(bytes(data))).decode("ascii")


def verify_signature(public_key_pem: str | bytes, data: bytes, signature_b64: str) -> bool:
    """True iff ``signature_b64`` is a valid Ed25519 signature of ``data``.

    Returns ``False`` (never raises) for any bad, malformed or wrong-length signature. Raises
    ``SignatureError`` only if the public key itself cannot be loaded.
    """
    key = load_public_key(public_key_pem)
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError, TypeError):
        return False
    if len(sig) != _SIGNATURE_BYTES or base64.b64encode(sig).decode("ascii") != signature_b64:
        return False  # wrong length, or a non-canonical base64 spelling of the bytes
    try:
        key.verify(sig, bytes(data))
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True
