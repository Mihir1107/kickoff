"""The API's published contract: ``docs/api/openapi.json`` (M17 step 4, plan §5).

    uv run python -m edisc_api.openapi_doc            # write docs/api/openapi.json
    uv run python -m edisc_api.openapi_doc --check    # fail when it differs from the code

The document is generated from the app as a PRODUCTION deployment builds it, from settings that do
not depend on the environment the generator runs in (``Settings.model_construct``: no .env file,
no ``EDISC_*`` variables): the same code always gives the same bytes. Sorted keys, two-space
indentation, a final newline, no ``servers``. Every ``operationId`` is the route function's name and
unique; every operation declares ``x-permission``. ``scripts/api_compat.py`` compares it with the
merge base (oasdiff + our rules); the frontend generates its types from it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from edisc_api.error_codes import complete
from edisc_core.settings import Environment, Settings

DOCUMENT = Path("docs/api/openapi.json")
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


class OpenApiError(RuntimeError):
    pass


def operations(doc: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, op)
        for path, item in doc.get("paths", {}).items()
        for method, op in item.items()
        if method in METHODS
    ]


def document() -> dict[str, Any]:
    from edisc_api.app import create_app  # the app module wires everything: import on use

    settings = Settings.model_construct(env=Environment.PRODUCTION)
    doc: dict[str, Any] = complete(create_app(settings).openapi())
    doc.pop("servers", None)
    check(doc)
    return doc


def check(doc: dict[str, Any]) -> None:
    """Unique operationIds, a declared permission on every operation."""
    ops = operations(doc)
    dupes = [n for n, c in Counter(op.get("operationId") for _, _, op in ops).items() if c > 1]
    if dupes:
        raise OpenApiError(f"operationIds must be unique: {sorted(map(str, dupes))}")
    undeclared = [f"{m.upper()} {p}" for p, m, op in ops if "x-permission" not in op]
    if undeclared:
        raise OpenApiError(f"operations without x-permission: {undeclared}")


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="generate or check docs/api/openapi.json")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=Path, default=DOCUMENT)
    args = parser.parse_args(argv)
    text = render(document())
    if args.check:
        current = args.out.read_text() if args.out.exists() else ""
        if current != text:
            sys.stderr.write(f"{args.out} is out of date: run `make openapi` and commit it\n")
            return 1
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
