#!/bin/sh
# One-shot: apply Temporal persistence schemas (idempotent), matching temporalio/auto-setup behaviour.
set -eu
SCHEMA=/etc/temporal/schema/postgresql/v12
tool() { temporal-sql-tool --plugin postgres12 --ep postgres -p 5432 -u temporal --pw "$TEMPORAL_DB_PASSWORD" "$@"; }
tool --db temporal setup-schema -v 0.0
tool --db temporal update-schema -d "$SCHEMA/temporal/versioned"
tool --db temporal_visibility setup-schema -v 0.0
tool --db temporal_visibility update-schema -d "$SCHEMA/visibility/versioned"
echo "temporal schema ready"
