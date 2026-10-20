"""The storage identities' policies, as files (M18 §3; infra/minio/policies): what each identity is
ALLOWED is exactly its job. The explicit denies (the second layer) are tested against MinIO in
``tests/integration/evidence/test_identities.py``; this pins the first layer, which a denial
would otherwise mask."""

from __future__ import annotations

import json
from pathlib import Path

POLICIES = Path(__file__).resolve().parents[2] / "infra" / "minio" / "policies"
ALLOWED = {
    "edisc-app": {
        "s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectRetention",
        "s3:PutObjectRetention", "s3:GetObjectLegalHold", "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts", "s3:ListBucket", "s3:ListBucketVersions",
        "s3:ListBucketMultipartUploads", "s3:GetBucketObjectLockConfiguration",
        "s3:GetBucketVersioning", "s3:DeleteObject",  # DeleteObject: staging only (below)
    },
    "edisc-hold": {"s3:PutObjectLegalHold", "s3:GetObjectLegalHold", "s3:ListBucketVersions"},
    "edisc-destroy": {
        "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:GetObjectVersion", "s3:GetObjectRetention",
        "s3:GetObjectLegalHold", "s3:ListBucketVersions", "s3:PutObject", "s3:PutObjectRetention",
    },
}  # fmt: skip


def _statements(name: str) -> list[dict[str, object]]:
    return list(json.loads((POLICIES / f"{name}.json").read_text())["Statement"])


def test_each_identity_is_allowed_exactly_its_job() -> None:
    for name, expected in ALLOWED.items():
        allowed = {
            a
            for s in _statements(name)
            if s["Effect"] == "Allow"
            for a in s["Action"]  # type: ignore[union-attr]
        }
        assert allowed == expected, (name, allowed ^ expected)
        assert not any("*" in a for a in allowed), name  # no wildcard grant


def test_the_app_deletes_only_staging_objects() -> None:
    for s in _statements("edisc-app"):
        if s["Effect"] == "Allow" and "s3:DeleteObject" in s["Action"]:  # type: ignore[operator]
            assert all("@STAGING@" in r for r in s["Resource"]), s  # type: ignore[union-attr]


def test_the_destroy_identity_writes_only_certificates() -> None:
    for s in _statements("edisc-destroy"):
        if s["Effect"] == "Allow" and "s3:PutObject" in s["Action"]:  # type: ignore[operator]
            assert s["Resource"] == ["arn:aws:s3:::@EVIDENCE@/t/*/destruction/*"], s
