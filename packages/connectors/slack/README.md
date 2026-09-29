# edisc-connector-slack (stub)

**Phase 3. Do not implement yet.**

- Tier 1: customer-installed internal app (history, read, users, files scopes).
- Tier 2: Slack Discovery API adapter (`discovery.conversations.list`,
  `discovery.conversations.history`, `discovery.conversations.edits`,
  `discovery.users.list`).
- Blind spots per tier must be returned from `validate_connection` and stated in every report.
- Thin connector: authenticate, enumerate, fetch raw only. Interpretation lives in `edisc-normalizer`.
- Phase 2 (before this) adds native Slack export zip ingestion.
