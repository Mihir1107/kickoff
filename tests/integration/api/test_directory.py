"""Directory snapshots for the scope picker (M17 plan §3.3; D12, D14; migration 0043).

The directory is captured in the background (``edisc_worker.directory``) and served from the
current COMPLETE generation (``GET /v1/connections/{id}/directory``). Expected entries come from
the dummy ``Dataset`` (the oracle), never from what was captured."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from edisc_api import directory_refresh
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import AuthenticationError
from edisc_db.session import tenant_tx
from edisc_normalizer.directory import search_key
from edisc_worker import directory
from edisc_worker.pipeline import CrashHooks

from ..custody.conftest import superuser
from .conftest import Api, TenantCtx, add_principal, new_api_tenant
from .test_jobs import World, make_world

ROOT = Path(__file__).resolve().parents[3]
SPEC = DatasetSpec.model_validate({"seed": 21, "conversations": 9, "days": 1,
                                   "messages_per_unit": 12, "users": 23, "page_size": 5})  # fmt: skip
TYPES = {"channel": "public_channel", "private_channel": "private_channel", "dm": "im",
         "group_dm": "mpim"}  # fmt: skip


def oracle(sp: DatasetSpec, epoch: int, kind: str) -> list[tuple[str, str | None, str, bool]]:
    """(external id, name, type, archived) per entry, in the served order (search key, id)."""
    ds = Dataset(sp)
    if kind == "user":
        rows = [(u.id, u.real_name, "bot" if u.is_bot else "user", u.deleted)
                for u in ds.users(epoch)]  # fmt: skip
    else:
        rows = []
        for c in ds.conversations():
            st = ds.conversation_state(c.id, epoch)
            rows.append((c.id, st["name"] or None, TYPES[c.kind], bool(st["archived"])))
    return sorted(rows, key=lambda r: (search_key(r[1] or r[0]), r[0]))


async def capture(api: Api, t: TenantCtx, w: World, *, hooks: CrashHooks | None = None,
                  deadline: float | None = None) -> tuple[str, int]:  # fmt: skip
    return await directory.refresh_one(
        api.sessions, api.resources.connectors["dummy"], tenant_id=t.tenant_id,
        connection_id=uuid.UUID(w.connection),
        deadline=deadline if deadline is not None else time.monotonic() + 600,
        hooks=hooks or CrashHooks(),
    )  # fmt: skip


async def read_all(api: Api, t: TenantCtx, w: World, kind: str, token: str | None = None,
                   **params: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:  # fmt: skip
    """Every page: (items in order, page bodies)."""
    items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    cursor = None
    async with api.client(t.subdomain, token or t.token(api.settings)) as c:
        while True:
            q = {"kind": kind, **params, **({"cursor": cursor} if cursor else {})}
            r = await c.get(f"/v1/connections/{w.connection}/directory", params=q)
            assert r.status_code == 200, r.text
            body = r.json()
            pages.append(body)
            items += body["items"] or []
            cursor = body["next_cursor"]
            if not cursor:
                return items, pages


def as_rows(items: list[dict[str, Any]]) -> list[tuple[str, str | None, str, bool]]:
    return [(i["external_id"], i["name"], i["entry_type"], i["archived"]) for i in items]


async def _set_epoch(api: Api, t: TenantCtx, w: World, epoch: int) -> None:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(
            text("UPDATE connections SET config = jsonb_set(config, '{epoch}', to_jsonb(CAST(:e AS integer)))"
                 " WHERE id = :c"), {"e": epoch, "c": uuid.UUID(w.connection)})  # fmt: skip


# ------------------------------------------------------------------ captured and served
async def test_not_captured_is_its_own_answer_never_an_empty_list(
    api: Api, tenant: TenantCtx
) -> None:
    w = await make_world(api, tenant, SPEC)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        r = await c.get(f"/v1/connections/{w.connection}/directory", params={"kind": "user"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "not_captured" and body["items"] is None
    assert body["snapshot_id"] is None and body["refresh_running"] is False
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for limit, status in ((500, 200), (501, 422)):  # D14: pages of at most 500
            r = await c.get(f"/v1/connections/{w.connection}/directory",
                            params={"kind": "user", "limit": limit})  # fmt: skip
            assert r.status_code == status, (limit, r.text)


async def test_a_captured_directory_is_served_whole_in_pages_exactly_once(
    api: Api, tenant: TenantCtx
) -> None:
    w = await make_world(api, tenant, SPEC)
    outcome, pages = await capture(api, tenant, w)
    users_pages = -(-SPEC.users // SPEC.page_size)
    assert outcome == "completed" and pages == users_pages + -(
        -SPEC.conversations // SPEC.page_size
    )
    for kind in ("user", "conversation"):
        items, bodies = await read_all(api, tenant, w, kind, limit=4)
        assert as_rows(items) == oracle(SPEC, 0, kind)  # every entry once, in the served order
        assert len(bodies) == -(-len(items) // 4)
        assert {b["snapshot_id"] for b in bodies} == {bodies[0]["snapshot_id"]}
        expected_total = SPEC.users if kind == "user" else SPEC.conversations
        assert all(b["approximate_total"] == expected_total and b["generation"] == 1
                   and b["status"] == "captured" and b["captured_at"] for b in bodies)  # fmt: skip


async def test_prefix_search_matches_the_oracle(api: Api, tenant: TenantCtx) -> None:
    w = await make_world(api, tenant, SPEC)
    await capture(api, tenant, w)
    for kind, q in [("user", "USER1"), ("user", "user0"), ("conversation", "conv-"),
                    ("conversation", "mpdm"), ("user", "zz-nothing")]:  # fmt: skip
        items, _ = await read_all(api, tenant, w, kind, q=q, limit=3)
        want = [r for r in oracle(SPEC, 0, kind) if search_key(r[1] or r[0]).startswith(
            search_key(q))]  # fmt: skip
        assert as_rows(items) == want, (kind, q)


def test_prefix_bounds_cover_exactly_the_prefix() -> None:
    from edisc_api.routes.directory import prefix_bounds

    assert prefix_bounds("Ab") == ("ab", "ac")
    assert prefix_bounds("a\U0010ffff") == ("a\U0010ffff", "b")
    assert prefix_bounds("x" + chr(0xD7FF)) == (
        "x" + chr(0xD7FF),
        "x" + chr(0xE000),
    )  # never a surrogate
    assert prefix_bounds(None) == (None, None)


async def test_a_get_never_calls_the_source(api: Api, tenant: TenantCtx) -> None:
    """Plan T24: the source is called only by the background capture, through the limiter."""
    w = await make_world(api, tenant, SPEC)
    await capture(api, tenant, w)

    class Refuse:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"a directory GET touched the connector ({name})")

    saved = dict(api.resources.connectors)
    api.resources.connectors.clear()
    api.resources.connectors["dummy"] = Refuse()  # type: ignore[assignment]
    try:
        items, _ = await read_all(api, tenant, w, "user", limit=500)
    finally:
        api.resources.connectors.clear()
        api.resources.connectors.update(saved)
    assert len(items) == SPEC.users


# ------------------------------------------------------------------ crashes, generations, failures
class CrashAfter(CrashHooks):
    def __init__(self, pages: int) -> None:
        self.left = pages

    async def hit(self, point: str) -> None:
        if point == "directory_after_page":
            self.left -= 1
            if self.left < 0:
                raise RuntimeError("crash point: between directory pages")


async def test_a_capture_killed_mid_way_resumes_from_its_checkpoint_never_served_partial(
    api: Api, tenant: TenantCtx
) -> None:
    w = await make_world(api, tenant, SPEC)
    with pytest.raises(RuntimeError, match="crash point"):
        await capture(api, tenant, w, hooks=CrashAfter(2))
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        body = (await c.get(f"/v1/connections/{w.connection}/directory",
                            params={"kind": "user"})).json()  # fmt: skip
    assert body["status"] == "not_captured" and body["refresh_running"]  # partial: never served
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        snap = (await s.execute(text("SELECT pages, users, status FROM directory_snapshots"
                                     " WHERE connection_id = :c"),
                                {"c": uuid.UUID(w.connection)})).one()  # fmt: skip
    assert (snap.status, snap.pages, snap.users) == ("capturing", 3, 3 * SPEC.page_size)
    outcome, pages = await capture(api, tenant, w)  # resumes at page 4
    total = -(-SPEC.users // SPEC.page_size) + -(-SPEC.conversations // SPEC.page_size)
    assert outcome == "completed" and pages == total - 3
    for kind in ("user", "conversation"):
        items, bodies = await read_all(api, tenant, w, kind, limit=500)
        assert as_rows(items) == oracle(SPEC, 0, kind)
        assert bodies[0]["generation"] == 1  # the same generation, completed


@pytest.mark.parametrize("point", ["directory_page_tx", "directory_after_page"])
async def test_a_sigkill_mid_capture_resumes_with_no_gap_and_no_duplicate(
    api: Api, tenant: TenantCtx, tmp_path: Path, point: str
) -> None:
    """Plan §8: a capture PROCESS killed (SIGKILL) inside a page's transaction (entries written,
    checkpoint not moved: rolled back) or right after a page committed, two pages in; a new run
    resumes from the checkpoint and the generation holds every entry exactly once."""
    w = await make_world(api, tenant, SPEC)
    with pytest.raises(RuntimeError, match="crash point"):
        await capture(api, tenant, w, hooks=CrashAfter(1))  # two pages committed
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.integration.api.directory_kill_target", str(tenant.tenant_id),
        w.connection, point, str(barrier), cwd=ROOT, env=dict(os.environ),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        reached = barrier / f"{point}.reached"
        async with asyncio.timeout(60):
            while not reached.exists():
                if proc.returncode is not None:
                    raise AssertionError(f"the capture process exited {proc.returncode}")
                await asyncio.sleep(0.02)
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGKILL)
        await proc.wait()
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        snap = (await s.execute(text("SELECT status, pages FROM directory_snapshots"
                                     " WHERE connection_id = :c"),
                                {"c": uuid.UUID(w.connection)})).one()  # fmt: skip
    # killed inside the transaction: page 3 rolled back; after the commit: page 3 kept
    assert (snap.status, snap.pages) == ("capturing", 2 if point == "directory_page_tx" else 3)
    outcome, _ = await capture(api, tenant, w)
    assert outcome == "completed"
    for kind in ("user", "conversation"):
        items, bodies = await read_all(api, tenant, w, kind, limit=500)
        assert as_rows(items) == oracle(SPEC, 0, kind)
        expected = SPEC.users if kind == "user" else SPEC.conversations
        assert bodies[0]["approximate_total"] == expected  # counted once


async def test_a_page_stored_twice_adds_nothing(api: Api, tenant: TenantCtx) -> None:
    """Two executors of one generation (a zombie attempt and its retry): the one whose checkpoint
    moved under it reloads; entries are unique per generation."""
    w = await make_world(api, tenant, SPEC)
    conn, snap = await directory._open(api.sessions, api.resources.connectors["dummy"],
                                       tenant.tenant_id, uuid.UUID(w.connection))  # fmt: skip
    assert snap is not None
    batch = await anext(api.resources.connectors["dummy"].fetch_directory(conn, None))
    found = await asyncio.to_thread(
        directory.parse_page, batch.body, batch.request["method"], "api"
    )
    first = await directory._store_page(api.sessions, tenant.tenant_id, snap, found,
                                        batch.next_cursor)  # fmt: skip
    again = await directory._store_page(api.sessions, tenant.tenant_id, snap, found,
                                        batch.next_cursor)  # fmt: skip
    assert (
        first == SPEC.page_size and again is None
    )  # the checkpoint moved: the second stores nothing
    await capture(api, tenant, w)
    items, _ = await read_all(api, tenant, w, "user", limit=500)
    assert as_rows(items) == oracle(SPEC, 0, "user")


async def test_paging_continues_in_its_generation_while_newer_ones_complete(
    api: Api, tenant: TenantCtx
) -> None:
    """A cursor names its generation: generation 1 keeps paging after 2 and 3 complete (3
    supersedes 1, which stays readable); a fresh read sees the newest."""
    w = await make_world(api, tenant, SPEC)
    await capture(api, tenant, w)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        first = (await c.get(f"/v1/connections/{w.connection}/directory",
                             params={"kind": "conversation", "limit": 2})).json()  # fmt: skip
        await _set_epoch(api, tenant, w, 2)  # conversation 0 renamed, 1 archived
        await capture(api, tenant, w)
        await capture(api, tenant, w)
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            gens = (await s.execute(text("SELECT generation, status FROM directory_snapshots"
                                         " WHERE connection_id = :c ORDER BY generation"),
                                    {"c": uuid.UUID(w.connection)})).all()  # fmt: skip
        assert [tuple(g) for g in gens] == [(1, "superseded"), (2, "complete"), (3, "complete")]
        rest = []
        cursor = first["next_cursor"]
        while cursor:
            page = (await c.get(f"/v1/connections/{w.connection}/directory",
                                params={"kind": "conversation", "limit": 2,
                                        "cursor": cursor})).json()  # fmt: skip
            assert page["generation"] == 1
            rest += page["items"]
            cursor = page["next_cursor"]
    assert as_rows(first["items"] + rest) == oracle(SPEC, 0, "conversation")
    items, bodies = await read_all(api, tenant, w, "conversation")
    assert bodies[0]["generation"] == 3 and as_rows(items) == oracle(SPEC, 2, "conversation")


async def test_refused_credentials_fail_the_generation_and_the_previous_stays_served(
    api: Api, tenant: TenantCtx
) -> None:
    w = await make_world(api, tenant, SPEC)
    await capture(api, tenant, w)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        await s.execute(text("UPDATE connections SET config = config || '{\"auth_revoked\": true}'"
                             " WHERE id = :c"), {"c": uuid.UUID(w.connection)})  # fmt: skip
    outcome, _ = await capture(api, tenant, w)
    assert outcome == "failed"
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        failed = (await s.execute(text("SELECT failed_reason FROM directory_snapshots"
                                       " WHERE connection_id = :c AND status = 'failed'"),
                                  {"c": uuid.UUID(w.connection)})).scalar_one()  # fmt: skip
    assert AuthenticationError.__name__ in failed
    items, bodies = await read_all(api, tenant, w, "user", limit=500)
    assert bodies[0]["generation"] == 1 and not bodies[0]["refresh_running"]
    assert as_rows(items) == oracle(SPEC, 0, "user")


async def test_the_sweep_captures_due_connections_and_skips_fresh_ones(
    api: Api, tenant: TenantCtx, sweeper_sessions: Any
) -> None:
    w = await make_world(api, tenant, SPEC)
    sweep = await directory.refresh_directories(
        sweeper_sessions, api.sessions, api.settings, dict(api.resources.connectors),
        tenant_id=tenant.tenant_id, limit=1_000_000,
    )  # fmt: skip
    assert sweep.completed >= 1 and not sweep.errors
    again = await directory.refresh_directories(
        sweeper_sessions, api.sessions, api.settings, dict(api.resources.connectors),
        tenant_id=tenant.tenant_id, limit=1_000_000,
    )  # fmt: skip
    assert again.completed == 0  # fresh: not due
    # a re-authorization (credentials replaced) makes it due at once
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        await s.execute(text("UPDATE connections SET token_updated_at = now() WHERE id = :c"),
                        {"c": uuid.UUID(w.connection)})  # fmt: skip
    third = await directory.refresh_directories(
        sweeper_sessions, api.sessions, api.settings, dict(api.resources.connectors),
        tenant_id=tenant.tenant_id, limit=1_000_000,
    )  # fmt: skip
    assert third.completed == 1
    _, bodies = await read_all(api, tenant, w, "user")
    assert bodies[0]["generation"] == 2


async def test_a_budget_stops_at_a_page_boundary_and_the_next_run_resumes(
    api: Api, tenant: TenantCtx
) -> None:
    w = await make_world(api, tenant, SPEC)
    outcome, pages = await capture(api, tenant, w, deadline=time.monotonic())  # already spent
    assert (outcome, pages) == ("partial", 1)
    outcome, _ = await capture(api, tenant, w)
    assert outcome == "completed"
    items, _ = await read_all(api, tenant, w, "user", limit=500)
    assert as_rows(items) == oracle(SPEC, 0, "user")


async def test_an_install_starts_a_capture_run(api: Api, tenant: TenantCtx) -> None:
    """``directory_refresh.start``: the maintenance run under its own id (best effort; the
    schedule captures the connection anyway)."""
    connection = uuid.uuid4()
    await directory_refresh.start(api.temporal, connection)
    await directory_refresh.start(api.temporal, connection)  # already started: no error
    handle = api.temporal.get_workflow_handle(f"directory-refresh-{connection}")
    desc = await handle.describe()
    assert desc.workflow_type == "MaintenanceWorkflow"
    await handle.terminate("test: no maintenance worker polls the queue here")


@pytest.fixture
async def other_tenant(api: Api, kms: Any) -> TenantCtx:
    return await new_api_tenant(api, kms)


# ------------------------------------------------------------------ who sees it (D12), tenants
@pytest.mark.parametrize(
    ("role", "scope", "status"),
    [("tenant_admin", "tenant", 200), ("client_admin", "client", 200),
     ("matter_manager", "matter", 200), ("collector", "matter", 200), ("reviewer", "matter", 404),
     ("auditor", "matter", 404), ("client_admin", "other_client", 404)],
)  # fmt: skip
async def test_who_sees_directory_names(
    api: Api, tenant: TenantCtx, role: str, scope: str, status: int
) -> None:
    """D12's default: ``connection.read`` on the client or ``job.start`` (a matter's picker);
    reviewers and auditors never see a workspace's names."""
    w = await make_world(api, tenant, SPEC)
    other = await make_world(api, tenant, SPEC)
    await capture(api, tenant, w)
    where = {"tenant": None, "client": uuid.UUID(w.client), "matter": uuid.UUID(w.matter),
             "other_client": uuid.UUID(other.client)}[scope]  # fmt: skip
    _, subject = await add_principal(
        api, tenant, roles=[(role, "tenant" if scope == "tenant" else scope.replace("other_", ""),
                             where)])  # fmt: skip
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        r = await c.get(f"/v1/connections/{w.connection}/directory", params={"kind": "user"})
    assert r.status_code == status, r.text
    if status == 200:
        assert len(r.json()["items"]) == min(200, SPEC.users)


