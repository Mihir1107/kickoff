# 2026-10-02: range requests per 1,000 archive entries (ADR 0014 R6)

`tests/integration/evidence/test_archive_source.py` (MinIO, test stack):
- export-like zip: 5,000 day files, deflated, 17.0 MB;
- full validation (`scan`: end records, central directory, every entry in local-header order, CRC and size
  checks).

| Reader | Requests | Per 1,000 entries |
|---|---:|---:|
| Coalesced (`CoalescingSource`, 8 MiB window) | 4 | **0.8** |
| Naive (one range per header, name and data read), measured on 200 entries | 600 | 3,000 |

The coalesced reader costs about one request per 8 MiB window of archive (`ceil(size / window)`), plus the
end records and the central directory, whatever the number of entries.

## Re-measured on the synthetic Slack export (M14.4)
`scripts/measure_export_reads.py --conversations 200 --days 100` (MinIO, test stack, pinned version of
the locked export, 8 MiB window). Export from the dummy oracle: 200 conversations (full tier) x 100 days,
240,000 messages, 20,000 day files.

- **Validation:** end records, two central-directory passes (wrapper detection, then classification) and
  streaming the conversation metadata files.
- **Full read:** every entry in local-header order with overlap limits, CRC, size and SHA-256 (what a job
  reads).

| Variant | Entries | Zip | Validation requests (per 1,000) | Full read requests (per 1,000) | Full read time | Naive read (per 1,000), time |
|---|---:|---:|---:|---:|---:|---:|
| plain | 20,205 | 43.9 MB | 3 (0.15) | 8 (**0.40**) | 0.97 s | 60,419 (2,990), 209 s |
| macOS + wrapper + data descriptors + forced ZIP64 | 40,213 | 51.6 MB | 3 (0.07) | 8 (**0.20**) | 1.71 s | 120,445 (2,995), 389 s |

Decompressed: 1.05 GB per variant. The coalesced reader stays at about one request per 8 MiB window plus
the tail. The worst real-world shape (twice the entries, ZIP64 extras, descriptors) costs no extra
requests. Naive reads are ~3 requests per entry and ~200x slower. Smaller runs (40 x 50, 4.4 MB) took 3
requests in all. To be re-measured on the real exports.
