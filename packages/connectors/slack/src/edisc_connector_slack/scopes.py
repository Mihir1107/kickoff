"""The Slack scopes our distributed app asks for, and what each missing one hides (M17 step 6).

ONE table: the install flow requests ``BOT_SCOPES``; the scopes Slack GRANTS (its exchange
response, never what we asked) give the blind spots recorded on the connection and in every later
``job_started`` (ADR 0018 §7.2). The live connector (Phase 3) reads the same table.
"""

from __future__ import annotations

# scope -> the blind spot recorded when it is not granted
BLIND_SPOT_IF_MISSING: dict[str, str] = {
    "channels:read": "public_channel_list",
    "channels:history": "public_channel_messages",
    "groups:read": "private_channel_list",
    "groups:history": "private_channel_messages",
    "im:read": "direct_message_list",
    "im:history": "direct_messages",
    "mpim:read": "group_dm_list",
    "mpim:history": "group_direct_messages",
    "users:read": "user_profiles",
    "users:read.email": "user_emails",
    "files:read": "files",
    "team:read": "workspace_profile",
}
BOT_SCOPES: tuple[str, ...] = tuple(sorted(BLIND_SPOT_IF_MISSING))


def granted(scope_field: object) -> tuple[str, ...]:
    """Slack's comma-separated ``scope`` field, as a sorted tuple (anything else: nothing granted)."""
    if not isinstance(scope_field, str):
        return ()
    return tuple(sorted({s.strip() for s in scope_field.split(",") if s.strip()}))


def blind_spots(scopes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(v for k, v in BLIND_SPOT_IF_MISSING.items() if k not in scopes))
