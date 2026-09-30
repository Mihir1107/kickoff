"""The dummy dataset is the golden dataset: deterministic, and its own oracle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from edisc_connector_dummy.dataset import Dataset
from edisc_connectors_base.types import BatchKind

from .conftest import batches, connection, connector, full_scope, make_spec, messages, units

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "dummy" / "small.json"


async def dataset_digest(
    spec_overrides: dict[str, object], epoch: int
) -> tuple[str, dict[str, int]]:
    """SHA-256 over every byte the source returns for a full collection (pages, directory, files)."""
    spec = make_spec(**spec_overrides)
    c, _ = connector()
    conn, scope = connection(spec, epoch), full_scope(spec, epoch)
    h, counts, files = hashlib.sha256(), {"batches": 0, "raw_messages": 0}, set()
    for u in await units(c, conn, scope):
        for b in await batches(c, conn, u, scope):
            h.update(b.body)
            counts["batches"] += 1
            counts["raw_messages"] += len(messages(b))
            files |= {f["id"] for m in messages(b) for f in m.get("files", [])}
    async for b in c.fetch_directory(conn, None):
        h.update(b.body)
    for file_id in sorted(files):
        async for chunk in c.open_file(conn, file_id):
            h.update(chunk)
    counts["files"] = len(files)
    return h.hexdigest(), counts


@pytest.mark.parametrize("epoch", [0, 1])
async def test_same_seed_and_epoch_is_byte_identical(epoch: int) -> None:
    assert await dataset_digest({}, epoch) == await dataset_digest({}, epoch)


async def test_seed_and_epoch_change_the_output() -> None:
    base, _ = await dataset_digest({}, 0)
    assert (await dataset_digest({"seed": 8}, 0))[0] != base
    assert (await dataset_digest({}, 1))[0] != base


async def test_golden_digest_is_stable_across_code_changes() -> None:
    """Changing the generator's output is a connector version bump: update the golden file deliberately."""
    golden = json.loads(GOLDEN.read_text())
    for entry in golden["epochs"]:
        digest, counts = await dataset_digest(golden["spec"], entry["epoch"])
        assert (digest, counts) == (entry["sha256"], entry["counts"]), f"epoch {entry['epoch']}"
        assert (
            Dataset(make_spec(**golden["spec"])).total_messages(entry["epoch"])
            == entry["total_messages"]
        )


@pytest.mark.parametrize("epoch", [0, 2])
async def test_counts_are_exactly_conversations_x_days_x_messages(epoch: int) -> None:
    spec = make_spec()
    ds = Dataset(spec)
    assert ds.total_messages(0) == 4 * 3 * 40
    assert ds.total_messages(epoch) == 4 * (3 + epoch) * 40
    c, _ = connector()
    conn, scope = connection(spec, epoch), full_scope(spec, epoch)
    all_units = await units(c, conn, scope)
    assert len(all_units) == 4 * (3 + epoch)
    for u in all_units:
        assert await c.expected_count(conn, u) == 40


@pytest.mark.parametrize("epoch", [0, 1])
async def test_collected_ids_equal_the_oracle_not_the_other_way_round(epoch: int) -> None:
    spec = make_spec()
    ds = Dataset(spec)
    c, _ = connector()
    conn, scope = connection(spec, epoch), full_scope(spec, epoch)
    raw_total = 0
    for u in await units(c, conn, scope):
        history = [b for b in await batches(c, conn, u, scope) if b.kind is BatchKind.HISTORY]
        seen = [m["ts"] for b in history for m in messages(b)]
        raw_total += len(seen)
        assert set(seen) == ds.expected_ids(u.conversation_id, ds.day_index(u.day), epoch)
    assert raw_total > ds.total_messages(
        epoch
    )  # pages overlap: duplicates must be deduplicated downstream


async def test_resume_from_any_cursor_returns_the_same_remaining_batches() -> None:
    spec = make_spec()
    c, _ = connector()
    conn, scope = connection(spec), full_scope(spec)
    u = (await units(c, conn, scope))[0]
    full = await batches(c, conn, u, scope)
    assert full[-1].next_cursor is None
    for i, batch in enumerate(full[:-1]):
        resumed = await batches(c, conn, u, scope, cursor=batch.next_cursor)
        assert [b.body for b in resumed] == [b.body for b in full[i + 1 :]]


async def test_large_units_generate_without_holding_the_dataset() -> None:
    spec = make_spec(conversations=2, days=2, messages_per_unit=600, page_size=200)
    c, _ = connector()
    conn, scope = connection(spec), full_scope(spec)
    ds = Dataset(spec)
    for u in await units(c, conn, scope):
        ids = {
            m["ts"]
            for b in await batches(c, conn, u, scope)
            if b.kind is BatchKind.HISTORY
            for m in messages(b)
        }
        assert len(ids) == 600 == ds.expected_count(u.conversation_id, ds.day_index(u.day), 0)
