# Golden datasets

Seeded dummy-connector specs and their expected counts. Each spec is a JSON file:
`{seed, conversations, days, messages_per_unit, ...}` plus the expected totals it must produce.
Changing the dummy generator must not change a golden spec's output. If it does, it is a
connector version bump.
