"""A day-bounded index on message items for the preview (ADR 0018 §1.2, §12, §29).

`RenderLoader.day_slice` reads one conversation-day of a job: the message items of the conversation
whose own time is inside the day, then their links to the job by primary key. An item is linked ONCE
per job, to whichever unit linked it first (a reply fetched as thread context lands under another
day's unit), so unit days cannot bound the read; the item's own time can. This partial expression
index makes that an exact range scan: (tenant, source, conversation id from the source item id
`<workspace>/<conversation>/<ts>`, sent_at), messages only.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

from alembic import op

revision = "0034"
down_revision: str | None = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_items_message_conversation_sent_at ON items"
        " (tenant_id, source, split_part(source_item_id, '/', 2), sent_at)"
        " WHERE item_type = 'message'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_items_message_conversation_sent_at")
