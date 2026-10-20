"""Storage identities and bucket policies on the test stack's MinIO (M18 §3.3, threats T1, T2, T12).

Every identity x every forbidden action must be refused; each allowed action succeeds once (the
positive controls: a matrix of refusals proves nothing if everything is refused). The identities
and policies are the ones `infra/minio/init.sh` provisions on every stack.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Environment, Settings
from edisc_core.time import utc_now
from edisc_evidence.identity import (
    OverPrivilegedIdentityError,
    assert_least_privilege,
    refuse_foreign_identities,
)
from edisc_evidence.s3 import s3_client

IDS = ("app", "hold", "destroy")


@pytest.fixture(scope="module")
async def clients(settings: Settings, s3_root: S3Client) -> AsyncIterator[dict[str, S3Client]]:
    async with AsyncExitStack() as stack:
        out = {i: await stack.enter_async_context(s3_client(settings, i)) for i in IDS}  # type: ignore[arg-type]
        out["root"] = s3_root
        yield out


def _key(prefix: str = "t") -> str:
    return f"{prefix}/identity-test/{uuid.uuid4()}"


async def _refused(call: Awaitable[Any]) -> str:
    try:
        await call
    except ClientError as exc:
        return str(exc.response["Error"]["Code"])
    raise AssertionError("allowed")


async def _put(c: S3Client, bucket: str, key: str, *, locked: bool = False) -> str:
    extra: dict[str, Any] = {}
    if locked:
        extra = {
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": utc_now() + timedelta(seconds=120),
        }
    return (await c.put_object(Bucket=bucket, Key=key, Body=b"x", **extra))["VersionId"]  # fmt: skip


async def test_forbidden_actions_are_refused_for_every_identity(
    clients: dict[str, S3Client], settings: Settings
) -> None:
    ev, st = settings.s3_evidence_bucket, settings.s3_staging_bucket
    app = clients["app"]
    key = _key()
    vid = await _put(app, ev, key)  # no retention: only the policies stand in the way
    locked = _key()
    locked_vid = await _put(app, ev, locked, locked=True)
    staged = _key()
    await app.put_object(Bucket=st, Key=staged, Body=b"s")
    soon = utc_now() + timedelta(seconds=1)
    refused: dict[str, dict[str, str]] = {}

    def cases(c: S3Client) -> dict[str, Awaitable[Any]]:
        return {
            "delete marker": c.delete_object(Bucket=ev, Key=key),
            "retention shortened": c.put_object_retention(
                Bucket=ev, Key=locked, VersionId=locked_vid,
                Retention={"Mode": "COMPLIANCE", "RetainUntilDate": soon},
            ),
            "governance bypass delete": c.delete_object(
                Bucket=ev, Key=locked, VersionId=locked_vid, BypassGovernanceRetention=True
            ),
            "versioning suspended": c.put_bucket_versioning(
                Bucket=ev, VersioningConfiguration={"Status": "Suspended"}
            ),
            "bucket policy replaced": c.put_bucket_policy(Bucket=ev, Policy='{"Version":"2012-10-17","Statement":[]}'),
            "bucket policy deleted": c.delete_bucket_policy(Bucket=ev),
            "bucket policy read": c.get_bucket_policy(Bucket=ev),
            "lifecycle replaced": c.put_bucket_lifecycle_configuration(
                Bucket=ev, LifecycleConfiguration={"Rules": []}
            ),
            "write outside the evidence prefixes": c.put_object(Bucket=ev, Key="elsewhere/x", Body=b"x"),
            "staging written outside its prefixes": c.put_object(Bucket=st, Key="elsewhere/x", Body=b"x"),
        }  # fmt: skip

    for ident in IDS:
        refused[ident] = {
            name: await _refused(call) for name, call in cases(clients[ident]).items()
        }
    # one identity each may do these; the others never
    only = {
        "version deleted": ("destroy", lambda c: c.delete_object(Bucket=ev, Key=key, VersionId=vid)),
        "hold flag set": ("hold", lambda c: c.put_object_legal_hold(
            Bucket=ev, Key=locked, VersionId=locked_vid, LegalHold={"Status": "ON"})),
        "staging read": ("app", lambda c: c.get_object(Bucket=st, Key=staged)),
        "staging deleted": ("app", lambda c: c.delete_object(Bucket=st, Key=staged)),
        "evidence written": ("app", lambda c: c.put_object(Bucket=ev, Key=_key(), Body=b"x")),
    }  # fmt: skip
    for name, (allowed, call) in only.items():
        for ident in IDS:
            if ident != allowed:
                refused[ident][name] = await _refused(call(clients[ident]))
    for ident, by_action in refused.items():
        for name, code in by_action.items():
            # COMPLIANCE refuses a shortening even where the policies would allow it (the app may
            # extend): either way the version keeps its date
            assert code in ("AccessDenied", "InvalidRequest"), (ident, name, code)
            # the destruction identity may delete versions: on a COMPLIANCE version the lock refuses
            locked_out = name == "governance bypass delete" and ident == "destroy"
            if name != "retention shortened" and not locked_out:
                assert code == "AccessDenied", (ident, name, code)
    # the positive controls of the one-identity actions, after the refusals above
    for allowed, call in only.values():
        await call(clients[allowed])
    head = await clients["root"].head_object(Bucket=ev, Key=locked, VersionId=locked_vid)
    assert head["ObjectLockLegalHoldStatus"] == "ON"
    versions = await clients["root"].list_object_versions(Bucket=ev, Prefix=key)
    assert [v for v in versions.get("Versions", []) if v["Key"] == key] == []
    assert versions.get("DeleteMarkers", []) == []  # never a marker
    await clients["hold"].put_object_legal_hold(
        Bucket=ev, Key=locked, VersionId=locked_vid, LegalHold={"Status": "OFF"}
    )


async def test_each_identity_can_do_its_own_work(
    clients: dict[str, S3Client], settings: Settings
) -> None:
    """The product's own operations as the app (the whole suite runs as the app too); the holds and
    destruction identities' reads."""
    ev = settings.s3_evidence_bucket
    app, hold, destroy = clients["app"], clients["hold"], clients["destroy"]
    key = _key()
    vid = await _put(app, ev, key, locked=True)
    await app.head_object(Bucket=ev, Key=key, VersionId=vid)
    await app.get_object(Bucket=ev, Key=key, VersionId=vid)
    await app.list_object_versions(Bucket=ev, Prefix=key)
    # a version that never had a hold: MinIO answers NoSuchObjectLockConfiguration, not "OFF"
    assert await _refused(app.get_object_legal_hold(Bucket=ev, Key=key, VersionId=vid)) == (
        "NoSuchObjectLockConfiguration"
    )
    await app.get_object_lock_configuration(Bucket=ev)  # the report states lock settings
    await app.get_bucket_versioning(Bucket=ev)
    await app.put_object_retention(
        Bucket=ev, Key=key, VersionId=vid,
        Retention={"Mode": "COMPLIANCE", "RetainUntilDate": utc_now() + timedelta(seconds=600)},
    )  # fmt: skip
    anchor = _key("custody-anchors")
    await _put(app, ev, anchor, locked=True)
    await hold.put_object_legal_hold(Bucket=ev, Key=key, VersionId=vid, LegalHold={"Status": "ON"})
    got = await app.get_object_legal_hold(Bucket=ev, Key=key, VersionId=vid)
    assert got["LegalHold"]["Status"] == "ON"
    await hold.get_object_legal_hold(Bucket=ev, Key=key, VersionId=vid)
    await hold.put_object_legal_hold(Bucket=ev, Key=key, VersionId=vid, LegalHold={"Status": "OFF"})
    await hold.list_object_versions(Bucket=ev, Prefix=key)
    await destroy.get_object_retention(Bucket=ev, Key=key, VersionId=vid)
    await destroy.head_object(Bucket=ev, Key=key, VersionId=vid)
    # no identity but the app reads content
    assert await _refused(hold.get_object(Bucket=ev, Key=key, VersionId=vid)) == "AccessDenied"
    # still locked: the store refuses, whoever asks (the destruction step reads this as "not yet")
    assert (
        await _refused(destroy.delete_object(Bucket=ev, Key=key, VersionId=vid)) != "AccessDenied"
    )


