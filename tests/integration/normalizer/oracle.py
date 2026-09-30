"""Expected normalizer output computed from the dummy MODEL (not from raw pages, not with normalizer code).

Simulates what should be recorded after collecting epochs 0..E in order, per ADR 0004 + M10 rules.
Returns the same projection ``project()`` computes from the database, so the two compare exactly.
"""

from __future__ import annotations

from typing import Any

from edisc_connector_dummy.dataset import Dataset, h64


class _Stream:
    """Versions of one subject with revert detection (mirrors the rules, independently)."""

    def __init__(self) -> None:
        self.versions: list[Any] = []
        self.latest: Any = None
        self.changes: list[tuple[Any, ...]] = []

    def observe(self, state: Any) -> bool:
        """Returns True if the state equals the latest (no version change)."""
        if self.latest is None and not self.versions:
            self.versions.append(state)
            self.latest = state
            return False
        if state == self.latest:
            return True
        if state in self.versions:
            self.changes.append(
                ("reverted", sum(1 for c in self.changes if c[0] == "reverted") + 1)
            )
        else:
            self.versions.append(state)
        self.latest = state
        return False


def expected(ds: Dataset, last_epoch: int) -> dict[str, Any]:
    ws = ds.spec.workspace_id
    messages: dict[str, _Stream] = {}
    hints: dict[str, dict[str, str]] = {}
    hint_changes: dict[str, list[tuple[Any, ...]]] = {}
    status: dict[str, list[str]] = {}
    reactions: dict[str, _Stream] = {}
    profiles: dict[str, _Stream] = {}
    embeds: dict[str, set[tuple[Any, ...]]] = {}
    files: set[str] = set()

    for epoch in range(last_epoch + 1):
        users = ds.users(epoch)
        index = {u.id: i for i, u in enumerate(users)}
        for conv in ds.conversations():
            for d in range(ds.n_days(epoch)):
                for m in ds.unit_messages(conv.id, d, epoch):
                    mid = f"{ws}/{conv.id}/{m.ts}"
                    deleted = m.deleted_ts is not None
                    obs = status.setdefault(mid, [])
                    if deleted and ds.omits_deleted:  # absent from the source: never a deletion
                        if mid in messages and (not obs or obs[-1] == "observed_again"):
                            obs.append("no_longer_observed")
                        continue
                    if obs and obs[-1] == "no_longer_observed":
                        obs.append("observed_again")
                    state = (
                        "" if deleted else m.text,
                        deleted,
                        () if deleted else tuple(sorted(f.id for f in m.files)),
                        m.user,
                        "message_deleted" if deleted else m.subtype,
                        m.thread_ts if m.thread_ts and m.thread_ts != m.ts else None,
                    )
                    h = (
                        {"deleted_ts": m.deleted_ts}
                        if deleted
                        else ({"edited_ts": m.edited[1]} if m.edited else {})
                    )
                    stream = messages.setdefault(mid, _Stream())
                    unchanged = stream.observe(state)
                    if unchanged:
                        before = hints.get(mid, {})
                        for key in sorted(set(h) | set(before)):
                            if before.get(key) != h.get(key):
                                hint_changes.setdefault(mid, []).append(
                                    ("hint", key, before.get(key), h.get(key))
                                )
                    hints[mid] = h
                    if deleted:
                        continue
                    files.update(f.id for f in m.files)
                    r_state = tuple(sorted((name, tuple(sorted(who))) for name, who in m.reactions))
                    if r_state or f"{mid}#reactions" in reactions:
                        reactions.setdefault(f"{mid}#reactions", _Stream()).observe(r_state)
                    if m.subtype != "channel_join":
                        u = users[index[m.user]]
                        i = index[m.user]
                        display = ds.display_name_at(i, m.day_index, epoch)
                        embeds.setdefault(f"{ws}/user/{m.user}#profile-embed", set()).add(
                            (
                                display,
                                u.real_name,
                                f"{h64(ds.seed, 'avatar', i, display):016x}"[:12],
                                u.team_id,
                            )
                        )
        for u in users:
            profiles.setdefault(f"{ws}/user/{u.id}#profile", _Stream()).observe(
                (u.display_name, u.real_name, u.email, u.avatar_hash, "", u.deleted)
            )

    out: dict[str, Any] = {}
    for mid, s in messages.items():
        out[mid] = s.versions
        changes = hint_changes.get(mid, []) + s.changes
        if changes:
            out[f"{mid}#change"] = sorted(changes, key=repr)
        if status.get(mid):
            out[f"{mid}#observation"] = status[mid]
    for rid, s in {**reactions, **profiles}.items():
        out[rid] = s.versions
        if s.changes:
            out[f"{rid}#change"] = sorted(s.changes, key=repr)
    for eid, states in embeds.items():
        out[eid] = sorted(states, key=repr)
    for fid in files:
        out[f"{ws}/file/{fid}"] = "file"
    return out


def project(recorded: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The same projection, from database rows (item_derivations of each version, in version order)."""
    out: dict[str, Any] = {}
    for sid, derived in recorded.items():
        first = derived[0]
        if sid.endswith("#change"):
            out[sid] = sorted(
                (
                    ("hint", d["hint"], d["old"], d["new"])
                    if d["kind"] == "hint"
                    else ("reverted", d["occurrence"])
                )
                for d in derived
            )
            out[sid] = sorted(out[sid], key=repr)
        elif sid.endswith("#observation"):
            out[sid] = [d["status"] for d in derived]
        elif sid.endswith("#reactions"):
            out[sid] = [tuple((r[0], tuple(r[1])) for r in d["reactions"]) for d in derived]
        elif sid.endswith("#profile-embed"):
            out[sid] = sorted(
                {(d["display_name"], d["real_name"], d["avatar_hash"], d["team"]) for d in derived},
                key=repr,
            )
        elif sid.endswith("#profile"):
            out[sid] = [
                (
                    d["display_name"],
                    d["real_name"],
                    d["email"],
                    d["avatar_hash"],
                    d["title"],
                    d["deactivated"],
                )
                for d in derived
            ]
        elif "/file/" in sid:
            out[sid] = "file"
        elif "file_id" not in first:
            out[sid] = [
                (
                    d["text"],
                    d["deleted"],
                    tuple(d["file_ids"]),
                    d["author_external_id"],
                    d["subtype"],
                    d["thread_root"],
                )
                for d in derived
            ]
    return out
