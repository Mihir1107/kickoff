#!/bin/sh
# One-shot, idempotent bucket setup (ADR 0002).
# - Evidence bucket: Object Lock (only possible at creation). A bucket default COMPLIANCE retention is a
#   safety net (DEFAULT_RETENTION_DAYS; 0 = none, used by the ephemeral test stack whose objects carry
#   seconds-level per-object retention). The evidence writer always sets an explicit retain-until.
# - Staging bucket: NO lock, objects expire after 1 day.
# - Local / test / ci only: lifecycle expiry on the evidence bucket so objects are actually REMOVED once
#   their retention lapses (Object Lock only blocks deletion; it never deletes). Never in production:
#   infra/aws/*.json has no expiration rule for evidence.
# Incomplete multipart uploads: MinIO expires stale uploads itself (api stale_uploads_expiry, default
# 24h); it rejects the S3 AbortIncompleteMultipartUpload lifecycle rule.
set -eu
mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing --with-lock "local/$BUCKET"
if [ "${DEFAULT_RETENTION_DAYS}" != "0" ]; then
  mc retention set --default COMPLIANCE "${DEFAULT_RETENTION_DAYS}d" "local/$BUCKET"
fi
mc retention info --default "local/$BUCKET" || true
mc mb --ignore-existing "local/$STAGING_BUCKET"
# import replaces the whole lifecycle configuration, so re-running never duplicates rules
echo '{"Rules":[{"ID":"expire-staging-objects","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":1}}]}' \
  | mc ilm import "local/$STAGING_BUCKET"
case "${EDISC_ENV:-production}" in
  local|test|ci)
    echo '{"Rules":[{"ID":"expire-disposable-evidence","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":2},"NoncurrentVersionExpiration":{"NoncurrentDays":1}},{"ID":"remove-expired-delete-markers","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"ExpiredObjectDeleteMarker":true}}]}' \
      | mc ilm import "local/$BUCKET"
    mc ilm rule ls "local/$BUCKET"
    ;;
  *) echo "no expiry rules on the evidence bucket in ${EDISC_ENV:-production}" ;;
esac
mc ilm rule ls "local/$STAGING_BUCKET"
