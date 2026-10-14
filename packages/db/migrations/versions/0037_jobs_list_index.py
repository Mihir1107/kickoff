"""The tenant-wide jobs list (M17 step 3, plan §3.2): newest first, keyset on (created_at, id).

`GET /v1/jobs` pages every job a caller may read in the tenant ordered by ``created_at DESC, id DESC``;
this index makes each page a range scan from the cursor's position instead of a sort of the tenant's
jobs.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

from alembic import op

revision = "0037"
down_revision: str | None = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_collection_jobs_tenant_created ON collection_jobs"
        " (tenant_id, created_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_collection_jobs_tenant_created")
