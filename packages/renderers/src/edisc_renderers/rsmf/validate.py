"""Manifest validation (ADR 0015 §8): the vendored RSMF 2.0.0 schema plus structural checks the
schema cannot express. Both run at render time, so an invalid manifest fails the render."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft7Validator

from edisc_renderers.rsmf.model import ManifestInvalidError
from edisc_renderers.rsmf.names import MANIFEST_NAME

SCHEMA_FILE = "rsmf_schema_2_0_0.json"
SCHEMA_SHA256 = "9658446c8ced7c92b1413dac0509a7645cf1578d25e30cb88d3e9c18696e2338"
"""Upstream commit c717cd322264b46115d27d034a6107c8c91043d8 (schema/SOURCE.md)."""

_REQUIRED_FORMATS = ("date-time", "idn-email")


def schema_bytes() -> bytes:
    data = resources.files("edisc_renderers.rsmf.schema").joinpath(SCHEMA_FILE).read_bytes()
    if hashlib.sha256(data).hexdigest() != SCHEMA_SHA256:
        raise ManifestInvalidError("vendored RSMF schema does not match its recorded SHA-256")
    return data


@cache
def validator() -> Draft7Validator:
    schema = json.loads(schema_bytes())
    Draft7Validator.check_schema(schema)
    checker = Draft7Validator.FORMAT_CHECKER
    # jsonschema skips a format silently when its optional checker library is missing
    missing = [f for f in _REQUIRED_FORMATS if f not in checker.checkers]
    if missing:
        raise ManifestInvalidError(
            f"format checks unavailable: {missing} (install rfc3339-validator)"
        )
    return Draft7Validator(schema, format_checker=checker)


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Raise `ManifestInvalidError` listing the first schema violations."""
    errors = sorted(validator().iter_errors(manifest), key=lambda e: list(e.absolute_path))
    if errors:
        shown = "; ".join(
            f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:200]}"
            for e in errors[:5]
        )
        raise ManifestInvalidError(f"{len(errors)} schema violation(s): {shown}")


def check_structure(manifest: Mapping[str, Any], zip_names: Collection[str]) -> None:
    """What the schema leaves open (ADR 0015 §8):

    - one conversation, and every event belongs to it;
    - event ids are unique, and every `parent` resolves to an event in the file;
    - every referenced participant (event, reaction, edit, conversation member, custodian) exists;
    - every attachment id is a zip entry, and every zip entry except the manifest is referenced.
    """
    problems: list[str] = []
    participants = {p["id"] for p in manifest["participants"]}
    if len(participants) != len(manifest["participants"]):
        problems.append("duplicate participant ids")
    conversations = manifest["conversations"]
    if len(conversations) != 1:
        problems.append(f"{len(conversations)} conversations (expected 1)")
    conversation_ids = {c["id"] for c in conversations}
    for c in conversations:
        refs = [*c["participants"], *([c["custodian"]] if "custodian" in c else [])]
        problems.extend(
            f"conversation {c['id']}: unknown participant {pid}"
            for pid in refs
            if pid not in participants
        )

    events = manifest["events"]
    ids = [e.get("id") for e in events]
    if any(i is None for i in ids):
        problems.append("event without id")
    if len(set(ids)) != len(ids):
        problems.append("duplicate event ids")
    known_ids = set(ids)
    referenced: set[str] = set()
    for e in events:
        eid = e.get("id")
        if e.get("conversation") not in conversation_ids:
            problems.append(f"event {eid}: unknown conversation")
        if "parent" in e and e["parent"] not in known_ids:
            problems.append(f"event {eid}: parent {e['parent']} not in file")
        if "parent" in e and e["parent"] == eid:
            problems.append(f"event {eid}: is its own parent")
        users = [e.get("participant")] if "participant" in e else []
        users += [u for r in e.get("reactions", []) for u in r.get("participants", [])]
        users += [x["participant"] for x in e.get("edits", [])]
        problems.extend(
            f"event {eid}: unknown participant {u}" for u in users if u not in participants
        )
        for a in e.get("attachments", []):
            referenced.add(a["id"])
            if a["id"] not in zip_names:
                problems.append(f"event {eid}: attachment {a['id']} not in zip")
    unreferenced = set(zip_names) - referenced - {MANIFEST_NAME}
    problems.extend(f"zip entry {n} not referenced" for n in sorted(unreferenced))
    if MANIFEST_NAME not in zip_names:
        problems.append("no manifest in zip")
    if problems:
        raise ManifestInvalidError(
            f"{len(problems)} structural problem(s): " + "; ".join(problems[:10])
        )
