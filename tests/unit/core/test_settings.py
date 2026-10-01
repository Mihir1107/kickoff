import pytest
from pydantic import ValidationError

from edisc_core.settings import Environment, Settings


def make(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize("env", [Environment.STAGING, Environment.PRODUCTION])
def test_retention_override_rejected_outside_local_ci(env: Environment) -> None:
    with pytest.raises(ValidationError, match="only permitted when EDISC_ENV is local, test or ci"):
        make(env=env, evidence_retention_override_days=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("env", [Environment.LOCAL, Environment.TEST, Environment.CI])
def test_retention_override_allowed_locally(env: Environment) -> None:
    s = make(env=env, evidence_retention_override_days=1)  # type: ignore[arg-type]
    assert s.evidence_retention_override_days == 1


def test_env_prefix_and_secret_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDISC_PG_APP_PASSWORD", "pg-password-canary-123")
    monkeypatch.setenv("EDISC_ENV", "ci")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.env is Environment.CI
    assert "pg-password-canary-123" not in repr(s)
    assert "pg-password-canary-123" in s.pg_dsn("app")


@pytest.mark.parametrize("env", [Environment.LOCAL, Environment.STAGING, Environment.PRODUCTION])
def test_seconds_retention_only_on_ephemeral_test_stacks(env: Environment) -> None:
    # local is refused too: the dev stack is long-lived, only test/ci stacks are destroyed per run
    with pytest.raises(ValidationError, match="only permitted when EDISC_ENV is test or ci"):
        make(env=env, evidence_retention_override_seconds=60)  # type: ignore[arg-type]


@pytest.mark.parametrize("env", [Environment.TEST, Environment.CI])
def test_seconds_retention_allowed_on_test_stacks(env: Environment) -> None:
    s = make(env=env, evidence_retention_override_seconds=60)  # type: ignore[arg-type]
    assert s.evidence_retention_override_seconds == 60