async def test_only_root_passes_the_bucket_policy_probe(
    clients: dict[str, S3Client], settings: Settings
) -> None:
    """Every process runs ``assert_least_privilege`` at startup: root (and anything that may read
    bucket configuration) is refused, every service identity passes."""
    for ident in IDS:
        await assert_least_privilege(clients[ident], settings)
    with pytest.raises(OverPrivilegedIdentityError):
        await assert_least_privilege(clients["root"], settings)


async def test_a_missing_identity_is_refused_never_replaced(settings: Settings) -> None:
    bare = settings.model_copy(update={"s3_hold_access_key": None})
    with pytest.raises(RuntimeError, match="hold storage identity is not configured"):
        async with s3_client(bare, "hold"):
            pass


def test_production_processes_refuse_another_identitys_credentials(settings: Settings) -> None:
    prod = settings.model_copy(update={"env": Environment.PRODUCTION})
    with pytest.raises(OverPrivilegedIdentityError, match="destroy, hold"):
        refuse_foreign_identities(prod, "app")
    with pytest.raises(OverPrivilegedIdentityError, match="destroy"):
        refuse_foreign_identities(prod, "hold")
    app_only = prod.model_copy(
        update=dict.fromkeys(("s3_hold_access_key", "s3_hold_secret_key", "s3_destroy_access_key", "s3_destroy_secret_key"))
    )  # fmt: skip
    refuse_foreign_identities(app_only, "app")
    refuse_foreign_identities(settings, "app")  # local/test/ci share one env file


