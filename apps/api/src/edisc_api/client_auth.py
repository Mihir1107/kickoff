"""Client authentication at an IdP's token endpoint: ``private_key_jwt`` (RFC 7523, OIDC Core §9).

M17 step 3b (``docs/plans/m17.md`` §1.6, ADR 0016 step-3b notes). Entra ID and Okta register a
server-side web application as a CONFIDENTIAL client: the code exchange must authenticate us. We do
it with a signed client assertion, never a client secret (nothing per tenant to store or leak).

- ONE platform key per environment (``EDISC_API_CLIENT_KEYS``, current first), an RSA 3072 KMS key:
  the private part never leaves KMS (``KmsSigner.sign`` over a SHA-256 digest, RS256).
- The assertion: header ``alg`` RS256, ``typ`` JWT, ``kid`` (the RFC 7638 thumbprint of the public
  key), ``x5t`` and ``x5t#S256`` (SHA-1 / SHA-256 of our certificate: Entra matches the uploaded
  certificate by thumbprint); claims ``iss`` = ``sub`` = the tenant's client id, ``aud`` = the token
  endpoint exactly as the IdP's discovery document gives it, a random ``jti``, ``iat`` = ``nbf`` =
  now, ``exp`` = now + 5 minutes. Header and claims are RFC 8785 JSON (deterministic bytes).
- Published for IdP administrators (``routes.wellknown``): the JWKS (Okta takes it or its URL) and a
  self-signed X.509 certificate over the same public key (Entra's app registration takes a
  certificate). The certificate is DETERMINISTIC per key and configured validity (serial from the
  key, fixed names and extensions, PKCS#1 v1.5 signatures are deterministic), so every replica and
  restart publishes the same bytes and the same thumbprints.
- Shared by the Teams client-credentials leg (M17 step 6): one place builds client assertions.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from edisc_core.canonical import canonical_json
from edisc_core.kms import SIGNING_ALGORITHM, KmsSigner
from edisc_core.settings import ClientKeySpec, Settings

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_SECONDS = 300
METHOD = "private_key_jwt"
ALG = "RS256"
MIN_KEY_BITS = 3072
CERT_NAME = "edisc client authentication"
# local/test/ci only, when EDISC_API_CLIENT_KEYS is empty: created in the local KMS on first use
DEV_KEY = ClientKeySpec(
    kms_key_id="platform/client-auth/dev",
    not_before=datetime(2026, 1, 1, tzinfo=UTC),
    not_after=datetime(2036, 1, 1, tzinfo=UTC),
)
# sha256WithRSAEncryption, NULL parameters (RFC 4055): the certificate's signatureAlgorithm
_SHA256_WITH_RSA = bytes.fromhex("300d06092a864886f70d01010b0500")


class ClientAuthError(RuntimeError):
    """The platform key cannot be used (missing, not RSA, too small, not configured)."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64url(value: int) -> str:
    return b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def jwk_thumbprint(n: str, e: str) -> str:
    """RFC 7638: SHA-256 over the required members in lexicographic order, no whitespace (exactly
    RFC 8785 for these string members)."""
    return b64url(hashlib.sha256(canonical_json({"e": e, "kty": "RSA", "n": n})).digest())


def _der_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(content)) + content


def tbs_certificate(public_der: bytes, spec: ClientKeySpec) -> bytes:
    """The TBSCertificate for our public key: fixed names and extensions, serial from the key,
    validity from the configuration. CPU-bound (a throwaway RSA key gives the builder a signer whose
    signature is discarded): run in a thread."""
    public = serialization.load_der_public_key(public_der)
    if not isinstance(public, rsa.RSAPublicKey):
        raise ClientAuthError("the client-authentication key is not RSA")
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_NAME)])
    serial = int.from_bytes(hashlib.sha256(public_der).digest()[:16], "big") >> 1 or 1
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(serial)
        .not_valid_before(spec.not_before)
        .not_valid_after(spec.not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
    )
    throwaway = rsa.generate_private_key(
        65537, 2048
    )  # same algorithm id; its signature is discarded
    return builder.sign(throwaway, hashes.SHA256()).tbs_certificate_bytes


def assemble_certificate(tbs: bytes, signature: bytes) -> bytes:
    """Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm, signatureValue BIT STRING }."""
    return _der(0x30, tbs + _SHA256_WITH_RSA + _der(0x03, b"\x00" + signature))


def check_certificate(cert_der: bytes, public_der: bytes) -> None:
    """The assembled certificate parses, carries our key and verifies as self-signed."""
    cert = x509.load_der_x509_certificate(cert_der)
    if (
        cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        != public_der
    ):
        raise ClientAuthError("certificate key differs from the KMS key")
    cert.verify_directly_issued_by(cert)


