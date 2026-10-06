"""Counting event-loop turns: deterministic, unlike timing (ADR 0015 §24)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any


async def turns_during(work: Awaitable[Any]) -> tuple[int, Any]:
    """How many turns a ticker task got while ``work`` ran, and its result. A reader that never
    gives the loop a turn leaves the ticker at about one."""
    turns, done = 0, False

    async def ticker() -> None:
        nonlocal turns
        while not done:
            turns += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        result = await work
    finally:
        done = True
        await task
    return turns, result
