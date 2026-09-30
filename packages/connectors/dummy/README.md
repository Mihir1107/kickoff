# edisc-connector-dummy

Deterministic fake source. It is the **golden dataset** for every pipeline test, and its own **oracle**:
expected counts and item sets come from `Dataset` (computed from the seed), never from what a pipeline
collected.

- `spec.py`: `DatasetSpec` (seed, size, page size, count mode, `FailureSpec`). Same `(spec, epoch)` =>
  byte-identical output (`tests/golden/dummy/small.json` pins a digest; changing it = version bump).
- `dataset.py`: the dialect-free model. `conversations x days x messages_per_unit` messages exactly at
  epoch 0; every epoch adds a day and changes existing messages.
- `dialects/slack.py`: Slack-shaped raw payloads. Add `dialects/teams.py` for a Teams-like shape.
- `connector.py`: the thin connector (enumerate, expected_count, fetch, fetch_directory, open_file),
  pagination quirks, failure injection, rate-limit hook.

Connection config: `{"spec": {...DatasetSpec...}, "epoch": 0}`.

| Area | What the dataset guarantees |
|---|---|
| Pagination | one empty page with `has_more: true` per unit, overlapping pages, out-of-order items, threads split across pages, resumable cursors |
| Time | a message at exactly 00:00:00.000 and one at 23:59:59.999 UTC in every unit; epoch edits timestamped after the original window |
| Content | emoji (ZWJ sequences, flags), RTL, zero-width and combining characters (with the precomposed twin), a 40k-character message, an empty body with only an attachment, files shared by several messages |
| Identity | user renamed mid-dataset (profile embeds), user renamed between epochs (directory), deactivated user, external shared-channel user, bot and app users |
| Epochs | new day of messages, content edits, edit-marker-only changes, deletions (tombstones), reaction changes, volatile-only changes (reply counters, file URL tokens) |
| Failures | deterministic exceptions, timeouts, 429s (first N attempts of a request), drops with true counts (gap), or a source that cannot count |
| Threads | same-day replies and next-day replies; parent-out-of-range policy (ADR 0011) |
