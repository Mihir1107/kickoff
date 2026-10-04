"""Structural checks on a rendered `.rsmf` (ADR 0015 §8), written independently of the renderer:
Python's `email` parser and `zipfile` read the bytes back, and the vendored schema is applied again."""

from __future__ import annotations

import email
import email.policy
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from edisc_renderers.rsmf.validate import validate_manifest


@dataclass(frozen=True)
class Parsed:
    headers: dict[str, str]
    manifest: dict[str, Any]
    zip_names: list[str]
    zip: zipfile.ZipFile
    text: str


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def check_eml(data: bytes) -> Parsed:
    # CRLF only, no bare LF, lines within the RFC 5322 hard limit
    assert b"\n" not in data.replace(b"\r\n", b""), "bare LF"
    assert b"\r" not in data.replace(b"\r\n", b""), "bare CR"
    assert all(len(line) <= 998 for line in data.split(b"\r\n")), "line over 998"
    head = data.split(b"\r\n\r\n", 1)[0]
    for line in head.split(b"\r\n"):
        assert line.isascii(), "non-ASCII header byte"

    msg = email.message_from_bytes(data, policy=email.policy.default)
    assert msg.is_multipart() and msg.get_content_type() == "multipart/mixed"
    assert msg["X-RSMF-Version"] == "2.0.0"
    headers = {k: str(v) for k, v in msg.items()}
    assert len(headers) == len(msg.keys()), "a header appears twice"

    parts = list(msg.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "application/zip"]
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1, "exactly one attachment"
    att = attachments[0]
    assert att.get_filename() == "rsmf.zip"
    assert att.get_content_disposition() == "attachment"
    assert att["Content-Transfer-Encoding"] == "base64"
    raw_b64 = att.get_payload(decode=False)
    assert all(len(line) <= 76 for line in raw_b64.splitlines())
    blob = att.get_content()
    text = parts[0].get_content()

    z = zipfile.ZipFile(io.BytesIO(blob))
    assert z.testzip() is None, "CRC mismatch"
    names = z.namelist()
    assert names == sorted(names, key=lambda n: n.encode("utf-8")), "zip not in name order"
    assert len(set(names)) == len(names)
    assert "rsmf_manifest.json" in names, "manifest at the zip root"
    for info in z.infolist():
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert info.compress_type == zipfile.ZIP_STORED
        assert info.extra == b"" and info.comment == b""
        assert "/" not in info.filename and "\\" not in info.filename
    manifest = json.loads(z.read("rsmf_manifest.json"))
    validate_manifest(manifest)

    # headers consistent with the manifest
    events = manifest["events"]
    assert int(headers["X-RSMF-EventCount"]) == len(events)
    assert int(headers["X-RSMF-AttachmentCount"]) == len(names) - 1
    stamps = [_ts(e["timestamp"]) for e in events]
    assert stamps == sorted(stamps), "events not in time order"
    assert _ts(headers["X-RSMF-BeginDate"]) == stamps[0]
    assert _ts(headers["X-RSMF-EndDate"]) == stamps[-1]
    assert headers["X-RSMF-EventCollectionID"] == manifest["eventcollectionid"]
    assert headers["X-RSMF-Generator"] == f"edisc-renderers/{headers['X-RSMF-RendererVersion']}"
    assert headers["Message-ID"] == f"<{headers['X-RSMF-SourceHash']}@rsmf.edisc>"

    # references resolve inside the file
    participants = {p["id"] for p in manifest["participants"]}
    ids = [e["id"] for e in events]
    assert len(set(ids)) == len(ids)
    (conversation,) = manifest["conversations"]
    assert set(conversation["participants"]) <= participants
    referenced: set[str] = set()
    for e in events:
        assert e["conversation"] == conversation["id"]
        if "parent" in e:
            assert e["parent"] in ids and e["parent"] != e["id"]
        assert e["participant"] in participants
        for r in e.get("reactions", []):
            assert set(r.get("participants", [])) <= participants
        for x in e.get("edits", []):
            assert x["participant"] in participants
        for a in e.get("attachments", []):
            assert a["id"] in names
            assert z.getinfo(a["id"]).file_size == a["size"]
            referenced.add(a["id"])
    assert referenced == set(names) - {"rsmf_manifest.json"}, "unreferenced zip entries"
    return Parsed(headers, manifest, names, z, text)


def custom(event: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for pair in event.get("custom", []):
        out.setdefault(pair["name"], []).append(pair["value"])
    return out
