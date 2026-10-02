"""Process settings, read from ``EDISC_*`` environment variables (and ``.env`` when present)."""

from __future__ import annotations

import ipaddress
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"  # the ephemeral integration-test stack (destroyed after every run)
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_disposable(self) -> bool:
        """True where data may be thrown away (short retention, volume wipes)."""
        return self in (Environment.LOCAL, Environment.TEST, Environment.CI)

    @property
    def is_ephemeral_test(self) -> bool:
        """Test stacks whose volumes are destroyed after the run: only these may lock for seconds."""
        return self in (Environment.TEST, Environment.CI)


class RateLimitConfig(BaseModel):
    """Token bucket for one ``source.method``: sustained ``rate_per_second`` with bursts up to ``burst``."""

    rate_per_second: float = Field(gt=0)
    burst: int = Field(ge=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # EDISC_ENV_FILE selects the env file (e.g. .env.test for the ephemeral test stack)
        env_prefix="EDISC_",
        env_file=os.environ.get("EDISC_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
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
    pg_sweeper_user: str = "edisc_sweeper"  # anchor sweeper's cross-tenant lookup only
    pg_sweeper_password: SecretStr = SecretStr("")
    pg_schema: str = "edisc"
    pg_lock_timeout_ms: int = Field(
        default=30_000,
        ge=0,
        description="lock_timeout for app/worker sessions: a lock wait longer than this raises a retryable "
        "error instead of hanging (e.g. a deadlock through the application, which Postgres cannot see).",
    )
    pg_idle_in_transaction_timeout_ms: int = Field(
        default=60_000,
        ge=0,
        description="idle_in_transaction_session_timeout for app/worker sessions: a transaction left open "
        "and idle (stuck caller) is terminated, releasing its locks.",
    )

    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint: str | None = None  # None = AWS default endpoint
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_evidence_bucket: str = "edisc-evidence"
    s3_staging_bucket: str = (
        "edisc-staging"  # unlocked, short expiry; files land here before the WORM copy
    )
    s3_default_retention_days: int = 1
    evidence_retention_window_days: int = Field(
        default=90,
        ge=1,
        description="Rolling COMPLIANCE window: objects are locked this far ahead and extended while the "
        "matter is active, so matter close + expiry can honour destruction requests (ADR 0002).",
    )
    evidence_retention_extend_floor_days: float = Field(
        default=60,
        gt=0,
        description="Extend an object's retention (to the rolling target) only once its remaining "
        "retention drops below this floor; never extended above it (ADR 0002).",
    )
    evidence_part_size_bytes: int = Field(default=8 * 1024 * 1024, ge=5 * 1024 * 1024)
    evidence_single_copy_max_bytes: int = Field(
        default=5 * 1024**3, ge=1, description="Above this, files are copied with UploadPartCopy."
    )
    evidence_copy_part_size_bytes: int = Field(default=512 * 1024 * 1024, ge=5 * 1024 * 1024)
    evidence_small_file_max_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=0,
        description="Files up to this size skip staging: hashed in memory, PUT straight to WORM (ADR 0002 amendment). 0 disables.",
    )
    # Slack export ingestion (ADR 0014): archive limits; a tenant admin may override them per upload
    export_max_archive_bytes: int = Field(default=200 * 10**9, ge=1)
    export_max_entries: int = Field(default=20_000_000, ge=1)
    export_max_entry_bytes: int = Field(default=1 << 30, ge=1)
    export_max_total_bytes: int = Field(default=2 * 10**12, ge=1)
    export_max_total_ratio: int = Field(default=100, ge=1)
    export_max_entry_ratio: int = Field(default=200, ge=1)
    export_ratio_floor_bytes: int = Field(default=1 << 20, ge=0)
    export_max_name_bytes: int = Field(default=1024, ge=16)
    export_read_window_bytes: int = Field(
        default=8 << 20, ge=1 << 16, description="sequential range-read window (ADR 0014 R6)"
    )
    export_upload_part_min_bytes: int = Field(default=8 << 20, ge=5 << 20)
    evidence_file_concurrency: int = Field(
        default=4,
        ge=1,
        description="Concurrent file writes per page; memory <= this x small-file max",
    )
    evidence_copy_timeout_seconds: float = Field(
        default=1800,
        gt=0,
        description="Max time a file promotion (copy into WORM) may hold its content lock.",
    )
    evidence_retention_override_days: int | None = Field(
        default=None,
        ge=1,
        description="Caps per-object retention. Only allowed when env is local/test/ci (ADR 0002).",
    )
    evidence_retention_override_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Seconds-level cap for EPHEMERAL TEST stacks only (env test/ci); refused anywhere else.",
    )

    custody_anchor_claim_timeout_seconds: float = Field(
        default=60,
        gt=0,
        description="An anchoring claim older than this is abandoned (killed writer) and may be taken over.",
    )
    custody_anchor_every_n_batches: int = Field(
        default=8, ge=1, description="Seal the chain head to WORM at least every N batch events."
    )
    custody_anchor_sweep_idle_seconds: int = Field(
        default=600,
        ge=0,
        description="The sweeper also anchors streams whose unanchored tail has been idle this long.",
    )

    rate_limits: dict[str, RateLimitConfig] = Field(
        default_factory=dict,
        description='Per "source.method" limits, e.g. {"slack.conversations.history": {...}}. JSON in env. '
        "An unconfigured method is an error, never unlimited.",
    )
    rate_limit_unavailable_backoff_max_seconds: float = Field(default=10.0, gt=0)

    # Temporal orchestration (ADR 0012)
    activity_time_box_seconds: float = Field(
        default=600, gt=0, description="collect_pages returns after this"
    )
    activity_max_attempts: int = Field(
        default=25, ge=1, description="transient retry budget per activity"
    )
    unit_retry_cooldown_seconds: float = Field(default=900, ge=0)
    unit_retry_horizon_seconds: float = Field(default=86_400, gt=0)
    file_retry_attempts: int = Field(
        default=3, ge=1, description="in-batch attempts for transient file refusals"
    )
    file_retry_backoff_seconds: float = Field(default=0.5, ge=0)
    max_units_in_flight: int = Field(default=8, ge=1)
    activity_retry_initial_seconds: float = Field(default=1, gt=0)
    activity_retry_max_seconds: float = Field(default=60, gt=0)
    activity_start_to_close_seconds: float = Field(
        default=1800, gt=0, description="time box + one batch + margin (ADR 0012 section 4)"
    )
    activity_heartbeat_timeout_seconds: float = Field(default=60, gt=0)
    unclassified_max_attempts: int = Field(
        default=3, ge=1, description="attempts for errors no class recognises, then the unit fails"
    )
    unit_pages_per_activity: int = Field(default=50, ge=1)
    unit_iterations_per_run: int = Field(
        default=200, ge=1, description="collect_pages calls before a unit continues-as-new"
    )
    job_poll_seconds: float = Field(
        default=120,
        gt=0,
        description="parent's DB reconcile interval (signals are only a fast path)",
    )

    # API (ADR 0013)
    api_base_domain: str = Field(
        default="edisc.localhost", description="Tenants are served at {subdomain}.{api_base_domain}"
    )
    api_dev_idp: bool = Field(
        default=False,
        description="Built-in dev token issuer (key in EDISC_LOCAL_KMS_DIR). Only local/test/ci.",
    )
    api_jwt_leeway_seconds: int = Field(default=60, ge=0, le=300)
    api_jwks_cache_seconds: int = Field(default=600, ge=10)
    api_page_size_max: int = Field(default=200, ge=1)
    api_trusted_proxies: list[str] = Field(
        default_factory=list,
        description="CIDRs of reverse proxies / load balancers whose X-Forwarded-For is believed. Empty: "
        "the socket peer is the client.",
    )
    api_auth_failures_per_minute: int = Field(
        default=30,
        ge=1,
        description="per client address and host; then 429 until the minute passes",
    )

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "edisc"

    es_url: str = "http://localhost:9200"
    local_kms_dir: Path = Path(".local-kms")

    @model_validator(mode="after")
    def _guard_disposable_only_settings(self) -> Settings:
        for cidr in self.api_trusted_proxies:
            ipaddress.ip_network(cidr, strict=False)  # raises on a malformed entry
        if self.api_dev_idp and not self.env.is_disposable:
            raise ValueError(
                "EDISC_API_DEV_IDP is only permitted when EDISC_ENV is local, test or ci"
            )
        if self.evidence_retention_extend_floor_days > self.evidence_retention_window_days:
            raise ValueError(
                "EDISC_EVIDENCE_RETENTION_EXTEND_FLOOR_DAYS must not exceed EDISC_EVIDENCE_RETENTION_WINDOW_DAYS"
            )
        if self.evidence_small_file_max_bytes > self.evidence_part_size_bytes:
            raise ValueError(
                "EDISC_EVIDENCE_SMALL_FILE_MAX_BYTES must not exceed EDISC_EVIDENCE_PART_SIZE_BYTES"
            )
        if self.evidence_retention_override_days is not None and not self.env.is_disposable:
            raise ValueError(
                "EDISC_EVIDENCE_RETENTION_OVERRIDE_DAYS is only permitted when EDISC_ENV is local, test or ci"
            )
        if self.evidence_retention_override_seconds is not None and not self.env.is_ephemeral_test:
            raise ValueError(
                "EDISC_EVIDENCE_RETENTION_OVERRIDE_SECONDS is only permitted when EDISC_ENV is test or ci"
            )
        return self

    def pg_dsn(
        self,
        role: Literal["app", "owner", "superuser", "sweeper"] = "app",
        *,
        db: str | None = None,
    ) -> str:
        user, pwd = {
            "app": (self.pg_app_user, self.pg_app_password),
            "sweeper": (self.pg_sweeper_user, self.pg_sweeper_password),
            "owner": (self.pg_owner_user, self.pg_owner_password),
            "superuser": (self.pg_superuser, self.pg_superuser_password),
        }[role]
        return f"postgresql://{user}:{pwd.get_secret_value()}@{self.pg_host}:{self.pg_port}/{db or self.pg_db}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
