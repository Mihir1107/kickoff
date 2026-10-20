"""Is it held? ONE answer for every caller (M18 §1.1, ADR 0019).

Every question "is X under a legal hold?" goes through here, and these go through the SQL
functions of migration 0047 (``matter_held``, ``client_held``, ``evidence_held``), which the close
triggers and retention extension use too: one definition, never a re-derived query. All run
inside the caller's tenant transaction (RLS applies).

- A matter is held by an active hold on it or on its client.
- A client is held by an active hold on it or on any of its matters (it cannot close otherwise).
- An evidence object is held when an active hold covers any of its owners: the matter of its job,
  of any job that linked an item behind it (deduplicated files), of its render's or report's job,
  of a job that read an archive entry behind it; or the client of an export whose archive it is.

A hold blocks closing, destruction (D2: also by the S3 flag) and any retention shortening; it does
not block new jobs, renders, reports or reads (preservation, not freezing).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def matter_held(session: AsyncSession, matter_id: uuid.UUID) -> bool:
    return bool(
        (await session.execute(text("SELECT matter_held(:m)"), {"m": matter_id})).scalar_one()
    )


async def client_held(session: AsyncSession, client_id: uuid.UUID) -> bool:
    return bool(
        (await session.execute(text("SELECT client_held(:c)"), {"c": client_id})).scalar_one()
    )


async def evidence_held(session: AsyncSession, evidence_id: uuid.UUID) -> bool:
    return bool(
        (await session.execute(text("SELECT evidence_held(:e)"), {"e": evidence_id})).scalar_one()
    )


async def cooling_off_until(session: AsyncSession, evidence_id: uuid.UUID) -> datetime | None:
    """The latest end of a cooling-off period (D4) of a RELEASED hold over any owner of the object:
    nothing a release uncovered may be destroyed before it. None: no released hold covers it."""
    until: datetime | None = (
        await session.execute(
            text(
                "SELECT max(h.cooling_off_until) FROM legal_holds h"
                " WHERE h.released_at IS NOT NULL AND ("
                "   h.matter_id IN (SELECT evidence_owner_matters(:e))"
                "   OR h.client_id IN (SELECT m.client_id FROM matters m"
                "                      WHERE m.id IN (SELECT evidence_owner_matters(:e)))"
                "   OR h.client_id IN (SELECT x.client_id FROM slack_exports x"
                "                      WHERE x.evidence_object_id = :e))"
            ),
            {"e": evidence_id},
        )
    ).scalar_one()
    return until
