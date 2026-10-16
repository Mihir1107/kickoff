"""The Microsoft Graph application permissions our Teams app needs (M17 step 6, ADR 0016 A4).

Admin consent grants application permissions; what was actually granted is read from a
client-credentials token for the customer's Entra tenant (its ``roles`` claim), never from the
consent redirect. Each missing permission is a blind spot recorded on the connection.
"""

from __future__ import annotations

BLIND_SPOT_IF_MISSING: dict[str, str] = {
    "Channel.ReadBasic.All": "channel_list",
    "ChannelMessage.Read.All": "channel_messages",
    "Chat.Read.All": "chats",
    "Team.ReadBasic.All": "team_list",
    "User.Read.All": "user_profiles",
    "Files.Read.All": "files",
}
APP_ROLES: tuple[str, ...] = tuple(sorted(BLIND_SPOT_IF_MISSING))


def granted(roles: object) -> tuple[str, ...]:
    if not isinstance(roles, list):
        return ()
    return tuple(sorted({r for r in roles if isinstance(r, str)}))


def blind_spots(roles: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(v for k, v in BLIND_SPOT_IF_MISSING.items() if k not in roles))
