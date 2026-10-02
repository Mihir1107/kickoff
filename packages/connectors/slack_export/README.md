# edisc-connector-slack-export

Slack export (zip) ingestion, ADR 0014.

- `layout`: classifies archive entries (metadata, day files, directories, unknown), parses the
  conversation metadata files and detects the export tier. Pure: no S3 or DB.
- The connector itself (enumerate day files, yield entry bytes in local-header order) arrives in M14.5.

Format assumptions marked *(confirm on real export)* in ADR 0014 are checked against the real exports
once they are fixtures.
