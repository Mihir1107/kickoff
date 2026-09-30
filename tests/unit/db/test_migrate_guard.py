import pytest

from edisc_core.settings import Environment, Settings
from edisc_db import migrate


@pytest.mark.parametrize("env", [Environment.STAGING, Environment.PRODUCTION])
def test_downgrade_refused_outside_local_ci(
    monkeypatch: pytest.MonkeyPatch, env: Environment
) -> None:
    monkeypatch.setattr(migrate, "get_settings", lambda: Settings(_env_file=None, env=env))  # type: ignore[call-arg]
    called: list[object] = []
    monkeypatch.setattr(migrate.command, "downgrade", lambda *a: called.append(a))
    with pytest.raises(migrate.DowngradeRefusedError, match=env.value):
        migrate.downgrade(revision="0003")
    assert called == []


@pytest.mark.parametrize("env", [Environment.LOCAL, Environment.CI])
def test_downgrade_allowed_in_disposable_envs(
    monkeypatch: pytest.MonkeyPatch, env: Environment
) -> None:
    monkeypatch.setattr(migrate, "get_settings", lambda: Settings(_env_file=None, env=env))  # type: ignore[call-arg]
    called: list[object] = []
    monkeypatch.setattr(migrate.command, "downgrade", lambda *a: called.append(a))
    migrate.downgrade(revision="0003")
    assert len(called) == 1
