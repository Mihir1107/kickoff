"""Every process runs as a least-privilege storage identity, never root (M18 §3, threat T12).

Root ignores bucket policies (M18 §0 spike), so a process holding it could clear legal holds and
delete versions whatever the policies say. No service identity is granted ``s3:GetBucketPolicy``,
and root always may read it: so at startup each process asks for the evidence bucket's policy and
refuses to run unless the store answers AccessDenied. A "no such policy" answer means the request
was AUTHORIZED, so it is refused too. This is a harmless read, and it holds on AWS as on MinIO.
"""

from __future__ import annotations

from botocore.exceptions import ClientError
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings


class OverPrivilegedIdentityError(RuntimeError):
    """The configured storage identity can read bucket configuration: root or close to it."""


async def assert_least_privilege(s3: S3Client, settings: Settings) -> None:
    try:
        await s3.get_bucket_policy(Bucket=settings.s3_evidence_bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "AccessDenied":
            return
        raise OverPrivilegedIdentityError(
            f"the storage identity may read the evidence bucket's policy (the store answered {code}):"
            " a service must never run as root or with bucket-configuration rights"
        ) from None
    raise OverPrivilegedIdentityError(
        "the storage identity read the evidence bucket's policy: a service must never run as root"
        " or with bucket-configuration rights"
    )


_PAIRS = {
    "hold": ("s3_hold_access_key", "s3_hold_secret_key"),
    "destroy": ("s3_destroy_access_key", "s3_destroy_secret_key"),
}


def refuse_foreign_identities(settings: Settings, own: str) -> None:
    """Outside local/test/ci a process loads ONLY its own storage identity: the API and the general
    workers (``app``) refuse to start with the hold or destroy credentials in their settings, the
    holds worker with the destroy ones, the destruction worker with the hold ones (M18 §3). Local,
    test and ci share one env file on purpose; each process still USES only its own pair."""
    if settings.env.is_disposable:
        return
    present = sorted(
        kind
        for kind, fields in _PAIRS.items()
        if kind != own and any(getattr(settings, f) is not None for f in fields)
    )
    if present:
        raise OverPrivilegedIdentityError(
            f"this {own} process has the {', '.join(present)} storage credentials in its"
            " environment: each process gets only its own identity"
        )
