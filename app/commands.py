"""Discord command logic, independent of discord.py (app.bot is thin glue).

Everything here takes plain values and a database connection and returns text
or embed dicts, so it's tested without Discord.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app import signing, store
from app.config import Settings
from app.rhythmverse import SearchOptions, Song

MAX_MATCHES = 5
PART_NAMES = {
    "guitar": "Guitar", "bass": "Bass", "drum": "Drums", "vocals": "Vocals", "keys": "Keys",
    "real_guitar": "Pro Guitar", "real_bass": "Pro Bass", "real_keys": "Pro Keys", "band": "Band",
}
TIER_DOTS = ["⚪", "🟢", "🟡", "🟠", "🔴", "🟣", "😈"]  # Warmup..Impossible, as RB3 shows tiers

STATE_TEXT = {
    "waiting": "⏳ waiting for the game",
    "approval": "⏸️ waiting for approval",
    "queued": "🕒 queued",
    "done": "📥 downloaded",
    "in_library": "✅ in library",
    "have": "✅ already had it",
    "failed": "❌ failed",
    "not_hosted": "🚫 can't download from that host",
    "no_folder": "⚠️ no download folder",
}


@dataclass
class Member:
    """Who's asking, as the glue reads it from Discord."""
    user_id: str
    name: str
    roles: list[str] = field(default_factory=list)  # role names and IDs
    owner: bool = False
    admin: bool = False


def has_role(member: Member, role: str) -> bool:
    if not role:
        return False
    wanted = role.strip().lstrip("@").lower()
    return any(r.lower() == wanted for r in member.roles)


def can_request(settings: Settings, member: Member) -> bool:
    return not settings.requester_role or has_role(member, settings.requester_role) or is_approver(settings, member)


def is_approver(settings: Settings, member: Member) -> bool:
    return member.owner or member.admin or has_role(member, settings.approver_role)


def search_options(text: str) -> SearchOptions:
    return SearchOptions(text=text.strip()[:200], downloadable_only=True)


