"""Secret redaction for everything that can reach a log sink.

Three layers, all applied by :func:`edisc_core.logs.configure_logging`:

1. **Registered values**: any secret the process has decrypted is registered with
   :func:`register_secret` and replaced wherever it appears, including inside exception messages,
   formatted tracebacks and nested structures.
2. **Sensitive keys**: values under keys like ``token``, ``password``, ``authorization`` are replaced
   at any depth of a mapping.
3. **Token-shaped patterns**: well-known credential formats (Slack ``xox*-``, bearer headers, JWTs,
   AWS access keys, PEM private keys) are replaced even if never registered.

The final formatted string is always scrubbed (layer 1 and 3), so a secret that slipped into an
exception message or ``repr`` is caught after the traceback is rendered.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr

REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 8

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|auth[_-]?header|cookie|"
    r"credential|private[_-]?key|dek|kek|encrypted_token_blob|client_secret)",
    re.IGNORECASE,
)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"xox[abeoprs]-[A-Za-z0-9-]{8,}"),  # Slack tokens
    re.compile(r"xapp-\d-[A-Za-z0-9-]{8,}"),  # Slack app-level tokens
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)

_lock = threading.Lock()
_secrets: set[str] = set()
_secret_re: re.Pattern[str] | None = None


def register_secret(value: str | SecretStr) -> None:
    """Register a secret value so it is scrubbed from all log output of this process."""
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if len(raw) < MIN_SECRET_LENGTH:
        raise ValueError("refusing to register a secret shorter than 8 chars (would over-redact)")
    global _secret_re
    with _lock:
        if raw in _secrets:
            return
        _secrets.add(raw)
        ordered = sorted(_secrets, key=len, reverse=True)
        _secret_re = re.compile("|".join(re.escape(s) for s in ordered))


def clear_registered_secrets() -> None:
    """Test helper."""
    global _secret_re
    with _lock:
        _secrets.clear()
        _secret_re = None


def redact_text(text: str) -> str:
    secret_re = _secret_re
    if secret_re is not None:
        text = secret_re.sub(REDACTED, text)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a structured value (mappings, sequences, exceptions, strings)."""
    if _depth > 32:
        return REDACTED
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes | bytearray):
        return redact_text(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        return {
            k: (
                REDACTED
                if isinstance(k, str) and is_sensitive_key(k) and v is not None
                else redact_value(v, _depth=_depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        items = [redact_value(v, _depth=_depth + 1) for v in value]
        return tuple(items) if type(value) is tuple else items
    if isinstance(value, BaseException):
        return redact_text(f"{type(value).__name__}: {value}")
    if value is None or isinstance(value, bool | int | float):
        return value
    return redact_text(repr(value))
