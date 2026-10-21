#!/bin/sh
# One-shot: register the application namespace once the frontend is healthy (idempotent).
# A healthy frontend can still refuse the next call for a moment (CI run 39530887546: health
# passed, the create then failed "context deadline exceeded" and set -e ended the stack's start;
# ADR 0015 §24.26). So: retry until the namespace DESCRIBES, at most MAX attempts, each failure
# printed; never "ready" before describe succeeds.
set -eu
MAX=120
until temporal operator cluster health --address temporal:7233 >/dev/null 2>&1; do sleep 1; done
i=0
until temporal operator namespace describe --address temporal:7233 --namespace "$NAMESPACE" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt "$MAX" ]; then
    echo "namespace $NAMESPACE not ready after $MAX attempts" >&2
    exit 1
  fi
  temporal operator namespace create --address temporal:7233 --namespace "$NAMESPACE" --retention 72h \
    || echo "namespace create attempt $i failed (retrying)" >&2
  sleep 1
done
echo "namespace $NAMESPACE ready"
