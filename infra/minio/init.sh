#!/bin/sh
# One-shot: create the evidence bucket with Object Lock and a default COMPLIANCE retention.
# Object Lock can only be enabled at bucket creation. The default retention is a safety net:
# the evidence writer always sets an explicit per-object retain-until from the matter.
set -eu
mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing --with-lock "local/$BUCKET"
mc retention set --default COMPLIANCE "${DEFAULT_RETENTION_DAYS}d" "local/$BUCKET"
mc retention info --default "local/$BUCKET"
