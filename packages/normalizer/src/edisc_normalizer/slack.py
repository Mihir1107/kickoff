"""Slack-dialect normalizer (pure, deterministic). Implements ADR 0004.

Identity
    message   ``{workspace}/{channel}/{ts}``      (channel + ts; ts alone is only unique per channel)
    file      ``{workspace}/file/{file_id}``
    user      ``{workspace}/user/{user_id}#profile``        directory snapshot (users.list)
              ``{workspace}/user/{user_id}#profile-embed``  what a message showed (user_profile embed)
    derived   ``{subject}#reactions`` | ``{subject}#change`` | ``{subject}#observation``

Version-defining fingerprints carry an ``fp`` tag; changing a definition changes the tag (and hashes).
Text is hashed exactly as delivered (RFC 8785, no Unicode normalization).

Rules
- A message version changes only when its fingerprint changes (content, author id, subtype, thread
  root, deleted state, attached files' content hashes). Reactions, reply counters, profile embeds,
  file URLs and change markers never create versions.
- A change marker (edited.ts, deleted_ts) that moves without a content change -> change observation.
- A subject returning to an earlier state (A -> B -> A) -> "reverted" change observation (the
  idempotency key would otherwise hide the revert).
- Reactions: a snapshot whenever present, and an EMPTY snapshot if reactions disappear from a live
  message that had them (Slack omits the key when empty). Tombstones say nothing about reactions.
- Absence is never deletion: only an explicit tombstone creates a deleted version. A message that was
  observed before and is missing now -> "no_longer_observed"; seen again -> "observed_again".
- Messages link to user IDs, never display names. Renames produce identity snapshot versions only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from edisc_core.canonical import canonical_hash
from edisc_core.jsonpath import build
from edisc_core.schemas import EventKind, ItemType
from edisc_core.time import day_bounds, ensure_utc
from edisc_normalizer.model import (
    EMPTY_PRIOR,
    Derived,
    EvidenceRef,
    FileEvidence,
    FileMeta,
    FileUnavailable,
    NormalizeContext,
    PageResult,
    PriorState,
)

FP_MESSAGE = "slack.message/2"  # v2: files by id/name/type, not bytes (ADR 0004, option B)
FP_FILE = "slack.file/1"
FP_REACTIONS = "slack.reactions/1"
FP_PROFILE = "slack.profile/1"
FP_PROFILE_EMBED = "slack.profile-embed/1"
FP_CHANGE = "slack.change/1"
FP_OBSERVATION = "slack.observation/1"
FP_AVAILABILITY = "slack.file-availability/1"
FP_ACCESS = "slack.access/1"

NO_LONGER_OBSERVED = "no_longer_observed"
UNAVAILABLE = "unavailable"
AVAILABLE = "available"
ACCESS_LOST = "access_lost"
ACCESS_RESTORED = "access_restored"
OBSERVED_AGAIN = "observed_again"
_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


class NormalizationError(ValueError):
    """The raw payload cannot be interpreted. Never skipped silently: the batch fails loudly."""


class MissingFileEvidenceError(NormalizationError):
    """A message references a file whose bytes were not collected first (ADR 0004 needs its hash)."""


# ------------------------------------------------------------------ ids
def message_id(workspace: str, channel: str, ts: str) -> str:
    if not channel or not ts:
        raise NormalizationError("message identity needs channel and ts")
    return f"{workspace}/{channel}/{ts}"


def file_item_id(workspace: str, file_id: str) -> str:
    return f"{workspace}/file/{file_id}"


def profile_id(workspace: str, user_id: str, *, embed: bool = False) -> str:
    return f"{workspace}/user/{user_id}#profile{'-embed' if embed else ''}"


def ts_datetime(ts: str) -> datetime:
    seconds, _, micros = ts.partition(".")
    return datetime.fromtimestamp(int(seconds), UTC) + timedelta(
        microseconds=int((micros + "000000")[:6])
    )


# ------------------------------------------------------------------ helpers
def _parse(page: bytes, key: str) -> list[dict[str, Any]]:
    try:
        doc = json.loads(page)
    except ValueError as exc:
        raise NormalizationError("page is not valid JSON") from exc
    if not isinstance(doc, dict) or doc.get("ok") is not True or not isinstance(doc.get(key), list):
        raise NormalizationError(f"not a successful Slack page with '{key}'")
    return doc[key]  # type: ignore[no-any-return]


def file_refs(page: bytes) -> list[FileMeta]:
    """Attachments referenced by a messages page: the pipeline downloads these BEFORE normalizing."""
    seen: dict[str, FileMeta] = {}
    for m in _parse(page, "messages"):
        for f in m.get("files", []) or []:
            if f.get("id") and f["id"] not in seen:
                seen[f["id"]] = FileMeta(
                    f["id"], f.get("name", ""), f.get("mimetype", ""), int(f.get("size", 0))
                )
    return list(seen.values())


def _in_scope(sent_at: datetime | None, ctx: NormalizeContext) -> bool:
    if sent_at is None or ctx.date_from is None or ctx.date_to is None:
        return True
    return ensure_utc(ctx.date_from) <= sent_at < ensure_utc(ctx.date_to)


def _hints(raw: Mapping[str, Any]) -> dict[str, str]:
    hints: dict[str, str] = {}
    edited = raw.get("edited")
    if isinstance(edited, dict) and edited.get("ts"):
        hints["edited_ts"] = str(edited["ts"])
    if raw.get("deleted_ts"):
        hints["deleted_ts"] = str(raw["deleted_ts"])
    return hints


def _changes(
    *,
    subject: str,
    current_hash: str,
    prior: PriorState,
    hints: Mapping[str, str] | None,
    evidence: EvidenceRef,
    json_path: str,
    raw_hash: str,
    sent_at: datetime | None,
    in_scope: bool,
) -> list[Derived]:
    """Change observations for one subject: moved change markers, and reverts to an earlier state."""
    out: list[Derived] = []
    fp: dict[str, Any]
    sequence = prior.change_count
    if prior.latest_content_hash is None:
        return out  # first sighting: nothing to compare with
    if current_hash == prior.latest_content_hash and hints is not None:
        for key in sorted(set(hints) | set(prior.current_hints)):
            old, new = prior.current_hints.get(key), hints.get(key)
            if old != new:
                fp = {
                    "fp": FP_CHANGE,
                    "subject": subject,
                    "kind": "hint",
                    "hint": key,
                    "old": old,
                    "new": new,
                    "base": current_hash,
                }
                out.append(
                    _event(
                        f"{subject}#change",
                        EventKind.CHANGE_OBSERVATION,
                        fp,
                        (subject, current_hash),
                        evidence,
                        json_path,
                        raw_hash,
                        sent_at,
                        in_scope,
                        {key: new} if new else {},
                    )
                )
    elif current_hash != prior.latest_content_hash and current_hash in prior.known_content_hashes:
        sequence += 1
        fp = {
            "fp": FP_CHANGE,
            "subject": subject,
            "kind": "reverted",
            "from": prior.latest_content_hash,
            "to": current_hash,
            "occurrence": sequence,
        }
        out.append(
            _event(
                f"{subject}#change",
                EventKind.CHANGE_OBSERVATION,
                fp,
                (subject, current_hash),
                evidence,
                json_path,
                raw_hash,
                sent_at,
                in_scope,
                {},
            )
        )
    return out


def _event(
    source_item_id: str,
    kind: EventKind,
    fingerprint: dict[str, Any],
    parent: tuple[str, str] | None,
    evidence: EvidenceRef,
    json_path: str,
    raw_hash: str,
    sent_at: datetime | None,
    in_scope: bool,
    hints: Mapping[str, str],
) -> Derived:
    return Derived(
        source_item_id=source_item_id,
        item_type=ItemType.EVENT,
        event_kind=kind,
        fingerprint=fingerprint,
        raw_hash=raw_hash,
        evidence=evidence,
        json_path=json_path,
        parent=parent,
        sent_at=sent_at,
        change_hints=dict(hints),
        in_scope=in_scope,
        derived={k: v for k, v in fingerprint.items() if k != "fp"} | {"event_kind": kind.value},
    )


# ------------------------------------------------------------------ messages
def normalize_messages_page(
    page: bytes,
    *,
    ctx: NormalizeContext,
    page_ref: EvidenceRef,
    prior: Mapping[str, PriorState],
    files: Mapping[str, FileEvidence | FileUnavailable],
) -> PageResult:
    """Derive every record from one conversations.history / conversations.replies page.

    ``files`` must cover every file the page references: collected bytes, or the source's refusal.
    """
    if ctx.conversation_id is None:
        raise NormalizationError("a messages page needs a conversation")
    ws, conv = ctx.workspace_id, ctx.conversation_id
    unit_bounds = day_bounds(ctx.unit_day) if ctx.unit_day else None
    items: list[Derived] = []
    observed: set[str] = set()
    subjects: set[str] = set()
    emitted: set[str] = set()
    unavailable: set[str] = set()

    def emit(d: Derived) -> None:
        key = d.idempotency_key(ctx.tenant_id, ctx.source)
        if key not in emitted:  # overlapping content within one page
            emitted.add(key)
            items.append(d)

    for index, raw in enumerate(_parse(page, "messages")):
        ts = raw.get("ts")
        if not isinstance(ts, str):
            raise NormalizationError(f"message {index} has no string ts")
        mid = message_id(ws, conv, ts)
        path = build("messages", index)
        raw_hash = canonical_hash(raw)
        sent_at = ts_datetime(ts)
        in_scope = _in_scope(sent_at, ctx)
        if unit_bounds and unit_bounds[0] <= sent_at < unit_bounds[1]:
            observed.add(mid)
        deleted = raw.get("subtype") == "message_deleted"

        # attachments: the message fingerprint carries id/name/type as shown in the message (option B);
        # bytes are versioned on file items; availability is its own observation stream
        file_entries: list[list[str]] = []
        availability: list[tuple[str, FileEvidence | FileUnavailable, str, str]] = []
        for index_f, f in enumerate([] if deleted else (raw.get("files") or [])):
            fid = f["id"]
            file_entries.append([fid, f.get("name", ""), f.get("mimetype", "")])
            outcome = files.get(fid)
            if outcome is None:
                raise MissingFileEvidenceError(
                    f"file {fid} of {mid} was not attempted before normalizing"
                )
            availability.append(
                (fid, outcome, build("messages", index, "files", index_f), canonical_hash(f))
            )
            if isinstance(outcome, FileUnavailable):
                unavailable.add(fid)
                continue
            fp_file = {
                "fp": FP_FILE,
                "sha256": outcome.sha256,
                "name": f.get("name", ""),
                "mimetype": f.get("mimetype", ""),
            }
            emit(
                Derived(
                    source_item_id=file_item_id(ws, fid),
                    item_type=ItemType.FILE,
                    event_kind=None,
                    fingerprint=fp_file,
                    raw_hash=outcome.sha256,
                    evidence=outcome.evidence,
                    json_path="$",
                    parent=None,
                    sent_at=None,
                    change_hints={},
                    in_scope=in_scope,
                    derived={
                        "file_id": fid,
                        "name": fp_file["name"],
                        "mimetype": fp_file["mimetype"],
                        "size": outcome.size,
                        "sha256": outcome.sha256,
                    },
                )
            )

        thread_ts = raw.get("thread_ts")
        author = raw.get("user") or raw.get("bot_id")
        if not author:
            raise NormalizationError(f"{mid} has neither user nor bot_id")
        fingerprint = {
            "fp": FP_MESSAGE,
            "author": author,
            "text": "" if deleted else raw.get("text", ""),
            "blocks": None if deleted else raw.get("blocks"),
            "subtype": raw.get("subtype"),
            "thread_root": thread_ts if thread_ts and thread_ts != ts else None,
            "deleted": deleted,
            "files": sorted(file_entries),
        }
        hints = _hints(raw)
        text = "" if deleted else raw.get("text", "")
        message = Derived(
            source_item_id=mid,
            item_type=ItemType.MESSAGE,
            event_kind=None,
            fingerprint=fingerprint,
            raw_hash=raw_hash,
            evidence=page_ref,
            json_path=path,
            parent=None,
            sent_at=sent_at,
            change_hints=hints,
            in_scope=in_scope,
            derived={
                "conversation_id": conv,
                "ts": ts,
                "sent_at": sent_at.isoformat(),
                "author_external_id": author,
                "text": text,
                "subtype": raw.get("subtype"),
                "thread_root": fingerprint["thread_root"],
                "deleted": deleted,
                "edited_ts": hints.get("edited_ts"),
                "deleted_ts": hints.get("deleted_ts"),
                "file_ids": sorted(e[0] for e in file_entries),
                "mentions": sorted(set(_MENTION.findall(text))),
            },
        )
        emit(message)
        subjects.add(mid)
        mprior = prior.get(mid, EMPTY_PRIOR)
        for d in _changes(
            subject=mid,
            current_hash=message.content_hash,
            prior=mprior,
            hints=hints,
            evidence=page_ref,
            json_path=path,
            raw_hash=raw_hash,
            sent_at=sent_at,
            in_scope=in_scope,
        ):
            emit(d)

        # file availability: refused now (once per change), or available again after a refusal
        for fid, outcome, f_path, f_raw in availability:
            aid = f"{file_item_id(ws, fid)}#availability"
            subjects.add(aid)
            aprior = prior.get(aid, EMPTY_PRIOR)
            if isinstance(outcome, FileUnavailable):
                if aprior.observation_status == UNAVAILABLE:
                    continue
                fp_a = {
                    "fp": FP_AVAILABILITY,
                    "file_id": fid,
                    "status": UNAVAILABLE,
                    "reason": outcome.reason,
                    "occurrence": aprior.observation_count + 1,
                }
                emit(
                    _event(
                        aid,
                        EventKind.FILE_UNAVAILABLE,
                        fp_a,
                        (mid, message.content_hash),
                        page_ref,
                        f_path,
                        f_raw,
                        sent_at,
                        in_scope,
                        {},
                    )
                )
            elif aprior.observation_status == UNAVAILABLE:
                fp_a = {
                    "fp": FP_AVAILABILITY,
                    "file_id": fid,
                    "status": AVAILABLE,
                    "reason": None,
                    "occurrence": aprior.observation_count + 1,
                }
                emit(
                    _event(
                        aid,
                        EventKind.FILE_BECAME_AVAILABLE,
                        fp_a,
                        (mid, message.content_hash),
                        page_ref,
                        f_path,
                        f_raw,
                        sent_at,
                        in_scope,
                        {},
                    )
                )

        # observed again after having been reported as no longer observed
        obs_id = f"{mid}#observation"
        subjects.add(obs_id)
        oprior = prior.get(obs_id, EMPTY_PRIOR)
        if oprior.observation_status == NO_LONGER_OBSERVED:
            fp_obs = {
                "fp": FP_OBSERVATION,
                "subject": mid,
                "status": OBSERVED_AGAIN,
                "occurrence": oprior.observation_count + 1,
            }
            emit(
                _event(
                    obs_id,
                    EventKind.OBSERVED_AGAIN,
                    fp_obs,
                    (mid, message.content_hash),
                    page_ref,
                    path,
                    raw_hash,
                    sent_at,
                    in_scope,
                    {},
                )
            )

        # reaction snapshots (never on tombstones: a tombstone says nothing about reactions)
        if not deleted:
            rid = f"{mid}#reactions"
            subjects.add(rid)
            rprior = prior.get(rid, EMPTY_PRIOR)
            reactions = raw.get("reactions")
            if reactions or rprior.latest_content_hash is not None:
                state = sorted([[r["name"], sorted(r.get("users", []))] for r in reactions or []])
                fp_r = {"fp": FP_REACTIONS, "message": mid, "reactions": state}
                r_path = build("messages", index, "reactions") if reactions else path
                r_raw = canonical_hash(reactions) if reactions else raw_hash
                snapshot = _event(
                    rid,
                    EventKind.REACTION_SNAPSHOT,
                    fp_r,
                    (mid, message.content_hash),
                    page_ref,
                    r_path,
                    r_raw,
                    sent_at,
                    in_scope,
                    {},
                )
                emit(snapshot)
                for d in _changes(
                    subject=rid,
                    current_hash=snapshot.content_hash,
                    prior=rprior,
                    hints=None,
                    evidence=page_ref,
                    json_path=r_path,
                    raw_hash=r_raw,
                    sent_at=sent_at,
                    in_scope=in_scope,
                ):
                    emit(d)

        # what the message showed about its author: an identity snapshot, never part of the message
        profile = raw.get("user_profile")
        if isinstance(profile, dict) and raw.get("user"):
            fp_e = {
                "fp": FP_PROFILE_EMBED,
                "user": raw["user"],
                "display_name": profile.get("display_name"),
                "real_name": profile.get("real_name"),
                "avatar_hash": profile.get("avatar_hash"),
                "team": profile.get("team"),
            }
            emit(
                _event(
                    profile_id(ws, raw["user"], embed=True),
                    EventKind.IDENTITY_SNAPSHOT,
                    fp_e,
                    None,
                    page_ref,
                    build("messages", index, "user_profile"),
                    canonical_hash(profile),
                    None,
                    True,
                    {},
                )
            )

    return PageResult(
        tuple(items), frozenset(observed), frozenset(subjects), frozenset(unavailable)
    )


# ------------------------------------------------------------------ directory
def normalize_directory_page(
    page: bytes, *, ctx: NormalizeContext, page_ref: EvidenceRef, prior: Mapping[str, PriorState]
) -> PageResult:
    items: list[Derived] = []
    subjects: set[str] = set()
    for index, raw in enumerate(_parse(page, "members")):
        uid = raw.get("id")
        if not isinstance(uid, str):
            raise NormalizationError(f"member {index} has no id")
        profile = raw.get("profile") or {}
        fp = {
            "fp": FP_PROFILE,
            "user": uid,
            "display_name": profile.get("display_name"),
            "real_name": profile.get("real_name") or raw.get("real_name"),
            "email": profile.get("email"),
            "avatar_hash": profile.get("avatar_hash"),
            "title": profile.get("title"),
            "deactivated": bool(raw.get("deleted")),
        }
        pid = profile_id(ctx.workspace_id, uid)
        path = build("members", index)
        raw_hash = canonical_hash(raw)
        snap = _event(
            pid, EventKind.IDENTITY_SNAPSHOT, fp, None, page_ref, path, raw_hash, None, True, {}
        )
        items.append(snap)
        subjects.add(pid)
        items.extend(
            _changes(
                subject=pid,
                current_hash=snap.content_hash,
                prior=prior.get(pid, EMPTY_PRIOR),
                hints=None,
                evidence=page_ref,
                json_path=path,
                raw_hash=raw_hash,
                sent_at=None,
                in_scope=True,
            )
        )
    return PageResult(tuple(items), frozenset(), frozenset(subjects))


# ------------------------------------------------------------------ unit completion: absence
def messages_fragment_hash(page: bytes) -> str:
    """Canonical hash of a page's message list (the ``$.messages`` fragment absence events point to)."""
    return canonical_hash(_parse(page, "messages"))