def _length(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else ""


def parts_line(tiers: dict[str, int]) -> str:
    return " · ".join(f"{PART_NAMES.get(p, p)} {TIER_DOTS[max(0, min(t, 6))]}"
                      for p, t in tiers.items() if p != "band")


def song_embed(song: Song, having: list[str], art_attached: bool) -> dict[str, Any]:
    meta = [x for x in (song.album, str(song.year) if song.year else "", _length(song.length_s)) if x]
    lines = []
    if meta:
        lines.append(" · ".join(meta))
    if song.tiers:
        lines.append(parts_line(song.tiers))
    if song.author:
        lines.append(f"Charted by {song.author}")
    if having:
        lines.append("✅ Already on: " + ", ".join(having))
    embed: dict[str, Any] = {
        "title": f"{song.title} — {song.artist}"[:256],
        "url": song.page_url or None,
        "description": "\n".join(lines)[:4000],
    }
    if art_attached:
        embed["thumbnail"] = {"url": f"attachment://{song.file_id}.jpg"}
    return {k: v for k, v in embed.items() if v is not None}


def match_option(song: Song) -> tuple[str, str, str]:
    """(label, description, value) for the dropdown."""
    label = f"{song.title} — {song.artist}"[:100]
    desc = " · ".join(x for x in (song.album, str(song.year) if song.year else "", song.author) if x)[:100]
    return label, desc, song.file_id


def _state_text(s: dict) -> str:
    if s["state"] == "downloading":
        pct = int(100 * s["received"] / s["total"]) if s["total"] else 0
        return f"⬇️ {pct}%"
    text = STATE_TEXT.get(s["state"], s["state"])
    if s["state"] == "failed" and s.get("error"):
        text += f": {s['error'][:100]}"
    return text


def render_request(conn: sqlite3.Connection, request: sqlite3.Row) -> str:
    """The request's message, as it's edited while games report."""
    head = f"🎸 **{request['title']}** — {request['artist']}   (requested by <@{request['user_id']}>)"
    if request["cancelled"]:
        return f"~~{head}~~\n   cancelled"
    states = store.game_states(conn, request)
    if not states:
        return head + "\n   no games are paired yet: `/pair` one"
    lines = [head]
    width = max(len(s["name"]) for s in states)
    for s in states:
        text = _state_text(s) if request["approved"] else STATE_TEXT["approval"]
        lines.append(f"`{s['name'].ljust(width)}`  {text}")
    return "\n".join(lines)[:2000]


def queue_text(conn: sqlite3.Connection) -> str:
    lines = []
    for r in store.open_requests(conn, 200):
        if not r["approved"]:
            lines.append(f"`#{r['id']}` **{r['title']}** — {r['artist']} (by {r['user_name']}): waiting for approval")
            continue
        states = store.game_states(conn, r)
        m = store.merged(states)
        if m["state"] == "done":
            continue
        progress = ", ".join(f"{s['name']} {_state_text(s)}" for s in states)
        lines.append(f"`#{r['id']}` **{r['title']}** — {r['artist']} (by {r['user_name']}): {progress}")
    if not lines:
        return "Nothing waiting: every paired game has every request."
    text = "\n".join(lines)
    return text if len(text) <= 1900 else text[:1900].rsplit("\n", 1)[0] + "\n…"


def cancel_choices(conn: sqlite3.Connection, settings: Settings, member: Member, current: str) -> list[tuple[str, str]]:
    approver = is_approver(settings, member)
    out = []
    for r in store.open_requests(conn, 200):
        if not approver and r["user_id"] != member.user_id:
            continue
        if not store.can_cancel(conn, r):
            continue
        label = f"#{r['id']} {r['title']} — {r['artist']}"
        if current.lower() in label.lower():
            out.append((label[:100], str(r["id"])))
    return out[:25]


def cancel(conn: sqlite3.Connection, settings: Settings, member: Member, request_ref: str) -> tuple[str, int | None]:
    """(reply, the cancelled request's ID or None)."""
    try:
        request_id = int(request_ref.strip().lstrip("#"))
    except ValueError:
        return "Pick a request from the list.", None
    row = store.get_request(conn, request_id)
    if row is None or row["cancelled"]:
        return "There's no such request.", None
    if row["user_id"] != member.user_id and not is_approver(settings, member):
        return "Only the requester or an approver can cancel it.", None
    if not store.cancel(conn, request_id):
        return "A game has started on it already, so it can't be cancelled.", None
    return f"Cancelled “{row['title']}”.", request_id


def pair(conn: sqlite3.Connection, settings: Settings, member: Member, code: str) -> str:
    if not is_approver(settings, member):
        return "Only an approver or the server owner can pair a game."
    return store.confirm_code(conn, code).message


def _ago(ts: int, now: int) -> str:
    if not ts:
        return "never"
    d = max(now - ts, 0)
    if d < 180:
        return "online now"
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        return f"{d // 3600} h ago"
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def games_text(conn: sqlite3.Connection, now: int | None = None) -> str:
    now = store.now_s() if now is None else now
    rows = store.games(conn)
    if not rows:
        return "No games are paired. In band3: Settings > Online > Pair, then `/pair <code>` here."
    lines = []
    for g in rows:
        songs = conn.execute("SELECT COUNT(*) FROM library WHERE game_id = ?", (g["game_id"],)).fetchone()[0]
        lines.append(f"**{g['name']}**: last online {_ago(g['last_seen'], now)}, {songs} songs shared")
    return "\n".join(lines) + "\n`/unpair` removes one."


def game_choices(conn: sqlite3.Connection, current: str) -> list[tuple[str, str]]:
    return [(g["name"][:100], g["game_id"]) for g in store.games(conn)
            if current.lower() in g["name"].lower()][:25]


def unpair(conn: sqlite3.Connection, settings: Settings, member: Member, game_id: str) -> str:
    if not is_approver(settings, member):
        return "Only an approver or the server owner can unpair a game."
    name = store.unpair(conn, game_id)
    return f"Unpaired **{name}**: it no longer gets requests." if name else "There's no such game."


def browse_link(secret: bytes, settings: Settings, member: Member, channel_id: str,
                now: int | None = None) -> str:
    if not settings.public_url:
        return "The bot has no PUBLIC_URL set, so it can't link to the page."
    token = signing.make(secret, member.user_id, member.name, channel_id, can_request(settings, member),
                         is_approver(settings, member), now)
    return (f"Your link to the song library (just for you, for 24 hours):\n"
            f"{settings.public_url}/browse?t={token}")
