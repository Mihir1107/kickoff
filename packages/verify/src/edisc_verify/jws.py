"""JSON Web Signatures for the destruction certificate (M18 plan §2.5, D7, D12): ONE fixed algorithm.

The verifier accepts exactly `ALG` (ES256: ECDSA on P-256 with SHA-256, RFC 7518 §3.4). A JWS whose
protected header names any other ``alg`` -- ``none``, ``RS256``, ``ES384``, a different case -- or
none at all, is refused before any key is used: the algorithm is never taken from the token. Also
refused: any header member other than ``alg``, ``kid`` and ``typ`` (``crit`` included: no
extension is understood), a duplicated member, a ``kid`` that is not the key's RFC 7638
thumbprint, non-canonical base64url, and a signature that is not exactly the 64-byte ``R || S`` of RFC 7518.

`verify_detached` checks a compact JWS with a detached payload (RFC 7515 Appendix F: the middle
segment is empty and the verifier supplies the payload bytes); `verify_compact` an ordinary one
(the RFC 7515 Appendix A.3 vector). Keys are public JWKs (``kty`` EC, ``crv`` P-256). Built on
`cryptography` alone (D12): no JOSE library.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

ALG = "ES256"
HEADER_MEMBERS = frozenset({"alg", "kid", "typ"})
_COORD = 32  # P-256 coordinate and signature-half length, octets
_B64URL = re.compile(r"[A-Za-z0-9_-]*")


class JwsError(ValueError):
    """The JWS is malformed, uses another algorithm or does not verify: never accepted."""


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict base64url without padding (RFC 7515 §2): only the URL alphabet, and the canonical
    encoding of what it decodes to (no stray low bits), so one value has one spelling."""
    if not isinstance(text, str) or not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise JwsError("not base64url")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise JwsError("not base64url") from exc
    if b64url_encode(data) != text:
        raise JwsError("not canonical base64url")
    return data


def _members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise JwsError(f"duplicate member {key!r}")
        out[key] = value
    return out


def _json_object(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_members)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JwsError(f"{what} is not JSON") from exc
    if not isinstance(value, dict):
        raise JwsError(f"{what} is not a JSON object")
    return value


def thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638 thumbprint (SHA-256, base64url) of an EC public JWK: the ``kid`` we publish."""
    public_key(jwk)  # only a valid P-256 key has a thumbprint here
    required = {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]}
    canonical = json.dumps(required, separators=(",", ":"), sort_keys=True).encode("ascii")
    return b64url_encode(hashlib.sha256(canonical).digest())


def public_key(jwk: Mapping[str, Any]) -> ec.EllipticCurvePublicKey:
    """The P-256 public key of a JWK; anything else is refused (other curves, RSA, symmetric)."""
    if not isinstance(jwk, Mapping) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise JwsError("the key is not an EC P-256 JWK")
    coords = []
    for name in ("x", "y"):
        value = jwk.get(name)
        if not isinstance(value, str):
            raise JwsError(f"the key has no {name}")
        raw = b64url_decode(value)
        if len(raw) != _COORD:
            raise JwsError(f"the key's {name} is not {_COORD} octets")
        coords.append(int.from_bytes(raw, "big"))
    try:
        return ec.EllipticCurvePublicNumbers(coords[0], coords[1], ec.SECP256R1()).public_key()
    except ValueError as exc:
        raise JwsError("the key is not a point on P-256") from exc


def header(segment: str) -> dict[str, Any]:
    """The protected header, checked: ``alg`` exactly `ALG`, only `HEADER_MEMBERS` (so ``crit``,
    ``jku``, ``jwk``, ``x5u``... are refused: no extension is understood, no key comes from the
    token)."""
    value = _json_object(b64url_decode(segment), "the protected header")
    unknown = sorted(set(value) - HEADER_MEMBERS)
    if unknown:
        raise JwsError(f"header members not accepted: {', '.join(map(repr, unknown))}")
    if value.get("alg") != ALG:  # never taken from the token: one fixed algorithm (D12)
        raise JwsError(f"alg must be {ALG!r}")
    for name in ("kid", "typ"):
        if name in value and not isinstance(value[name], str):
            raise JwsError(f"{name} must be a string")
    return value


def signing_input(header_segment: str, payload: bytes) -> bytes:
    """RFC 7515 §5.1: ASCII(BASE64URL(header) || '.' || BASE64URL(payload))."""
    return f"{header_segment}.{b64url_encode(payload)}".encode("ascii")


def raw_signature(der: bytes) -> bytes:
    """A DER ECDSA signature (what KMS and `cryptography` produce) as JWS ``R || S``."""
    r, s = decode_dss_signature(der)
    return r.to_bytes(_COORD, "big") + s.to_bytes(_COORD, "big")


def detached(header_value: Mapping[str, Any], signature_der: bytes) -> str:
    """Assemble a detached compact JWS (``header..signature``) from the header the signer signed
    (``signing_input(b64url(json(header)), payload)``) and its DER signature."""
    segment = encode_header(header_value)
    return f"{segment}..{b64url_encode(raw_signature(signature_der))}"


def encode_header(header_value: Mapping[str, Any]) -> str:
    """The header segment; a header the verifier would refuse is refused here too."""
    segment = b64url_encode(json.dumps(dict(header_value), separators=(",", ":")).encode("utf-8"))
    header(segment)
    return segment


def _check(header_segment: str, payload: bytes, signature: str, jwk: Mapping[str, Any]) -> None:
    head = header(header_segment)
    key = public_key(jwk)
    if "kid" in head and head["kid"] != thumbprint(jwk):
        raise JwsError("kid does not name this key")
    raw = b64url_decode(signature)
    if len(raw) != 2 * _COORD:
        raise JwsError(f"an {ALG} signature is {2 * _COORD} octets (R || S)")
    der = encode_dss_signature(
        int.from_bytes(raw[:_COORD], "big"), int.from_bytes(raw[_COORD:], "big")
    )
    try:
        key.verify(der, signing_input(header_segment, payload), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise JwsError("the signature does not verify") from exc


def _segments(token: str) -> list[str]:
    if not isinstance(token, str):
        raise JwsError("a compact JWS is text")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise JwsError("a compact JWS has three segments")
    return parts


def verify_compact(token: str, jwk: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    """An ordinary compact JWS: its header and payload, or `JwsError`."""
    head, body, signature = _segments(token)
    payload = b64url_decode(body)
    _check(head, payload, signature, jwk)
    return header(head), payload


def verify_detached(token: str, payload: bytes, jwk: Mapping[str, Any]) -> dict[str, Any]:
    """A compact JWS with a detached payload (empty middle segment) over ``payload``: its header,
    or `JwsError`. A token that carries its own payload is refused (it would be signing something
    other than what the verifier was given)."""
    head, body, signature = _segments(token)
    if body:
        raise JwsError("the payload must be detached (an empty middle segment)")
    _check(head, payload, signature, jwk)
    return header(head)
