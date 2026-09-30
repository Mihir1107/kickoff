"""Local KMS stub has AWS KMS encryption-context semantics; envelope binds tenant/connection/purpose."""

import base64
import json
import stat
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

from edisc_core.envelope import (
    DecryptionError,
    SecretBox,
    SecretContext,
    SecretPurpose,
    blob_key_info,
)
from edisc_core.kms import InvalidCiphertextError, KmsError, LocalKmsClient
from edisc_core.redaction import REDACTED, clear_registered_secrets, redact_text
from edisc_core.settings import Environment, Settings

TOKEN = "xoxb-UNIT-CANARY-0123456789abcdef"
T1, T2 = uuid.uuid4(), uuid.uuid4()
C1, C2 = uuid.uuid4(), uuid.uuid4()
CTX = {"tenant_id": str(T1), "connection_id": str(C1), "purpose": "connection.access_token"}


@pytest.fixture
def kms(tmp_path: Path) -> LocalKmsClient:
    client = LocalKmsClient(Settings(_env_file=None, env=Environment.CI), directory=tmp_path)  # type: ignore[call-arg]
    client.create_key("local:t1")
    return client


def ctx(
    tenant: uuid.UUID = T1,
    conn: uuid.UUID = C1,
    purpose: SecretPurpose = SecretPurpose.CONNECTION_ACCESS_TOKEN,
) -> SecretContext:
    return SecretContext(tenant_id=tenant, connection_id=conn, purpose=purpose)


# ------------------------------------------------------------------ KMS semantics
async def test_encryption_context_must_match_exactly(kms: LocalKmsClient) -> None:
    dk = await kms.generate_data_key(key_id="local:t1", encryption_context=CTX)
    assert (
        await kms.decrypt(ciphertext_blob=dk.ciphertext_blob, encryption_context=dict(CTX))
        == dk.plaintext
    )
    for bad in (
        {**CTX, "purpose": "connection.refresh_token"},  # different value
        {k: v for k, v in CTX.items() if k != "purpose"},  # missing pair
        {**CTX, "extra": "x"},  # extra pair
    ):
        with pytest.raises(InvalidCiphertextError):
            await kms.decrypt(ciphertext_blob=dk.ciphertext_blob, encryption_context=bad)


async def test_tampered_or_unknown_ciphertext_is_indistinguishable(kms: LocalKmsClient) -> None:
    dk = await kms.generate_data_key(key_id="local:t1", encryption_context=CTX)
    doc = json.loads(dk.ciphertext_blob)
    doc["sealed"] = base64.b64encode(b"\x00" + base64.b64decode(doc["sealed"])[1:]).decode()
    for blob in (
        json.dumps(doc).encode(),
        b"garbage",
        dk.ciphertext_blob.replace(b"local:t1", b"local:zz"),
    ):
        with pytest.raises(
            InvalidCiphertextError, match="cannot be decrypted with this encryption context"
        ):
            await kms.decrypt(ciphertext_blob=blob, encryption_context=CTX)


async def test_context_values_must_be_strings(kms: LocalKmsClient) -> None:
    with pytest.raises(KmsError, match="strings"):
        await kms.generate_data_key(key_id="local:t1", encryption_context={"n": 1})  # type: ignore[dict-item]
    with pytest.raises(KmsError, match="required"):
        await kms.generate_data_key(key_id="local:t1", encryption_context={})


async def test_rotation_and_reencrypt(kms: LocalKmsClient) -> None:
    old = await kms.generate_data_key(key_id="local:t1", encryption_context=CTX)
    assert old.key_version == "1"
    assert kms.rotate("local:t1") == "2"
    assert (
        await kms.generate_data_key(key_id="local:t1", encryption_context=CTX)
    ).key_version == "2"
    assert (
        await kms.decrypt(ciphertext_blob=old.ciphertext_blob, encryption_context=CTX)
        == old.plaintext
    )  # old still works
    moved = await kms.re_encrypt(
        ciphertext_blob=old.ciphertext_blob,
        source_encryption_context=CTX,
        destination_key_id="local:t1",
        destination_encryption_context=CTX,
    )
    assert moved.key_version == "2"
    kms.disable_version("local:t1", "1")
    with pytest.raises(InvalidCiphertextError):
        await kms.decrypt(ciphertext_blob=old.ciphertext_blob, encryption_context=CTX)
    assert (
        await kms.decrypt(ciphertext_blob=moved.ciphertext_blob, encryption_context=CTX)
        == old.plaintext
    )


def test_local_kms_refuses_non_disposable_envs(tmp_path: Path) -> None:
    with pytest.raises(KmsError, match="local/ci only"):
        LocalKmsClient(Settings(_env_file=None, env=Environment.PRODUCTION), directory=tmp_path)  # type: ignore[call-arg]