async def test_tenants_never_see_each_others_directories(
    api: Api, tenant: TenantCtx, other_tenant: TenantCtx
) -> None:
    """Both tenants' workspaces have the same external ids (one dummy spec): each host serves
    only its own generation; A's connection on B's host is 404; A's cursor on B's connection is
    refused (bound to A's path)."""
    a = await make_world(api, tenant, SPEC)
    b = await make_world(api, other_tenant, SPEC)
    await capture(api, tenant, a)
    await capture(api, other_tenant, b)
    first = None
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        first = (await c.get(f"/v1/connections/{a.connection}/directory",
                             params={"kind": "user", "limit": 3})).json()  # fmt: skip
    async with api.client(other_tenant.subdomain, other_tenant.token(api.settings)) as c:
        r = await c.get(f"/v1/connections/{a.connection}/directory", params={"kind": "user"})
        assert r.status_code == 404
        r = await c.get(f"/v1/connections/{b.connection}/directory",
                        params={"kind": "user", "limit": 3, "cursor": first["next_cursor"]})  # fmt: skip
        assert (r.status_code, r.json()["error"]) == (422, "cursor_mismatch")
        mine = (await c.get(f"/v1/connections/{b.connection}/directory",
                            params={"kind": "user", "limit": 3})).json()  # fmt: skip
    assert mine["snapshot_id"] != first["snapshot_id"]
    assert [i["external_id"] for i in mine["items"]] == [i["external_id"] for i in first["items"]]


