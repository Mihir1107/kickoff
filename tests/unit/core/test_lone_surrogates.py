"""Lone UTF-16 surrogates (ADR 0018 §23): source hashes are lossless (`canonical_source_json`, checked
against Node's well-formed `JSON.stringify`), stored and published records are valid (replacement
text plus the exact code units, `edisc_verify.lone_surrogates`)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from edisc_verify.canonical import (
    CanonicalizationError,
    canonical_hash,
    canonical_json,
    canonical_source_hash,
    canonical_source_json,
)
from edisc_verify.lone_surrogates import (
    FIELD,
    from_utf16_units,
    has_lone,
    make_valid,
    recorded_text,
    replacement,
    restore,
    utf16_units,
)

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "jcs"
NODE = shutil.which("node")


def _no_joined_pair(s: str) -> bool:
    """No high surrogate directly before a low one: a JSON decoder would join their escapes."""
    return not any(
        0xD800 <= ord(a) <= 0xDBFF and 0xDC00 <= ord(b) <= 0xDFFF for a, b in pairwise(s)
    )


HOSTILE = (
    st.lists(st.one_of(st.characters(), st.integers(0xD800, 0xDFFF).map(chr)), max_size=12)
    .map("".join)
    .filter(_no_joined_pair)
)
VALUES = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-(2**53) + 1, 2**53 - 1), HOSTILE),
    lambda inner: st.one_of(
        st.lists(inner, max_size=4), st.dictionaries(HOSTILE, inner, max_size=4)
    ),
    max_leaves=20,
)


def test_valid_input_is_byte_identical_to_rfc8785() -> None:
    doc = {"b": ["x", 1, None, True], "a": 'caf\u00e9 \U0001f600 "\\\n', "\u20ac": {"k": 2.5}}
    assert canonical_source_json(doc) == canonical_json(doc)
    assert canonical_source_hash(doc) == canonical_hash(doc)


def test_lone_surrogates_are_written_as_lowercase_escapes() -> None:
    doc = json.loads('{"t": "a\\ud83d b \\uDC00", "\\ude00k": ["\\ude00\\ud83d"]}')
    out = canonical_source_json(doc)
    assert out == b'{"t":"a\\ud83d b \\udc00","\\ude00k":["\\ude00\\ud83d"]}'
    assert json.loads(out) == doc  # lossless
    with pytest.raises(CanonicalizationError):
        canonical_json(doc)  # everything else stays strict


def test_lone_surrogate_keys_raise_canonicalization_error_in_strict_mode() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json({"\ud800": 1})  # not a bare UnicodeEncodeError from the key sort


def test_a_high_then_low_pair_of_code_points_is_refused() -> None:
    """ "\\ud83d\\ude00" escaped would decode as U+1F600: refused, never ambiguous."""
    with pytest.raises(CanonicalizationError):
        canonical_source_json(chr(0xD83D) + chr(0xDE00))
    assert canonical_source_json("\U0001f600") == canonical_json("\U0001f600")


@given(VALUES)
@settings(max_examples=300)
def test_source_json_round_trips_and_is_injective_against_replacement(value: Any) -> None:
    out = canonical_source_json(value)
    out.decode("utf-8")  # always valid UTF-8 bytes
    assert json.loads(out) == value
    try:
        valid, found = make_valid(value)
    except ValueError:  # a key with a lone surrogate: records never hold one (our keys)
        return
    if found:  # two different strings never hash alike because one was replaced
        assert canonical_source_json(valid) != out
        assert restore(valid, found) == value
    else:
        assert out == canonical_json(value)


@pytest.mark.skipif(NODE is None and not os.environ.get("CI"), reason="node not installed")
@settings(max_examples=1, suppress_health_check=list(HealthCheck), deadline=None)
@given(st.lists(VALUES, min_size=300, max_size=300))
def test_source_json_matches_node_json_stringify(values: list[Any]) -> None:
    """The escape form is ECMAScript's (well-formed JSON.stringify, Node >= 12), keys sorted by
    UTF-16 code units: an independent implementation agrees byte for byte."""
    stdin = "\n".join(json.dumps(v, ensure_ascii=True) for v in values) + "\n"
    result = subprocess.run(
        [NODE, str(GOLDEN / "jcs_reference.mjs")],  # type: ignore[list-item]
        input=stdin.encode(),
        capture_output=True,
        check=True,
        timeout=60,
    )
    lines = result.stdout.split(b"\n")[:-1]
    assert len(lines) == len(values)
    for value, node_out in zip(values, lines, strict=True):
        assert canonical_source_json(value) == node_out, value


def test_utf16_units_round_trip() -> None:
    s = "a\ud83d\U0001f600\udc00"
    assert utf16_units(s) == [0x61, 0xD83D, 0xD83D, 0xDE00, 0xDC00]
    assert from_utf16_units(utf16_units(s)) == s
    assert replacement(s) == "a�\U0001f600�"
    assert has_lone(s) and not has_lone(replacement(s))
    with pytest.raises(ValueError, match=r"0\.\.65535"):
        from_utf16_units([0x10000])


def test_make_valid_names_every_string_by_json_pointer() -> None:
    record = {"text": "x\ud800", "files": [["F1", "n\udfff.pdf", "a/b"]], "a/b~": "\udc00", "ok": 1}
    valid, found = make_valid(record)
    assert valid == {"text": "x�", "files": [["F1", "n�.pdf", "a/b"]], "a/b~": "�",
                     "ok": 1}  # fmt: skip
    assert found == {"/text": [0x78, 0xD800], "/files/0/1": [0x6E, 0xDFFF, 0x2E, 0x70, 0x64, 0x66],
                     "/a~1b~0": [0xDC00]}  # fmt: skip
    canonical_json(valid)  # storable, publishable
    assert restore(valid, found) == record
    assert make_valid({"t": "fine"}) == ({"t": "fine"}, {})
    with pytest.raises(ValueError, match="a key with a lone surrogate"):
        make_valid({"\ud800": 1})  # a record's keys are ours: never merged by replacement


def test_restore_refuses_a_text_unlike_its_code_units() -> None:
    with pytest.raises(ValueError, match="not the replacement"):
        restore({"t": "y�"}, {"/t": [0x78, 0xD800]})
    with pytest.raises(ValueError, match="no such field"):
        restore({"t": "x�"}, {"/missing": [0x78, 0xD800]})


def test_recorded_text_cuts_then_records_exactly() -> None:
    assert recorded_text("plain", 3) == ("pla", {})
    text, exact = recorded_text("ab\ud800cd", 4)
    assert (text, exact) == ("ab�c", {"utf16": [0x61, 0x62, 0xD800, 0x63]})
    assert FIELD == "lone_surrogates"
