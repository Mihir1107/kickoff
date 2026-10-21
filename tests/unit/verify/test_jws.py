"""The verifier's JWS (M18 D12): one fixed algorithm (ES256), any other header `alg` refused, the
RFC 7515 test vectors, `cryptography` only."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from edisc_verify import jws

# RFC 7515 Appendix A (rfc-editor.org/rfc/rfc7515.txt, SHA-256 ecbf8467...2e40), copied verbatim
A3_JWK = {
    "kty": "EC",
    "crv": "P-256",
    "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
    "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
}  # fmt: skip  (A.3.1; the private "d" is not needed to verify)
PAYLOAD = (
    "eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ"
)
A3 = (
    "eyJhbGciOiJFUzI1NiJ9."
    + PAYLOAD
    + ".DtEhU3ljbEg8L38VWAfUAqOyKAM6-Xx-F4GawxaepmXFCgfTjDxw5djxLa8ISlSA"
    "pmWQxfKTUJqPP3-Kg6NU1Q"
)
A2_RS256 = (
    "eyJhbGciOiJSUzI1NiJ9."
    + PAYLOAD
    + ".cC4hiUPoj9Eetdgtv3hF80EGrhuB__dzERat0XF9g2VtQgr9PJbu3XOiZj5RZmh7"
    "AAuHIm4Bh-0Qc_lF5YKt_O8W2Fp5jujGbds9uJdbF9CUAr7t1dnZcAcQjbKBYNX4"
    "BAynRFdiuB--f_nZLgrnbyTyWzO75vRK5h6xBArLIARNPvkSjtQBMHlb1L07Qe7K"
    "0GarZRmB_eSN9383LcOLn6_dO--xi12jzDwusC-eOkHWEsqtFZESc6BfI7noOPqv"
    "hJ1phCnvWh6IeYI2w9QOYEUipUTI8np6LbgGY9Fs98rqVt5AXLIhWkWywlVmtVrB"
    "p0igcN_IoypGlUPQGe77Rw"
)
A4_ES512 = (
    "eyJhbGciOiJFUzUxMiJ9.UGF5bG9hZA."
    "AdwMgeerwtHoh-l192l60hp9wAHZFVJbLfD_UxMi70cwnZOYaRI1bKPWROc-mZZq"
    "wqT2SI-KGDKB34XO0aw_7XdtAG8GaSwFKdCAPZgoXD2YBJZCPEX3xKpRwcdOO8Kp"
    "EHwJjyqOgzDO7iKvU8vcnwNrmxYbSW9ERBXukOXolLzeO_Jn"
)
A5_NONE = "eyJhbGciOiJub25lIn0." + PAYLOAD + "."


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _segment(head: dict[str, Any] | str) -> str:
    return _b64((head if isinstance(head, str) else json.dumps(head)).encode())


def test_rfc7515_a3_es256_verifies() -> None:
    head, payload = jws.verify_compact(A3, A3_JWK)
    assert head == {"alg": "ES256"}
    assert json.loads(payload) == {
        "iss": "joe", "exp": 1300819380, "http://example.com/is_root": True
    }  # fmt: skip


@pytest.mark.parametrize(
    "token", [A2_RS256, A4_ES512, A5_NONE], ids=["A.2 RS256", "A.4 ES512", "A.5 none"]
)
def test_rfc7515_vectors_of_other_algorithms_are_refused_by_their_header(token: str) -> None:
    with pytest.raises(jws.JwsError, match="alg must be 'ES256'"):
        jws.verify_compact(token, A3_JWK)


def test_the_a3_vector_tampered_does_not_verify() -> None:
    head, body, sig = A3.split(".")
    other = _b64(b'{"iss":"joe","exp":1300819380,"http://example.com/is_root":false}')
    for token in (f"{head}.{other}.{sig}", f"{head}.{body}.{sig[:-2]}AA"):
        with pytest.raises(jws.JwsError):
            jws.verify_compact(token, A3_JWK)


@pytest.fixture
def signer() -> tuple[ec.EllipticCurvePrivateKey, dict[str, str]]:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "x": _b64(numbers.x.to_bytes(32, "big")),
           "y": _b64(numbers.y.to_bytes(32, "big"))}  # fmt: skip
    return key, jwk


def _sign(key: ec.EllipticCurvePrivateKey, head: dict[str, Any] | str, payload: bytes) -> str:
    segment = _segment(head)
    der = key.sign(jws.signing_input(segment, payload), ec.ECDSA(hashes.SHA256()))
    return f"{segment}..{_b64(jws.raw_signature(der))}"


def test_a_detached_signature_round_trips(
    signer: tuple[ec.EllipticCurvePrivateKey, dict[str, str]],
) -> None:
    key, jwk = signer
    payload = b"\x00" + bytes(range(256)) * 3
    head = {"alg": "ES256", "kid": jws.thumbprint(jwk), "typ": "edisc-certificate+jws"}
    der = key.sign(jws.signing_input(jws.encode_header(head), payload), ec.ECDSA(hashes.SHA256()))
    token = jws.detached(head, der)
    assert jws.verify_detached(token, payload, jwk) == head
    with pytest.raises(jws.JwsError, match="does not verify"):
        jws.verify_detached(token, payload + b"x", jwk)
    other = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    wrong = {**jwk, "x": _b64(other.x.to_bytes(32, "big")), "y": _b64(other.y.to_bytes(32, "big"))}
    with pytest.raises(jws.JwsError, match="kid does not name this key"):
        jws.verify_detached(token, payload, wrong)
    no_kid = _sign(key, {"alg": "ES256"}, payload)
    with pytest.raises(jws.JwsError, match="does not verify"):
        jws.verify_detached(no_kid, payload, wrong)


@pytest.mark.parametrize(
    "head",
    [
        {"alg": "none"},
        {"alg": "RS256"},
        {"alg": "HS256"},
        {"alg": "ES384"},
        {"alg": "es256"},
        {"alg": "ES256 "},
        {"alg": ["ES256"]},
        {},
        {"typ": "JWT"},
        '{"alg":"ES256","alg":"none"}',
        '{"alg":"none","alg":"ES256"}',
        {"alg": "ES256", "crit": ["exp"], "exp": 1},
        {"alg": "ES256", "jku": "https://x"},
        {"alg": "ES256", "jwk": {}},
        {"alg": "ES256", "kid": 7},
        '["ES256"]',
        "not json",
    ],
    ids=lambda h: str(h)[:40],
)
def test_any_header_but_exactly_es256_is_refused_even_with_a_valid_signature(
    signer: tuple[ec.EllipticCurvePrivateKey, dict[str, str]], head: dict[str, Any] | str
) -> None:
    """The signature is genuine (made by the right key over that exact header): the header alone
    decides the refusal, before any key is used."""
    key, jwk = signer
    token = _sign(key, head, b"payload")
    with pytest.raises(jws.JwsError):
        jws.verify_detached(token, b"payload", jwk)


def test_malformed_tokens_and_keys_are_refused(
    signer: tuple[ec.EllipticCurvePrivateKey, dict[str, str]],
) -> None:
    key, jwk = signer
    good = _sign(key, {"alg": "ES256"}, b"p")
    head, _, sig = good.split(".")
    assert jws.verify_detached(good, b"p", jwk) == {"alg": "ES256"}
    bad_tokens = [
        f"{head}.{_b64(b'p')}.{sig}",  # payload not detached
        f"{head}..{sig}=",  # padding
        f"{head}..{sig}+",  # not the URL alphabet
        f"{head}..{sig[:-4]}",  # short signature
        f"{head}..{sig}AAAA",  # long signature
        f"{head}.{sig}",  # two segments
        f"{head}...{sig}",  # four segments
        f"{head}..{sig[:-1]}" + ("B" if sig[-1] == "A" else "A"),  # flipped
    ]
    for token in bad_tokens:
        with pytest.raises(jws.JwsError):
            jws.verify_detached(token, b"p", jwk)
    with pytest.raises(jws.JwsError, match="not canonical"):
        jws.b64url_decode("AB")  # "AB" decodes to one byte whose canonical spelling is "AA"
    for bad_key in (
        {**jwk, "crv": "P-384"}, {**jwk, "kty": "RSA"}, {"kty": "oct", "k": "AAAA"},
        {**jwk, "x": jwk["x"][:-2]}, {k: v for k, v in jwk.items() if k != "y"},
        {**jwk, "y": jwk["x"]},  # not on the curve
    ):  # fmt: skip
        with pytest.raises(jws.JwsError):
            jws.verify_detached(good, b"p", bad_key)
    with pytest.raises(jws.JwsError):
        jws.encode_header({"alg": "RS256"})  # the signer helper refuses what the verifier does


def test_rfc7638_thumbprint() -> None:
    """RFC 7638 §3.1 defines the input; checked against the canonical members of the A.3 key."""
    expected = _b64(
        hashlib.sha256(
            b'{"crv":"P-256","kty":"EC","x":"f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",'
            b'"y":"x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0"}'
        ).digest()
    )
    assert jws.thumbprint({**A3_JWK, "use": "sig"}) == expected


def test_a_crit_header_is_refused_as_such(
    signer: tuple[ec.EllipticCurvePrivateKey, dict[str, str]],
) -> None:
    """RFC 7515 §4.1.11: a recipient that does not understand a critical extension must reject."""
    key, jwk = signer
    for head in ({"alg": "ES256", "crit": ["b64"], "b64": False}, {"alg": "ES256", "crit": []}):
        with pytest.raises(jws.JwsError, match="crit"):
            jws.verify_detached(_sign(key, head, b"p"), b"p", jwk)
