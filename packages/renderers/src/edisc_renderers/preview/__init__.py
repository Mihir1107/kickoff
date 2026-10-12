"""The HTML preview of one conversation-day (ADR 0018 §1.2, §3.2, §4.7, §10). Pure: no DB, S3 or
clock (the purity test covers it)."""

from edisc_renderers.preview.html import (
    CSP_HEADER,
    CSP_META,
    PREVIEW_PAGE_EVENTS,
    PREVIEW_RENDERER_VERSION,
    STYLE,
    DayUnit,
    PreviewInput,
    PreviewJob,
    PreviewPage,
    PreviewPageError,
    plan_pages,
    render_preview,
)

__all__ = [
    "CSP_HEADER",
    "CSP_META",
    "PREVIEW_PAGE_EVENTS",
    "PREVIEW_RENDERER_VERSION",
    "STYLE",
    "DayUnit",
    "PreviewInput",
    "PreviewJob",
    "PreviewPage",
    "PreviewPageError",
    "plan_pages",
    "render_preview",
]
