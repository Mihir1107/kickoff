"""M1: every compose service is reachable and the evidence bucket is WORM-configured."""

from collections.abc import Mapping

import asyncpg
import boto3
import httpx
import pytest
import redis.asyncio as aioredis
from temporalio.client import Client


async def test_postgres_is_16_with_temporal_databases(env: Mapping[str, str]) -> None:
    conn = await asyncpg.connect(
        host=env["EDISC_PG_HOST"],
        port=int(env["EDISC_PG_PORT"]),
        user=env["EDISC_PG_SUPERUSER"],
        password=env["EDISC_PG_SUPERUSER_PASSWORD"],
        database=env["EDISC_PG_DB"],
    )
    try:
        version = await conn.fetchval("SHOW server_version")
        dbs = {r["datname"] for r in await conn.fetch("SELECT datname FROM pg_database")}
    finally:
        await conn.close()
    assert version.startswith("16.")
    assert {env["EDISC_PG_DB"], "temporal", "temporal_visibility"} <= dbs


async def test_redis_roundtrip(env: Mapping[str, str]) -> None:
    client = aioredis.from_url(env["EDISC_REDIS_URL"])
    try:
        assert await client.ping()
        await client.set("edisc:smoke", "1", ex=10)
        assert await client.get("edisc:smoke") == b"1"
    finally:
        await client.aclose()


def test_evidence_bucket_has_compliance_object_lock(env: Mapping[str, str]) -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=env["EDISC_S3_ENDPOINT"],
        region_name=env["EDISC_S3_REGION"],
        aws_access_key_id=env["EDISC_S3_ACCESS_KEY"],
        aws_secret_access_key=env["EDISC_S3_SECRET_KEY"],
    )
    bucket = env["EDISC_S3_EVIDENCE_BUCKET"]
    lock = s3.get_object_lock_configuration(Bucket=bucket)["ObjectLockConfiguration"]
    assert lock["ObjectLockEnabled"] == "Enabled"
    retention = lock["Rule"]["DefaultRetention"]
    assert retention["Mode"] == "COMPLIANCE"
    assert retention["Days"] == int(env["EDISC_S3_DEFAULT_RETENTION_DAYS"])
    # Object Lock implies versioning; it must never be suspended.
    assert s3.get_bucket_versioning(Bucket=bucket)["Status"] == "Enabled"


async def test_temporal_namespace_reachable(env: Mapping[str, str]) -> None:
    client = await Client.connect(
        env["EDISC_TEMPORAL_ADDRESS"], namespace=env["EDISC_TEMPORAL_NAMESPACE"]
    )
    count = await client.count_workflows()
    assert count.count >= 0


@pytest.mark.elasticsearch
async def test_elasticsearch_healthy(env: Mapping[str, str]) -> None:
    async with httpx.AsyncClient(base_url=env["EDISC_ES_URL"], timeout=10) as http:
        resp = await http.get("/_cluster/health")
    assert resp.status_code == 200
    assert resp.json()["status"] in {"green", "yellow"}
