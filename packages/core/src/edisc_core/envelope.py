"""Envelope encryption for tenant secrets (connection access and refresh tokens).

    DEK   = KMS.GenerateDataKey(tenant KEK, encryption_context)      one fresh DEK per sealed secret
    blob  = {v, alg, key_id, key_version, wrapped_dek, nonce, ct}    canonical JSON, stored in Postgres
    ct    = AES-256-GCM(DEK, nonce, plaintext, AAD = canonical_json(context))

The same context (tenant_id, connection_id, purpose) is bound twice: as the KMS encryption context of
the wrapped DEK, and as the GCM AAD of the data. A blob copied to another tenant's or connection's row,
or from the refresh-token column to the access-token column, cannot be opened.

Rotation (``rewrap``) calls KMS ReEncrypt on the wrapped DEK only: the data ciphertext is untouched and
no plaintext (DEK or secret) enters this process.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from edisc_core.canonical import canonical_json
from edisc_core.kms import InvalidCiphertextError, KmsClient
from edisc_core.redaction import MIN_SECRET_LENGTH, register_secret

BLOB_VERSION = 1
ALG = "AES-256-GCM"


class SecretPurpose(StrEnum):
    CONNECTION_ACCESS_TOKEN = "connection.access_token"  # noqa: S105 - a label, not a secret
    CONNECTION_REFRESH_TOKEN = "connection.refresh_token"  # noqa: S105 - a label, not a secret


class DecryptionError(RuntimeError):
    """Opaque on purpose: never says which part failed and never includes secret material."""


@dataclass(frozen=True)
class SecretContext:
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    purpose: SecretPurpose

    def as_encryption_context(self) -> dict[str, str]:
        return {
            "tenant_id": str(self.tenant_id),
            "connection_id": str(self.connection_id),
            "purpose": self.purpose.value,
        }

    def aad(self) -> bytes:
        return canonical_json(self.as_encryption_context())


@dataclass(frozen=True)
class SealedSecret:
    blob: bytes
    key_id: str
    key_version: str


def _parse(blob: bytes) -> dict[str, Any]:
    try:
        doc: dict[str, Any] = json.loads(blob)
    except ValueError as exc:
        raise DecryptionError("secret blob is not readable") from exc
    if doc.get("v") != BLOB_VERSION or doc.get("alg") != ALG:
        raise DecryptionError("unsupported secret blob version")
    return doc


def blob_key_info(blob: bytes) -> tuple[str, str]:
    """(key_id, key_version) recorded in a blob; lets rotation find stale rows without decrypting."""
    doc = _parse(blob)
    return str(doc["key_id"]), str(doc["key_version"])


class SecretBox:
    def __init__(self, kms: KmsClient) -> None:
        self._kms = kms

    async def seal(
        self, plaintext: SecretStr, *, key_id: str, context: SecretContext
    ) -> SealedSecret:
        value = plaintext.get_secret_value()
        if len(value) >= MIN_SECRET_LENGTH:
            register_secret(value)  # anything we ever seal is scrubbed from this process's logs
        data_key = await self._kms.generate_data_key(
            key_id=key_id, encryption_context=context.as_encryption_context()
        )
        nonce = os.urandom(12)
        ct = AESGCM(data_key.plaintext).encrypt(
            nonce, plaintext.get_secret_value().encode("utf-8"), context.aad()
        )
        blob = canonical_json(
            {
                "v": BLOB_VERSION,
                "alg": ALG,
                "key_id": data_key.key_id,
                "key_version": data_key.key_version,
                "wrapped_dek": base64.b64encode(data_key.ciphertext_blob).decode(),
                "nonce": base64.b64encode(nonce).decode(),
                "ct": base64.b64encode(ct).decode(),
            }
        )
        return SealedSecret(blob, data_key.key_id, data_key.key_version)

    async def open(self, blob: bytes, *, context: SecretContext) -> SecretStr:
        """Decrypt; the plaintext is registered with the log redactor before it is returned."""
        doc = _parse(blob)
        try:
            dek = await self._kms.decrypt(
                ciphertext_blob=base64.b64decode(doc["wrapped_dek"]),
                encryption_context=context.as_encryption_context(),
            )
            raw = AESGCM(dek).decrypt(
                base64.b64decode(doc["nonce"]), base64.b64decode(doc["ct"]), context.aad()
            )
        except (InvalidCiphertextError, InvalidTag, KeyError, ValueError) as exc:
            raise DecryptionError(
                f"cannot decrypt {context.purpose.value} for connection {context.connection_id}"
            ) from exc
        value = raw.decode("utf-8")
        if len(value) >= MIN_SECRET_LENGTH:
            register_secret(value)
        return SecretStr(value)

    async def rewrap(
        self, blob: bytes, *, context: SecretContext, destination_key_id: str | None = None
    ) -> SealedSecret:
        """Re-wrap the DEK under the destination key's CURRENT version via KMS ReEncrypt. The data
        ciphertext and nonce are unchanged; no plaintext DEK or secret is handled here."""
        doc = _parse(blob)
        ctx = context.as_encryption_context()
        try:
            wrapped = await self._kms.re_encrypt(
                ciphertext_blob=base64.b64decode(doc["wrapped_dek"]),
                source_encryption_context=ctx,
                destination_key_id=destination_key_id or str(doc["key_id"]),
                destination_encryption_context=ctx,
            )
        except InvalidCiphertextError as exc:
            raise DecryptionError(
                f"cannot rewrap {context.purpose.value} for connection {context.connection_id}"
            ) from exc
        new_blob = canonical_json(
            {
                **doc,
                "key_id": wrapped.key_id,
                "key_version": wrapped.key_version,
                "wrapped_dek": base64.b64encode(wrapped.ciphertext_blob).decode(),
            }
        )
        return SealedSecret(new_blob, wrapped.key_id, wrapped.key_version)
