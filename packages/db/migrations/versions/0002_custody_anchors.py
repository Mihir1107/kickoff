"""Custody anchoring bookkeeping (ADR 0003).

- custody_chain_heads.last_anchored_seq: highest seq sealed to WORM.
- custody_chain_heads.anchor_due: set in the SAME transaction as a lifecycle event or when the
  batch threshold is crossed; cleared only after the anchor object is durably written. Survives
  crashes, so a missed anchor is retried by the next writer, finalizer or sweeper.
- guard: a head update either appends (seq + 1, anchored seq unchanged) or records an anchor
  (seq/hash unchanged, anchored seq monotonic and <= seq).
- evidence_objects.kind gains 'anchor'.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE custody_chain_heads
    ADD COLUMN last_anchored_seq bigint NOT NULL DEFAULT 0,
    ADD COLUMN anchor_due boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT ck_custody_chain_heads_anchored_le_seq CHECK (last_anchored_seq >= 0 AND last_anchored_seq <= last_seq),
    ADD CONSTRAINT ck_custody_chain_heads_seq_nonnegative CHECK (last_seq >= 0);

CREATE OR REPLACE FUNCTION guard_custody_chain_heads() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.stream_id <> OLD.stream_id OR NEW.tenant_id <> OLD.tenant_id THEN
        RAISE EXCEPTION 'custody_chain_heads: identity is immutable' USING ERRCODE = 'EA003';
    END IF;
    IF NEW.last_seq = OLD.last_seq + 1 THEN
        IF NEW.last_anchored_seq <> OLD.last_anchored_seq THEN
            RAISE EXCEPTION 'custody_chain_heads: append may not change anchored seq' USING ERRCODE = 'EA003';
        END IF;
    ELSIF NEW.last_seq = OLD.last_seq THEN
        IF NEW.last_hash <> OLD.last_hash OR NEW.last_anchored_seq < OLD.last_anchored_seq THEN
            RAISE EXCEPTION 'custody_chain_heads: anchor bookkeeping may only move forward' USING ERRCODE = 'EA003';
        END IF;
    ELSE
        RAISE EXCEPTION 'custody_chain_heads: head may only advance by exactly one' USING ERRCODE = 'EA003';
    END IF;
    RETURN NEW;
END
$$;

ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_kind;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_kind
    CHECK (kind IN ('page', 'file', 'seal', 'report', 'anchor'));
"""

DOWNGRADE = """
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_kind;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_kind
    CHECK (kind IN ('page', 'file', 'seal', 'report'));
CREATE OR REPLACE FUNCTION guard_custody_chain_heads() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.stream_id <> OLD.stream_id OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.last_seq <> OLD.last_seq + 1 THEN
        RAISE EXCEPTION 'custody_chain_heads: head may only advance by exactly one' USING ERRCODE = 'EA003';
    END IF;
    RETURN NEW;
END
$$;
ALTER TABLE custody_chain_heads
    DROP CONSTRAINT ck_custody_chain_heads_seq_nonnegative,
    DROP CONSTRAINT ck_custody_chain_heads_anchored_le_seq,
    DROP COLUMN anchor_due,
    DROP COLUMN last_anchored_seq;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
