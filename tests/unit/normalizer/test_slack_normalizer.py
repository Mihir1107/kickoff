"""Pure normalizer rules (ADR 0004), on realistic pages from the dummy source."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from edisc_connector_dummy.dataset import Dataset
from edisc_connectors_base.types import BatchKind, WorkUnit
from edisc_core.schemas import EventKind, ItemType
from edisc_normalizer.model import (
    NORMALIZER_VERSION,
    EvidenceRef,
    FileEvidence,
    FileUnavailable,
    NormalizeContext,
    PriorState,
)
from edisc_normalizer.slack import (
    MissingFileEvidenceError,
    access_lost,
    access_restored,
    finalize_unit,
    message_id,
    messages_fragment_hash,
    normalize_directory_page,
    normalize_messages_page,
)

from ..dummy.conftest import batches, connection, connector, full_scope, make_spec

REF = EvidenceRef(uuid.UUID(int=1), "t/x/page.json")
TENANT = uuid.UUID(int=7)


def ctx(conv: str = "C1", day: datetime | None = None) -> NormalizeContext:
    return NormalizeContext(
        TENANT,
        "dummy",
        "T0DUMMY01",
        conv,
        (day or datetime(2026, 1, 5, tzinfo=UTC)).date(),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 2, 1, tzinfo=UTC),
    )


def page(*messages: dict[str, Any]) -> bytes:
    return json.dumps({"ok": True, "messages": list(messages), "has_more": False}).encode()


def msg(ts: str = "1767571200.000100", text: str = "hello", **extra: Any) -> dict[str, Any]:
    return {"type": "message", "user": "U1", "text": text, "ts": ts, **extra}


def only(result: Any, item_type: ItemType, kind: EventKind | None = None) -> list[Any]:
    return [d for d in result.items if d.item_type is item_type and d.event_kind is kind]


def norm(
    p: bytes,
    prior: dict[str, PriorState] | None = None,
    files: dict[str, FileEvidence] | None = None,
    c: NormalizeContext | None = None,
) -> Any:
    return normalize_messages_page(
        p, ctx=c or ctx(), page_ref=REF, prior=prior or {}, files=files or {}
    )


def test_combining_sequence_and_precomposed_twin_hash_differently() -> None:
    decomposed = only(norm(page(msg(text="café"))), ItemType.MESSAGE)[0]
    precomposed = only(norm(page(msg(text="café"))), ItemType.MESSAGE)[0]
    assert decomposed.content_hash != precomposed.content_hash
    assert decomposed.derived["text"] == "café"  # stored exactly as delivered


def test_identity_is_channel_plus_ts_never_ts_alone() -> None:
    a = only(norm(page(msg()), c=ctx("C1")), ItemType.MESSAGE)[0]
    b = only(norm(page(msg()), c=ctx("C2")), ItemType.MESSAGE)[0]
    assert a.source_item_id == "T0DUMMY01/C1/1767571200.000100" != b.source_item_id
    assert a.idempotency_key(TENANT, "dummy") != b.idempotency_key(TENANT, "dummy")
    with pytest.raises(ValueError, match="channel and ts"):
        message_id("T", "", "1.0")


def test_volatile_fields_never_change_the_version() -> None:
    base = only(norm(page(msg(thread_ts="1767571200.000100"))), ItemType.MESSAGE)[0]
    noisy = only(
        norm(
            page(
                msg(
                    thread_ts="1767571200.000100",
                    reply_count=9,
                    reply_users=["U2"],
                    latest_reply="2.0",
                    user_profile={
                        "display_name": "Renamed",
                        "real_name": "X",
                        "avatar_hash": "zz",
                        "team": "T",
                    },
                    reactions=[{"name": "eyes", "users": ["U2"], "count": 1}],
                    client_msg_id="abc",
                )
            )
        ),
        ItemType.MESSAGE,
    )[0]
    assert base.content_hash == noisy.content_hash
    assert base.fingerprint["thread_root"] is None  # becoming a parent is not a new version


def test_messages_link_to_user_ids_and_renames_are_identity_snapshots() -> None:
    before = norm(page(msg(user_profile={"display_name": "Alice", "team": "T"})))
    after = norm(page(msg(user_profile={"display_name": "Alice (renamed)", "team": "T"})))
    m_before, m_after = only(before, ItemType.MESSAGE)[0], only(after, ItemType.MESSAGE)[0]
    assert m_before.content_hash == m_after.content_hash  # the message is unaffected
    assert m_before.fingerprint["author"] == "U1"
    assert "Alice" not in json.dumps(dict(m_before.fingerprint))
    e_before = only(before, ItemType.EVENT, EventKind.IDENTITY_SNAPSHOT)[0]
    e_after = only(after, ItemType.EVENT, EventKind.IDENTITY_SNAPSHOT)[0]
    assert e_before.source_item_id == e_after.source_item_id == "T0DUMMY01/user/U1#profile-embed"
    assert e_before.content_hash != e_after.content_hash


def test_moved_edit_marker_without_content_change_is_a_change_observation() -> None:
    first = only(norm(page(msg(edited={"user": "U1", "ts": "1.0"}))), ItemType.MESSAGE)[0]
    prior = {
        first.source_item_id: PriorState(
            first.content_hash, frozenset({first.content_hash}), {"edited_ts": "1.0"}
        )
    }
    again = norm(page(msg(edited={"user": "U1", "ts": "2.0"})), prior)
    assert only(again, ItemType.MESSAGE)[0].content_hash == first.content_hash
    (obs,) = only(again, ItemType.EVENT, EventKind.CHANGE_OBSERVATION)
    assert (obs.derived["hint"], obs.derived["old"], obs.derived["new"]) == (
        "edited_ts",
        "1.0",
        "2.0",
    )
    assert obs.parent == (first.source_item_id, first.content_hash)
    # the same marker again: nothing to report; first sighting: nothing to compare with
    same_prior = {
        first.source_item_id: PriorState(
            first.content_hash, frozenset({first.content_hash}), {"edited_ts": "2.0"}
        )
    }
    assert not only(
        norm(page(msg(edited={"user": "U1", "ts": "2.0"})), same_prior),
        ItemType.EVENT,
        EventKind.CHANGE_OBSERVATION,
    )
    assert not only(
        norm(page(msg(edited={"user": "U1", "ts": "9.0"}))),
        ItemType.EVENT,
        EventKind.CHANGE_OBSERVATION,
    )


def test_tombstone_is_a_deleted_version_and_says_nothing_about_reactions() -> None:
    live = only(norm(page(msg(reactions=[{"name": "eyes", "users": ["U2"]}]))), ItemType.MESSAGE)[0]
    tomb_result = norm(
        page(
            {
                "type": "message",
                "subtype": "message_deleted",
                "user": "U1",
                "text": "",
                "ts": "1767571200.000100",
                "deleted_ts": "9.0",
            }
        ),
        {f"{live.source_item_id}#reactions": PriorState("r", frozenset({"r"}))},
    )
    tomb = only(tomb_result, ItemType.MESSAGE)[0]
    assert tomb.fingerprint["deleted"] is True
    assert tomb.content_hash != live.content_hash
    assert tomb.change_hints == {"deleted_ts": "9.0"}
    assert not only(tomb_result, ItemType.EVENT, EventKind.REACTION_SNAPSHOT)


def test_reactions_removed_from_a_live_message_is_an_empty_snapshot() -> None:
    with_r = norm(page(msg(reactions=[{"name": "eyes", "users": ["U2", "U1"]}])))
    (snap,) = only(with_r, ItemType.EVENT, EventKind.REACTION_SNAPSHOT)
    assert snap.derived["reactions"] == [["eyes", ["U1", "U2"]]]
    assert not only(norm(page(msg())), ItemType.EVENT, EventKind.REACTION_SNAPSHOT)  # never had any
    prior = {snap.source_item_id: PriorState(snap.content_hash, frozenset({snap.content_hash}))}
    (empty,) = only(norm(page(msg()), prior), ItemType.EVENT, EventKind.REACTION_SNAPSHOT)
    assert empty.derived["reactions"] == []


def test_revert_to_an_earlier_state_is_observed() -> None:
    a = only(
        norm(page(msg(reactions=[{"name": "eyes", "users": ["U2"]}]))),
        ItemType.EVENT,
        EventKind.REACTION_SNAPSHOT,
    )[0]
    b = only(
        norm(page(msg(reactions=[{"name": "tada", "users": ["U2"]}]))),
        ItemType.EVENT,
        EventKind.REACTION_SNAPSHOT,
    )[0]
    prior = {
        a.source_item_id: PriorState(
            b.content_hash, frozenset({a.content_hash, b.content_hash}), change_count=0
        )
    }
    result = norm(page(msg(reactions=[{"name": "eyes", "users": ["U2"]}])), prior)
    (revert,) = only(result, ItemType.EVENT, EventKind.CHANGE_OBSERVATION)
    assert (
        revert.derived["kind"],
        revert.derived["from"],
        revert.derived["to"],
        revert.derived["occurrence"],
    ) == ("reverted", b.content_hash, a.content_hash, 1)


def test_absence_is_never_deletion() -> None:
    present = only(norm(page(msg())), ItemType.MESSAGE)[0]
    mid = present.source_item_id
    prior = {mid: PriorState(present.content_hash, frozenset({present.content_hash}))}
    empty_page = page()
    (nlo,) = finalize_unit(
        ctx=ctx(),
        previously_observed={mid},
        observed=set(),
        prior=prior,
        last_page_fragment_hash=messages_fragment_hash(empty_page),
        last_page_ref=REF,
    )
    assert nlo.event_kind is EventKind.NO_LONGER_OBSERVED
    assert nlo.parent == (mid, present.content_hash)
    assert not any(d.fingerprint.get("deleted") for d in [nlo])
    # already reported: nothing new while it stays absent
    reported = {
        **prior,
        f"{mid}#observation": PriorState(
            "x", frozenset({"x"}), observation_status="no_longer_observed", observation_count=1
        ),
    }
    assert (
        finalize_unit(
            ctx=ctx(),
            previously_observed={mid},
            observed=set(),
            prior=reported,
            last_page_fragment_hash=messages_fragment_hash(empty_page),
            last_page_ref=REF,
        )
        == ()
    )
    # it comes back: observed again, occurrence 2
    back = norm(page(msg()), reported)
    (again,) = only(back, ItemType.EVENT, EventKind.OBSERVED_AGAIN)
    assert again.derived["occurrence"] == 2


FILE_PAGE = page(
    msg(files=[{"id": "F1", "name": "a.pdf", "mimetype": "application/pdf", "size": 3}])
)


def _fe(digest: str) -> dict[str, FileEvidence | FileUnavailable]:
    return {"F1": FileEvidence("F1", digest, 3, EvidenceRef(uuid.UUID(int=2), "t/x/files/F1"))}


def test_every_referenced_file_must_be_attempted_first() -> None:
    with pytest.raises(MissingFileEvidenceError):
        norm(FILE_PAGE)


def test_file_bytes_version_the_file_item_not_the_message() -> None:
    """ADR 0004 option B: the message fingerprint carries file id/name/type, never bytes."""
    one, two = norm(FILE_PAGE, files=_fe("a" * 64)), norm(FILE_PAGE, files=_fe("b" * 64))
    assert (
        only(one, ItemType.MESSAGE)[0].content_hash == only(two, ItemType.MESSAGE)[0].content_hash
    )
    (f1,), (f2,) = only(one, ItemType.FILE), only(two, ItemType.FILE)
    assert f1.source_item_id == f2.source_item_id == "T0DUMMY01/file/F1"
    assert f1.content_hash != f2.content_hash  # new bytes under the same id = a new FILE version
    assert (f1.raw_hash, f1.json_path, f1.evidence.storage_key) == ("a" * 64, "$", "t/x/files/F1")
    assert only(one, ItemType.MESSAGE)[0].fingerprint["files"] == [
        ["F1", "a.pdf", "application/pdf"]
    ]


def test_unavailable_file_is_recorded_not_raised_and_later_availability_is_observed() -> None:
    gone = norm(FILE_PAGE, files={"F1": FileUnavailable("F1", "permission")})
    assert gone.unavailable_files == {"F1"}
    assert not only(gone, ItemType.FILE)
    (event,) = only(gone, ItemType.EVENT, EventKind.FILE_UNAVAILABLE)
    assert (event.source_item_id, event.derived["reason"], event.derived["status"]) == (
        "T0DUMMY01/file/F1#availability",
        "permission",
        "unavailable",
    )
    assert event.json_path == "$.messages[0].files[0]"
    message_gone = only(gone, ItemType.MESSAGE)[0]
    message_back = only(norm(FILE_PAGE, files=_fe("a" * 64)), ItemType.MESSAGE)[0]
    assert (
        message_gone.content_hash == message_back.content_hash
    )  # availability never versions the message

    unavailable_prior = {
        "T0DUMMY01/file/F1#availability": PriorState(
            "x", frozenset({"x"}), observation_status="unavailable", observation_count=1
        )
    }
    assert not only(
        norm(FILE_PAGE, unavailable_prior, files={"F1": FileUnavailable("F1", "permission")}),
        ItemType.EVENT,
        EventKind.FILE_UNAVAILABLE,
    )  # still unavailable: nothing new
    back = norm(FILE_PAGE, unavailable_prior, files=_fe("a" * 64))
    (avail,) = only(back, ItemType.EVENT, EventKind.FILE_BECAME_AVAILABLE)
    assert (avail.derived["status"], avail.derived["occurrence"]) == ("available", 2)
    assert only(back, ItemType.FILE)


def test_conversation_access_lost_is_one_observation_and_restoration_another() -> None:
    body = b'{"ok":false,"error":"not_in_channel"}'
    (lost,) = access_lost(
        ctx=ctx(), reason="not_in_channel", response=body, response_ref=REF, prior={}
    )
    assert (lost.source_item_id, lost.derived["status"], lost.derived["reason"]) == (
        "T0DUMMY01/C1#access",
        "access_lost",
        "not_in_channel",
    )
    lost_prior = {
        "T0DUMMY01/C1#access": PriorState(
            lost.content_hash,
            frozenset({lost.content_hash}),
            observation_status="access_lost",
            observation_count=1,
        )
    }
    assert (
        access_lost(
            ctx=ctx(), reason="not_in_channel", response=body, response_ref=REF, prior=lost_prior
        )
        == ()
    )
    (restored,) = access_restored(ctx=ctx(), page=page(msg()), page_ref=REF, prior=lost_prior)
    assert restored.derived["status"] == "access_restored"
    assert access_restored(ctx=ctx(), page=page(msg()), page_ref=REF, prior={}) == ()


async def test_pure_and_deterministic_on_realistic_pages() -> None:
    spec = make_spec(conversations=1, days=1)
    ds = Dataset(spec)
    c, _ = connector()
    conn, scope = connection(spec), full_scope(spec)
    u = WorkUnit(ds.conversations()[0].id, ds.day(0))
    for b in await batches(c, conn, u, scope):
        assert b.kind is BatchKind.HISTORY
        refs = {m_["id"] for m in json.loads(b.body)["messages"] for m_ in m.get("files", [])}
        evid = {
            fid: FileEvidence(
                fid, hashlib.sha256(ds.file_bytes(fid)).hexdigest(), len(ds.file_bytes(fid)), REF
            )
            for fid in refs
        }
        cx = NormalizeContext(
            TENANT,
            "dummy",
            spec.workspace_id,
            u.conversation_id,
            u.day,
            scope.date_from,
            scope.date_to,
        )
        first = normalize_messages_page(b.body, ctx=cx, page_ref=REF, prior={}, files=evid)
        second = normalize_messages_page(b.body, ctx=cx, page_ref=REF, prior={}, files=evid)
        assert first == second
        assert cx.normalizer_version == NORMALIZER_VERSION


def test_directory_snapshots() -> None:
    p = json.dumps(
        {
            "ok": True,
            "members": [
                {"id": "U1", "deleted": True, "profile": {"display_name": "Old", "real_name": "R"}},
            ],
        }
    ).encode()
    dctx = NormalizeContext(TENANT, "dummy", "T0DUMMY01", None, None, None, None)
    (snap,) = normalize_directory_page(p, ctx=dctx, page_ref=REF, prior={}).items
    assert snap.source_item_id == "T0DUMMY01/user/U1#profile"
    assert snap.derived["deactivated"] is True


def test_message_derivation_carries_the_attachment_references_of_its_fingerprint() -> None:
    """Renders name an unfetched file after the message's own reference (ADR 0015 §12)."""
    gone = norm(FILE_PAGE, files={"F1": FileUnavailable("F1", "permission")})
    (m,) = only(gone, ItemType.MESSAGE)
    assert m.derived["files"] == m.fingerprint["files"] == [["F1", "a.pdf", "application/pdf"]]
    assert NORMALIZER_VERSION == "0.2.0"


