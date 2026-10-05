"""The synthetic render corpus (M15 step 5): named cases, each a dummy dataset, a collection plan and
render options. Correctness comes from the oracle (`oracle.py`, computed from the dataset alone);
the goldens are regression guards only.

Every case is collected through the real pipeline (live dialects) or the real upload, lock, validate
and collection path (export dialect), then rendered through the render steps. Earlier epochs are
collected over the whole dataset; the LAST collection is the rendered job, over ``first_day`` onwards.
Real Slack exports join the corpus as `tests/fixtures/slack_exports/<name>/` (see `real_exports`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from edisc_connector_dummy.spec import DatasetSpec, FailureSpec
from edisc_connectors_base.types import ThreadParentPolicy
from edisc_renderers.rsmf import RenderOptions

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "slack_exports"


@dataclass(frozen=True)
class Case:
    spec: DatasetSpec
    epochs: tuple[int, ...] = (0,)  # collections in order; the last one is rendered
    first_day: int = 0  # the rendered job's scope starts at this day index
    policy: ThreadParentPolicy = ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD
    options: RenderOptions = field(default_factory=RenderOptions)
    source: Literal["live", "export"] = "live"
    export_tier: Literal["full", "public_only"] = "full"
    export_legacy_layout: bool = False  # an older export layout (reply_broadcast, no blocks)
    custodians: int = (
        0  # >0: custodian scopes (the first N members of conversation 0) instead of a channel scope
    )
    batch_size: int = 500  # render_files_batch_size
    check_threads: bool = True  # thread assertions (off for the cap cases, whose parts move roots)
    expect_files: int | None = None  # exact number of output files, when the case is about it
    entry_limit: int | None = None  # a tiny per-test zip entry limit (ADR 0015 §20.10)


def _spec(**kw: object) -> DatasetSpec:
    base: dict[str, object] = {
        "seed": 31, "conversations": 4, "days": 2, "messages_per_unit": 16, "page_size": 7,
        "users": 8, "p_file": 0.3, "p_reply_prev_day": 0.25, "p_broadcast": 0.3,
        "p_me_message": 0.08, "p_uninterpretable": 0.06,
    }  # fmt: skip
    return DatasetSpec.model_validate({**base, **kw})


UNAVAILABLE = FailureSpec(seed=5, file_unavailable_rate=0.4)

CASES: dict[str, Case] = {
    # every conversation type, three epochs (edits, hint-only edits, deletes as tombstones, reaction
    # changes, a renamed and an archived channel, a renamed user), the forced broadcast edited at
    # epoch 1 and deleted at epoch 2, unavailable files of every reason
    "slack_three_epochs": Case(_spec(failures=UNAVAILABLE), epochs=(0, 1, 2)),
    # the omission dialect: deleted messages vanish (no tombstone, never a deletion)
    "history_two_epochs": Case(_spec(dialect="slack_history", seed=32), epochs=(0, 1)),
    # day 0 out of range: roots (including the broadcast's) come back as marked context
    "context_out_of_range": Case(_spec(seed=33), epochs=(0, 1), first_day=1),
    # the same without context: every cut thread is recorded, never silently dropped
    "no_context": Case(
        _spec(seed=33), epochs=(0, 1), first_day=1, options=RenderOptions(include_context=False)
    ),
    # replies only: roots the job never collected are declared missing
    "replies_only": Case(
        _spec(seed=34), first_day=1, policy=ThreadParentPolicy.REPLIES_ONLY, epochs=(0, 1)
    ),
    # several custodian scopes over one conversation
    "custodians": Case(_spec(seed=35, conversations=2), custodians=2),
    # time zones: a US DST start (23 h day) and end (25 h day), +05:30 and +05:45 days
    "new_york_dst_spring": Case(
        _spec(seed=36, start_day=date(2026, 3, 7), days=3),
        options=RenderOptions(time_zone="America/New_York"),
    ),
    "new_york_dst_fall": Case(
        _spec(seed=37, start_day=date(2026, 10, 31), days=3),
        options=RenderOptions(time_zone="America/New_York"),
    ),
    "kolkata": Case(_spec(seed=38), options=RenderOptions(time_zone="Asia/Kolkata")),
    "kathmandu": Case(_spec(seed=39), options=RenderOptions(time_zone="Asia/Kathmandu")),
    # file batch boundary at a small configured size: exactly B files, then B + 1
    "batch_exact": Case(_spec(seed=40, conversations=3, days=1), batch_size=3, expect_files=3),
    "batch_plus_one": Case(_spec(seed=40, conversations=2, days=2), batch_size=3, expect_files=4),
    # the event cap on both sides: one slice of exactly 10,000 events (one file), one of 10,001 (two)
    "cap_10000": Case(
        _spec(
            seed=41,
            conversations=1,
            days=1,
            messages_per_unit=10_000,
            page_size=1000,
            p_file=0.0,
            p_thread_parent=0.0,
            p_reply_same_day=0.0,
            p_reply_prev_day=0.0,
        ),
        check_threads=False,
        expect_files=1,
    ),
    "cap_10001": Case(
        _spec(
            seed=41,
            conversations=1,
            days=1,
            messages_per_unit=10_001,
            page_size=1000,
            p_file=0.0,
            p_thread_parent=0.0,
            p_reply_same_day=0.0,
            p_reply_prev_day=0.0,
        ),
        check_threads=False,
        expect_files=2,
    ),
    # natives (ADR 0015 §11, §20): files from 768 KiB to 1.25 MiB against a 1 MiB threshold, so
    # some leave the zip and some stay; each file is reused across messages and days (one native,
    # several slices), some are unavailable (placeholders next to natives)
    "externals": Case(
        _spec(
            seed=46,
            file_size_min=768 << 10,
            file_size_span=512 << 10,
            p_file=0.35,
            failures=UNAVAILABLE,
        ),
        options=RenderOptions(external_over_bytes=1 << 20),
    ),
    # a native in a thread root rendered as context (day 0 out of range)
    "externals_context": Case(
        _spec(
            seed=47,
            file_size_min=768 << 10,
            file_size_span=512 << 10,
            p_file=0.5,
            p_reply_prev_day=0.5,
        ),
        epochs=(0, 1),
        first_day=1,
        options=RenderOptions(external_over_bytes=1 << 20),
    ),
    # parts split by the zip entry count, with a tiny per-test limit (6 entries per zip)
    "entries_split": Case(
        _spec(seed=48, p_file=0.6, conversations=2), entry_limit=6, check_threads=False
    ),
    # the export dialect: completeness against the archive, the ADR 0014 caveat in every file
    "export_full": Case(_spec(seed=42, dialect="slack_history"), source="export"),
    "export_legacy_layout": Case(
        _spec(seed=45, dialect="slack_history"), source="export", export_legacy_layout=True
    ),
    "export_public_only": Case(
        _spec(seed=43, dialect="slack_history"), source="export", export_tier="public_only"
    ),
}

LIVE = {name: c for name, c in CASES.items() if c.source == "live"}
EXPORT = {name: c for name, c in CASES.items() if c.source == "export"}
# the 500/501 boundary at the production batch size (an acceptance run: tests/integration/acceptance)
PRODUCTION_BATCH = 500


def real_exports() -> list[Path]:
    """Real Slack exports provided by the user: `<name>/export.zip` + a hand-reviewed `expected.json`
    (counts, conversations, caveats), used instead of the oracle. Listed, never silently skipped."""
    if not FIXTURES.is_dir():
        return []
    return sorted(p for p in FIXTURES.iterdir() if (p / "export.zip").is_file())