# ------------------------------------------------------------------ Grid scale
GRID = DatasetSpec.model_validate({"seed": 3, "conversations": 4, "days": 1,
                                   "messages_per_unit": 12, "users": 30_000, "page_size": 1000})  # fmt: skip


async def test_a_grid_sized_directory_pages_on_its_index(api: Api, tenant: TenantCtx) -> None:
    """30,000 users in 1,000-entry source pages, served in pages of 500: every user exactly once,
    and a deep page is an index range read (no sort, no sequential scan) whatever its depth."""
    w = await make_world(api, tenant, GRID)
    outcome, pages = await capture(api, tenant, w)
    assert outcome == "completed" and pages == 31
    items, bodies = await read_all(api, tenant, w, "user", limit=500)
    assert len(bodies) == 60 and len(items) == GRID.users
    assert [i["external_id"] for i in items] == [r[0] for r in oracle(GRID, 0, "user")]
    deep = bodies[-2]["items"][-1]
    conn = await superuser(api.settings)
    try:
        await conn.execute("ANALYZE directory_entries")
        snapshot = uuid.UUID(bodies[0]["snapshot_id"])
        from edisc_api.routes.directory import PAGE

        sql = (PAGE.replace("CAST(:lo AS text)", "NULL").replace("CAST(:hi AS text)", "NULL")
               .replace(":after_key", "$3").replace(":after_id", "$4").replace(":s", "$1")
               .replace(":k", "$2").replace(":n", "501"))  # fmt: skip
        plan = "\n".join(r[0] for r in await conn.fetch(
            f"EXPLAIN {sql}", snapshot, "user", search_key(deep["name"]), deep["external_id"]))  # fmt: skip
    finally:
        await conn.close()
    assert (
        "ix_directory_entries_search" in plan and "Sort" not in plan and "Seq Scan" not in plan
    ), plan


