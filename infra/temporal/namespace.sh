#!/bin/sh
# One-shot: register the application namespace once the frontend is healthy (idempotent).
set -eu
until temporal operator cluster health --address temporal:7233 >/dev/null 2>&1; do sleep 1; done
temporal operator namespace describe --address temporal:7233 --namespace "$NAMESPACE" >/dev/null 2>&1 \
  || temporal operator namespace create --address temporal:7233 --namespace "$NAMESPACE" --retention 72h
echo "namespace $NAMESPACE ready"
