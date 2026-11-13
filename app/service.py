"""Making a request, from Discord or the page, and telling Discord what changed."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app import store
from app.config import Settings
from app.rhythmverse import Song


@dataclass
class Outcome:
    kind: str  # requested | have | duplicate | limit | not_downloadable | forbidden
    message: str
    request_id: int | None = None


def request_song(conn: sqlite3.Connection, settings: Settings, song: Song, user_id: str, user_name: str,
                 *, can_request: bool, approver: bool, channel_id: str = "",
                 now: int | None = None) -> Outcome:
    if not can_request:
        return Outcome("forbidden", f"Requests need the {settings.requester_role} role.")
    if not song.downloadable:
        return Outcome("not_downloadable",
                       "band3 can't download that one (it's zipped, or on a site band3 doesn't download from).")
    paired = [g["name"] for g in store.games(conn)]
    having = store.games_having(conn, song)
    if paired and all(name in having for name in paired):
        return Outcome("have", "Already in the song folders")
    existing = store.active_request_for(conn, song.file_id)
    if existing is not None:
        return Outcome("duplicate", f"“{song.title}” is already requested (by {existing['user_name']}).",
                       existing["id"])
    if not approver and settings.per_user_limit and \
            store.requests_today(conn, user_id, now) >= settings.per_user_limit:
        return Outcome("limit", f"That's your {settings.per_user_limit} requests for today; try again tomorrow.")
    needs_approval = bool(settings.approver_role) and not approver
    request_id = store.create_request(conn, song, user_id, user_name, approved=not needs_approval,
                                      channel_id=channel_id, now=now)
    message = f"Requested “{song.title}”" + (": waiting for an approver" if needs_approval else "")
    return Outcome("requested", message, request_id)


class Events:
    """What the web app tells the Discord side; the bot replaces it. No-ops by default."""

    def request_created(self, request_id: int) -> None:
        pass

    def request_changed(self, request_id: int) -> None:
        pass
