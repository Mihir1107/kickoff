"""A statement error's text never carries its parameters (ADR 0016 amendment, D11): it reaches log
lines, unit errors recorded in the chain and API answers. The SQLSTATE stays on the error."""

from __future__ import annotations

import secrets

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from edisc_core.settings import Settings
from edisc_db.session import create_engine, sqlstate_of


@pytest.mark.parametrize("role", ["app", "sweeper", "owner"])
async def test_statement_errors_never_carry_their_parameters(settings: Settings, role: str) -> None:
    canary = f"Zq{secrets.token_hex(12)}"
    engine = create_engine(settings, role)  # type: ignore[arg-type]
    try:
        async with engine.connect() as conn:
            with pytest.raises(DBAPIError) as caught:
                await conn.execute(text("SELECT length(:v) / 0"), {"v": canary})
    finally:
        await engine.dispose()
    assert sqlstate_of(caught.value) == "22012"  # division_by_zero: classification still works
    assert canary not in str(caught.value) and canary not in repr(caught.value)