async def test_root_is_not_in_the_settings(env: Mapping[str, str], settings: Settings) -> None:
    """Root is a provisioning credential: no service setting carries it."""
    assert env["EDISC_S3_ACCESS_KEY"] != env["EDISC_S3_ROOT_USER"]
    secrets = {
        getattr(settings, f).get_secret_value()
        for f in type(settings).model_fields
        if f.startswith("s3_") and getattr(settings, f) is not None and hasattr(getattr(settings, f), "get_secret_value")
    }  # fmt: skip
    assert env["EDISC_S3_ROOT_PASSWORD"] not in secrets
    async with get_session().create_client(
        "s3", endpoint_url=env["EDISC_S3_ENDPOINT"], region_name=env["EDISC_S3_REGION"],
        aws_access_key_id=env["EDISC_S3_ACCESS_KEY"], aws_secret_access_key=env["EDISC_S3_SECRET_KEY"],
    ) as c:  # fmt: skip
        assert (
            await _refused(c.get_bucket_policy(Bucket=settings.s3_evidence_bucket))
            == "AccessDenied"
        )


MC_IMAGE = "pgsty/mc:RELEASE.2026-09-16T00-00-00Z@sha256:cfc83108c3abb371f8fb84d99c1fdc88f8c237e022409b0081fb7c0a3be634dd"
POLICIES = Path(__file__).resolve().parents[3] / "infra" / "minio" / "policies"


def _mc(env: Mapping[str, str], script: str) -> None:
    """Admin calls as root through the stack's own mc image (provisioning, like init.sh)."""
    network = f"{env.get('EDISC_COMPOSE_PROJECT', 'edisc-test')}_default"
    full = (
        f"mc alias set l http://minio:9000 {env['EDISC_S3_ROOT_USER']}"
        f" {env['EDISC_S3_ROOT_PASSWORD']} >/dev/null && {script}"
    )
    subprocess.run(
        ["docker", "run", "--rm", "--network", network, "--entrypoint", "bash", MC_IMAGE, "-c", full],
        check=True, capture_output=True,
    )  # fmt: skip


