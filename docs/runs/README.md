# Recorded runs

Measurements and manual acceptance runs, one file per run, named `YYYY-MM-DD-<topic>.md`. Each records the
command, the environment (machine, stack versions, settings that matter), the raw result and what it means.

## Fuzzing the ZIP reader (ADR 0014 R5)
- CI runs 300 examples (`tests/unit/custody/test_archive_fuzz.py`). Long manual run:
  `EDISC_FUZZ_EXAMPLES=20000 uv run pytest -o timeout=1800 tests/unit/custody/test_archive_fuzz.py`.
- 2026-10-02: 20,000 examples, no failure, 66 s.
- **Teeth (mutation):** with the central directory's extra-field bounds check removed, the fuzzer finds the
  resulting `struct.error` within 3,000 examples. This needed the structure-aware mutations and the ZIP64
  central-directory seeds; plain byte flips did not find it.
