"""KMS abstraction with AWS KMS semantics, and a local stub for dev/CI.

The interface mirrors the three AWS KMS calls envelope encryption needs, with the same semantics, so
``AwsKmsClient`` (backlog) is a thin mapping and no caller changes:

- ``generate_data_key(key_id, encryption_context)`` -> AWS ``GenerateDataKey(KeyId, KeySpec=AES_256,
  EncryptionContext)``: returns a plaintext DEK and the DEK wrapped under the KEK.
- ``decrypt(ciphertext_blob, encryption_context)`` -> AWS ``Decrypt``: the key id is embedded in the
  ciphertext blob; the encryption context must match EXACTLY (no missing, extra or different pairs),
  otherwise ``InvalidCiphertextError`` (AWS: InvalidCiphertextException). Same error for tampering.
- ``re_encrypt(...)`` -> AWS ``ReEncrypt``: re-wraps a DEK under the (current version of the) destination
  key entirely inside KMS. The plaintext DEK never enters our process; this is how rotation works.

Encryption context values are strings (AWS requirement). Context is authenticated, not secret.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings


class KmsError(RuntimeError):
    pass


class InvalidCiphertextError(KmsError):
    """Wrong encryption context, wrong key, disabled key version, or tampered ciphertext."""


class KeyNotFoundError(KmsError):
    pass


@dataclass(frozen=True)
class DataKey:
    plaintext: bytes  # 32 bytes; caller must not log or persist it
    ciphertext_blob: bytes  # the DEK wrapped under the KEK: safe to store
    key_id: str
    key_version: str

    def __repr__(self) -> str:  # never print key material
        return f"DataKey(key_id={self.key_id!r}, key_version={self.key_version!r}, plaintext=<redacted>)"


@dataclass(frozen=True)
class WrappedKey:
    ciphertext_blob: bytes
    key_id: str
    key_version: str


class KmsClient(Protocol):
    async def generate_data_key(
        self, *, key_id: str, encryption_context: Mapping[str, str]
    ) -> DataKey: ...

    async def decrypt(
        self, *, ciphertext_blob: bytes, encryption_context: Mapping[str, str]
    ) -> bytes: ...

    async def re_encrypt(
        self,
        *,
        ciphertext_blob: bytes,
        source_encryption_context: Mapping[str, str],
        destination_key_id: str,
        destination_encryption_context: Mapping[str, str],
    ) -> WrappedKey: ...


def _check_context(ctx: Mapping[str, str]) -> dict[str, str]:
    if not ctx:
        raise KmsError("an encryption context is required")
    for k, v in ctx.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise KmsError("encryption context keys and values must be strings (AWS KMS semantics)")
    return dict(ctx)


class LocalKmsClient:
    """File-backed KMS stub with AWS semantics. Local/CI only: refuses to start anywhere else.

    Each key is ``<dir>/<sha256(key_id)>.json``: ``{"key_id", "current", "versions": {n: {"material",
    "enabled"}}}`` written 0600. Wrapped blobs embed key id + version (like AWS embeds its key reference).
    """

    FORMAT = "edisc-local-kms/1"

    def __init__(self, settings: Settings, directory: Path | None = None) -> None:
        if not settings.env.is_disposable:
            raise KmsError("LocalKmsClient is for local/ci only; use AWS KMS elsewhere")
        self._dir = directory or settings.local_kms_dir
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()

    # -- key management (what an operator/Terraform does in AWS)
    def _path(self, key_id: str) -> Path:
        return self._dir / f"{hashlib.sha256(key_id.encode()).hexdigest()}.json"

    def _load(self, key_id: str) -> dict[str, object]:
        path = self._path(key_id)
        if not path.exists():
            raise KeyNotFoundError(f"KMS key not found: {key_id}")
        data: dict[str, object] = json.loads(path.read_text())
        return data

    def _save(self, key_id: str, data: dict[str, object]) -> None:
        path = self._path(key_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(path)

    def create_key(self, key_id: str) -> None:
        with self._lock:
            if self._path(key_id).exists():
                return
            self._save(
                key_id, {"key_id": key_id, "current": 1, "versions": {"1": self._new_version()}}
            )

    def rotate(self, key_id: str) -> str:
        """New key material becomes current; old versions stay usable for decrypt (like AWS rotation)."""
        with self._lock:
            data = self._load(key_id)
            versions: dict[str, object] = data["versions"]  # type: ignore[assignment]
            new = str(max(int(v) for v in versions) + 1)
            versions[new] = self._new_version()
            data["current"] = int(new)
            self._save(key_id, data)
            return new

    def disable_version(self, key_id: str, version: str) -> None:
        with self._lock:
            data = self._load(key_id)
            versions: dict[str, dict[str, object]] = data["versions"]  # type: ignore[assignment]
            versions[version]["enabled"] = False
            self._save(key_id, data)

    @staticmethod
    def _new_version() -> dict[str, object]:
        return {
            "material": base64.b64encode(AESGCM.generate_key(bit_length=256)).decode(),
            "enabled": True,
        }

    def _material(self, key_id: str, version: str | None) -> tuple[bytes, str]:
        data = self._load(key_id)
        versions: dict[str, dict[str, object]] = data["versions"]  # type: ignore[assignment]
        ver = version or str(data["current"])
        entry = versions.get(ver)
        if entry is None or not entry["enabled"]:
            raise InvalidCiphertextError("key version unavailable")
        return base64.b64decode(str(entry["material"])), ver

    @staticmethod
    def _aad(key_id: str, version: str, ctx: Mapping[str, str]) -> bytes:
        return canonical_json({"key_id": key_id, "key_version": version, "context": dict(ctx)})

    def _wrap(self, key_id: str, dek: bytes, ctx: Mapping[str, str]) -> WrappedKey:
        material, version = self._material(key_id, None)
        nonce = os.urandom(12)
        sealed = AESGCM(material).encrypt(nonce, dek, self._aad(key_id, version, ctx))
        blob = canonical_json(
            {
                "format": self.FORMAT,
                "key_id": key_id,
                "key_version": version,
                "nonce": base64.b64encode(nonce).decode(),
                "sealed": base64.b64encode(sealed).decode(),
            }
        )
        return WrappedKey(blob, key_id, version)

    def _unwrap(self, blob: bytes, ctx: Mapping[str, str]) -> tuple[bytes, str]:
        try:
            doc = json.loads(blob)
            if doc.get("format") != self.FORMAT:
                raise InvalidCiphertextError("unrecognized ciphertext")
            key_id, version = str(doc["key_id"]), str(doc["key_version"])
            material, _ = self._material(key_id, version)
            dek = AESGCM(material).decrypt(
                base64.b64decode(doc["nonce"]),
                base64.b64decode(doc["sealed"]),
                self._aad(key_id, version, ctx),
            )
        except (InvalidTag, ValueError, KeyError, TypeError, KeyNotFoundError) as exc:
            # One opaque error for context mismatch, tampering or unknown key (AWS behaves the same).
            raise InvalidCiphertextError(
                "ciphertext cannot be decrypted with this encryption context"
            ) from exc
        return dek, key_id

    # -- AWS-shaped API
    async def generate_data_key(
        self, *, key_id: str, encryption_context: Mapping[str, str]
    ) -> DataKey:
        ctx = _check_context(encryption_context)
        dek = AESGCM.generate_key(bit_length=256)
        wrapped = self._wrap(key_id, dek, ctx)
        return DataKey(dek, wrapped.ciphertext_blob, wrapped.key_id, wrapped.key_version)

    async def decrypt(
        self, *, ciphertext_blob: bytes, encryption_context: Mapping[str, str]
    ) -> bytes:
        dek, _ = self._unwrap(ciphertext_blob, _check_context(encryption_context))
        return dek

    async def re_encrypt(
        self,
        *,
        ciphertext_blob: bytes,
        source_encryption_context: Mapping[str, str],
        destination_key_id: str,
        destination_encryption_context: Mapping[str, str],
    ) -> WrappedKey:
        # Inside the "KMS" boundary: the DEK is never returned to the caller.
        dek, _ = self._unwrap(ciphertext_blob, _check_context(source_encryption_context))
        return self._wrap(destination_key_id, dek, _check_context(destination_encryption_context))