@dataclass(frozen=True)
class PublishedKey:
    kms_key_id: str
    kid: str
    n: str
    e: str
    certificate_der: bytes

    @property
    def x5t(self) -> str:
        return b64url(hashlib.sha1(self.certificate_der, usedforsecurity=False).digest())

    @property
    def x5t_s256(self) -> str:
        return b64url(hashlib.sha256(self.certificate_der).digest())

    def jwk(self) -> dict[str, Any]:
        return {
            "kty": "RSA", "use": "sig", "alg": ALG, "kid": self.kid, "n": self.n, "e": self.e,
            "x5c": [base64.b64encode(self.certificate_der).decode()], "x5t": self.x5t,
            "x5t#S256": self.x5t_s256,
        }  # fmt: skip

    def certificate_pem(self) -> bytes:
        cert = x509.load_der_x509_certificate(self.certificate_der)
        return cert.public_bytes(serialization.Encoding.PEM)


@dataclass
class ClientSigner:
    """Builds client assertions with the CURRENT key and publishes every configured key."""

    kms: KmsSigner
    specs: Sequence[ClientKeySpec]
    # local/test/ci with no configured key: create the dev key in the local KMS when first used
    create: Callable[[str], None] | None = None
    clock: Callable[[], float] = time.time
    _keys: list[PublishedKey] | None = field(default=None, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @classmethod
    def from_settings(cls, settings: Settings, kms: object) -> ClientSigner:
        if not (hasattr(kms, "sign") and hasattr(kms, "get_public_key")):
            raise ClientAuthError("the KMS client cannot sign")
        signer = cast(KmsSigner, kms)
        if settings.api_client_keys:
            return cls(signer, tuple(settings.api_client_keys))
        if not settings.env.is_disposable:  # fail closed: no sign-in without client authentication
            raise ClientAuthError("EDISC_API_CLIENT_KEYS is required outside local/test/ci")
        create = getattr(kms, "create_signing_key", None)
        return cls(signer, (DEV_KEY,), create=create)

    async def keys(self) -> list[PublishedKey]:
        if self._keys is not None:
            return self._keys
        async with self._lock:
            if self._keys is None:
                self._keys = [await self._load(spec) for spec in self.specs]
            return self._keys

    async def _load(self, spec: ClientKeySpec) -> PublishedKey:
        if self.create is not None:
            await asyncio.to_thread(self.create, spec.kms_key_id)
        public_der = await self.kms.get_public_key(key_id=spec.kms_key_id)
        public = serialization.load_der_public_key(public_der)
        if not isinstance(public, rsa.RSAPublicKey) or public.key_size < MIN_KEY_BITS:
            raise ClientAuthError(f"the client-authentication key must be RSA >= {MIN_KEY_BITS}")
        numbers = public.public_numbers()
        n, e = _int_b64url(numbers.n), _int_b64url(numbers.e)
        tbs = await asyncio.to_thread(tbs_certificate, public_der, spec)
        signature = await self._sign(spec.kms_key_id, tbs)
        cert = assemble_certificate(tbs, signature)
        check_certificate(cert, public_der)
        return PublishedKey(spec.kms_key_id, jwk_thumbprint(n, e), n, e, cert)

    async def _sign(self, kms_key_id: str, data: bytes) -> bytes:
        return await self.kms.sign(
            key_id=kms_key_id,
            digest=hashlib.sha256(data).digest(),
            signing_algorithm=SIGNING_ALGORITHM,
        )

    async def assertion(
        self, *, client_id: str, audience: str, jti: str | None = None, now: float | None = None
    ) -> str:
        """A client assertion for ONE token request at ``audience`` (the token endpoint)."""
        key = (await self.keys())[0]
        issued = int(self.clock() if now is None else now)
        header = {"alg": ALG, "typ": "JWT", "kid": key.kid, "x5t": key.x5t,
                  "x5t#S256": key.x5t_s256}  # fmt: skip
        claims = {
            "iss": client_id, "sub": client_id, "aud": audience,
            "jti": jti or secrets.token_urlsafe(32), "iat": issued, "nbf": issued,
            "exp": issued + ASSERTION_SECONDS,
        }  # fmt: skip
        signing_input = f"{b64url(canonical_json(header))}.{b64url(canonical_json(claims))}"
        signature = await self._sign(key.kms_key_id, signing_input.encode())
        return f"{signing_input}.{b64url(signature)}"

    async def form(self, *, client_id: str, audience: str) -> Mapping[str, str]:
        """The token request's client-authentication parameters."""
        assertion = await self.assertion(client_id=client_id, audience=audience)
        return {"client_assertion_type": ASSERTION_TYPE, "client_assertion": assertion}

    async def jwks(self) -> dict[str, Any]:
        return {"keys": [key.jwk() for key in await self.keys()]}

    async def certificates_pem(self) -> bytes:
        return b"".join(key.certificate_pem() for key in await self.keys())
