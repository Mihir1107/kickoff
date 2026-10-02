"""The ``export`` dialect (ADR 0014 section 7): a day file is a bare JSON array; the same message gives
the same identity and fingerprint as in a Web API page; ``select`` keeps real array indexes."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from edisc_core.schemas import ARCHIVE_CAVEAT, ItemType
from edisc_normalizer.slack import NormalizationError, file_refs, normalize_messages_page

from .test_slack_normalizer import REF, TENANT, ctx, msg, page


def export_ctx():  # type: ignore[no-untyped-def]
    return dataclasses.replace(ctx(), dialect="export")


def messages(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {d.source_item_id: d for d in result.items if d.item_type is ItemType.MESSAGE}


def test_same_message_same_identity_and_fingerprint_in_both_dialects() -> None:
    m = [
        msg("1767571200.000100", "a", edited={"user": "U1", "ts": "2.0"}),
        msg("1767571300.000100", "b"),
    ]
    api = normalize_messages_page(page(*m), ctx=ctx(), page_ref=REF, prior={}, files={})
    export = normalize_messages_page(
        json.dumps(m).encode(), ctx=export_ctx(), page_ref=REF, prior={}, files={}
    )
    a, e = messages(api), messages(export)
    assert set(a) == set(e)
    for sid in a:
        assert a[sid].idempotency_key(TENANT, "slack") == e[sid].idempotency_key(TENANT, "slack")  # type: ignore[attr-defined]
        assert a[sid].raw_hash == e[sid].raw_hash  # type: ignore[attr-defined]
    assert {d.json_path for d in e.values()} == {"$[0]", "$[1]"}  # type: ignore[attr-defined]


def test_select_keeps_real_indexes_and_limits_files() -> None:
    f = {"id": "F1", "name": "x", "mimetype": "text/plain"}
    body = json.dumps([msg("1.000001", "skip", files=[f]), msg("2.000001", "keep")]).encode()
    result = normalize_messages_page(
        body, ctx=export_ctx(), page_ref=REF, prior={}, files={}, select=frozenset({"2.000001"})
    )
    assert [d.json_path for d in messages(result).values()] == ["$[1]"]  # type: ignore[attr-defined]
    assert file_refs(body, "export", frozenset({"2.000001"})) == []
    assert [r.file_id for r in file_refs(body, "export")] == ["F1"]


def test_an_export_file_must_be_an_array_of_objects() -> None:
    for bad in (b'{"ok": true, "messages": []}', b"[1, 2]", b"not json"):
        with pytest.raises(NormalizationError):
            normalize_messages_page(bad, ctx=export_ctx(), page_ref=REF, prior={}, files={})


def test_archive_caveat_is_the_adr_text_verbatim() -> None:
    adr = Path(__file__).resolve().parents[3] / "docs" / "adr" / "0014-slack-export-ingestion.md"
    quoted = re.search(r"> \"(Completeness was verified.*?)\"\n", adr.read_text(), re.S)
    assert quoted is not None
    assert " ".join(quoted.group(1).replace("> ", "").split()) == ARCHIVE_CAVEAT
