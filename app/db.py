"""SQLite storage. One short-lived connection per unit of work."""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- paired games; a game_id paired again keeps its row (cursor, statuses)
CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    cursor       INTEGER NOT NULL DEFAULT 0,  -- the highest `since` the game has sent
    paired_at    INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL DEFAULT 0,
    library_hash TEXT NOT NULL DEFAULT '',
    library_at   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pairings (
    pair_id     TEXT PRIMARY KEY,
    game_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    tries       INTEGER NOT NULL DEFAULT 0,
    confirmed   INTEGER NOT NULL DEFAULT 0
);
-- seq is the request's ID on the wire: given when games may see it (on approval),
-- so a game's cursor never skips a request approved after a later one
CREATE TABLE IF NOT EXISTS requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    seq            INTEGER UNIQUE,
    file_id        TEXT NOT NULL,
    title          TEXT NOT NULL,
    artist         TEXT NOT NULL,
    song_id        INTEGER NOT NULL DEFAULT 0,
    song_json      TEXT NOT NULL DEFAULT '{}',
    user_id        TEXT NOT NULL,
    user_name      TEXT NOT NULL,
    requested_at   INTEGER NOT NULL,
    approved       INTEGER NOT NULL DEFAULT 0,
    approved_by    TEXT NOT NULL DEFAULT '',
    cancelled      INTEGER NOT NULL DEFAULT 0,
    channel_id     TEXT NOT NULL DEFAULT '',
    message_id     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS requests_user ON requests(user_id, requested_at);
CREATE INDEX IF NOT EXISTS requests_file ON requests(file_id);
CREATE TABLE IF NOT EXISTS request_status (
    request_id  INTEGER NOT NULL,
    game_id     TEXT NOT NULL,
    state       TEXT NOT NULL,
    received    INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (request_id, game_id)
);
CREATE TABLE IF NOT EXISTS library (
    game_id     TEXT NOT NULL,
    shortname   TEXT NOT NULL,
    song_id     INTEGER NOT NULL DEFAULT 0,
    title       TEXT NOT NULL DEFAULT '',
    artist      TEXT NOT NULL DEFAULT '',
    album       TEXT NOT NULL DEFAULT '',
    origin      TEXT NOT NULL DEFAULT '',
    genre       TEXT NOT NULL DEFAULT '',
    year        INTEGER NOT NULL DEFAULT 0,
    length_ms   INTEGER NOT NULL DEFAULT 0,
    vocal_parts INTEGER NOT NULL DEFAULT 0,
    tiers       TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (game_id, shortname)
);
CREATE INDEX IF NOT EXISTS library_song_id ON library(song_id);
-- shortnames a game was asked for art of, so a song without any isn't asked every poll
CREATE TABLE IF NOT EXISTS art_asked (
    game_id    TEXT NOT NULL,
    shortname  TEXT NOT NULL,
    asked_at   INTEGER NOT NULL,
    PRIMARY KEY (game_id, shortname)
);
-- RhythmVerse songs seen in searches, so a request by file ID has the song's info
CREATE TABLE IF NOT EXISTS rv_songs (
    file_id  TEXT PRIMARY KEY,
    json     TEXT NOT NULL,
    seen_at  INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)


def kv_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
