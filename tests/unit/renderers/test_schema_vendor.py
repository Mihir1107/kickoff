"""The vendored RSMF schema (ADR 0015 §8): pinned bytes, licence, and checks that actually run."""

from __future__ import annotations

import hashlib
import re
from importlib import resources
from typing import Any

import pytest

from edisc_renderers.rsmf import ManifestInvalidError
from edisc_renderers.rsmf.validate import (
    SCHEMA_SHA256,
    check_structure,
    validate_manifest,
    validator,
)

SCHEMA_DIR = resources.files("edisc_renderers.rsmf.schema")


def test_vendored_files_match_source_note() -> None:
    note = SCHEMA_DIR.joinpath("SOURCE.md").read_text()
    assert "c717cd322264b46115d27d034a6107c8c91043d8" in note
    schema = SCHEMA_DIR.joinpath("rsmf_schema_2_0_0.json").read_bytes()
    licence = SCHEMA_DIR.joinpath("LICENSE").read_bytes()
    assert hashlib.sha256(schema).hexdigest() == SCHEMA_SHA256
    hashes = re.findall(r"`([0-9a-f]{64})`", note)
    assert hashes == [SCHEMA_SHA256, hashlib.sha256(licence).hexdigest()]
    assert licence.startswith(b"Copyright (c) 2016, kCura LLC")
    assert b"Redistributions of source code must retain" in licence


def _minimal() -> dict[str, Any]:
    return {
        "version": "2.0.0",
        "participants": [{"id": "U1"}],
        "conversations": [{"id": "C1", "platform": "slack", "participants": ["U1"]}],
        "events": [
            {"id": "1", "type": "message", "participant": "U1", "conversation": "C1",
             "timestamp": "2026-01-05T09:00:00.000100Z"}
        ],
    }  # fmt: skip


def test_minimal_manifest_is_valid() -> None:
    validate_manifest(_minimal())
    check_structure(_minimal(), ["rsmf_manifest.json"])


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("events", 0, "timestamp"), "2026-01-05 09:00"),  # format checks are on
        (("events", 0, "type"), "reaction"),
        (("conversations", 0, "type"), "group"),
        (("participants", 0, "id"), ""),
        (("events", 0, "custom"), [{"name": "x", "value": ""}]),
        (("version",), "2.0"),
    ],
)
def test_schema_violations_fail(path: tuple[Any, ...], value: Any) -> None:
    manifest = _minimal()
    target: Any = manifest
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ManifestInvalidError):
        validate_manifest(manifest)


def test_format_checker_is_active() -> None:
    # jsonschema silently skips a format whose optional checker library is missing
    assert "date-time" in validator().format_checker.checkers  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "breakage",
    [
        "dangling_parent",
        "unknown_participant",
        "missing_attachment",
        "unreferenced_entry",
        "dup_id",
    ],
)
def test_structural_problems_fail(breakage: str) -> None:
    m = _minimal()
    names = ["rsmf_manifest.json"]
    event = m["events"][0]
    if breakage == "dangling_parent":
        event["parent"] = "0"
    elif breakage == "unknown_participant":
        event["participant"] = "U9"
    elif breakage == "missing_attachment":
        event["attachments"] = [{"id": "F1_a.txt"}]
    elif breakage == "unreferenced_entry":
        names.append("F1_a.txt")
    else:
        m["events"].append(dict(event))
    validate_manifest(m)
    with pytest.raises(ManifestInvalidError):
        check_structure(m, names)
