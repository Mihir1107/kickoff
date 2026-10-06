"""The collection report's oracle (ADR 0018 §15): what `units.jsonl` and `report.json` must say,
from the dummy connector's `Dataset` and the scenario's injected conditions, never from collected
data."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from edisc_connector_dummy.dataset import Dataset, unit
from edisc_connector_dummy.spec import CountMode
from edisc_core.schemas import JobStatus
from edisc_renderers.report import model as m


@dataclass(frozen=True)
class Unit:
    key: str
    status: str
    recon_status: str
    expected: int | None  # None: not determined by the oracle (not compared)
    collected: int | None


def units(ds: Dataset, epoch: int) -> dict[str, Unit]:
    """Every conversation-day unit of a ``"*"`` job over all the epoch's days, plus the directory."""
    sp, f = ds.spec, ds.spec.failures
    out: dict[str, Unit] = {"directory": Unit("directory", "done", "not_applicable", None, 0)}
    for index, conv in enumerate(ds.conversations()):
        lost_from = f.inaccessible_from_epoch.get(index)
        for d in range(ds.n_days(epoch)):
            key = f"{conv.id}/{ds.day(d).isoformat()}"
            truth = ds.expected_count(conv.id, d, epoch)
            if index in f.unavailable_conversations or index in f.corrupt_conversations:
                # unavailable: every request fails; corrupt: the cursor of page 2 (every unit of
                # the report specs has more than one page: messages_per_unit > page_size)
                out[key] = Unit(key, "failed", "failed", None, None)
            elif lost_from is not None and epoch >= lost_from:
                out[key] = Unit(key, "done", "access_lost", None, 0)
            elif sp.count_mode is CountMode.UNAVAILABLE:
                out[key] = Unit(key, "done", "unverifiable", None, truth)
            else:
                dropped = sum(
                    1
                    for msg in ds.visible_messages(conv.id, d, epoch)
                    if f.drop_rate > 0
                    and unit(ds.seed, "drop", f.seed, conv.id, msg.ts) < f.drop_rate
                )
                recon = "gap" if dropped else "matched"
                out[key] = Unit(key, "done", recon, truth, truth - dropped)
    return out


def job_status(us: dict[str, Unit], *, archive: bool = False) -> str:
    counted = [u for k, u in us.items() if k != "directory"]
    if any(u.status == "failed" for u in counted):
        return JobStatus.COMPLETED_WITH_FAILED_UNITS.value
    if any(u.recon_status in ("gap", "surplus", "access_lost") for u in counted):
        return JobStatus.COMPLETED_WITH_GAPS.value
    if any(u.recon_status == "unverifiable" for u in counted):
        return JobStatus.COMPLETED_UNVERIFIED.value
    return (JobStatus.COMPLETED_AGAINST_ARCHIVE if archive else JobStatus.COMPLETED).value


def check(
    document: dict[str, Any], rows: list[dict[str, Any]], us: dict[str, Unit], status: str
) -> None:
    """The report states exactly the oracle: every unit once with its status, the counts, the clean
    verdict and its banner."""
    got = {r["unit_key"]: r for r in rows}
    assert set(got) == set(us), (sorted(set(got) ^ set(us)))[:5]
    for key, want in us.items():
        r = got[key]
        assert (r["status"], r["recon_status"]) == (want.status, want.recon_status), (key, r)
        if want.expected is not None:
            assert r["expected"] == want.expected, (key, r)
        if want.collected is not None:
            assert r["collected"] == want.collected, (key, r)
        assert r["source"] == "job_chain" and not r["divergent"], (key, r)
        assert r["day"] == (None if key == "directory" else key.rsplit("/", 1)[1])
    assert [unit_order(r) for r in rows] == sorted(unit_order(r) for r in rows)
    assert document["job"]["status"] == status
    is_clean = status == "completed"
    assert document["job"]["clean"] is is_clean
    if is_clean:
        assert document["banner"] == ["Complete: every unit reconciled against the source"]
    else:
        assert "Complete: every unit reconciled against the source" not in document["banner"]
    recon = Counter(u.recon_status for k, u in us.items() if k != "directory")
    rows_by_value = {r["value"]: r["count"] for r in document["counts"]["units_by_recon_status"]}
    for value, n in rows_by_value.items():
        assert n == recon.get(value, 0), (value, n, recon)
    exceptions = sum(n for v, n in recon.items() if v in m.EXCEPTION_RECON)
    assert document["exceptions"]["units"]["total"] == exceptions


def unit_order(r: dict[str, Any]) -> tuple[str, str, str]:
    return m.unit_order(r)
