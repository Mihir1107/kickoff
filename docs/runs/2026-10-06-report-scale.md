# Collection report at scale (ADR 0018 §12), 2026-10-06

`scripts/measure_report.py --units 10000 100000` on the ephemeral test stack (laptop, Docker
Desktop). Synthetic sealed jobs: work-unit rows and a real hash chain (`job_started`, one
`unit_reconciled` per unit, `job_finished`, the WORM seal), then `ReportLoader.build` with every
file streamed to a counting sink. Peak memory is tracemalloc's peak around the build.

| units | report time | peak memory | units.jsonl | note |
|---:|---:|---:|---:|---|
| 10,000 | 6.7 s | 6.1 MiB | 3.5 MiB | without the `(job_id, conversation_id, day, unit_key)` index |
| 100,000 | 102.9 s | 5.6 MiB | 35.0 MiB | without the index: superlinear (each keyset page sorted every unit) |
| 10,000 | 7.5 s | 6.1 MiB | 3.5 MiB | with the index (migration 0030) |
| 100,000 | 53.5 s | 5.6 MiB | 35.0 MiB | with the index: linear |

- **Memory is flat** (x0.92 for x10 units): pages of 2,000 rows, the 4,096-bucket digests, the capped
  lists; the full lists only ever stream.
- Time is dominated by the chain verification and the two passes over the chain (about 0.5 ms per
  unit here). 1M units (BACKLOG, cloud VM) would take about 9 minutes on this laptop; the report
  activity heartbeats throughout (all folding runs in threads).
