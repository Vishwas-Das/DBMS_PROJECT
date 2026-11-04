from __future__ import annotations

import base64
import hashlib
import pickle
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lae.audit.signing import Signer, load_public_key, public_key_id, verify_signature
from lae.errors import SignatureError

pytestmark = pytest.mark.unit

KEY_PATH = Path(__file__).parent / "vectors" / "test_signing_key.pem"


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer.load(KEY_PATH)


def test_round_trip(signer: Signer) -> None:
    sig = signer.sign(b"hello")
    assert len(base64.b64decode(sig)) == 64
    assert verify_signature(signer.public_key_pem, b"hello", sig)
    assert verify_signature(signer.public_key_pem.encode(), b"hello", sig)


def test_deterministic(signer: Signer) -> None:
    assert signer.sign(b"x") == signer.sign(b"x")


def test_tampered_data_fails(signer: Signer) -> None:
    sig = signer.sign(b"hello")
    assert not verify_signature(signer.public_key_pem, b"hellO", sig)
    assert not verify_signature(signer.public_key_pem, b"", sig)


def test_tampered_signature_fails(signer: Signer) -> None:
    raw = bytearray(base64.b64decode(signer.sign(b"hello")))
    for i in (0, 31, 32, 63):
        bad = bytearray(raw)
        bad[i] ^= 0x01
        assert not verify_signature(signer.public_key_pem, b"hello", base64.b64encode(bad).decode())


@pytest.mark.parametrize("sig", ["", "not base64!", "AAAA", base64.b64encode(b"\0" * 65).decode()])
def test_malformed_signatures_return_false(signer: Signer, sig: str) -> None:
    assert not verify_signature(signer.public_key_pem, b"hello", sig)


def test_non_canonical_base64_rejected(signer: Signer) -> None:
    sig = signer.sign(b"hello")
    assert not verify_signature(signer.public_key_pem, b"hello", sig.rstrip("="))
    assert not verify_signature(signer.public_key_pem, b"hello", "é" + sig)


def test_wrong_key_fails(signer: Signer) -> None:
    other = Signer.generate()
    assert other.key_id != signer.key_id
    assert not verify_signature(other.public_key_pem, b"hello", signer.sign(b"hello"))


def test_key_id(signer: Signer) -> None:
    raw = signer.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert signer.key_id == hashlib.sha256(raw).hexdigest()[:16]
    assert public_key_id(signer.public_key_pem) == signer.key_id
    assert signer.key_id in repr(signer)


def test_save_load_and_refuse_overwrite(tmp_path: Path) -> None:
    s = Signer.generate()
    path = tmp_path / "k.pem"
    s.save(path)
    assert path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert Signer.load(path).key_id == s.key_id
    before = path.read_bytes()
    with pytest.raises(SignatureError):
        Signer.generate().save(path)
    assert path.read_bytes() == before
    with pytest.raises(SignatureError):
        s.save(tmp_path / "missing_dir" / "k.pem")


def test_unloadable_keys(tmp_path: Path) -> None:
    with pytest.raises(SignatureError):
        Signer.load(tmp_path / "nope.pem")
    bad = tmp_path / "bad.pem"
    bad.write_bytes(b"garbage")
    with pytest.raises(SignatureError):
        Signer.load(bad)
    ec = generate_private_key(SECP256R1())
    ec_pem = ec.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(SignatureError):
        Signer.from_pem(ec_pem)
    ec_pub = ec.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(SignatureError):
        load_public_key(ec_pub)
    with pytest.raises(SignatureError):
        verify_signature("garbage", b"x", "AAAA")
    with pytest.raises(SignatureError):
        Signer(ec)  # type: ignore[arg-type]


def test_encrypted_key_rejected() -> None:
    enc = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"pw"),
    )
    with pytest.raises(SignatureError):
        Signer.from_pem(enc)


def test_sign_requires_bytes(signer: Signer) -> None:
    with pytest.raises(SignatureError):
        signer.sign("text")  # type: ignore[arg-type]


def test_private_key_not_exposed(signer: Signer) -> None:
    with pytest.raises(TypeError):
        pickle.dumps(signer)
    assert "PRIVATE" not in repr(signer)
