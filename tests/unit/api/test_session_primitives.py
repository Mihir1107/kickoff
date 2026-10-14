"""Session primitives (ADR 0016 §1-§3; M17 step 2): return_to, CSRF tokens under key rotation, the
derived PKCE verifier, the keyring and the settings that guard it."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from edisc_api.sessions import (
    Keyring,
    ReturnToError,
    bounce_page,
    new_token,
    pkce_challenge,
    stored_user_agent,
    token_hash,
    validate_return_to,
    well_formed,
)
from edisc_core.settings import Environment, Settings

KEY_A, KEY_B = "a" * 32, "b" * 32


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "value",
    ["https://e.x", "//e.x", "/\\e.x", "\\e.x", "/%2F/e.x", "/%2f%2fe.x", "/%5Ce.x", "e.x",
     "javascript:x", "/a\tb", "/a\rb", "/a\x00b", "/a%00b", "/a%09b", "/a\x7f", "/" + "a" * 2048,
     "?q", "#f", " /x"],
)  # fmt: skip
def test_return_to_refuses_everything_but_a_same_origin_path(value: str) -> None:
    with pytest.raises(ReturnToError):
        validate_return_to(value)


@pytest.mark.parametrize("value", ["/", "/a", "/a/b?c=d#e", "/%E2%9C%93", "/" + "a" * 2047])
def test_return_to_keeps_a_path_exactly(value: str) -> None:
    assert validate_return_to(value) == value


def test_return_to_defaults_to_the_root() -> None:
    assert validate_return_to(None) == validate_return_to("") == "/"


@given(st.text(max_size=80))
def test_an_accepted_return_to_never_leaves_the_origin(value: str) -> None:
    try:
        kept = validate_return_to(value)
    except ReturnToError:
        return
    import urllib.parse

    for candidate in (kept, urllib.parse.unquote(kept)):
        assert candidate.startswith("/") and not candidate.startswith(("//", "/\\"))
        parts = urllib.parse.urlsplit(candidate)
        assert not parts.scheme and not parts.netloc


def test_tokens_are_256_bit_and_well_formed() -> None:
    token = new_token()
    assert len(token) == 43 and well_formed(token) and len(token_hash(token)) == 32
    assert not well_formed(token + "x") and not well_formed("a" * 42) and not well_formed(None)
    assert not well_formed("a" * 42 + "=") and new_token() != token


def test_csrf_tokens_bind_the_session_and_survive_one_rotation() -> None:
    old = Keyring({"k1": KEY_A.encode()}, "k1")
    rotated = Keyring({"k1": KEY_A.encode(), "k2": KEY_B.encode()}, "k2")
    session, other = new_token(), new_token()
    token = old.csrf_token(session)
    assert token.startswith("k1.") and old.csrf_valid(session, token)
    assert rotated.csrf_valid(session, token)  # the previous kid still verifies
    assert rotated.csrf_token(session).startswith("k2.")
    assert not old.csrf_valid(other, token)  # another session's token
    assert not Keyring({"k2": KEY_B.encode()}, "k2").csrf_valid(session, token)  # kid retired
    for bad in (None, "", "k1", "k1.", "nokid.x", "k1." + "A" * 43, token + "x", "x" * 200):
        assert not old.csrf_valid(session, bad)


def test_the_pkce_verifier_is_rebuilt_from_the_handle_and_meets_rfc_7636() -> None:
    ring = Keyring({"k1": KEY_A.encode()}, "k1")
    handle = new_token()
    verifier = ring.pkce_verifier("k1", handle)
    assert verifier == ring.pkce_verifier("k1", handle) != ring.pkce_verifier("k1", new_token())
    assert 43 <= len(verifier) <= 128 and well_formed(verifier)
    assert pkce_challenge(verifier) != verifier
    with pytest.raises(KeyError):
        ring.pkce_verifier("gone", handle)


def test_the_keyring_needs_real_keys_outside_local_test_ci() -> None:
    """The API refuses to start without keys there; other processes' settings do not need them."""
    assert Keyring.from_settings(_settings(env=Environment.TEST)).current == "dev"
    with pytest.raises(ValueError, match="SESSION_KEYS"):
        Keyring.from_settings(_settings(env=Environment.PRODUCTION))
    with pytest.raises(ValueError, match="SESSION_KEY_ID"):
        _settings(env=Environment.PRODUCTION, api_session_keys={"k1": SecretStr(KEY_A)},
                  api_session_key_id="k2")  # fmt: skip
    with pytest.raises(ValueError, match="https"):
        _settings(env=Environment.PRODUCTION, api_public_scheme="http")
    with pytest.raises(ValueError, match="32 bytes"):
        _settings(env=Environment.TEST, api_session_keys={"k1": SecretStr("short")})
    good = _settings(env=Environment.PRODUCTION, api_session_keys={"k1": SecretStr(KEY_A)},
                     api_session_key_id="k1")  # fmt: skip
    assert Keyring.from_settings(good).keys == {"k1": KEY_A.encode()}


def test_the_bounce_page_escapes_its_target_and_has_no_script() -> None:
    page = bounce_page('/a?x="><script>&y=1')
    assert "<script" not in page and "&quot;&gt;&lt;script&gt;&amp;y=1" in page
    assert '<meta http-equiv="refresh" content="0;url=' in page


def test_the_stored_user_agent_is_bounded_printable_ascii() -> None:
    assert stored_user_agent(None) is None
    assert stored_user_agent("Mozilla/5.0 ‮\x00") == "Mozilla/5.0 ??"
    assert len(stored_user_agent("x" * 1000) or "") == 256
