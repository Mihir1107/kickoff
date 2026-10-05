"""Slices and parts (ADR 0015 §2).

A slice is one conversation over one local calendar day: UTC by default, or the matter time zone via
`local_day_bounds`, so DST days are 23 or 25 hours long. An event belongs to the slice containing its
own timestamp. A slice holding more than `cap` events, context included, is split into parts in
timestamp order. The split depends on the data and the options only, so a re-render gives the same parts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, tzinfo

from edisc_core.time import day_bounds, ensure_utc, local_day_bounds
from edisc_renderers.rsmf.model import Message


def slice_day(at: datetime, zone: tzinfo) -> date:
    """The local calendar day (in `zone`) whose slice contains the instant `at`."""
    return ensure_utc(at).astimezone(zone).date()


def slice_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """Half-open UTC interval of the slice for the local `day`."""
    if zone is UTC or getattr(zone, "key", None) == "UTC":
        return day_bounds(day)
    return local_day_bounds(day, zone)


def event_order(message: Message) -> tuple[datetime, str]:
    """Events are sorted by (timestamp, id); the id is the Slack ts."""
    return (message.sent_at, message.ts)


def split_parts(
    primaries: Sequence[Message],
    context_root: Callable[[Message], str | None],
    cap: int,
) -> list[list[Message]]:
    """Split a slice's primary events (already in event order) into parts of at most `cap` events,
    counting the context roots each part needs.

    `context_root(m)` is the root ts `m` would need as context in a part that does not contain the
    root as a primary, or None. A part closes when the next event (plus its context root, if that
    root is not yet in the part) would not fit. The count is conservative: a root counted as context
    that later turns out to be a primary of the same part only makes the part smaller.
    """
    parts: list[list[Message]] = []
    current: list[Message] = []
    present: set[str] = set()  # primaries and context roots already counted in this part
    for message in primaries:
        root = context_root(message)
        need = 1 + (root is not None and root not in present)
        if current and len(present) + need > cap:
            parts.append(current)
            current, present = [], set()
            need = 1 + (root is not None)
        current.append(message)
        present.add(message.ts)
        if root is not None:
            present.add(root)
        if len(present) > cap:  # cap >= 2, so a single event plus its root always fits
            raise AssertionError("part over the cap")
    if current:
        parts.append(current)
    return parts


def split_by_entries(
    part: Sequence[Message],
    context_root: Callable[[Message], str | None],
    entries_of: Callable[[str], int],
    budget: int,
) -> list[list[Message]]:
    """Split one part (its primaries, in event order) so that the zip entries its events need stay
    within ``budget`` (ADR 0015 §20.10): the entry count never moves a file out of the zip.

    ``entries_of(ts)`` is the number of attachments or placeholders of the event with that ts, as
    listed (a file two events reference counts twice, so the count is conservative). A part closes
    before the first event whose entries, plus those of its context root when the root is not yet in
    the part, would take the total over ``budget``. Context roots are counted exactly as in
    ``split_parts``. One event that cannot fit even in a part of its own raises ``ValueError``.
    """
    parts: list[list[Message]] = []
    current: list[Message] = []
    present: set[str] = set()
    total = 0
    for message in part:
        root = context_root(message)
        need = entries_of(message.ts)
        if root is not None and root not in present:
            need += entries_of(root)
        if current and total + need > budget:
            parts.append(current)
            current, present, total = [], set(), 0
            need = entries_of(message.ts) + (entries_of(root) if root is not None else 0)
        if need > budget:
            raise ValueError(
                f"event {message.ts} needs {need} zip entries, more than a part holds ({budget})"
            )
        current.append(message)
        present.add(message.ts)
        if root is not None:
            present.add(root)
        total += need
    if current:
        parts.append(current)
    return parts