def _rendered(name: str, settings: Settings, env: Mapping[str, str]) -> str:
    text_ = (POLICIES / f"{name}.json").read_text()
    for k, v in {
        "@EVIDENCE@": settings.s3_evidence_bucket, "@STAGING@": settings.s3_staging_bucket,
    }.items():  # fmt: skip
        text_ = text_.replace(k, v)
    return json.dumps(json.loads(text_))


@pytest.mark.parametrize("identity", ["edisc-app", "edisc-hold", "edisc-destroy"])
async def test_explicit_denies_survive_a_later_wide_grant(
    identity: str, settings: Settings, env: Mapping[str, str], clients: dict[str, S3Client]
) -> None:
    """The second layer on MinIO (which does not evaluate bucket policies for IAM users): each
    identity's policy denies what it must never do, and an explicit Deny wins over any Allow, so
    a later over-broad grant (an operator's mistake) still cannot delete markers, bypass
    governance, set holds it may not set, or change bucket configuration."""
    ev = settings.s3_evidence_bucket
    user, secret = f"wide-{uuid.uuid4().hex[:8]}", "w" + uuid.uuid4().hex
    own, wide = _rendered(identity, settings, env), json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": ["arn:aws:s3:::*"]}],
    })  # fmt: skip
    await asyncio.to_thread(
        _mc, env,
        f"mc admin user add l {user} {secret} >/dev/null"
        f" && echo '{own}' > /tmp/o.json && mc admin policy create l {user}-own /tmp/o.json >/dev/null"
        f" && echo '{wide}' > /tmp/w.json && mc admin policy create l {user}-wide /tmp/w.json >/dev/null"
        f" && mc admin policy attach l {user}-own {user}-wide --user {user} >/dev/null",
    )  # fmt: skip
    key = _key()
    vid = await _put(clients["app"], ev, key)
    async with get_session().create_client(
        "s3", endpoint_url=env["EDISC_S3_ENDPOINT"], region_name=env["EDISC_S3_REGION"],
        aws_access_key_id=user, aws_secret_access_key=secret,
    ) as c:  # fmt: skip
        assert await _refused(c.delete_object(Bucket=ev, Key=key)) == "AccessDenied"  # a marker
        assert (
            await _refused(
                c.put_bucket_tagging(Bucket=ev, Tagging={"TagSet": [{"Key": "a", "Value": "b"}]})
            )
            == "AccessDenied"
        )
        if identity != "edisc-hold":
            assert await _refused(c.put_object_legal_hold(
                Bucket=ev, Key=key, VersionId=vid, LegalHold={"Status": "ON"})) == "AccessDenied"  # fmt: skip
        if identity != "edisc-destroy":
            assert (
                await _refused(c.delete_object(Bucket=ev, Key=key, VersionId=vid)) == "AccessDenied"
            )
        if identity != "edisc-app":
            assert await _refused(c.put_object(Bucket=ev, Key=_key(), Body=b"x")) == "AccessDenied"
        if identity == "edisc-hold":  # flags only: never the content
            assert await _refused(c.get_object(Bucket=ev, Key=key, VersionId=vid)) == "AccessDenied"
        # the wide grant itself is live (the denies, not a missing allow, refused the above), and
        # the startup probe catches it: such an identity never runs
        with pytest.raises(OverPrivilegedIdentityError):
            await assert_least_privilege(c, settings)


async def test_the_probe_refuses_an_identity_that_reads_an_absent_policy(
    settings: Settings, clients: dict[str, S3Client]
) -> None:
    """On a bucket with no policy (every fresh stack) root's GetBucketPolicy answers "no such
    policy": that request was authorized, so the probe refuses it like a successful read."""
    bucket = f"probe-{uuid.uuid4().hex[:12]}"
    await clients["root"].create_bucket(Bucket=bucket)
    bare = settings.model_copy(update={"s3_evidence_bucket": bucket})
    with pytest.raises(OverPrivilegedIdentityError, match="NoSuchBucketPolicy"):
        await assert_least_privilege(clients["root"], bare)
    await clients["root"].delete_bucket(Bucket=bucket)
