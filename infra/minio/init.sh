#!/bin/sh
# One-shot, idempotent bucket setup (ADR 0002).
# - Evidence bucket: Object Lock (only possible at creation) with a small default COMPLIANCE retention
#   as a safety net; the evidence writer always sets an explicit per-object retain-until.
# - Staging bucket: NO lock, objects expire after 1 day. Files are streamed here (hashed on the way in)
#   and then server-side copied into the content-addressed WORM key. Holds client data transiently.
# Incomplete multipart uploads: MinIO expires stale uploads itself (api stale_uploads_expiry, default
# 24h); it rejects the S3 AbortIncompleteMultipartUpload lifecycle rule. On AWS apply infra/aws/*.json.
set -eu
mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing --with-lock "local/$BUCKET"
mc retention set --default COMPLIANCE "${DEFAULT_RETENTION_DAYS}d" "local/$BUCKET"
mc retention info --default "local/$BUCKET"
mc mb --ignore-existing "local/$STAGING_BUCKET"
# import replaces the whole lifecycle configuration, so re-running never duplicates rules
echo '{"Rules":[{"ID":"expire-staging-objects","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":1}}]}' \
  | mc ilm import "local/$STAGING_BUCKET"
mc ilm rule ls "local/$STAGING_BUCKET"
