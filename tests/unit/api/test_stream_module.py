"""The job stream's boundaries (M17 step 5, plan §4): it imports nothing that reads evidence, it
runs only its listed statements, those touch only status tables and the chain's sequence numbers
and unit keys, and the SSE compat rule watches its published event schemas."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path

from scripts.api_compat import compare

from edisc_api import openapi_doc, stream

SOURCES = [
    Path(stream.__file__),
    Path(stream.__file__).with_name("stream_events.py"),
]
FORBIDDEN_IMPORTS = (
    "edisc_evidence", "edisc_normalizer", "edisc_renderers", "edisc_custody", "edisc_connector",
    "edisc_worker", "types_aiobotocore", "aiobotocore", "boto", "edisc_api.routes",
)  # fmt: skip
TABLES = {"collection_jobs", "custody_events", "work_units", "job_pauses", "sessions", "principals"}


def test_the_stream_imports_nothing_that_reads_evidence() -> None:
    for source in SOURCES:
        tree = ast.parse(source.read_text())
        names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        bad = [m for m in names if m.startswith(FORBIDDEN_IMPORTS)]
        assert bad == [], f"{source.name} imports {bad}"


def test_the_stream_runs_only_its_listed_statements() -> None:
    tree = ast.parse(SOURCES[0].read_text())
    listed = {name for name in vars(stream) if getattr(stream, name) in stream.STATEMENTS}
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "text":
            (arg,) = call.args
            assert isinstance(arg, ast.Name) and arg.id in listed, ast.unparse(call)


def test_the_statements_read_only_status_tables_and_unit_keys_of_the_chain() -> None:
    for sql in stream.STATEMENTS:
        tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", sql))
        assert tables <= TABLES, (tables - TABLES, sql)
        # the chain: sequence numbers, event types and the unit key, never another payload value
        for use in re.findall(r"payload\S*", sql):
            assert use.startswith("payload->>'unit_key'"), sql
        assert "derived" not in sql and "items" not in sql.replace("work_units", "")


def _schemas(doc: dict[str, object]) -> dict[str, dict[str, object]]:
    return doc["components"]["schemas"]  # type: ignore[index, return-value]


def test_a_change_to_a_published_event_schema_is_breaking() -> None:
    """`edisc-sse-event-changed` on REAL input: the published stream operation's events."""
    doc = openapi_doc.document()
    for name in ("JobEvent", "UnitState", "ResetEvent", "ResetReason", "EndReason"):
        new = copy.deepcopy(doc)
        schema = _schemas(new)[name]
        if "enum" in schema:
            schema["enum"] = [*schema["enum"], "new_reason"]  # type: ignore[list-item]
        else:
            schema["properties"]["note"] = {"type": "string"}  # type: ignore[index]
        found = [(f.id, f.operation_id) for f in compare(doc, new, []).breaking]
        assert ("edisc-sse-event-changed", "stream_job") in found, (name, found)
