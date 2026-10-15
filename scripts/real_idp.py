"""Prepare a client-authentication key for a REAL IdP developer tenant (M17 step 3b).

    uv run python scripts/real_idp.py prepare --dir ~/edisc-real-idp [--days 365]

Creates (once) an RSA 3072 key in a local KMS directory, fixes the certificate's validity in
``spec.json``, and writes what an IdP administrator registers:

- ``client-cert.pem``: Entra ID > App registrations > the app > Certificates & secrets > Upload
  certificate.
- ``client-jwks.json``: Okta > Applications > the app > Client authentication "Public key / Private
  key" > Add key (paste the key) or "Use a URL to fetch keys dynamically" (the production route
  ``/.well-known/edisc-client-jwks.json`` once the API is public).

Re-running with an existing directory rewrites the same bytes (the certificate is deterministic
for the key and ``spec.json``). Then run ``tests/real_idp`` with ``EDISC_REAL_IDP_KEY_DIR`` set
(see its docstring). This directory holds a PRIVATE KEY: keep it out of the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from edisc_api.client_auth import ClientSigner, PublishedKey
from edisc_core.kms import LocalKmsClient
from edisc_core.settings import ClientKeySpec, Environment, Settings

KEY_ID = "platform/client-auth/real-idp"


def load(directory: Path) -> tuple[LocalKmsClient, ClientKeySpec]:
    directory = directory.expanduser()
    kms = LocalKmsClient(
        Settings(env=Environment.LOCAL, local_kms_dir=directory / "kms"), directory / "kms"
    )
    spec = ClientKeySpec.model_validate_json((directory / "spec.json").read_text())
    return kms, spec


async def _published(signer: ClientSigner) -> tuple[PublishedKey, bytes, dict[str, Any]]:
    (key,) = await signer.keys()
    return key, await signer.certificates_pem(), await signer.jwks()


def prepare(directory: Path, days: int) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    spec_file = directory / "spec.json"
    if not spec_file.exists():
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        spec = ClientKeySpec(
            kms_key_id=KEY_ID, not_before=start, not_after=start + timedelta(days=days)
        )
        spec_file.write_text(spec.model_dump_json(indent=2) + "\n")
    kms, spec = load(directory)
    kms.create_signing_key(spec.kms_key_id)
    key, pem, jwks = asyncio.run(_published(ClientSigner(kms, (spec,))))
    (directory / "client-cert.pem").write_bytes(pem)
    (directory / "client-jwks.json").write_text(json.dumps(jwks, indent=2) + "\n")
    print(f"key dir      {directory}")
    print(f"kid          {key.kid}")
    print(f"x5t (SHA-1)  {key.x5t}   (Entra shows the SHA-1 thumbprint in hex)")
    print(f"x5t#S256     {key.x5t_s256}")
    print(f"valid        {spec.not_before.isoformat()} .. {spec.not_after.isoformat()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--days", type=int, default=365)
    args = parser.parse_args()
    prepare(args.dir.expanduser(), args.days)


if __name__ == "__main__":
    main()
