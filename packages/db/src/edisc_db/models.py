"""SQLAlchemy 2.0 models mirroring migration 0001.

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


class Matter(Base):
    __tablename__ = "matters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(Text)
    retention_until: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(Text)
    external_org_id: Mapped[str] = mapped_column(Text)
    plan_tier: Mapped[str | None] = mapped_column(Text)
    granted_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    encrypted_token_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
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


class EvidenceObject(Base):
    __tablename__ = "evidence_objects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("storage_key"),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["collection_jobs.tenant_id", "collection_jobs.id"]
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


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
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


class JobItem(Base):
    __tablename__ = "job_items"
    __table_args__ = (
        ForeignKeyConstraint(["job_id", "unit_key"], ["work_units.job_id", "work_units.unit_key"]),
        ForeignKeyConstraint(["tenant_id", "item_id"], ["items.tenant_id", "items.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "custody_event_id"], ["custody_events.tenant_id", "custody_events.id"]
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
