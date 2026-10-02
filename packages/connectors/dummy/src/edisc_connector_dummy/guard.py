"""The dummy connector exists only where data is disposable (EDISC_ENV local, test or ci).

It shares the ``slack`` identity namespace (ADR 0004 amendment): in a real environment its synthetic
items could take the idempotency keys of real Slack messages and be deduplicated against them. So it
is never wired, never gets a connection and never runs a job anywhere else.
"""

from __future__ import annotations

from collections.abc import Iterable

from edisc_core.settings import Environment

SOURCE = "dummy"


class DummyNotPermittedError(RuntimeError):
    pass


def dummy_permitted(env: Environment) -> bool:
    return env.is_disposable


def source_permitted(env: Environment, source: str) -> bool:
    return source != SOURCE or dummy_permitted(env)


def ensure_dummy_permitted(env: Environment, sources: Iterable[str]) -> None:
    """Raise if ``sources`` include the dummy outside local/test/ci."""
    if SOURCE in sources and not dummy_permitted(env):
        raise DummyNotPermittedError(
            f"the dummy connector is only available when EDISC_ENV is local, test or ci (not {env.value})"
        )
