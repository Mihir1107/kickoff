"""The client-assertion signer against a FIXED key and clock (M17 step 3b; `edisc_api.client_auth`).

The key is derived from a seed (deterministic primes), so no private key is committed and every
run signs with the same key: the assertion and the certificate are byte-identical across runs
(PKCS#1 v1.5 signatures are deterministic), which is what lets every replica publish one
certificate. Expected values come from the RFCs and from PyJWT / cryptography decoding the output,
never from the signer itself."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtensionOID, NameOID

from edisc_api import client_auth
from edisc_api.client_auth import ClientAuthError, ClientSigner
from edisc_core.kms import KmsError, LocalKmsClient
from edisc_core.settings import ClientKeySpec, Environment, Settings

KEY_ID = "platform/client-auth/unit"
SPEC = ClientKeySpec(
    kms_key_id=KEY_ID,
    not_before=datetime(2026, 10, 1, tzinfo=UTC),
    not_after=datetime(2028, 10, 1, tzinfo=UTC),
)
NOW = 1_792_000_000
AUD = "https://login.example.test/tenant/oauth2/v2.0/token"


def _probable_prime(rng: random.Random, bits: int) -> int:
    def miller_rabin(n: int) -> bool:
        d, r = n - 1, 0
        while d % 2 == 0:
            d, r = d // 2, r + 1
        for _ in range(40):
            x = pow(rng.randrange(2, n - 1), d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                return False
        return True

    small = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p**0.5) + 1, 2))]
    while True:
        n = rng.getrandbits(bits) | (3 << (bits - 2)) | 1
        if all(n % p for p in small) and (n - 1) % 65537 and miller_rabin(n):
            return n


@pytest.fixture(scope="module")
def fixed_key() -> rsa.RSAPrivateKey:
    rng = random.Random("edisc client-auth unit key")
    p, q = _probable_prime(rng, 1536), _probable_prime(rng, 1536)
    e, n = 65537, p * q
    d = pow(e, -1, (p - 1) * (q - 1))
    numbers = rsa.RSAPrivateNumbers(
        p, q, d, d % (p - 1), d % (q - 1), pow(q, -1, p), rsa.RSAPublicNumbers(e, n)
    )
    return numbers.private_key()


@pytest.fixture
def kms(tmp_path: Path, fixed_key: rsa.RSAPrivateKey) -> LocalKmsClient:
    client = LocalKmsClient(Settings(env=Environment.TEST, local_kms_dir=tmp_path))
    client.import_signing_key(KEY_ID, fixed_key)
    return client


@pytest.fixture
def signer(kms: LocalKmsClient) -> ClientSigner:
    return ClientSigner(kms, (SPEC,), clock=lambda: NOW)


def run[T](coro: object) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_the_assertion_carries_exactly_the_claims_entra_and_okta_require(
    signer: ClientSigner, fixed_key: rsa.RSAPrivateKey
) -> None:
    token: str = run(signer.assertion(client_id="client-1", audience=AUD, jti="jti-1"))
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        token, fixed_key.public_key(), algorithms=["RS256"], audience=AUD,
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )  # fmt: skip
    assert claims == {
        "iss": "client-1", "sub": "client-1", "aud": AUD, "jti": "jti-1", "iat": NOW,
        "nbf": NOW, "exp": NOW + 300,
    }  # fmt: skip
    keys = run(signer.keys())
    der = keys[0].certificate_der
    assert header == {
        "alg": "RS256", "typ": "JWT", "kid": keys[0].kid,
        "x5t": base64.urlsafe_b64encode(hashlib.sha1(der, usedforsecurity=False).digest())
        .rstrip(b"=").decode(),
        "x5t#S256": base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode(),
    }  # fmt: skip
    # header and claims are RFC 8785 JSON: sorted keys, no whitespace
    raw_claims = base64.urlsafe_b64decode(token.split(".")[1] + "==")
    assert raw_claims.startswith(b'{"aud":"') and b" " not in raw_claims


def test_the_assertion_is_deterministic_for_one_key_clock_and_jti(
    signer: ClientSigner, kms: LocalKmsClient
) -> None:
    a = run(signer.assertion(client_id="c", audience=AUD, jti="j"))
    b = run(
        ClientSigner(kms, (SPEC,), clock=lambda: NOW).assertion(
            client_id="c", audience=AUD, jti="j"
        )
    )
    assert a == b
    fresh = {run(signer.assertion(client_id="c", audience=AUD)).split(".")[1] for _ in range(3)}
    assert len(fresh) == 3  # a random jti per assertion by default


def test_the_form_names_the_jwt_bearer_assertion_type(signer: ClientSigner) -> None:
    form = run(signer.form(client_id="c", audience=AUD))
    assert set(form) == {"client_assertion_type", "client_assertion"}
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    assert jwt.decode(form["client_assertion"], options={"verify_signature": False})["aud"] == AUD


def test_the_kid_is_the_rfc_7638_thumbprint() -> None:
    # RFC 7638 §3.1, the example key and its thumbprint
    n = (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECP"
        "ebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY"
        "368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0f"
        "M4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
    )
    assert client_auth.jwk_thumbprint(n, "AQAB") == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_the_certificate_is_self_signed_by_the_kms_key_with_fixed_contents(
    signer: ClientSigner, fixed_key: rsa.RSAPrivateKey
) -> None:
    (key,) = run(signer.keys())
    cert = x509.load_der_x509_certificate(key.certificate_der)
    cert.verify_directly_issued_by(cert)
    public_der = fixed_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert (
        cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        == public_der
    )
    cn = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    assert cn == ["edisc client authentication"] and cert.issuer == cert.subject
    assert cert.not_valid_before_utc == SPEC.not_before
    assert cert.not_valid_after_utc == SPEC.not_after
    assert cert.serial_number == int.from_bytes(hashlib.sha256(public_der).digest()[:16]) >> 1
    basic = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS)
    usage = cert.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE)
    assert basic.critical and not basic.value.ca
    assert usage.critical and usage.value.digital_signature and not usage.value.key_cert_sign
    # every signer over the same key and validity builds the same bytes (replicas, restarts)
    again = run(ClientSigner(signer.kms, (SPEC,)).keys())[0]
    assert again.certificate_der == key.certificate_der
    jwk = key.jwk()
    assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e", "x5c", "x5t", "x5t#S256"}


def test_keys_are_published_current_first_and_only_the_first_signs(
    kms: LocalKmsClient, tmp_path: Path
) -> None:
    kms.create_signing_key("platform/client-auth/next")
    nxt = SPEC.model_copy(update={"kms_key_id": "platform/client-auth/next"})
    signer = ClientSigner(kms, (SPEC, nxt), clock=lambda: NOW)
    keys = run(signer.keys())
    assert [k.kms_key_id for k in keys] == [KEY_ID, "platform/client-auth/next"]
    token = run(signer.assertion(client_id="c", audience=AUD))
    assert jwt.get_unverified_header(token)["kid"] == keys[0].kid
    assert len(run(signer.jwks())["keys"]) == 2
    assert run(signer.certificates_pem()).count(b"-----BEGIN CERTIFICATE-----") == 2


def test_a_key_smaller_than_3072_bits_is_refused(kms: LocalKmsClient) -> None:
    kms.import_signing_key("small", rsa.generate_private_key(65537, 2048))
    spec = SPEC.model_copy(update={"kms_key_id": "small"})
    with pytest.raises(ClientAuthError):
        run(ClientSigner(kms, (spec,)).keys())


def test_no_client_key_outside_local_test_ci_refuses_to_start(tmp_path: Path) -> None:
    kms = LocalKmsClient(Settings(env=Environment.TEST, local_kms_dir=tmp_path))
    staging = Settings(env=Environment.TEST, local_kms_dir=tmp_path).model_copy(
        update={"env": Environment.STAGING}
    )
    with pytest.raises(ClientAuthError, match="required outside"):
        ClientSigner.from_settings(staging, kms)
    configured = staging.model_copy(update={"api_client_keys": [SPEC]})
    assert ClientSigner.from_settings(configured, kms).specs == (SPEC,)
    dev = ClientSigner.from_settings(Settings(env=Environment.TEST, local_kms_dir=tmp_path), kms)
    assert dev.specs == (client_auth.DEV_KEY,) and dev.create is not None
    with pytest.raises(ClientAuthError, match="cannot sign"):
        ClientSigner.from_settings(configured, object())


def test_the_kms_signs_only_rs256_over_a_sha256_digest(kms: LocalKmsClient) -> None:
    with pytest.raises(KmsError):
        run(kms.sign(key_id=KEY_ID, digest=b"x" * 32, signing_algorithm="RSASSA_PSS_SHA_256"))
    with pytest.raises(KmsError):
        run(kms.sign(key_id=KEY_ID, digest=b"short", signing_algorithm="RSASSA_PKCS1_V1_5_SHA_256"))
    kms.create_key("symmetric")
    with pytest.raises(KmsError):
        run(kms.get_public_key(key_id="symmetric"))


def test_client_key_validity_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="not_after"):
        ClientKeySpec(kms_key_id="k", not_before=SPEC.not_after, not_after=SPEC.not_before)
