"""RFC 8785 conformance: published vectors, independent Node.js cross-check, domain rules."""

import json
import math
import os
import shutil
import struct
import subprocess
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from edisc_core.canonical import CanonicalizationError, canonical_hash, canonical_json, to_jsonable

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "jcs"
VECTORS = json.loads((GOLDEN / "vectors.json").read_text(encoding="utf-8"))


def _double(hex_bits: str) -> float:
    return float(struct.unpack(">d", bytes.fromhex(hex_bits))[0])


@pytest.mark.parametrize("doc", VECTORS["documents"], ids=lambda d: d["name"])
def test_rfc8785_documents(doc: dict[str, str]) -> None:
    assert canonical_json(json.loads(doc["input"])) == doc["expected"].encode("utf-8")


@pytest.mark.parametrize(("bits", "expected"), VECTORS["numbers"])
def test_rfc8785_appendix_b_numbers(bits: str, expected: str) -> None:
    assert canonical_json(_double(bits)) == expected.encode()


@pytest.mark.parametrize("bits", VECTORS["invalid_numbers"])
def test_nan_and_infinity_rejected(bits: str) -> None:
    value = _double(bits)
    assert math.isnan(value) or math.isinf(value)
    with pytest.raises(CanonicalizationError):
        canonical_json(value)


def test_lone_surrogate_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json({"s": "\ud800"})


def test_no_unicode_normalization() -> None:
    nfc = "caf\u00e9"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert canonical_hash({"t": nfc}) != canonical_hash({"t": nfd})
    assert canonical_hash({"t": "a b"}) != canonical_hash({"t": "a  b"})


def test_domain_conversions() -> None:
    u = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
    at = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)
    assert canonical_json({"id": u, "at": at}) == (
        b'{"at":"2026-01-02T03:04:05.000006Z","id":"0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"}'
    )
    with pytest.raises(ValueError, match="naive"):
        to_jsonable(datetime(2026, 1, 2))  # noqa: DTZ001
    with pytest.raises(CanonicalizationError, match="bytes"):
        to_jsonable(b"raw")
    with pytest.raises(CanonicalizationError, match="keys must be str"):
        to_jsonable({1: "x"})


def test_integers_outside_ijson_range_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(2**53 + 1)


# ------------------------------------------------------------------ cross-language (Node.js)
json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(st.characters(exclude_categories=("Cs",)), max_size=20)
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(
            st.text(st.characters(exclude_categories=("Cs",)), max_size=8), children, max_size=5
        )
    ),
    max_leaves=25,
)

NODE = shutil.which("node")


# In CI the cross-check is mandatory: a missing node must fail, not silently skip.
@pytest.mark.skipif(NODE is None and not os.environ.get("CI"), reason="node not installed")
@settings(max_examples=1, suppress_health_check=list(HealthCheck), deadline=None)
@given(st.lists(json_values, min_size=500, max_size=500))
def test_matches_independent_node_implementation(values: list[Any]) -> None:
    stdin = "\n".join(json.dumps(v, ensure_ascii=True, allow_nan=False) for v in values) + "\n"
    result = subprocess.run(
        [NODE, str(GOLDEN / "jcs_reference.mjs")],  # type: ignore[list-item]
        input=stdin.encode(),
        capture_output=True,
        check=True,
        timeout=60,
    )
    node_lines = result.stdout.decode("utf-8").split("\n")[:-1]
    assert len(node_lines) == len(values)
    for value, node_out in zip(values, node_lines, strict=True):
        # JSON.stringify of -0 is "0" and Python ints/floats that are integral agree after JCS
        assert canonical_json(value).decode("utf-8") == node_out, value
