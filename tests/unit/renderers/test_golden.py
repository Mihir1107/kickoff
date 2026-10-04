"""Golden bytes, keyed by renderer version (ADR 0015 §6).

`tests/golden/rsmf/<RENDERER_VERSION>/<case>/` holds every file of a render plus `index.json`.
Changing the output bytes without bumping `RENDERER_VERSION` fails here. After a deliberate bump,
record the new generation (the old ones stay as history):

    EDISC_RECORD_RSMF=1 uv run pytest tests/unit/renderers/test_golden.py

Recording never overwrites: it only writes a version directory that does not exist yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from dataclasses import dataclass

import pytest

from edisc_connector_dummy.spec import DatasetSpec
from edisc_renderers.rsmf import RENDERER_VERSION, RenderOptions, render_job
from tests.unit.renderers.emlcheck import check_eml
from tests.unit.renderers.oracle import build_job

GOLDEN = pathlib.Path(__file__).parents[2] / "golden" / "rsmf"
SPEC = DatasetSpec(seed=20261004, conversations=4, days=2, messages_per_unit=12)


@dataclass(frozen=True)
class Case:
    spec: DatasetSpec
    job: dict[str, object]
    options: RenderOptions


CASES = {
    # every conversation type; day 0 is out of range, so its roots come back as marked context
    "utc_context": Case(SPEC, {"epoch": 1, "day_from": 1, "unavailable_every": 3}, RenderOptions()),
    # a matter time zone, context off (edisc.parent_not_rendered) and a small cap (parts)
    "new_york_no_context_cap5": Case(
        SPEC,
        {"epoch": 1, "day_from": 1, "unavailable_every": 3},
        RenderOptions(include_context=False, time_zone="America/New_York", cap=5),
    ),
    # an export job: completeness basis "archive" and the ADR 0014 caveat in the text part
    "archive": Case(
        DatasetSpec(seed=7, conversations=1, days=1, messages_per_unit=12, dialect="slack_history"),
        {"completeness_basis": "archive"},
        RenderOptions(),
    ),
}


def _render(case: Case) -> tuple[dict[str, bytes], dict[str, object]]:
    oj = build_job(case.spec, **case.job)  # type: ignore[arg-type]
    result = render_job(
        oj.job, oj.conversations, oj.messages, oj.identities, oj.files, case.options
    )
    files = {f.name: b"".join(f.stream(oj.opener)) for f in result.files}
    index = {
        "renderer_version": RENDERER_VERSION,
        "options": case.options.as_payload(),
        "files": [
            {"name": n, "sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
            for n, b in files.items()
        ],
        "reconciliation": result.reconciliation.as_payload(),
    }
    return files, index


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_bytes(name: str) -> None:
    files, index = _render(CASES[name])
    for data in files.values():
        check_eml(data)
    directory = GOLDEN / RENDERER_VERSION / name
    if not directory.exists():
        if os.environ.get("EDISC_RECORD_RSMF") != "1":
            pytest.fail(
                f"no golden files for renderer {RENDERER_VERSION}/{name}; record them with "
                "EDISC_RECORD_RSMF=1 (only after a deliberate RENDERER_VERSION bump)"
            )
        directory.mkdir(parents=True)
        for file_name, data in files.items():
            (directory / file_name).write_bytes(data)
        (directory / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        return
    recorded = json.loads((directory / "index.json").read_text())
    assert index == recorded, (
        f"render output changed under renderer version {RENDERER_VERSION}: bump RENDERER_VERSION "
        "and record a new golden generation"
    )
    on_disk = sorted(p.name for p in directory.glob("*.rsmf"))
    assert on_disk == sorted(files)
    for file_name, data in files.items():
        assert (directory / file_name).read_bytes() == data, file_name


def test_every_recorded_version_has_its_cases() -> None:
    """Old generations stay as history; the current one must exist (no silent skip)."""
    assert (GOLDEN / RENDERER_VERSION).is_dir()
    assert sorted(p.name for p in (GOLDEN / RENDERER_VERSION).iterdir()) == sorted(CASES)
