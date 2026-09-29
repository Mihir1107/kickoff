from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from edisc_core.ids import new_id
from edisc_core.schemas import CanonicalMessage, JobStatus


def _msg(**overrides: object) -> CanonicalMessage:
    base: dict[str, object] = {
        "id": new_id(),
        "tenant_id": new_id(),
        "matter_id": new_id(),
        "job_id": new_id(),
        "source": "dummy",
        "source_workspace_id": "w1",
        "conversation_id": "c1",
        "conversation_type": "channel",
        "source_message_id": "c1/1",
        "author_external_id": "u1",
        "sent_at_utc": "2026-01-01T12:00:00+05:00",
        "message_type": "message",
        "body_text": "hi",
        "version": 1,
        "content_hash": "a" * 64,
        "raw_hash": "b" * 64,
        "raw_storage_key": "k",
        "raw_json_path": "$.messages[0]",
        "connector_version": "0.1.0",
        "collected_at_utc": "2026-01-02T00:00:00Z",
    }
    base.update(overrides)
    return CanonicalMessage.model_validate(base)


def test_offsets_normalized_to_utc() -> None:
    m = _msg()
    assert m.sent_at_utc.utcoffset() == timedelta(0)
    assert m.sent_at_utc.hour == 7


def test_naive_timestamps_rejected() -> None:
    with pytest.raises(ValidationError):
        _msg(sent_at_utc="2026-01-01T12:00:00")
    with pytest.raises(ValidationError):
        _msg(edited_at_utc=datetime(2026, 1, 1))  # noqa: DTZ001


def test_body_text_not_normalized() -> None:
    nfd = "café  "
    assert _msg(body_text=nfd).body_text == nfd


def test_frozen_and_strict_fields() -> None:
    m = _msg(sent_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc))  # noqa: UP017
    with pytest.raises(ValidationError):
        m.body_text = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _msg(unexpected_field=1)


def test_only_completed_is_clean() -> None:
    assert [s for s in JobStatus if s.is_clean] == [JobStatus.COMPLETED]
    assert not JobStatus.COMPLETED_UNVERIFIED.is_clean
