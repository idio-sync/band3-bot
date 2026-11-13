"""What the web app's parts share: settings, the database, RhythmVerse, Discord's hooks."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field

from app import db
from app.config import Settings
from app.rhythmverse import RhythmVerse
from app.service import Events


@dataclass
class AppState:
    settings: Settings
    secret: bytes = b""
    rv: RhythmVerse | None = None
    events: Events = field(default_factory=Events)

    @contextlib.contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        conn = db.connect(self.settings.db_path)
        try:
            yield conn
        finally:
            conn.close()