def finalize_unit(
    *,
    ctx: NormalizeContext,
    previously_observed: Iterable[str],
    observed: Iterable[str],
    prior: Mapping[str, PriorState],
    last_page_fragment_hash: str,
    last_page_ref: EvidenceRef,
) -> tuple[Derived, ...]:
    """After a unit's pages are ALL processed: messages recorded before for this conversation-day but
    not seen now -> ``no_longer_observed`` (never a deletion). Evidence: the unit's last page, whose
    message list is where they were absent (``messages_fragment_hash`` of that page, computed when the
    page was processed, so finalize never has to read it back)."""
    seen = set(observed)
    fragment_hash = last_page_fragment_hash
    out: list[Derived] = []
    for mid in sorted(set(previously_observed) - seen):
        obs_id = f"{mid}#observation"
        oprior = prior.get(obs_id, EMPTY_PRIOR)
        if oprior.observation_status == NO_LONGER_OBSERVED:
            continue  # still absent: already recorded
        mprior = prior.get(mid, EMPTY_PRIOR)
        if mprior.latest_content_hash is None:
            raise NormalizationError(
                f"{mid} is listed as previously observed but has no recorded version"
            )
        fp = {
            "fp": FP_OBSERVATION,
            "subject": mid,
            "status": NO_LONGER_OBSERVED,
            "occurrence": oprior.observation_count + 1,
        }
        out.append(
            _event(
                obs_id,
                EventKind.NO_LONGER_OBSERVED,
                fp,
                (mid, mprior.latest_content_hash),
                last_page_ref,
                "$.messages",
                fragment_hash,
                None,
                True,
                {},
            )
        )
    return tuple(out)


