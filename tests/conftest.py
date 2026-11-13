from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, signing, store
from app.config import Settings
from app.main import create_app
from app.rhythmverse import Song

JPEG = b"\xff\xd8\xff\xe0fake jpeg\xff\xd9"


def rv_entry(file_id: str, title: str, artist: str = "The Testers", custom_id: str = "",
             url: str | None = None, zipped: bool = False) -> dict:
    return {
        "file": {
            "file_id": file_id, "file_title": title, "file_artist": artist, "file_album": "Demo Album",
            "file_year": 2001, "song_length": 200, "size": 1000, "downloads": 5,
            "author": {"name": "Charter", "shortname": "charter"}, "custom_id": custom_id,
            "download_url": url if url is not None else f"/download_file/u/{file_id}/{title}_rb3con",
            "zippata": zipped, "file_url": f"/song/{file_id}", "album_art": f"/img/{file_id}.jpg",
            "diff_guitar": 3, "diff_drums": 5, "diff_vocals": 0,
        },
        "data": {},
    }


class FakeRhythmVerse:
    """httpx transport answering RhythmVerse's search and art."""

    def __init__(self):
        self.entries = [
            rv_entry("aaa111", "First Song", custom_id="1001"),
            rv_entry("bbb222", "Second Song", custom_id="tester_second"),
            rv_entry("ccc333", "Zipped Song", zipped=True),
            rv_entry("ddd444", "Mega Song", url="https://mega.nz/file/xyz"),
            rv_entry("eee555", "Fire Song", url="https://www.mediafire.com/file/abc/fire.con/file"),
        ]
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.method == "GET" and request.url.path.startswith("/img/"):
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        body = {"status": "success", "data": {
            "records": {"total_filtered": len(self.entries)}, "pagination": {"page": 1},
            "songs": self.entries}}
        return httpx.Response(200, content=json.dumps(body), headers={"content-type": "application/json"})


@pytest.fixture
def settings(tmp_path):
    return Settings(config_dir=str(tmp_path), public_url="https://bot.example", bot_name="Test Bot",
                    library_page=str(tmp_path / "library.html"), per_user_limit=3)


@pytest.fixture
def fake_rv():
    return FakeRhythmVerse()


def make_client(settings, fake_rv) -> TestClient:
    Path(settings.library_page).write_text("<html>library page</html>")
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_rv))
    app = create_app(settings, run_background=False, http=http)
    app.state.bot_state.rv.pause_s = 0
    return TestClient(app)


@pytest.fixture
def client(settings, fake_rv):
    with make_client(settings, fake_rv) as c:
        yield c


@pytest.fixture
def conn(settings, client):
    with contextlib.closing(db.connect(settings.db_path)) as c:
        yield c


def pair_game(client: TestClient, conn, game_id: str = "game-1", name: str = "Desktop") -> str:
    r = client.post("/api/v1/pair/start", json={"game_id": game_id, "name": name})
    assert r.status_code == 200, r.text
    started = r.json()
    assert store.confirm_code(conn, started["code"]).ok
    r = client.post("/api/v1/pair/finish", json={"pair_id": started["pair_id"]})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def song(file_id: str = "aaa111", title: str = "First Song", song_id: int = 1001, downloadable: bool = True) -> Song:
    return Song(file_id=file_id, title=title, artist="The Testers", song_id=song_id,
                download_url=f"https://rhythmverse.co/download_file/{file_id}" if downloadable else "")


def sign_in(client: TestClient, conn, user_id: str = "42", name: str = "someone", **kw) -> None:
    secret = client.app.state.bot_state.secret
    client.cookies.set(signing.COOKIE, signing.make(secret, user_id, name, **kw))