def _channels(*channels: dict[str, Any]) -> bytes:
    return json.dumps({"ok": True, "channels": list(channels)}).encode()


def test_conversation_snapshots_are_versioned_and_reverts_observed() -> None:
    dctx = NormalizeContext(TENANT, "dummy", "T0DUMMY01", None, None, None, None)
    base = {
        "id": "C1", "name": "general", "is_channel": True, "is_private": False,
        "is_archived": False, "members": ["U2", "U1"], "num_members": 2, "updated": 1,
        "topic": {"value": "Ship it", "creator": "U1", "last_set": 1},
        "purpose": {"value": "", "creator": "", "last_set": 0},
    }  # fmt: skip
    (first,) = normalize_directory_page(_channels(base), ctx=dctx, page_ref=REF, prior={}).items
    assert first.source_item_id == "T0DUMMY01/C1#conversation"
    assert first.event_kind is EventKind.CONVERSATION_SNAPSHOT
    assert first.derived | {"event_kind": None} == {
        "conversation": "C1", "type": "public_channel", "name": "general", "topic": "Ship it",
        "purpose": None, "members": ["U1", "U2"], "archived": False, "shared": False,
        "ext_shared": False, "event_kind": None,
    }  # fmt: skip
    # volatile fields (counts, update times, who set the topic) never make a version
    noisy = base | {"num_members": 9, "updated": 99, "topic": {"value": "Ship it", "creator": "U9", "last_set": 7}}  # fmt: skip
    (same,) = normalize_directory_page(_channels(noisy), ctx=dctx, page_ref=REF, prior={}).items
    assert same.content_hash == first.content_hash
    renamed = normalize_directory_page(
        _channels(base | {"name": "general-2"}), ctx=dctx, page_ref=REF, prior={}
    ).items[0]
    assert renamed.content_hash != first.content_hash
    prior = {
        first.source_item_id: PriorState(
            latest_content_hash=renamed.content_hash,
            known_content_hashes=frozenset({first.content_hash, renamed.content_hash}),
        )
    }
    back = normalize_directory_page(_channels(base), ctx=dctx, page_ref=REF, prior=prior).items
    assert [d.event_kind for d in back] == [
        EventKind.CONVERSATION_SNAPSHOT,
        EventKind.CHANGE_OBSERVATION,
    ]
    assert back[1].derived["kind"] == "reverted"
    for flags, kind in (
        ({"is_im": True}, "im"),
        ({"is_mpim": True, "is_private": True}, "mpim"),
        ({"is_group": True, "is_private": True}, "private_channel"),
    ):
        (snap,) = normalize_directory_page(
            _channels({"id": "D1", **flags}), ctx=dctx, page_ref=REF, prior={}
        ).items
        assert snap.derived["type"] == kind