def message_page_subjects(page: bytes, *, ctx: NormalizeContext) -> frozenset[str]:
    """Subjects whose prior state ``normalize_messages_page`` consults, known before normalizing."""
    if ctx.conversation_id is None:
        raise NormalizationError("a messages page needs a conversation")
    out: set[str] = set()
    for raw in _parse(page, "messages"):
        mid = message_id(ctx.workspace_id, ctx.conversation_id, str(raw.get("ts")))
        out |= {mid, f"{mid}#reactions", f"{mid}#observation"}
        out |= {
            f"{file_item_id(ctx.workspace_id, f['id'])}#availability"
            for f in raw.get("files") or []
        }
    return frozenset(out)


def directory_page_subjects(page: bytes, *, ctx: NormalizeContext) -> frozenset[str]:
    return frozenset(
        profile_id(ctx.workspace_id, str(m.get("id"))) for m in _parse(page, "members")
    )


# ------------------------------------------------------------------ conversation access
def access_subject(workspace: str, conversation_id: str) -> str:
    return f"{workspace}/{conversation_id}#access"


def access_lost(
    *,
    ctx: NormalizeContext,
    reason: str,
    response: bytes,
    response_ref: EvidenceRef,
    prior: Mapping[str, PriorState],
) -> tuple[Derived, ...]:
    """ONE conversation-level observation when a whole conversation becomes inaccessible. Per-message
    absence detection is suppressed for it (the pipeline never finalizes an inaccessible unit)."""
    if ctx.conversation_id is None:
        raise NormalizationError("access observations need a conversation")
    sid = access_subject(ctx.workspace_id, ctx.conversation_id)
    aprior = prior.get(sid, EMPTY_PRIOR)
    if aprior.observation_status == ACCESS_LOST:
        return ()
    try:
        doc = json.loads(response)
    except ValueError as exc:
        raise NormalizationError("access-loss response is not JSON") from exc
    fp = {
        "fp": FP_ACCESS,
        "conversation": ctx.conversation_id,
        "status": ACCESS_LOST,
        "reason": reason,
        "occurrence": aprior.observation_count + 1,
    }
    return (
        _event(
            sid,
            EventKind.ACCESS_LOST,
            fp,
            None,
            response_ref,
            "$",
            canonical_hash(doc),
            None,
            True,
            {},
        ),
    )


def access_restored(
    *, ctx: NormalizeContext, page: bytes, page_ref: EvidenceRef, prior: Mapping[str, PriorState]
) -> tuple[Derived, ...]:
    """The conversation answers again after an access loss: one observation (first page as evidence)."""
    if ctx.conversation_id is None:
        raise NormalizationError("access observations need a conversation")
    sid = access_subject(ctx.workspace_id, ctx.conversation_id)
    aprior = prior.get(sid, EMPTY_PRIOR)
    if aprior.observation_status != ACCESS_LOST:
        return ()
    fp = {
        "fp": FP_ACCESS,
        "conversation": ctx.conversation_id,
        "status": ACCESS_RESTORED,
        "reason": None,
        "occurrence": aprior.observation_count + 1,
    }
    return (
        _event(
            sid,
            EventKind.ACCESS_RESTORED,
            fp,
            None,
            page_ref,
            "$.messages",
            canonical_hash(_parse(page, "messages")),
            None,
            True,
            {},
        ),
    )
