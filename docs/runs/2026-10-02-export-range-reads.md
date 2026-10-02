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
end records and the central directory, whatever the number of entries. To be re-measured on the
synthetic Slack export (M14.4) and on the real exports when they arrive.
