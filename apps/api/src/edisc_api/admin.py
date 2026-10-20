"""Tenant onboarding (operator tool, not an API route): tenant + KMS key + IdP + first tenant admin.

    uv run python -m edisc_api.admin --subdomain acme --name "Acme LLP" \
        --issuer https://login.example/acme --audience edisc --jwks-url https://login.example/acme/jwks \
        --admin-subject 00u1abc --admin-name "Alice Admin"

Every step is recorded as an audit event (actor ``operator:{name}``).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_api import audit
from edisc_core.ids import new_id
from edisc_db.session import create_tenant, tenant_tx


@dataclass(frozen=True)
class Onboarded:
    tenant_id: uuid.UUID
    admin_principal_id: uuid.UUID
    default_client_id: uuid.UUID


async def onboard_tenant(
    sessions: async_sessionmaker[AsyncSession],
    *,
    kms_key_ref: str,
    subdomain: str,
    name: str,
    issuer: str,
    audience: str,
    jwks_url: str,
    admin_subject: str,
    admin_name: str,
    operator: str,
    reserved: Collection[str] = ("oauth",),
) -> Onboarded:
    """``kms_key_ref``: the tenant's KEK, created beforehand by the operator (KMS key management is
    infrastructure, not application code). ``reserved``: subdomains no tenant may take (the OAuth
    relay host, ADR 0016 A3)."""
    if subdomain in reserved:
        raise ValueError(f"the subdomain {subdomain!r} is reserved")
    tenant_id, admin = new_id(), new_id()
    await create_tenant(
        sessions, tenant_id=tenant_id, name=name, subdomain=subdomain, kms_key_ref=kms_key_ref
    )
    actor = f"operator:{operator}"
    async with tenant_tx(sessions, tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO tenant_idps (id, tenant_id, issuer, audience, jwks_url) VALUES (:i, :t, :iss, :aud, :j)"
            ),
            {"i": new_id(), "t": tenant_id, "iss": issuer, "aud": audience, "j": jwks_url},
        )
        await s.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind, issuer, subject, display_name)"
                " VALUES (:p, :t, 'user', :iss, :sub, :n)"
            ),
            {"p": admin, "t": tenant_id, "iss": issuer, "sub": admin_subject, "n": admin_name},
        )
        await s.execute(
            text(
                "INSERT INTO role_assignments (id, tenant_id, principal_id, role, scope_type, created_by)"
                " VALUES (:i, :t, :p, 'tenant_admin', 'tenant', :by)"
            ),
            {"i": new_id(), "t": tenant_id, "p": admin, "by": actor},
        )
        client: uuid.UUID = (
            await s.execute(
                text(
                    "INSERT INTO clients (id, tenant_id, name, is_default) VALUES (:i, :t, 'Default client', true)"
                    " RETURNING id"
                ),
                {"i": new_id(), "t": tenant_id},
            )
        ).scalar_one()
        await audit.record(
            s,
            tenant_id=tenant_id,
            actor=actor,
            event_type="tenant_onboarded",
            payload={
                "subdomain": subdomain,
                "issuer": issuer,
                "audience": audience,
                "admin_principal_id": str(admin),
                "admin_subject": admin_subject,
            },
        )
    return Onboarded(tenant_id, admin, client)


# ------------------------------------------------------------------ moving an external id (A2.3)
class MoveRefusedError(RuntimeError):
    pass


async def move_binding(
    owner: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    *,
    provider: str,
    external_id: str,
    to_tenant: uuid.UUID,
    reason: str,
    operator: str,
) -> uuid.UUID:
    """Move an external id (Entra tid, Slack team or enterprise id) to another of our tenants
    (ADR 0016 A2.3): an OPERATOR action, never an API route. The source tenant's connections on it
    must be revoked first; the move is recorded in BOTH tenants' audit streams (each naming the
    other, the operator and the reason) and both tenants' admins get an alert. Returns the source
    tenant."""
    if not reason.strip():
        raise MoveRefusedError("a reason is required")
    async with owner() as s, s.begin():
        row = (await s.execute(text(
            "SELECT bound_tenant_id FROM external_bindings WHERE provider = :p AND external_id = :x"
            " FOR UPDATE"), {"p": provider, "x": external_id})).one_or_none()  # fmt: skip
        if row is None:
            raise MoveRefusedError("no such binding")
        source: uuid.UUID = row.bound_tenant_id
        if source == to_tenant:
            raise MoveRefusedError("already bound to that tenant")
        async with tenant_tx(sessions, source) as t:
            live: int = (await t.execute(text(
                "SELECT count(*) FROM connections WHERE status <> 'revoked'"
                " AND (external_org_id = :x OR config->>'enterprise_id' = :x)"),
                {"x": external_id})).scalar_one()  # fmt: skip
        if live:
            raise MoveRefusedError("revoke the source tenant's connections on it first")
        await s.execute(text(
            "UPDATE external_bindings SET bound_tenant_id = :to, connection_id = NULL,"
            " bound_at = now(), bound_by = :by WHERE provider = :p AND external_id = :x"),
            {"to": to_tenant, "by": f"operator:{operator}", "p": provider, "x": external_id})  # fmt: skip
        for tenant, other, direction in ((source, to_tenant, "moved_out"),
                                         (to_tenant, source, "moved_in")):  # fmt: skip
            async with tenant_tx(sessions, tenant) as t:
                await audit.record(
                    t, tenant_id=tenant, actor=f"operator:{operator}",
                    event_type="external_binding_moved",
                    payload={"provider": provider, "external_id": external_id,
                             "direction": direction, "other_tenant_id": str(other),
                             "reason": reason},
                )  # fmt: skip
                await t.execute(text(
                    "INSERT INTO alerts (id, tenant_id, kind, message) VALUES (:i, :t,"
                    " 'external_binding_moved', :m)"),
                    {"i": new_id(), "t": tenant,
                     "m": f"an operator moved {provider} {external_id} ({direction}): {reason}"[:2000]})  # fmt: skip
    return source


def main() -> None:
    from edisc_core.kms import LocalKmsClient
    from edisc_core.settings import Settings
    from edisc_db.session import create_engine, session_factory

    if sys.argv[1:2] == ["move-binding"]:
        _move_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["quota"]:
        from edisc_api import quota_admin

        quota_admin.main(sys.argv[2:])
        return
    ap = argparse.ArgumentParser(prog="edisc_api.admin", description=__doc__)
    for flag in (
        "subdomain",
        "name",
        "issuer",
        "audience",
        "jwks-url",
        "admin-subject",
        "admin-name",
        "operator",
    ):
        ap.add_argument(f"--{flag}", required=True)
    args = ap.parse_args()
    settings = Settings()

    async def run() -> None:
        engine = create_engine(settings, "app")
        try:
            key_ref = f"tenant/{args.subdomain}"
            LocalKmsClient(settings).create_key(
                key_ref
            )  # local/test only; AWS: created by Terraform
            result = await onboard_tenant(
                session_factory(engine),
                kms_key_ref=key_ref,
                subdomain=args.subdomain,
                name=args.name,
                issuer=args.issuer,
                audience=args.audience,
                jwks_url=args.jwks_url,
                admin_subject=args.admin_subject,
                admin_name=args.admin_name,
                operator=args.operator,
                reserved=(settings.api_oauth_relay_subdomain,),
            )
            sys.stdout.write(f"tenant {result.tenant_id} admin {result.admin_principal_id}\n")
        finally:
            await engine.dispose()

    asyncio.run(run())


def _move_main(argv: list[str]) -> None:
    """``python -m edisc_api.admin move-binding --provider slack_team --external-id T123
    --to-tenant <uuid> --reason "..." --operator <name>`` (owner credentials: an operator host)."""
    from edisc_core.settings import Settings
    from edisc_db.session import create_engine, session_factory

    ap = argparse.ArgumentParser(
        prog="edisc_api.admin move-binding", description=move_binding.__doc__
    )
    ap.add_argument("--provider", required=True,
                    choices=["slack_team", "slack_enterprise", "microsoft_tenant"])  # fmt: skip
    for flag in ("external-id", "to-tenant", "reason", "operator"):
        ap.add_argument(f"--{flag}", required=True)
    args = ap.parse_args(argv)
    settings = Settings()

    async def run() -> None:
        owner, app = create_engine(settings, "owner"), create_engine(settings, "app")
        try:
            source = await move_binding(
                session_factory(owner), session_factory(app), provider=args.provider,
                external_id=args.external_id, to_tenant=uuid.UUID(args.to_tenant),
                reason=args.reason, operator=args.operator,
            )  # fmt: skip
            sys.stdout.write(f"moved from {source} to {args.to_tenant}\n")
        finally:
            await owner.dispose()
            await app.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
