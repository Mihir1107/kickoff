"""Programmatic migrations (used by ``make migrate`` and tests). Runs Alembic as the owner role."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from edisc_core.settings import get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config(database: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    if database:
        cfg.attributes["database"] = database
    return cfg


def upgrade(database: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(database), revision)


class DowngradeRefusedError(RuntimeError):
    pass


def downgrade(database: str | None = None, *, revision: str) -> None:
    """Downgrade to an explicit revision, local/ci only. There is no default: "base" drops everything.

    Outside disposable environments schema changes only move forward (fix-forward migrations): a
    downgrade would drop append-only evidence tables or custody columns.
    """
    settings = get_settings()
    if not settings.env.is_disposable:
        raise DowngradeRefusedError(
            f"downgrade refused: EDISC_ENV={settings.env.value} (only local/ci)"
        )
    command.downgrade(alembic_config(database), revision)


def main() -> None:
    args = sys.argv[1:] or ["upgrade"]
    if args[0] == "upgrade":
        upgrade()
    elif args[0] == "downgrade" and len(args) == 2:
        downgrade(revision=args[1])
    else:
        raise SystemExit("usage: python -m edisc_db.migrate upgrade | downgrade <revision>")


if __name__ == "__main__":
    main()
