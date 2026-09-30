import pytest
from hypothesis import given
from hypothesis import strategies as st

from edisc_core.idempotency import idempotency_key
from edisc_core.jsonpath import JsonPathError, build, parse, resolve

H = "ab" * 32


def test_idempotency_key_is_stable_and_component_sensitive() -> None:
    k = idempotency_key("t1", "slack", "W/C/1.0", H)
    assert k == idempotency_key("t1", "slack", "W/C/1.0", H)
    assert len(k) == 64
    assert k != idempotency_key("t2", "slack", "W/C/1.0", H)
    assert k != idempotency_key("t1", "teams", "W/C/1.0", H)
    assert k != idempotency_key("t1", "slack", "W/C/1.1", H)
    assert k != idempotency_key("t1", "slack", "W/C/1.0", "cd" * 32)


def test_idempotency_key_rejects_ambiguous_input() -> None:
    with pytest.raises(ValueError, match="U\\+001F"):
        idempotency_key("t", "s", "a\x1fb", H)
    with pytest.raises(ValueError, match="64 lowercase hex"):
        idempotency_key("t", "s", "x", H.upper())


def test_jsonpath_examples() -> None:
    doc = {"messages": [{"ts": "1"}, {"weird key": [None, {"x": 1}]}]}
    assert build("messages", 1, "weird key", 1, "x") == '$.messages[1]["weird key"][1].x'
    assert resolve(doc, '$.messages[1]["weird key"][1].x') == 1
    assert resolve(doc, "$") is doc
    for bad in ["messages", "$.", "$[01]", "$[-1]", "$.messages[9]", "$.nope", '$["unterminated]']:
        with pytest.raises(JsonPathError):
            resolve(doc, bad)


@given(
    st.lists(st.one_of(st.integers(min_value=0, max_value=10**6), st.text(max_size=12)), max_size=6)
)
def test_build_parse_roundtrip(segments: list[str | int]) -> None:
    assert parse(build(*segments)) == segments
