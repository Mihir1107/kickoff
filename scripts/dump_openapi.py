"""Write the API's OpenAPI document, the source of the web app's generated types.

    uv run python scripts/dump_openapi.py apps/web/openapi.json

Runs offline: building the app does not touch any service (they start in its lifespan). Keys are
sorted and indented so a schema change shows up as a readable diff. CI regenerates this file and the
TypeScript types (``npm run check:api`` in apps/web) and fails when either differs from the commit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from edisc_api.app import create_app
from edisc_core.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    spec = create_app(Settings()).openapi()
    # Display formatting for a reviewed file, not a hash input (those use edisc_core.canonical).
    args.out.write_text(json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