# ------------------------------------------------------------------ an export: no source call
async def test_an_exports_directory_comes_from_its_validated_rows_and_users_json(
    exp: Api, tenant: TenantCtx
) -> None:
    """Plan §3.3: an export's snapshot is built once from ``export_conversations`` and the locked
    archive's ``users.json``; nothing calls Slack (the export connector has no source)."""
    from .conftest import FileHost, export_connector
    from .test_exports import settled, slack_export, upload

    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id, done = await upload(c, tenant.default_client_id, slack_export(full=True))
        assert done.status_code == 202, done.text
        out = await settled(c, export_id)
    assert out["status"] == "ready"
    connector = export_connector(exp, FileHost())
    outcome, pages = await directory.refresh_one(
        exp.sessions, connector, tenant_id=tenant.tenant_id,
        connection_id=uuid.UUID(out["connection_id"]), deadline=time.monotonic() + 60,
        hooks=CrashHooks(),
    )  # fmt: skip
    assert (outcome, pages) == ("completed", 1)  # users.json; the conversations came from rows
    w = World(str(tenant.default_client_id), "", out["connection_id"], SPEC)
    conversations, _ = await read_all(exp, tenant, w, "conversation")
    assert as_rows(conversations) == [
        ("D1", None, "im", False),
        ("C1", "general", "public_channel", False),
        ("C3", "quiet", "public_channel", False),
        ("C2", "random", "public_channel", False),
    ]  # search order: the DM by its id ("d1"), then the names
    users, _ = await read_all(exp, tenant, w, "user")
    assert as_rows(users) == [("U1", "alice", "user", False)]
