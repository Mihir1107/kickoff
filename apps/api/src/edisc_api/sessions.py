"""Browser session primitives (ADR 0016 §1-§3, Amendments 1 and 2; M17 step 2).

Pure helpers, no DB: random ids and their hashes, the server keyring (CSRF tokens and the PKCE
verifier derived from the login cookie's handle), ``return_to`` validation, cookie headers, the
same-site bounce page and the user agent as it is stored.

- Session ids, login handles and states are 256-bit random values (base64url, 43 characters). Only
  their SHA-256 is stored; a value the browser presents is hashed and looked up, never compared.
- The CSRF token is ``<kid>.<HMAC(csrf key of kid, session id)>``: bound to the session (a new id at
  every sign-in invalidates it) and checked against every kid of the keyring (rotation).
- The PKCE verifier is ``HMAC(pkce key of kid, login handle)``: the login attempt row stores no
  secret; only the browser's handle plus our key rebuild the verifier at the callback.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import secrets
import urllib.parse
from dataclasses import dataclass

from edisc_core.settings import Settings

SESSION_COOKIE = "__Host-edisc_session"
LOGIN_COOKIE = "__Host-edisc_login"
MAX_TOKEN_CHARS = 64  # a presented id/handle/state longer than this is refused before hashing
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
# a fixed, public development key: local/test/ci only (settings refuse an empty keyring elsewhere)
_DEV_KID = "dev"
_DEV_KEY = b"edisc-development-session-key-not-a-secret!"


class ReturnToError(ValueError):
    """``return_to`` is not a same-origin relative path (400 ``invalid_return_to``)."""


def new_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def token_hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def well_formed(value: str | None) -> bool:
    """A value we issued has exactly this shape; anything else is refused before any lookup."""
    return value is not None and len(value) <= MAX_TOKEN_CHARS and bool(_TOKEN.match(value))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True)
class Keyring:
    keys: dict[str, bytes]
    current: str

    @classmethod
    def from_settings(cls, settings: Settings) -> Keyring:
        """The API's keyring; it refuses to start without real keys outside local/test/ci."""
        if settings.api_session_keys:
            keys = {k: v.get_secret_value().encode() for k, v in settings.api_session_keys.items()}
            current = settings.api_session_key_id or ""
            if current not in keys:
                raise ValueError("EDISC_API_SESSION_KEY_ID does not name a key")
            return cls(keys, current)
        if not settings.env.is_disposable:
            raise ValueError(
                "EDISC_API_SESSION_KEYS and EDISC_API_SESSION_KEY_ID are required outside local/test/ci"
            )
        return cls({_DEV_KID: _DEV_KEY}, _DEV_KID)

    def _sub(self, kid: str, label: str) -> bytes:
        return hmac.new(self.keys[kid], f"edisc-session/{label}".encode(), hashlib.sha256).digest()

    def csrf_token(self, session_id: str) -> str:
        mac = hmac.new(self._sub(self.current, "csrf"), session_id.encode(), hashlib.sha256)
        return f"{self.current}.{_b64(mac.digest())}"

    def csrf_valid(self, session_id: str, presented: str | None) -> bool:
        if not presented or len(presented) > 128 or "." not in presented:
            return False
        kid, _, mac = presented.partition(".")
        if kid not in self.keys:
            return False
        expected = _b64(
            hmac.new(self._sub(kid, "csrf"), session_id.encode(), hashlib.sha256).digest()
        )
        return hmac.compare_digest(expected.encode(), mac.encode())

    def pkce_verifier(self, kid: str, handle: str) -> str:
        """RFC 7636 verifier (43 characters of base64url) rebuilt from the login handle."""
        if kid not in self.keys:
            raise KeyError("login attempt key is no longer in the keyring")
        return _b64(hmac.new(self._sub(kid, "pkce"), handle.encode(), hashlib.sha256).digest())

    def install_mac(self, kid: str, message: str) -> str:
        """The MAC of an install's signed state (ADR 0016 A3), under its own derived key."""
        if kid not in self.keys:
            raise KeyError("install state key is no longer in the keyring")
        return _b64(
            hmac.new(self._sub(kid, "install-state"), message.encode(), hashlib.sha256).digest()
        )

    def install_verifier(self, kid: str, handle: str) -> str:
        """An install's PKCE verifier, derived from its cookie handle (nothing secret is stored)."""
        if kid not in self.keys:
            raise KeyError("install key is no longer in the keyring")
        return _b64(
            hmac.new(self._sub(kid, "install-pkce"), handle.encode(), hashlib.sha256).digest()
        )


def pkce_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode()).digest())


# ------------------------------------------------------------------ return_to (ADR 0016 §1)
MAX_RETURN_TO_BYTES = 2048
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_return_to(value: str | None) -> str:
    """A same-origin relative path or :class:`ReturnToError`; never rewritten (ADR 0016 §1)."""
    if value is None or value == "":
        return "/"
    if len(value.encode("utf-8", "surrogatepass")) > MAX_RETURN_TO_BYTES:
        raise ReturnToError("too long")
    for candidate in (value, urllib.parse.unquote(value)):
        if not candidate.startswith("/") or candidate.startswith(("//", "/\\")):
            raise ReturnToError("not a relative path")
        if "\\" in candidate or _CONTROL.search(candidate):
            raise ReturnToError("backslash or control character")
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.scheme or parsed.netloc:  # defence in depth: a single-slash path has neither
            raise ReturnToError("scheme or host")
    return value


# ------------------------------------------------------------------ cookies
def set_cookie(name: str, value: str, *, max_age: int, same_site: str) -> str:
    """A ``__Host-`` cookie: Secure, Path=/, no Domain (host-only), httpOnly."""
    return f"{name}={value}; Max-Age={max_age}; Path=/; Secure; HttpOnly; SameSite={same_site}"


def clear_cookie(name: str, same_site: str) -> str:
    return set_cookie(name, "", max_age=0, same_site=same_site)


# ------------------------------------------------------------------ bounce page (ADR 0016 A1)
BOUNCE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def bounce_page(target: str) -> str:
    """A static page whose meta refresh starts a NEW, same-site navigation to ``target`` (a path we
    validated or built), so the Strict session cookie set with it is sent (a 303 is not: the redirect
    chain the IdP started is cross-site). No script; the link is the fallback."""
    url = html.escape(target, quote=True)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={url}">'
        "<title>Signing in</title></head>"
        f'<body><p><a href="{url}">Continue</a></p></body></html>'
    )


# ------------------------------------------------------------------ stored request facts
def stored_user_agent(value: str | None) -> str | None:
    """Printable ASCII only (anything else becomes ``?``), at most 256 characters."""
    if not value:
        return None
    return "".join(c if " " <= c <= "~" else "?" for c in value[:256])
