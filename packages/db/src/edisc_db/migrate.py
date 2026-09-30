"""Programmatic migrations (used by ``make migrate`` and tests). Runs Alembic as the owner role."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config(database: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    if database:
        cfg.attributes["database"] = database
    return cfg


def upgrade(database: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(database), revision)


def downgrade(database: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(database), revision)


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "upgrade"
    {"upgrade": upgrade, "downgrade": downgrade}[action]()


if __name__ == "__main__":
    main()
