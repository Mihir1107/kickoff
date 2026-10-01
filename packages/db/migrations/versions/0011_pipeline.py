"""Collection pipeline (M11).

- job_items.in_scope: in/out of the job's date range. On the LINK, not the item: one item can be in
  range for one job and out of range for another.
- job_items -> custody_events FK is DEFERRABLE INITIALLY DEFERRED: a batch inserts its links first
  (with the pre-allocated event id), learns which links are NEW, and then appends the custody event
  whose Merkle root covers exactly those links, all in one transaction.
- work_units: kind (conversation_day | directory), file_gaps, access_lost_reason, last_page_evidence_id,
  recon_status gains 'access_lost' and 'not_applicable'.
- collection_scopes.thread_parent_policy.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE job_items ADD COLUMN in_scope boolean NOT NULL DEFAULT true;
ALTER TABLE job_items DROP CONSTRAINT fk_job_items_tenant_id_custody_event_id_custody_events;
ALTER TABLE job_items ADD CONSTRAINT fk_job_items_tenant_id_custody_event_id_custody_events
    FOREIGN KEY (tenant_id, custody_event_id) REFERENCES custody_events (tenant_id, id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE work_units
    ADD COLUMN kind text NOT NULL DEFAULT 'conversation_day',
    ADD COLUMN file_gaps integer NOT NULL DEFAULT 0,
    ADD COLUMN access_lost_reason text,
    ADD COLUMN last_page_evidence_id uuid,
    ADD CONSTRAINT ck_work_units_kind CHECK (kind IN ('conversation_day', 'directory')),
    ADD CONSTRAINT ck_work_units_file_gaps CHECK (file_gaps >= 0);
ALTER TABLE work_units DROP CONSTRAINT ck_work_units_recon_status;
ALTER TABLE work_units ADD CONSTRAINT ck_work_units_recon_status CHECK (recon_status IN (
    'pending', 'matched', 'gap', 'surplus', 'unverifiable', 'failed', 'access_lost', 'not_applicable'));

ALTER TABLE collection_scopes ADD COLUMN thread_parent_policy text NOT NULL DEFAULT 'include_parent_and_thread',
    ADD CONSTRAINT ck_collection_scopes_thread_parent_policy CHECK (thread_parent_policy IN (
        'include_parent_and_thread', 'include_parent_only', 'replies_only'));
"""

DOWNGRADE = """
ALTER TABLE collection_scopes DROP CONSTRAINT ck_collection_scopes_thread_parent_policy,
    DROP COLUMN thread_parent_policy;
ALTER TABLE work_units DROP CONSTRAINT ck_work_units_recon_status;
ALTER TABLE work_units ADD CONSTRAINT ck_work_units_recon_status CHECK (recon_status IN (
    'pending', 'matched', 'gap', 'surplus', 'unverifiable', 'failed'));
ALTER TABLE work_units DROP CONSTRAINT ck_work_units_file_gaps, DROP CONSTRAINT ck_work_units_kind,
    DROP COLUMN last_page_evidence_id, DROP COLUMN access_lost_reason, DROP COLUMN file_gaps, DROP COLUMN kind;
ALTER TABLE job_items DROP CONSTRAINT fk_job_items_tenant_id_custody_event_id_custody_events;
ALTER TABLE job_items ADD CONSTRAINT fk_job_items_tenant_id_custody_event_id_custody_events
    FOREIGN KEY (tenant_id, custody_event_id) REFERENCES custody_events (tenant_id, id);
ALTER TABLE job_items DROP COLUMN in_scope;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
