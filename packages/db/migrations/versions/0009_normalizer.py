"""Normalizer support (M10).

- items.event_kind gains 'no_longer_observed' and 'observed_again' (absence is never deletion).
- item_derivations: the normalizer's derived record for an item, one row per (item, normalizer
  version), append-only. Reprocessing raw pages with a newer normalizer adds rows here; items (keyed by
  the ADR 0004 fingerprint) and evidence objects are never touched.
- index to find previously observed messages of a conversation-day.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None


def _upgrade(app_role: str) -> str:
    return f"""
ALTER TABLE items DROP CONSTRAINT ck_items_event_kind;
ALTER TABLE items ADD CONSTRAINT ck_items_event_kind CHECK (
    (item_type = 'event') = (event_kind IS NOT NULL)
    AND (event_kind IS NULL OR event_kind IN (
        'reaction_snapshot', 'identity_snapshot', 'change_observation',
        'no_longer_observed', 'observed_again')));
CREATE INDEX ix_items_tenant_id_source_sent_at ON items (tenant_id, source, sent_at);
CREATE INDEX ix_items_tenant_id_source_source_item_id ON items (tenant_id, source, source_item_id);

CREATE TABLE item_derivations (
    tenant_id           uuid        NOT NULL,
    item_id             uuid        NOT NULL,
    normalizer_version  text        NOT NULL,
    derived             jsonb       NOT NULL,
    derived_hash        text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_item_derivations PRIMARY KEY (item_id, normalizer_version),
    CONSTRAINT fk_item_derivations_tenant_id_item_id_items
        FOREIGN KEY (tenant_id, item_id) REFERENCES items (tenant_id, id),
    CONSTRAINT ck_item_derivations_derived_hash CHECK (derived_hash ~ '^[0-9a-f]{{64}}$')
);
ALTER TABLE item_derivations ENABLE ROW LEVEL SECURITY;
ALTER TABLE item_derivations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON item_derivations
    USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_item_derivations_no_update BEFORE UPDATE ON item_derivations
    FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_item_derivations_no_delete BEFORE DELETE ON item_derivations
    FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_item_derivations_no_truncate BEFORE TRUNCATE ON item_derivations
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON item_derivations TO "{app_role}";
"""


DOWNGRADE = """
DROP TABLE IF EXISTS item_derivations;
DROP INDEX IF EXISTS ix_items_tenant_id_source_source_item_id;
DROP INDEX IF EXISTS ix_items_tenant_id_source_sent_at;
ALTER TABLE items DROP CONSTRAINT ck_items_event_kind;
ALTER TABLE items ADD CONSTRAINT ck_items_event_kind CHECK (
    (item_type = 'event') = (event_kind IS NOT NULL)
    AND (event_kind IS NULL OR event_kind IN (
        'reaction_snapshot', 'identity_snapshot', 'change_observation')));
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
