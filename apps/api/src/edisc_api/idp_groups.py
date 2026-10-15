"""The IdP's groups claim, read without ever guessing "no groups" (ADR 0016 step-3b notes, Entra
overage; ADR 0013 groups).

Groups grant roles: a group with an ``external_id`` matches the IdP's group values, and its role
assignments apply to the caller. Two IdP answers would silently drop those roles if read as an
empty list, so both REFUSE the sign-in (and a bearer token) instead:

- **Overage** (``groups_overage``): Entra ID puts ``_claim_names`` / ``_claim_sources`` (or
  ``hasgroups``) in the token instead of the list when a user is in more groups than fit (200 in a
  JWT). Always refused, whatever the tenant's mapping: the list exists, we just did not get it.
  Fetching it from Microsoft Graph is a BACKLOG item.
- **Missing** (``groups_missing``): the claim is absent (or not a list of strings) while the
  tenant's role mapping DEPENDS on groups: a live role assignment held by a group with an
  ``external_id``. Okta omits a filtered groups claim when no group matches, so its filter must emit
  the claim for every user of such a tenant (to confirm on the real tenant). A present, EMPTY list is
  an answer ("in no group") and is accepted.

Each refusal is audited (``authentication_refused``) and alerts the tenant's admins once per
principal and reason until acknowledged.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edisc_api import audit
from edisc_core.ids import new_id

OVERAGE = "groups_overage"
MISSING = "groups_missing"
ALERT_TEXT = {
    OVERAGE: "the identity provider sent a groups overage instead of the user's groups (Entra ID:"
    " more groups than fit in a token); the sign-in was refused because the roles those groups"
    " grant cannot be known. Reduce the groups emitted (Entra: 'Groups assigned to the"
    " application') or wait for group lookup through Microsoft Graph",
    MISSING: "the identity provider's token carries no groups claim, but this tenant grants roles"
    " through IdP groups; the sign-in was refused. Configure the IdP to always emit the groups"
    " claim (Okta: a filter that emits it for every user)",
}


@dataclass(frozen=True)
class Groups:
    """What the token said about groups: ``values`` (None = absent or unusable) and ``problem``
    (``overage``, ``absent``, ``malformed`` or None)."""

    values: list[str] | None
    problem: str | None = None


def read(claims: Mapping[str, Any], groups_claim: str) -> Groups:
    names = claims.get("_claim_names")
    if (isinstance(names, dict) and groups_claim in names) or claims.get("hasgroups") is True:
        return Groups(None, "overage")
    if groups_claim not in claims:
        return Groups(None, "absent")
    value = claims[groups_claim]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return Groups(None, "malformed")
    return Groups(list(value))


async def depends_on_groups(s: AsyncSession) -> bool:
    """A live role assignment held by a group the IdP's claim maps to (RLS: this tenant)."""
    return bool(
        (
            await s.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM role_assignments r JOIN groups g"
                    " ON g.tenant_id = r.tenant_id AND g.id = r.group_id"
                    " WHERE r.revoked_at IS NULL AND g.external_id IS NOT NULL)"
                )
            )
        ).scalar_one()
    )


def refusal(groups: Groups, depends: bool) -> str | None:
    """The refusal code, or None when the groups may be used (``groups.values or []``)."""
    if groups.problem == "overage":
        return OVERAGE
    if groups.values is None and depends:
        return MISSING
    return None


async def record_refusal(
    s: AsyncSession, *, tenant_id: uuid.UUID, principal_id: uuid.UUID, idp_issuer: str,
    code: str, groups: Groups, via: str, request_id: str | None,
) -> None:  # fmt: skip
    """Audit the refusal and alert the tenant admins (once per principal and reason while an
    earlier alert is unacknowledged). ``via``: ``sign_in`` or ``bearer``."""
    await audit.record(
        s, tenant_id=tenant_id, actor=f"user:{principal_id}", event_type="authentication_refused",
        payload={"reason": code, "detail": groups.problem, "via": via, "issuer": idp_issuer,
                 "principal_id": str(principal_id)},
        request_id=request_id,
    )  # fmt: skip
    message = f"{ALERT_TEXT[code]} (principal {principal_id})"
    await s.execute(
        text(
            "INSERT INTO alerts (id, tenant_id, kind, message) SELECT :i, :t, :k, :m"
            " WHERE NOT EXISTS (SELECT 1 FROM alerts WHERE kind = :k AND message = :m"
            " AND acknowledged_at IS NULL)"
        ),
        {"i": new_id(), "t": tenant_id, "k": code, "m": message},
    )
