# 2026-10-01: resume soak (M12 acceptance)

Driver: `scripts/resume_soak.py` (the 50k CI test calls the same code). Worker processes are
`python -m edisc_worker --queue <soak queue>`, SIGKILLed at random points, plus one kill-all/restart.

Environment: MacBook Air M3, 8 cores, 16 GB RAM; Docker Desktop VM with 8 CPUs / 8 GB; ephemeral test
stack (`make test-env-up`: Postgres 16, MinIO, Redis, Temporal); rate limits raised to 2,000 req/s for the
soak (`FAST_LIMITS`), `RunConfig(max_units_in_flight=8, pages_per_activity=5, heartbeat 10 s)`.

## 50,000 messages: passed

```
make test-integration-only TESTS=tests/integration/acceptance PYTEST_ARGS="-s"
50k soak: 167s, kills: ['pid 36010 at 5207 links', 'pid 36012 at 18086 links',
  'ALL at 20918 links', 'pid 36101 at 31324 links', 'pid 36099 at 32878 links']
1 passed in 192.22s
```

10 conversations x 10 days x 500 messages, page size 200, 3 worker processes, 4 single kills and one
kill-all, spread over the run. Result: status `completed`; every derived record equals the dataset oracle;
zero duplicate items; every job link backed by a custody event; no pending evidence; all 100 units
`matched`; the custody chain, its anchors and the seal verify. About 300 msg/s end to end including
the kill/recovery pauses.

The first attempt found a real bug: a worker killed inside `finalize_unit` (no heartbeat timeout) left the
unit waiting for the 30-minute start-to-close. Fixed by background heartbeats for every activity except
`collect_pages` and a heartbeat timeout on every activity (ADR 0012 implementation notes, commit 3).

## 1,000,000 messages: NOT completed on this machine (aborted to protect the disk)

```
EDISC_ENV_FILE=.env.test uv run python scripts/resume_soak.py --messages 1000000 --workers 3 --kills 10
```

Aborted by hand after about 10 minutes (8 of 101 units done, 136,103 job links, roughly 110,000 messages):

- the compose volumes grew about 2.8 GB for those ~110k messages (**~26 KB per message**: items 331 MB,
  derivations 161 MB, job links 62 MB at that point, plus MinIO page and file evidence), so 1M messages
  needs about 26 GB for the volumes alone;
- the host had 17 GB free and was swapping heavily (7 GB of swap in use: 3 workers + the Docker VM on 16
  GB RAM), and free space was falling toward the 15 GB guard.

Throughput before the abort was ~220 job links/s (~180 msg/s), lower than the 50k run because of memory
pressure. The run is not evidence of correctness at 1M; it is evidence that the soak needs a bigger host.

`resume_soak.py` now refuses to start unless free disk >= 15 GB + 2 x 26 KB x messages (63 GB for 1M).

**To do (owner: whoever has a host with >= 64 GB free disk and >= 32 GB RAM, or a cloud VM):** run the
command above with `--kills 10` and append the JSON result here. Expected runtime at the 50k rate: about
an hour.
