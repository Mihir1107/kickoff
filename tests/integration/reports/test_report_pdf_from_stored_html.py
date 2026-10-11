"""`report.pdf` is rendered from the STORED `report.html` (ADR 0018 §10, §22.8): read back by its
pinned VersionId and checked against its record. Here the PDF child is replaced by a recorder (the
host has no report image; the image tests render for real): the intact path hands it exactly the
stored bytes, and when storage returns ALTERED bytes for that version the render refuses: nothing
rendered, no `report.pdf` object or row, no `report_generated`, the report failed as an integrity
error."""

from __future__ import annotations

import unicodedata
from typing import Any

import pytest
from sqlalchemy import text
from types_aiobotocore_s3 import S3Client

import edisc_worker.reports as reports_module
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_renderers.report.html import glyph_set_id
from edisc_renderers.report.version import REPORT_RENDERER_VERSION
from edisc_worker.pipeline import CrashHooks
from edisc_worker.reports import ReportRun

from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..report.synthetic import sealed_job, unit_keys
from .conftest import drive, new_report, report_state, stored_bytes

IDENTITY = {"renderer_version": REPORT_RENDERER_VERSION, "toolchain_id": "f" * 64,
            "unicode_version": unicodedata.unidata_version, "glyph_set": glyph_set_id()}  # fmt: skip
FAKE_PDF = b"%PDF-1.7\nfake\n%%EOF\n"


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        data, self._data = (self._data, b"") if n < 0 else (self._data[:n], self._data[n:])
        return data

    async def __aenter__(self) -> _Body:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> _Body:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self.read(1 << 16)
        if not chunk:
            raise StopAsyncIteration
        return chunk


class AlteringS3:
    """Storage that returns different bytes for the pinned `report.html` version (one byte
    flipped), as a corrupted or substituted object would; everything else passes through."""

    def __init__(self, inner: S3Client) -> None:
        self._inner = inner
        self.altered = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get_object(self, **kw: Any) -> Any:
        resp = await self._inner.get_object(**kw)
        if not str(kw.get("Key", "")).endswith("/report.html"):
            return resp
        async with resp["Body"] as body:
            data = bytearray(await body.read())
        data[len(data) // 2] ^= 0x01
        self.altered += 1
        return {**resp, "Body": _Body(bytes(data))}


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """The HTML each PDF render was given (the child is replaced: no report image on the host)."""
    seen: list[bytes] = []

    async def fake_render_pdf(html: bytes, **_: Any) -> bytes:
        seen.append(html)
        return FAKE_PDF

    monkeypatch.setattr(reports_module, "render_pdf", fake_render_pdf)
    return seen


async def _job_and_report(
    sessions: Sessions, s3: S3Client, settings: Settings
) -> tuple[Tenant, Any]:
    t = await new_tenant(sessions)
    job = await sealed_job(sessions, s3, settings, t, unit_keys(3))
    return t, await new_report(sessions, t, job, identity=IDENTITY)


def _run(sessions: Sessions, s3: Any, settings: Settings) -> ReportRun:
    return ReportRun(sessions, s3, settings, CrashHooks(), IDENTITY)


async def test_the_pdf_is_rendered_from_the_stored_html(
    app_sessions: Sessions, s3: S3Client, settings: Settings, rendered: list[bytes]
) -> None:
    t, report_id = await _job_and_report(app_sessions, s3, settings)
    out = await drive(_run(app_sessions, s3, settings), t.tenant_id, report_id)
    assert out["status"] == "completed", out
    st = await report_state(app_sessions, t.tenant_id, report_id)
    files = await stored_bytes(s3, settings, t, report_id, st["files"])
    assert rendered == [files["report.html"]]  # exactly the stored bytes, rendered once
    assert files["report.pdf"] == FAKE_PDF


async def test_altered_stored_html_is_refused_and_nothing_is_written(
    app_sessions: Sessions, s3: S3Client, settings: Settings, rendered: list[bytes]
) -> None:
    t, report_id = await _job_and_report(app_sessions, s3, settings)
    altering = AlteringS3(s3)
    out = await drive(_run(app_sessions, altering, settings), t.tenant_id, report_id)  # type: ignore[arg-type]
    assert altering.altered == 1
    assert rendered == []  # the child never saw the altered bytes
    assert out["status"] == "failed", out
    st = await report_state(app_sessions, t.tenant_id, report_id)
    assert st["row"].reason == "ReportIntegrityError"
    assert "stored report.html is not its record" in st["row"].detail
    assert "report_generated" not in [e.event_type for e in st["events"]]
    assert "report.pdf" not in {f.name for f in st["files"]}
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        pdf_rows = (await s.execute(text(
            "SELECT count(*) FROM evidence_objects WHERE report_id = :r AND storage_key LIKE '%/report.pdf'"
        ), {"r": report_id})).scalar_one()  # fmt: skip
    assert pdf_rows == 0
    listed = await s3.list_object_versions(
        Bucket=settings.s3_evidence_bucket, Prefix=f"t/{t.tenant_id}/reports/{report_id}/report.pdf"
    )
    assert not listed.get("Versions")