def test_key_files_are_private_and_repr_hides_material(kms: LocalKmsClient, tmp_path: Path) -> None:
    for f in tmp_path.glob("*.json"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


async def test_datakey_repr_hides_plaintext(kms: LocalKmsClient) -> None:
    dk = await kms.generate_data_key(key_id="local:t1", encryption_context=CTX)
    assert "redacted" in repr(dk)
    assert dk.plaintext.hex() not in repr(dk)


# ------------------------------------------------------------------ envelope
async def test_seal_open_roundtrip_and_blob_has_no_plaintext(kms: LocalKmsClient) -> None:
    box = SecretBox(kms)
    sealed = await box.seal(SecretStr(TOKEN), key_id="local:t1", context=ctx())
    assert TOKEN.encode() not in sealed.blob
    assert blob_key_info(sealed.blob) == ("local:t1", "1")
    assert (await box.open(sealed.blob, context=ctx())).get_secret_value() == TOKEN


@pytest.mark.parametrize(
    "other",
    [ctx(tenant=T2), ctx(conn=C2), ctx(purpose=SecretPurpose.CONNECTION_REFRESH_TOKEN)],
    ids=["other-tenant", "other-connection", "other-purpose"],
)
async def test_blob_is_bound_to_tenant_connection_and_purpose(
    kms: LocalKmsClient, other: SecretContext
) -> None:
    box = SecretBox(kms)
    sealed = await box.seal(SecretStr(TOKEN), key_id="local:t1", context=ctx())
    with pytest.raises(DecryptionError) as exc:
        await box.open(sealed.blob, context=other)
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(exc.value.__cause__)


async def test_data_aad_binds_even_if_kms_context_were_bypassed(kms: LocalKmsClient) -> None:
    """Splice: a valid wrapped DEK for context B with the data ciphertext of context A must not open."""
    box = SecretBox(kms)
    a = json.loads((await box.seal(SecretStr(TOKEN), key_id="local:t1", context=ctx())).blob)
    b = json.loads(
        (
            await box.seal(SecretStr("other-secret-value"), key_id="local:t1", context=ctx(conn=C2))
        ).blob
    )
    spliced = json.dumps({**b, "nonce": a["nonce"], "ct": a["ct"]}).encode()
    with pytest.raises(DecryptionError):
        await box.open(spliced, context=ctx(conn=C2))


async def test_rewrap_keeps_data_ciphertext_and_moves_key_version(kms: LocalKmsClient) -> None:
    box = SecretBox(kms)
    sealed = await box.seal(SecretStr(TOKEN), key_id="local:t1", context=ctx())
    kms.rotate("local:t1")
    rewrapped = await box.rewrap(sealed.blob, context=ctx())
    before, after = json.loads(sealed.blob), json.loads(rewrapped.blob)
    assert (after["key_version"], after["ct"], after["nonce"]) == (
        "2",
        before["ct"],
        before["nonce"],
    )
    assert after["wrapped_dek"] != before["wrapped_dek"]
    kms.disable_version("local:t1", "1")
    assert (await box.open(rewrapped.blob, context=ctx())).get_secret_value() == TOKEN
    with pytest.raises(DecryptionError):
        await box.open(sealed.blob, context=ctx())


async def test_sealed_and_opened_secrets_are_redacted_from_logs(kms: LocalKmsClient) -> None:
    clear_registered_secrets()
    box = SecretBox(kms)
    await box.seal(SecretStr("opaque-provider-token-no-pattern"), key_id="local:t1", context=ctx())
    assert redact_text("token=opaque-provider-token-no-pattern") == f"token={REDACTED}"
    clear_registered_secrets()


class _ContextBlindKms:
    """A KMS that does NOT enforce encryption context (e.g. a misconfigured or compromised one)."""

    def __init__(self) -> None:
        self._keys: dict[bytes, bytes] = {}

    async def generate_data_key(self, *, key_id: str, encryption_context: dict[str, str]) -> object:
        import os

        from edisc_core.kms import DataKey

        dek, handle = os.urandom(32), os.urandom(16)
        self._keys[handle] = dek
        return DataKey(dek, handle, key_id, "1")

    async def decrypt(self, *, ciphertext_blob: bytes, encryption_context: dict[str, str]) -> bytes:
        return self._keys[ciphertext_blob]  # ignores the context entirely

    async def re_encrypt(self, **_: object) -> object:
        raise NotImplementedError


async def test_data_aad_binds_context_even_if_kms_does_not() -> None:
    """Defense in depth: with a context-blind KMS, only the GCM AAD stops a blob opening under another
    tenant/connection/purpose."""
    box = SecretBox(_ContextBlindKms())  # type: ignore[arg-type]
    sealed = await box.seal(SecretStr(TOKEN), key_id="k", context=ctx())
    assert (await box.open(sealed.blob, context=ctx())).get_secret_value() == TOKEN
    for other in (
        ctx(tenant=T2),
        ctx(conn=C2),
        ctx(purpose=SecretPurpose.CONNECTION_REFRESH_TOKEN),
    ):
        with pytest.raises(DecryptionError):
            await box.open(sealed.blob, context=other)
