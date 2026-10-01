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
) -> Onboarded:
    """``kms_key_ref``: the tenant's KEK, created beforehand by the operator (KMS key management is
    infrastructure, not application code)."""
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


def main() -> None:
    from edisc_core.kms import LocalKmsClient
    from edisc_core.settings import Settings
    from edisc_db.session import create_engine, session_factory

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
            )
            sys.stdout.write(f"tenant {result.tenant_id} admin {result.admin_principal_id}\n")
        finally:
            await engine.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
