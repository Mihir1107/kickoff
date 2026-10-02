"""SQLAlchemy 2.0 models mirroring the migrations.

The migrations are the source of truth (hand-written SQL for RLS, triggers and grants). These models
exist for typed queries; ``tests/integration/db/test_migrations.py`` fails if they drift from the
migrated schema. Constraint names follow the naming convention below and match the SQL exactly.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}

TZ = DateTime(timezone=True)
NOW = text("now()")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (UniqueConstraint("subdomain"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    subdomain: Mapped[str] = mapped_column(Text)
    kms_key_ref: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        Index(
            "uq_clients_one_default", "tenant_id", unique=True, postgresql_where=text("is_default")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    closed_at: Mapped[datetime | None] = mapped_column(TZ)
    closed_by: Mapped[str | None] = mapped_column(Text)


class Matter(Base):
    __tablename__ = "matters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        ForeignKeyConstraint(["tenant_id", "client_id"], ["clients.tenant_id", "clients.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(Text)
    retention_until: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    closed_at: Mapped[datetime | None] = mapped_column(TZ)
    closed_by: Mapped[str | None] = mapped_column(Text)


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        ForeignKeyConstraint(["tenant_id", "client_id"], ["clients.tenant_id", "clients.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(Text)
    external_org_id: Mapped[str] = mapped_column(Text)
    plan_tier: Mapped[str | None] = mapped_column(Text)
    granted_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    encrypted_access_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    token_key_id: Mapped[str | None] = mapped_column(Text)
    token_key_version: Mapped[str | None] = mapped_column(Text)
    token_version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    token_updated_at: Mapped[datetime | None] = mapped_column(TZ)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Custodian(Base):
    __tablename__ = "custodians"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    display_name: Mapped[str] = mapped_column(Text)
    primary_email: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class CustodianIdentity(Base):
    __tablename__ = "custodian_identities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "external_user_id"),
        ForeignKeyConstraint(
            ["tenant_id", "custodian_id"], ["custodians.tenant_id", "custodians.id"]
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    custodian_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(Text)
    external_user_id: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    merged_by: Mapped[str | None] = mapped_column(Text)
    merged_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class CollectionJob(Base):
    __tablename__ = "collection_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "matter_id"], ["matters.tenant_id", "matters.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["connections.tenant_id", "connections.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "rerun_of"], ["collection_jobs.tenant_id", "collection_jobs.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"], ["workspaces.tenant_id", "workspaces.id"]
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    access_tier: Mapped[str | None] = mapped_column(Text)
    connector_version: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    status_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    seal_storage_key: Mapped[str | None] = mapped_column(Text)
    stop_requested_at: Mapped[datetime | None] = mapped_column(TZ)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    sealed_at: Mapped[datetime | None] = mapped_column(TZ)
    rerun_of: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    explicit_units: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)


class CollectionScope(Base):
    __tablename__ = "collection_scopes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["collection_jobs.tenant_id", "collection_jobs.id"]
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    scope_type: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    date_from: Mapped[datetime] = mapped_column(TZ)
    date_to: Mapped[datetime] = mapped_column(TZ)
    thread_parent_policy: Mapped[str] = mapped_column(
        Text, server_default=text("'include_parent_and_thread'")
    )


class WorkUnit(Base):
    __tablename__ = "work_units"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["collection_jobs.tenant_id", "collection_jobs.id"]
        ),
        Index(None, "job_id", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    unit_key: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text)
    day: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    cursor: Mapped[str | None] = mapped_column(Text)
    pages_done: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    expected_count: Mapped[int | None] = mapped_column(Integer)
    collected_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    recon_status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    kind: Mapped[str] = mapped_column(Text, server_default=text("'conversation_day'"))
    file_gaps: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    access_lost_reason: Mapped[str | None] = mapped_column(Text)
    last_page_evidence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    last_page_fragment_hash: Mapped[str | None] = mapped_column(Text)
    retry_after: Mapped[datetime | None] = mapped_column(TZ)
    first_failure_at: Mapped[datetime | None] = mapped_column(TZ)
    failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    archive_accounted: Mapped[int | None] = mapped_column(Integer)
    day_anomalies: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class EvidenceObject(Base):
    __tablename__ = "evidence_objects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("storage_key"),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["collection_jobs.tenant_id", "collection_jobs.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "archive_evidence_id"],
            ["evidence_objects.tenant_id", "evidence_objects.id"],
        ),
        Index(None, "job_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    storage_key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    sha256: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    retain_until: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    upload_id: Mapped[str | None] = mapped_column(Text)
    version_id: Mapped[str | None] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(Text)
    source_hash_origin: Mapped[str | None] = mapped_column(Text)
    archive_evidence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    entry_path: Mapped[str | None] = mapped_column(Text)
    entry_raw_name: Mapped[bytes | None] = mapped_column(LargeBinary)
    entry_crc32: Mapped[int | None] = mapped_column(BigInteger)
    entry_compressed_size: Mapped[int | None] = mapped_column(BigInteger)


class CustodyEvent(Base):
    __tablename__ = "custody_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("stream_id", "seq"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["collection_jobs.tenant_id", "collection_jobs.id"]
        ),
        Index(None, "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    stream_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    seq: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(Text)
    event_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ)


class CustodyChainHead(Base):
    __tablename__ = "custody_chain_heads"
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),)

    stream_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    last_seq: Mapped[int] = mapped_column(BigInteger)
    last_hash: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    last_anchored_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    anchor_due: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    anchoring_seq: Mapped[int | None] = mapped_column(BigInteger)
    anchoring_since: Mapped[datetime | None] = mapped_column(TZ)
    pending_lifecycle_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        Index(None, "tenant_id", "source", "sent_at"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "idempotency_key"),
        UniqueConstraint("tenant_id", "source", "source_item_id", "version"),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["collection_jobs.tenant_id", "collection_jobs.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "evidence_object_id"],
            ["evidence_objects.tenant_id", "evidence_objects.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "parent_item_id"], ["items.tenant_id", "items.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(Text)
    source_item_id: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    item_type: Mapped[str] = mapped_column(Text)
    event_kind: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    raw_hash: Mapped[str] = mapped_column(Text)
    evidence_object_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    storage_key: Mapped[str] = mapped_column(Text)
    json_path: Mapped[str] = mapped_column(Text)
    parent_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    change_hints: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    sent_at: Mapped[datetime | None] = mapped_column(TZ)
    connector_version: Mapped[str] = mapped_column(Text)
    normalizer_version: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class TokenRefreshJournal(Base):
    __tablename__ = "token_refresh_journal"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["connections.tenant_id", "connections.id"]
        ),
        Index(None, "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    based_on_version: Mapped[int] = mapped_column(BigInteger)
    encrypted_access_token: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    token_key_id: Mapped[str] = mapped_column(Text)
    token_key_version: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'received'"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    resolved_at: Mapped[datetime | None] = mapped_column(TZ)


class ItemDerivation(Base):
    __tablename__ = "item_derivations"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "item_id"], ["items.tenant_id", "items.id"]),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    normalizer_version: Mapped[str] = mapped_column(Text, primary_key=True)
    derived: Mapped[dict[str, Any]] = mapped_column(JSONB)
    derived_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class JobPause(Base):
    __tablename__ = "job_pauses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["collection_jobs.tenant_id", "collection_jobs.id"]
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    paused_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    resumed_at: Mapped[datetime | None] = mapped_column(TZ)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TZ)


class JobItem(Base):
    __tablename__ = "job_items"
    __table_args__ = (
        ForeignKeyConstraint(["job_id", "unit_key"], ["work_units.job_id", "work_units.unit_key"]),
        ForeignKeyConstraint(["tenant_id", "item_id"], ["items.tenant_id", "items.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "custody_event_id"],
            ["custody_events.tenant_id", "custody_events.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(None, "job_id", "unit_key"),
        Index(None, "custody_event_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    unit_key: Mapped[str] = mapped_column(Text)
    custody_event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    in_scope: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "matter_id"], ["matters.tenant_id", "matters.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class TenantIdp(Base):
    __tablename__ = "tenant_idps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "issuer"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    issuer: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(Text)
    jwks_url: Mapped[str] = mapped_column(Text)
    groups_claim: Mapped[str] = mapped_column(Text, server_default=text("'groups'"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Principal(Base):
    __tablename__ = "principals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "issuer", "subject"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    issuer: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "name"),
        UniqueConstraint("tenant_id", "external_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "group_id"], ["groups.tenant_id", "groups.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"], ["principals.tenant_id", "principals.id"]
        ),
        Index(
            "uq_group_members_active",
            "group_id",
            "principal_id",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    added_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    removed_at: Mapped[datetime | None] = mapped_column(TZ)


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"], ["principals.tenant_id", "principals.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "group_id"], ["groups.tenant_id", "groups.id"]),
        Index(None, "tenant_id", "principal_id"),
        Index(None, "tenant_id", "group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    role: Mapped[str] = mapped_column(Text)
    scope_type: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    created_by: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_by: Mapped[str | None] = mapped_column(Text)


class ApiIdempotency(Base):
    __tablename__ = "api_idempotency"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["collection_jobs.tenant_id", "collection_jobs.id"]
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    request_hash: Mapped[str] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class WorkUnitScope(Base):
    __tablename__ = "work_unit_scopes"
    __table_args__ = (
        ForeignKeyConstraint(["job_id", "unit_key"], ["work_units.job_id", "work_units.unit_key"]),
        ForeignKeyConstraint(["scope_id"], ["collection_scopes.id"]),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    unit_key: Mapped[str] = mapped_column(Text, primary_key=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)


class SlackExport(Base):
    __tablename__ = "slack_exports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        ForeignKeyConstraint(["tenant_id", "client_id"], ["clients.tenant_id", "clients.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "evidence_object_id"],
            ["evidence_objects.tenant_id", "evidence_objects.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["connections.tenant_id", "connections.id"]
        ),
        Index(None, "tenant_id", "client_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text, server_default=text("'uploading'"))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    reject_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    declared_size: Mapped[int] = mapped_column(BigInteger)
    declared_sha256: Mapped[str | None] = mapped_column(Text)
    declared_plan: Mapped[str | None] = mapped_column(Text)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    staging_key: Mapped[str] = mapped_column(Text)
    upload_id: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    evidence_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version_id: Mapped[str | None] = mapped_column(Text)
    entry_count: Mapped[int | None] = mapped_column(BigInteger)
    detected_tier: Mapped[str | None] = mapped_column(Text)
    tier_confirmed: Mapped[bool | None] = mapped_column(Boolean)
    findings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    root_prefix: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(TZ)
    validated_at: Mapped[datetime | None] = mapped_column(TZ)


class ExportUploadPart(Base):
    __tablename__ = "export_upload_parts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "export_id"], ["slack_exports.tenant_id", "slack_exports.id"]
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    export_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    part_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    etag: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class ExportEntry(Base):
    __tablename__ = "export_entries"
    __table_args__ = (
        UniqueConstraint("export_id", "folded_name"),
        ForeignKeyConstraint(
            ["tenant_id", "export_id"], ["slack_exports.tenant_id", "slack_exports.id"]
        ),
        Index(None, "export_id", "kind", "folder"),
        Index(None, "export_id", "local_header_offset"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    export_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    idx: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    folded_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    folder: Mapped[str | None] = mapped_column(Text)
    hint_day: Mapped[date | None] = mapped_column(Date)
    method: Mapped[int] = mapped_column(SmallInteger)
    crc32: Mapped[int] = mapped_column(BigInteger)
    compressed_size: Mapped[int] = mapped_column(BigInteger)
    uncompressed_size: Mapped[int] = mapped_column(BigInteger)
    local_header_offset: Mapped[int] = mapped_column(BigInteger)
    raw_name: Mapped[bytes] = mapped_column(LargeBinary)
    name_encoding: Mapped[str] = mapped_column(Text)
    flags: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class ExportConversation(Base):
    __tablename__ = "export_conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "export_id"], ["slack_exports.tenant_id", "slack_exports.id"]
        ),
        Index(None, "export_id", "folder"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    export_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    metadata_entry: Mapped[str] = mapped_column(Text)
    team_id: Mapped[str | None] = mapped_column(Text)


class RetentionGap(Base):
    __tablename__ = "retention_gaps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "evidence_object_id"],
            ["evidence_objects.tenant_id", "evidence_objects.id"],
        ),
        Index(None, "tenant_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    evidence_object_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    owner_type: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    unprotected_from: Mapped[datetime] = mapped_column(TZ)
    unprotected_until: Mapped[datetime] = mapped_column(TZ)
    outcome: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class ExportDayFile(Base):
    __tablename__ = "export_day_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["export_id", "entry_idx"], ["export_entries.export_id", "export_entries.idx"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "export_id"], ["slack_exports.tenant_id", "slack_exports.id"]
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    export_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    entry_idx: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    elements: Mapped[int | None] = mapped_column(Integer)
    anomalies: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    parse_error: Mapped[str | None] = mapped_column(Text)


class ExportThread(Base):
    __tablename__ = "export_threads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "export_id"], ["slack_exports.tenant_id", "slack_exports.id"]
        ),
        Index(None, "export_id", "conversation_id", "thread_ts"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    export_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    entry_idx: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    element_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text)
    thread_ts: Mapped[str] = mapped_column(Text)
    ts: Mapped[str] = mapped_column(Text)
