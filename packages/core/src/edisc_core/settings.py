"""Process settings, read from ``EDISC_*`` environment variables (and ``.env`` when present)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_disposable(self) -> bool:
        """True where data may be thrown away (short retention, volume wipes)."""
        return self in (Environment.LOCAL, Environment.CI)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EDISC_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Environment = Environment.LOCAL

    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "edisc"
    pg_superuser: str = "postgres"
    pg_superuser_password: SecretStr = SecretStr("")
    pg_owner_user: str = "edisc_owner"  # owns the schema; runs migrations only
    pg_owner_password: SecretStr = SecretStr("")
    pg_app_user: str = "edisc_app"  # API + workers: not owner, not superuser, no BYPASSRLS
    pg_app_password: SecretStr = SecretStr("")
    pg_schema: str = "edisc"

    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint: str | None = None  # None = AWS default endpoint
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_evidence_bucket: str = "edisc-evidence"
    s3_default_retention_days: int = 1
    evidence_retention_override_days: int | None = Field(
        default=None,
        ge=1,
        description="Caps per-object retention. Only allowed when env is local/ci (ADR 0002).",
    )

    custody_anchor_every_n_batches: int = Field(
        default=8, ge=1, description="Seal the chain head to WORM at least every N batch events."
    )
    custody_tenant_anchor_retention_days: int = Field(
        default=3650, ge=1, description="Retention for anchors of tenant-level streams (no matter)."
    )

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "edisc"

    es_url: str = "http://localhost:9200"
    local_kms_dir: Path = Path(".local-kms")

    @model_validator(mode="after")
    def _guard_disposable_only_settings(self) -> Settings:
        if self.evidence_retention_override_days is not None and not self.env.is_disposable:
            raise ValueError(
                "EDISC_EVIDENCE_RETENTION_OVERRIDE_DAYS is only permitted when EDISC_ENV is local or ci"
            )
        return self

    def pg_dsn(
        self, role: Literal["app", "owner", "superuser"] = "app", *, db: str | None = None
    ) -> str:
        user, pwd = {
            "app": (self.pg_app_user, self.pg_app_password),
            "owner": (self.pg_owner_user, self.pg_owner_password),
            "superuser": (self.pg_superuser, self.pg_superuser_password),
        }[role]
        return f"postgresql://{user}:{pwd.get_secret_value()}@{self.pg_host}:{self.pg_port}/{db or self.pg_db}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
