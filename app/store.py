"""The bot's state: pairing, games, requests and their per-game states, libraries.

Plain functions over a sqlite3 connection, so the API, the page and the Discord
commands share them and tests drive them directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.db import kv_get, kv_set
from app.rhythmverse import Song

PAIR_TTL_S = 300
PAIR_TRIES = 5
PAIR_MAX_WAITING = 3
ONLINE_S = 180
PAGE_LIMIT = 50
WANT_ART_MAX = 20
# a game asked for a shortname's art isn't asked again for this long (a song
# without art would otherwise be asked for at every poll)
ART_ASK_AGAIN_S = 24 * 3600
DAY_S = 24 * 3600

STATES = ("queued", "downloading", "done", "in_library", "have", "failed", "not_hosted", "no_folder")
GOOD = frozenset({"done", "in_library", "have"})
BAD = frozenset({"failed", "not_hosted", "no_folder"})
BAD_TEXT = {
    "not_hosted": "band3 can't download it from where it's hosted",
    "no_folder": "the game has no folder to download to",
}

_GAME_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_SHORTNAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")


def now_s() -> int:
    return int(time.time())


def valid_game_id(game_id: Any) -> bool:
    return isinstance(game_id, str) and bool(_GAME_ID.fullmatch(game_id))


def valid_shortname(shortname: Any) -> bool:
    return isinstance(shortname, str) and bool(_SHORTNAME.fullmatch(shortname)) and ".." not in shortname


def clean_name(name: Any, fallback: str = "band3") -> str:
    text = " ".join(str(name or "").split())[:64]
    return text or fallback


# ---- identity ---------------------------------------------------------------

def bot_id(conn: sqlite3.Connection) -> str:
    value = kv_get(conn, "bot_id")
    if not value:
        value = str(uuid.uuid4())
        kv_set(conn, "bot_id", value)
    return value


def secret(conn: sqlite3.Connection, configured: str = "") -> bytes:
    if configured:
        return configured.encode()
    value = kv_get(conn, "secret")
    if not value:
        value = secrets.token_hex(32)
        kv_set(conn, "secret", value)
    return value.encode()


# ---- pairing ------------------------------------------------------------------

class TooManyCodes(Exception):
    pass


def _expire_pairings(conn: sqlite3.Connection, now: int) -> None:
    conn.execute("DELETE FROM pairings WHERE expires_at < ?", (now - 3600,))


def start_pairing(conn: sqlite3.Connection, game_id: str, name: str, now: int | None = None) -> dict:
    now = now_s() if now is None else now
    _expire_pairings(conn, now)
    waiting = conn.execute(
        "SELECT COUNT(*) FROM pairings WHERE game_id = ? AND expires_at > ? AND tries < ? AND confirmed = 0",
        (game_id, now, PAIR_TRIES)).fetchone()[0]
    if waiting >= PAIR_MAX_WAITING:
        raise TooManyCodes
    live = {r[0] for r in conn.execute(
        "SELECT code FROM pairings WHERE expires_at > ? AND tries < ?", (now, PAIR_TRIES))}
    code = f"{secrets.randbelow(10000):04d}"
    for _ in range(50):
        if code not in live:
            break
        code = f"{secrets.randbelow(10000):04d}"
    pair_id = secrets.token_urlsafe(18)
    conn.execute(
        "INSERT INTO pairings (pair_id, game_id, name, code, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (pair_id, game_id, clean_name(name), code, now, now + PAIR_TTL_S))
    return {"pair_id": pair_id, "code": code, "expires_in": PAIR_TTL_S}


@dataclass
class PairResult:
    ok: bool
    name: str = ""
    message: str = ""


def confirm_code(conn: sqlite3.Connection, code: str, now: int | None = None) -> PairResult:
    """/pair <code>. A wrong code counts as a try against every code waiting, so
    five guesses expire them all (a code can't be found by trying them)."""
    now = now_s() if now is None else now
    code = (code or "").strip()
    row = conn.execute(
        "SELECT * FROM pairings WHERE code = ? AND expires_at > ? AND tries < ? AND confirmed = 0 "
        "ORDER BY created_at DESC LIMIT 1", (code, now, PAIR_TRIES)).fetchone()
    if row is None:
        conn.execute("UPDATE pairings SET tries = tries + 1 WHERE expires_at > ? AND confirmed = 0", (now,))
        return PairResult(False, message="No game is showing that code. Check Settings > Online in band3.")
    conn.execute("UPDATE pairings SET confirmed = 1 WHERE pair_id = ?", (row["pair_id"],))
    return PairResult(True, name=row["name"], message=f"Paired **{row['name']}**.")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def finish_pairing(conn: sqlite3.Connection, pair_id: str, bot: str, bot_name: str,
                   now: int | None = None) -> tuple[int, dict]:
    now = now_s() if now is None else now
    row = conn.execute("SELECT * FROM pairings WHERE pair_id = ?", (pair_id or "",)).fetchone()
    if row is None or (not row["confirmed"] and (row["expires_at"] <= now or row["tries"] >= PAIR_TRIES)):
        return 410, {"state": "expired"}
    if not row["confirmed"]:
        return 202, {"state": "waiting"}
    token = secrets.token_bytes(32).hex()
    # pairing a game_id the bot already has replaces its token and keeps its cursor
    conn.execute(
        "INSERT INTO games (game_id, name, token_hash, paired_at, last_seen) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(game_id) DO UPDATE SET name = excluded.name, token_hash = excluded.token_hash, "
        "paired_at = excluded.paired_at, last_seen = excluded.last_seen",
        (row["game_id"], row["name"], hash_token(token), now, now))
    conn.execute("DELETE FROM pairings WHERE pair_id = ?", (pair_id,))
    return 200, {"state": "paired", "token": token, "bot_id": bot, "name": bot_name}


def game_for_token(conn: sqlite3.Connection, token: str, now: int | None = None) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute("SELECT * FROM games WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if row is not None:
        conn.execute("UPDATE games SET last_seen = ? WHERE game_id = ?",
                     (now_s() if now is None else now, row["game_id"]))
    return row


def games(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM games ORDER BY name COLLATE NOCASE, game_id").fetchall()


def unpair(conn: sqlite3.Connection, game_id: str) -> str | None:
    row = conn.execute("SELECT name FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if row is None:
        return None
    for table in ("games", "library", "art_asked", "request_status"):
        conn.execute(f"DELETE FROM {table} WHERE game_id = ?", (game_id,))
    return row["name"]


def is_online(game: sqlite3.Row, now: int | None = None) -> bool:
    return (now_s() if now is None else now) - game["last_seen"] <= ONLINE_S


# ---- requests -------------------------------------------------------------------

def remember_songs(conn: sqlite3.Connection, songs: list[Song], now: int | None = None) -> None:
    now = now_s() if now is None else now
    conn.executemany(
        "INSERT INTO rv_songs (file_id, json, seen_at) VALUES (?, ?, ?) "
        "ON CONFLICT(file_id) DO UPDATE SET json = excluded.json, seen_at = excluded.seen_at",
        [(s.file_id, json.dumps(s.to_json()), now) for s in songs])


def known_song(conn: sqlite3.Connection, file_id: str) -> Song | None:
    row = conn.execute("SELECT json FROM rv_songs WHERE file_id = ?", (file_id,)).fetchone()
    return Song.from_json(json.loads(row["json"])) if row else None


def requests_today(conn: sqlite3.Connection, user_id: str, now: int | None = None) -> int:
    now = now_s() if now is None else now
    return conn.execute("SELECT COUNT(*) FROM requests WHERE user_id = ? AND requested_at > ?",
                        (user_id, now - DAY_S)).fetchone()[0]


def active_request_for(conn: sqlite3.Connection, file_id: str) -> sqlite3.Row | None:
    """An uncancelled request for the upload that not every game has done yet."""
    for row in conn.execute("SELECT * FROM requests WHERE file_id = ? AND cancelled = 0 ORDER BY id DESC",
                            (file_id,)):
        if merged(game_states(conn, row))["state"] != "done":
            return row
    return None


def _next_seq(conn: sqlite3.Connection) -> int:
    return (conn.execute("SELECT MAX(seq) FROM requests").fetchone()[0] or 0) + 1


def create_request(conn: sqlite3.Connection, song: Song, user_id: str, user_name: str, approved: bool,
                   channel_id: str = "", now: int | None = None) -> int:
    now = now_s() if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO requests (seq, file_id, title, artist, song_id, song_json, user_id, user_name, "
            "requested_at, approved, channel_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_next_seq(conn) if approved else None, song.file_id, song.title, song.artist, song.song_id,
             json.dumps(song.to_json()), user_id, clean_name(user_name, "someone"), now, int(approved),
             channel_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return int(cur.lastrowid)


def approve(conn: sqlite3.Connection, request_id: int, by: str) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT approved, cancelled FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None or row["approved"] or row["cancelled"]:
            conn.execute("ROLLBACK")
            return False
        conn.execute("UPDATE requests SET approved = 1, approved_by = ?, seq = ? WHERE id = ?",
                     (by, _next_seq(conn), request_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


def get_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()


def set_message(conn: sqlite3.Connection, request_id: int, channel_id: str, message_id: str) -> None:
    conn.execute("UPDATE requests SET channel_id = ?, message_id = ? WHERE id = ?",
                 (channel_id, message_id, request_id))


def can_cancel(conn: sqlite3.Connection, request: sqlite3.Row) -> bool:
    """Only while no game has started it (reported anything)."""
    if request["cancelled"]:
        return False
    return conn.execute("SELECT COUNT(*) FROM request_status WHERE request_id = ?",
                        (request["id"],)).fetchone()[0] == 0


def cancel(conn: sqlite3.Connection, request_id: int) -> bool:
    row = get_request(conn, request_id)
    if row is None or not can_cancel(conn, row):
        return False
    conn.execute("UPDATE requests SET cancelled = 1 WHERE id = ?", (request_id,))
    return True


def requests_for_game(conn: sqlite3.Connection, game: sqlite3.Row, since: int) -> tuple[int, list[dict]]:
    since = max(int(since), 0)
    conn.execute("UPDATE games SET cursor = MAX(cursor, ?) WHERE game_id = ?", (since, game["game_id"]))
    rows = conn.execute(
        "SELECT * FROM requests WHERE approved = 1 AND cancelled = 0 AND seq > ? ORDER BY seq LIMIT ?",
        (since, PAGE_LIMIT)).fetchall()
    out = [{"id": r["seq"], "file_id": r["file_id"], "title": r["title"], "artist": r["artist"],
            "requested_by": r["user_name"], "requested_at": r["requested_at"]} for r in rows]
    cursor = rows[-1]["seq"] if rows else since
    return cursor, out


def set_status(conn: sqlite3.Connection, game: sqlite3.Row, seq: int, state: str, received: int, total: int,
               error: str, now: int | None = None) -> int | None:
    """The request's internal ID, or None when there's no such request."""
    row = conn.execute("SELECT id FROM requests WHERE seq = ?", (seq,)).fetchone()
    if row is None:
        return None
    conn.execute(
        "INSERT INTO request_status (request_id, game_id, state, received, total, error, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(request_id, game_id) DO UPDATE SET state = excluded.state, "
        "received = excluded.received, total = excluded.total, error = excluded.error, "
        "updated_at = excluded.updated_at",
        (row["id"], game["game_id"], state, received, total, error[:500], now_s() if now is None else now))
    return int(row["id"])


def game_states(conn: sqlite3.Connection, request: sqlite3.Row) -> list[dict]:
    """Each paired game's state of the request: a protocol state, or "waiting"
    until the game reports one."""
    reported = {r["game_id"]: r for r in conn.execute(
        "SELECT * FROM request_status WHERE request_id = ?", (request["id"],))}
    out = []
    for g in games(conn):
        r = reported.get(g["game_id"])
        out.append({
            "game_id": g["game_id"], "name": g["name"],
            "state": r["state"] if r else "waiting",
            "received": r["received"] if r else 0, "total": r["total"] if r else 0,
            "error": r["error"] if r else "",
        })
    return out


def merged(states: list[dict]) -> dict:
    """One state across games, as the page's /rv/downloads gives one:
    queued | downloading | done | failed."""
    if not states:
        return {"state": "queued", "received": 0, "total": 0, "error": "", "in_library": None}
    names = [s["state"] for s in states]
    in_library = all(n in ("in_library", "have") for n in names)
    if all(n in GOOD for n in names):
        return {"state": "done", "received": 0, "total": 0, "error": "", "in_library": in_library}
    going = [s for s in states if s["state"] == "downloading"]
    if going:
        return {"state": "downloading", "received": sum(s["received"] for s in going),
                "total": sum(s["total"] for s in going), "error": "", "in_library": False}
    if any(n in ("waiting", "queued") for n in names):
        return {"state": "queued", "received": 0, "total": 0, "error": "", "in_library": False}
    bad = next(s for s in states if s["state"] in BAD)
    error = bad["error"] or BAD_TEXT.get(bad["state"], "")
    return {"state": "failed", "received": 0, "total": 0, "error": f"{bad['name']}: {error}".rstrip(": "),
            "in_library": False}


def open_requests(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM requests WHERE cancelled = 0 ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()


# ---- libraries --------------------------------------------------------------------

def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _str(value: Any, limit: int = 256) -> str:
    return str(value)[:limit] if isinstance(value, str | int | float) else ""


def replace_library(conn: sqlite3.Connection, game: sqlite3.Row, body: dict, now: int | None = None) -> int:
    songs = body.get("songs")
    if not isinstance(songs, list):
        raise ValueError("songs must be a list")
    rows = []
    for s in songs:
        if not isinstance(s, dict) or not valid_shortname(s.get("shortname")):
            continue
        tiers = s.get("tiers") if isinstance(s.get("tiers"), dict) else {}
        tiers = {str(k)[:32]: _int(v) for k, v in tiers.items()}
        rows.append((game["game_id"], s["shortname"], _int(s.get("song_id")), _str(s.get("title")),
                     _str(s.get("artist")), _str(s.get("album")), _str(s.get("origin"), 64),
                     _str(s.get("genre"), 64), _int(s.get("year")), _int(s.get("length_ms")),
                     _int(s.get("vocal_parts")), json.dumps(tiers)))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM library WHERE game_id = ?", (game["game_id"],))
        conn.executemany("INSERT OR REPLACE INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("UPDATE games SET library_hash = ?, library_at = ? WHERE game_id = ?",
                     (_str(body.get("hash"), 128), now_s() if now is None else now, game["game_id"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def want_library(game: sqlite3.Row, header_hash: str) -> bool:
    return not game["library_at"] or not header_hash or game["library_hash"] != header_hash


def want_art(conn: sqlite3.Connection, game: sqlite3.Row, art_dir: str, share: bool,
             now: int | None = None) -> list[str]:
    if not share:
        return []
    now = now_s() if now is None else now
    asked = {r["shortname"] for r in conn.execute(
        "SELECT shortname FROM art_asked WHERE game_id = ? AND asked_at > ?",
        (game["game_id"], now - ART_ASK_AGAIN_S))}
    out = []
    for r in conn.execute("SELECT shortname FROM library WHERE game_id = ? ORDER BY shortname",
                          (game["game_id"],)):
        name = r["shortname"]
        if name in asked or os.path.exists(os.path.join(art_dir, "game", f"{name}.jpg")):
            continue
        out.append(name)
        if len(out) >= WANT_ART_MAX:
            break
    conn.executemany(
        "INSERT INTO art_asked (game_id, shortname, asked_at) VALUES (?, ?, ?) "
        "ON CONFLICT(game_id, shortname) DO UPDATE SET asked_at = excluded.asked_at",
        [(game["game_id"], n, now) for n in out])
    return out


def merged_library(conn: sqlite3.Connection) -> list[dict]:
    """Every paired game's songs by shortname, with the names of the games that have each."""
    by_name: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT l.*, g.name AS game_name FROM library l JOIN games g ON g.game_id = l.game_id "
            "ORDER BY g.name COLLATE NOCASE, l.shortname"):
        song = by_name.get(r["shortname"])
        if song is None:
            song = by_name[r["shortname"]] = {
                "shortname": r["shortname"], "song_id": r["song_id"], "title": r["title"],
                "artist": r["artist"], "album": r["album"], "origin": r["origin"], "genre": r["genre"],
                "year": r["year"], "length_ms": r["length_ms"], "vocal_parts": r["vocal_parts"],
                "tiers": json.loads(r["tiers"]), "games": [],
            }
        else:
            # what one game's upload leaves out, another's may have
            for key in ("song_id", "title", "artist", "album", "origin", "genre", "year", "length_ms",
                        "vocal_parts"):
                if not song[key] and r[key]:
                    song[key] = r[key]
            if not song["tiers"]:
                song["tiers"] = json.loads(r["tiers"])
        if r["game_name"] not in song["games"]:
            song["games"].append(r["game_name"])
    return list(by_name.values())


def games_with_song_ids(conn: sqlite3.Connection) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for r in conn.execute("SELECT DISTINCT l.song_id, g.name FROM library l JOIN games g "
                          "ON g.game_id = l.game_id WHERE l.song_id != 0"):
        out.setdefault(r["song_id"], []).append(r["name"])
    return out


def games_with_file(conn: sqlite3.Connection, file_id: str) -> set[str]:
    """Names of the games that reported having a request's upload."""
    return {r["name"] for r in conn.execute(
        "SELECT g.name FROM request_status s JOIN requests q ON q.id = s.request_id "
        "JOIN games g ON g.game_id = s.game_id WHERE q.file_id = ? AND s.state IN ('done', 'in_library', 'have')",
        (file_id,))}


def games_having(conn: sqlite3.Connection, song: Song) -> list[str]:
    names = set(games_with_file(conn, song.file_id))
    if song.song_id:
        names.update(r["name"] for r in conn.execute(
            "SELECT DISTINCT g.name FROM library l JOIN games g ON g.game_id = l.game_id WHERE l.song_id = ?",
            (song.song_id,)))
    return sorted(names, key=str.lower)
