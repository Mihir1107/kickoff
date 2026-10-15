"""An IdP's groups claim is never read as "no groups" when it is not a list (`edisc_api.idp_groups`).

Entra ID's groups overage (`_claim_names` / `_claim_sources`, or `hasgroups`) refuses the sign-in
and a bearer token, whatever the tenant's mapping. An ABSENT (or malformed) claim refuses them when
the tenant grants roles through IdP groups; a present, empty list is accepted. Each refusal is
audited (`authentication_refused`, and `login_failed` for a sign-in) and alerts the tenant admins
once per principal and reason."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from edisc_api.auth import dev_token
from edisc_core.ids import new_id
from edisc_db.session import tenant_tx

from .conftest import AUDIENCE, Api, TenantCtx
from .oidc import Browser
from .sessions_flow import call, enable_login, sign_in

OVERAGE = {
    "_claim_names": {"groups": "src1"},
    "_claim_sources": {
        "src1": {"endpoint": "https://graph.microsoft.com/v1.0/users/x/getMemberObjects"}
    },
}


@pytest.fixture
async def t(api: Api, tenant: TenantCtx) -> TenantCtx:
    await enable_login(api, tenant)
    return tenant


async def _map_group(api: Api, t: TenantCtx, external_id: str = "lit-team") -> uuid.UUID:
    """A group the IdP's claim maps to, holding a tenant-wide role: roles now depend on groups."""
    group = new_id()
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(
            text("INSERT INTO groups (id, tenant_id, name, external_id) VALUES (:i, :t, :n, :e)"),
            {"i": group, "t": t.tenant_id, "n": f"g-{external_id}", "e": external_id},
        )
        await s.execute(
            text(
                "INSERT INTO role_assignments (id, tenant_id, group_id, role, scope_type, created_by)"
                " VALUES (:i, :t, :g, 'reviewer', 'tenant', 'tests')"
            ),
            {"i": new_id(), "t": t.tenant_id, "g": group},
        )
    return group


async def _rows(api: Api, t: TenantCtx, sql: str) -> list[Any]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return list((await s.execute(text(sql))).all())


async def _refusals(api: Api, t: TenantCtx) -> list[dict[str, Any]]:
    rows = await _rows(
        api, t,
        "SELECT payload FROM custody_events WHERE event_type = 'audit.authentication_refused'"
        " ORDER BY seq",
    )  # fmt: skip
    return [r.payload for r in rows]


async def _alerts(api: Api, t: TenantCtx) -> list[str]:
    return [r.kind for r in await _rows(api, t, "SELECT kind FROM alerts ORDER BY created_at")]


async def _sessions(api: Api, t: TenantCtx) -> int:
    return len(await _rows(api, t, "SELECT id FROM sessions"))


def _bearer(api: Api, t: TenantCtx, **claims: Any) -> str:
    return dev_token(api.settings, subject=t.admin_subject, audience=AUDIENCE, **claims)


# ------------------------------------------------------------------ overage
@pytest.mark.parametrize("indicator", ["claim_names", "hasgroups"])
async def test_an_overage_refuses_the_sign_in_even_without_group_mapping(
    api: Api, t: TenantCtx, indicator: str
) -> None:
    claims = OVERAGE if indicator == "claim_names" else {"hasgroups": True}
    r = await sign_in(api, t, Browser(), **claims)
    assert r.status_code == 403 and "url=/?auth_error=groups_overage" in r.text
    assert await _sessions(api, t) == 0
    (refused,) = await _refusals(api, t)
    assert refused["reason"] == "groups_overage" and refused["detail"] == "overage"
    assert refused["via"] == "sign_in" and refused["principal_id"] == str(t.admin_id)
    failed = await _rows(
        api, t, "SELECT payload FROM custody_events WHERE event_type = 'audit.login_failed'"
    )
    assert failed[-1].payload["reason"] == "groups_overage"
    assert await _alerts(api, t) == ["groups_overage"]


async def test_an_overage_refuses_a_bearer_token_with_the_generic_401(
    api: Api, tenant: TenantCtx
) -> None:
    token = _bearer(api, tenant, **OVERAGE)
    for _ in range(2):
        async with api.client(tenant.subdomain, token) as c:
            r = await c.get("/v1/me")
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    refusals = await _refusals(api, tenant)
    assert [x["via"] for x in refusals] == ["bearer", "bearer"]  # every refusal audited
    assert await _alerts(api, tenant) == ["groups_overage"]  # one alert until acknowledged


# ------------------------------------------------------------------ missing when roles depend on groups
async def test_a_missing_groups_claim_refuses_the_sign_in_when_roles_depend_on_groups(
    api: Api, t: TenantCtx
) -> None:
    await _map_group(api, t)
    r = await sign_in(api, t, Browser())  # the test IdP sends no groups claim unless told to
    assert r.status_code == 403 and "url=/?auth_error=groups_missing" in r.text
    assert await _sessions(api, t) == 0
    (refused,) = await _refusals(api, t)
    assert refused["reason"] == "groups_missing" and refused["detail"] == "absent"
    assert await _alerts(api, t) == ["groups_missing"]
    malformed = await sign_in(api, t, Browser(), groups="lit-team")  # a string, not a list
    assert "url=/?auth_error=groups_missing" in malformed.text
    assert (await _refusals(api, t))[-1]["detail"] == "malformed"


async def test_a_present_groups_claim_signs_in_and_grants_the_mapped_roles(
    api: Api, t: TenantCtx
) -> None:
    group = await _map_group(api, t)
    browser = Browser()
    r = await sign_in(api, t, browser, groups=["lit-team"])
    assert r.status_code == 200, r.text
    perms = await call(api, t, browser, "GET", "/v1/me/permissions")
    roles = {r for item in perms.json()["items"] for r in item["roles"]}
    assert "reviewer" in roles, perms.text  # through the group the claim named
    empty = await sign_in(api, t, Browser(), groups=[])  # "in no group" is an answer
    assert empty.status_code == 200
    assert await _refusals(api, t) == [] and await _alerts(api, t) == []
    assert group


async def test_a_missing_groups_claim_is_accepted_when_no_role_depends_on_groups(
    api: Api, t: TenantCtx
) -> None:
    async with tenant_tx(api.sessions, t.tenant_id) as s:  # a mapped group WITHOUT a role
        await s.execute(
            text("INSERT INTO groups (id, tenant_id, name, external_id) VALUES (:i, :t, 'n', 'x')"),
            {"i": new_id(), "t": t.tenant_id},
        )
    assert (await sign_in(api, t, Browser())).status_code == 200
    async with api.client(t.subdomain, _bearer(api, t)) as c:
        assert (await c.get("/v1/me")).status_code == 200
    assert await _refusals(api, t) == []


async def test_a_missing_groups_claim_refuses_a_bearer_token_when_roles_depend_on_groups(
    api: Api, tenant: TenantCtx
) -> None:
    await _map_group(api, tenant)
    async with api.client(tenant.subdomain, _bearer(api, tenant)) as c:
        assert (await c.get("/v1/me")).status_code == 401
    async with api.client(tenant.subdomain, _bearer(api, tenant, groups=[])) as c:
        assert (await c.get("/v1/me")).status_code == 200
    (refused,) = await _refusals(api, tenant)
    assert refused["reason"] == "groups_missing" and refused["via"] == "bearer"
    assert await _alerts(api, tenant) == ["groups_missing"]
