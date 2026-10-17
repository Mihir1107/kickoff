"""Completing a source install (M17 step 6): one place writes it, whether the API's callback does it
in the request or the worker's token reconciler does it after a crash (``connection_tokens``).

The facts (``InstallFacts.as_journal()``, journaled with the install's tokens, no secret): the
install and connection ids, the acting principal, the request id, whether it re-authorized, the
connection's identity and grant (external id, scopes, blind spots, config), and the audit payload of
``oauth_install_completed``. The caller writes the audit event itself (API: ``audit.record``;
worker: the same event type and payload through ``edisc_custody``), in the same transaction.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class InstallFacts:
    install_id: uuid.UUID
    connection_id: uuid.UUID
    principal_id: uuid.UUID
    request_id: str | None
    reauth: bool
    external_org_id: str
    granted_scopes: tuple[str, ...]
    blind_spots: tuple[str, ...]
    config: dict[str, Any]
    audit: dict[str, Any]

    def as_journal(self) -> dict[str, Any]:
        return {
            "install_id": str(self.install_id), "connection_id": str(self.connection_id),
            "principal_id": str(self.principal_id), "request_id": self.request_id,
            "reauth": self.reauth, "external_org_id": self.external_org_id,
            "granted_scopes": list(self.granted_scopes), "blind_spots": list(self.blind_spots),
            "config": self.config, "audit": self.audit,
        }  # fmt: skip

    @classmethod
    def from_journal(cls, d: dict[str, Any]) -> InstallFacts:
        return cls(
            uuid.UUID(d["install_id"]), uuid.UUID(d["connection_id"]),
            uuid.UUID(d["principal_id"]), d.get("request_id"), bool(d["reauth"]),
            d["external_org_id"], tuple(d["granted_scopes"]), tuple(d["blind_spots"]),
            dict(d["config"]), dict(d["audit"]),
        )  # fmt: skip


async def complete_install(session: AsyncSession, facts: InstallFacts) -> bool:
    """The connection takes the install's identity and grant and turns active; the install's
    outcome becomes ``completed``. Returns False when the install already has an outcome (nothing
    is written then: an install completes once)."""
    done: uuid.UUID | None = (
        await session.execute(
            text(
                "UPDATE oauth_installs SET outcome = 'completed', outcome_detail = NULL,"
                " external_id = :x WHERE id = :i AND outcome IS NULL RETURNING id"
            ),
            {"x": facts.external_org_id, "i": facts.install_id},
        )
    ).scalar_one_or_none()
    if done is None:
        return False
    await session.execute(
        text(
            "UPDATE connections SET external_org_id = :x, granted_scopes = :sc, blind_spots = :b,"
            " config = config || CAST(:cfg AS jsonb), status = 'active', updated_at = now()"
            " WHERE id = :i"
        ),
        {"x": facts.external_org_id, "sc": list(facts.granted_scopes),
         "b": list(facts.blind_spots), "cfg": json.dumps(facts.config), "i": facts.connection_id},
    )  # fmt: skip
    return True


async def fail_install(session: AsyncSession, facts: InstallFacts, detail: str) -> bool:
    """The install ends ``failed`` with ``detail`` (its connection unchanged). False when it already
    has an outcome."""
    done: uuid.UUID | None = (
        await session.execute(
            text(
                "UPDATE oauth_installs SET outcome = 'failed', outcome_detail = :d"
                " WHERE id = :i AND outcome IS NULL RETURNING id"
            ),
            {"d": detail, "i": facts.install_id},
        )
    ).scalar_one_or_none()
    return done is not None
