"""Runtime configuration (env prefix LAE_). Secrets are SecretStr and never logged."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from lae.errors import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433/lae"
    admin_url: str | None = None  # migrations + seeding (superuser); the app never uses it
    reader_url: str | None = None
    executor_url: str | None = None
    target_schemas: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["app"])
    hmac_key: SecretStr | None = None
    signing_key_path: Path | None = None
    api_token: SecretStr | None = None  # X-LAE-Token (local prototype auth), >= 24 chars
    require_four_eyes: bool = True  # approver may not execute (separation of duties)
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 5_000

    @field_validator("target_schemas", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("target_schemas")
    @classmethod
    def _no_engine_schema(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one target schema is required")
        if "lae" in v:
            raise ValueError("the engine schema 'lae' can never be an erasure target")
        return v

    @field_validator(
        "api_token", "hmac_key", "admin_url", "reader_url", "executor_url", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """An empty value in .env (e.g. `LAE_API_TOKEN=`) means "not set", not an invalid value."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("api_token")
    @classmethod
    def _token_strength(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < 24:
            raise ValueError("LAE_API_TOKEN must be at least 24 characters")
        return v

    def url_for(self, role: str) -> str:
        if role == "owner":
            return self.database_url
        if role == "reader":
            return self.reader_url or self.database_url
        if role == "executor":
            return self.executor_url or self.database_url
        raise ConfigurationError(f"unknown DB role {role!r}")

    def hmac_key_bytes(self) -> bytes:
        if self.hmac_key is None:
            raise ConfigurationError("LAE_HMAC_KEY is not set")
        raw = self.hmac_key.get_secret_value().encode("utf-8")
        if len(raw) < 32:
            raise ConfigurationError("LAE_HMAC_KEY must be at least 32 bytes")
        return raw


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
