"""Export entry names exactly as stored, and the export root (M14.4).

- ``export_entries.raw_name``: the name bytes from the central directory. The local header must repeat
  them, and ``edisc-verify`` finds entries by them; the decoded ``name`` alone cannot be re-encoded
  reliably when the archive did not flag UTF-8.
- ``export_entries.name_encoding``: how ``name`` was decoded (``edisc_custody.archive.NameEncoding``);
  anything but ascii/utf-8 is reported with the export.
- ``slack_exports.root_prefix``: a single wrapper folder around the export, when there is one.

Rows written before this revision are backfilled from their decoded name as UTF-8 (the append-only
trigger is suspended inside this migration only). That is exact for ASCII and UTF-8-flagged names; the
rare pre-0020 CP437 name gets the wrong bytes, which re-validating that export would correct (no real
exports had been ingested before 0020).

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0020"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE export_entries ADD COLUMN raw_name bytea, ADD COLUMN name_encoding text;
ALTER TABLE export_entries NO FORCE ROW LEVEL SECURITY;
ALTER TABLE export_entries DISABLE TRIGGER trg_export_entries_no_update;
UPDATE export_entries SET raw_name = convert_to(name, 'UTF8'),
    name_encoding = CASE WHEN name ~ '^[ -~]*$' THEN 'ascii' ELSE 'utf-8' END;
ALTER TABLE export_entries ENABLE TRIGGER trg_export_entries_no_update;
ALTER TABLE export_entries FORCE ROW LEVEL SECURITY;
ALTER TABLE export_entries ALTER COLUMN raw_name SET NOT NULL, ALTER COLUMN name_encoding SET NOT NULL,
    ADD CONSTRAINT ck_export_entries_name_encoding
        CHECK (name_encoding IN ('ascii', 'utf-8', 'utf-8-extra', 'utf-8-unflagged', 'cp437'));
ALTER TABLE slack_exports ADD COLUMN root_prefix text;
"""

DOWNGRADE = """
ALTER TABLE slack_exports DROP COLUMN IF EXISTS root_prefix;
ALTER TABLE export_entries DROP CONSTRAINT IF EXISTS ck_export_entries_name_encoding,
    DROP COLUMN IF EXISTS name_encoding, DROP COLUMN IF EXISTS raw_name;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
