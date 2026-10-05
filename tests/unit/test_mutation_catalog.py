"""The mutation catalog (scripts/mutation) stays runnable: every edit applies exactly once to the
current source, names are unique, and every named test file exists. A refactor that moves a guarded
line must update its catalog entry in the same change."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "mutation"))

from catalog import CATALOG  # noqa: E402
from run import apply  # noqa: E402


def test_every_edit_applies_once_and_every_test_exists() -> None:
    assert len({m.name for m in CATALOG}) == len(CATALOG)
    for m in CATALOG:
        assert (ROOT / m.test).is_file(), m.name
        mutated = apply((ROOT / m.path).read_text(), m)  # raises unless each edit occurs once
        assert mutated != (ROOT / m.path).read_text(), m.name
