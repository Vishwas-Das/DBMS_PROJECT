"""Process-wide runtime: engines, keyring, signer, clock (Lead-owned)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine

from lae.audit.hmac_keys import HmacKeyring
from lae.audit.signing import Signer
from lae.config import Settings, get_settings
from lae.contracts.types import DiscoveryLimits
from lae.db.engine import make_engine
from lae.errors import ConfigurationError


def derive_subkey(master: bytes, purpose: str) -> bytes:
    """HKDF-SHA256 per-purpose subkey (security review M3: domain separation).

    The audit/evidence identity key never reaches the database. Only the probe and pseudonym
    subkeys do (they must, for in-DB HMAC matching and pseudonym generation).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"lae-hkdf-salt-v1", info=purpose.encode("ascii")
    ).derive(master)


def utc_now() -> datetime:
    return datetime.now(UTC)


def configure_logging(level: int = logging.INFO) -> None:
    """Structured-ish logging. SQLGlot echoes SQL text on warnings, so silence it (CR disc-1 §9)."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@dataclass
class Runtime:
    settings: Settings
    owner: Engine
    reader: Engine
    keyring: HmacKeyring  # audit/evidence/request identity -- never sent to PostgreSQL
    probe_keyring: HmacKeyring  # residual probes; its key IS sent to PG for the scan
    probe_key: bytes
    pseudonym_key: bytes  # PSEUDONYM column values; sent to PG by the writer
    signer: Signer | None
    clock: Callable[[], datetime] = utc_now
    limits: DiscoveryLimits = field(default_factory=DiscoveryLimits)
    anchor_dir: Path = Path("var/anchors")

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, require_signer: bool = True
    ) -> Runtime:
        s = settings or get_settings()
        key = s.hmac_key_bytes()
        signer: Signer | None = None
        if s.signing_key_path is not None and s.signing_key_path.exists():
            signer = Signer.load(s.signing_key_path)
        elif require_signer:
            raise ConfigurationError(
                "signing key not found; set LAE_SIGNING_KEY_PATH and run `lae keys generate`"
            )
        probe_key = derive_subkey(key, "lae/residual-probe/v1")
        return cls(
            settings=s,
            owner=make_engine(s, "owner"),
            reader=make_engine(s, "reader"),
            keyring=HmacKeyring(derive_subkey(key, "lae/audit-identity/v1")),
            probe_keyring=HmacKeyring(probe_key),
            probe_key=probe_key,
            pseudonym_key=derive_subkey(key, "lae/pseudonym/v1"),
            signer=signer,
        )

    def require_signer(self) -> Signer:
        if self.signer is None:
            raise ConfigurationError("no signing key loaded; evidence cannot be issued")
        return self.signer

    def dispose(self) -> None:
        self.owner.dispose()
        if self.reader is not self.owner:
            self.reader.dispose()
