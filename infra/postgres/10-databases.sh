#!/bin/sh
# Runs once on first container start (empty volume). Creates the Temporal role and databases.
# The application database (EDISC_PG_DB) is created by the image from POSTGRES_DB; its roles,
# grants, triggers and RLS are owned by Alembic migrations, not by this script.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE temporal LOGIN PASSWORD '${TEMPORAL_DB_PASSWORD}';
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;
SQL
