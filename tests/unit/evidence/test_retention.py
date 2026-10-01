from datetime import timedelta

import pytest

from edisc_core.settings import Environment, Settings
from edisc_core.time import utc_now
from edisc_evidence.retention import effective_retain_until


def _s(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg, arg-type]


def test_rolling_window_not_full_matter_retention() -> None:
    s = _s(env=Environment.PRODUCTION, evidence_retention_window_days=90)
    until = effective_retain_until(s, utc_now() + timedelta(days=3650))
    assert timedelta(days=89) < until - utc_now() <= timedelta(days=90)


def test_matter_date_caps_the_window() -> None:
    s = _s(env=Environment.PRODUCTION)
    matter = utc_now() + timedelta(days=10)
    assert effective_retain_until(s, matter) == matter


def test_local_override_caps_further() -> None:
    s = _s(env=Environment.LOCAL, evidence_retention_override_days=1)
    assert effective_retain_until(s, utc_now() + timedelta(days=30)) - utc_now() <= timedelta(
        days=1
    )


def test_ended_matter_refuses_collection() -> None:
    with pytest.raises(ValueError, match="retention ended"):
        effective_retain_until(_s(), utc_now() - timedelta(seconds=1))


def test_seconds_override_caps_on_test_stack() -> None:
    s = _s(env=Environment.TEST, evidence_retention_override_seconds=300)
    until = effective_retain_until(s, utc_now() + timedelta(days=30))
    assert timedelta(seconds=290) < until - utc_now() <= timedelta(seconds=300)


def test_seconds_override_refused_at_use_time_outside_test_stack() -> None:
    # defense in depth: even if validation were bypassed (model_construct), the writer refuses
    s = _s(env=Environment.TEST, evidence_retention_override_seconds=300).model_copy(
        update={"env": Environment.PRODUCTION}
    )
    with pytest.raises(RuntimeError, match="seconds-level retention"):
        effective_retain_until(s, utc_now() + timedelta(days=30))
