from __future__ import annotations

import os

from app import signing, store
from tests.conftest import JPEG, auth, pair_game, sign_in, song


def _libraries(client, conn):
    desk = pair_game(client, conn, "g1", "Desktop")
    deck = pair_game(client, conn, "g2", "Deck")
    client.put("/api/v1/library", headers=auth(desk), json={"hash": "a", "songs": [
        {"shortname": "firstsong", "song_id": 1001, "title": "First Song", "artist": "The Testers",
         "album": "Demo\nAlbum", "origin": "ugc_plus", "genre": "rock", "year": 2001, "length_ms": 1,
         "vocal_parts": 1, "tiers": {"guitar": 3}},
        {"shortname": "onlydesk", "song_id": 7, "title": "Only Desk", "artist": "A"},
    ]})
    client.put("/api/v1/library", headers=auth(deck), json={"hash": "b", "songs": [
        {"shortname": "firstsong", "song_id": 1001, "title": "First Song", "artist": "The Testers",
         "origin": "ugc_plus", "tiers": {"guitar": 3}},
    ]})
    return desk, deck


def test_page_endpoints_need_the_cookie(client):
    for path in ("/status", "/list_songs", "/song_details", "/song_1", "/album_art?shortname=x",
                 "/rv/search?text=a", "/rv/downloads"):
        assert client.get(path).status_code == 401, path
    assert client.post("/rv/download", json={"file_id": "aaa111"}).status_code == 401
    assert client.get("/browse").status_code == 401


def test_signed_link_sets_cookie_and_serves_page(client):
    secret = client.app.state.bot_state.secret
    token = signing.make(secret, "42", "someone")
    r = client.get(f"/browse?t={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/browse"
    assert "httponly" in r.headers["set-cookie"].lower() and "secure" in r.headers["set-cookie"].lower()
    client.cookies.set(signing.COOKIE, token)
    r = client.get("/browse")
    assert r.status_code == 200 and "library page" in r.text
    assert r.headers["server"] == "band3-bot"
    assert client.get("/browse?t=bad.token", follow_redirects=False).status_code == 401


def test_status(client, conn):
    sign_in(client, conn)
    pair_game(client, conn, "g1", "Desktop")
    r = client.get("/status").json()
    assert r == {"host": "band3-bot", "name": "Test Bot", "games": [{"name": "Desktop", "online": True}]}


def test_list_songs_merged(client, conn):
    _libraries(client, conn)
    sign_in(client, conn)
    r = client.get("/list_songs")
    assert r.status_code == 200 and r.headers["server"] == "band3-bot"
    assert r.text == (
        "[firstsong]\r\nshortname=firstsong\r\ntitle=First Song\r\nartist=The Testers\r\n"
        "album=Demo Album\r\norigin=ugc_plus\r\n\r\n"
        "[onlydesk]\r\nshortname=onlydesk\r\ntitle=Only Desk\r\nartist=A\r\nalbum=\r\norigin=\r\n\r\n")


def test_song_by_id(client, conn):
    _libraries(client, conn)
    sign_in(client, conn)
    r = client.get("/song_7")
    assert r.status_code == 200 and r.text.startswith("shortname=onlydesk\r\n")
    assert client.get("/song_99").status_code == 404


def test_song_details_has_games(client, conn):
    _libraries(client, conn)
    sign_in(client, conn)
    d = client.get("/song_details").json()
    assert sorted(d["firstsong"]["games"]) == ["Deck", "Desktop"]
    assert d["onlydesk"]["games"] == ["Desktop"]
    assert d["firstsong"]["tiers"] == {"guitar": 3} and d["firstsong"]["year"] == 2001


def test_album_art(client, conn, settings):
    desk, _ = _libraries(client, conn)
    sign_in(client, conn)
    assert client.get("/album_art?shortname=firstsong").status_code == 404
    client.put("/api/v1/art/firstsong", content=JPEG, headers={**auth(desk), "Content-Type": "image/jpeg"})
    r = client.get("/album_art?shortname=firstsong")
    assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"
    assert client.get("/album_art?shortname=../band3-bot.db").status_code == 404
    assert os.path.exists(os.path.join(settings.art_dir, "game", "firstsong.jpg"))


def test_unsupported_endpoints_are_404(client, conn):
    sign_in(client, conn)
    for path in ("/history/summary", "/jump?shortname=x", "/library/duplicates", "/rv/updates", "/karaoke",
                 "/live/events"):
        assert client.get(path).status_code == 404, path


def test_rv_search_marks_what_games_have(client, conn, fake_rv):
    _libraries(client, conn)
    sign_in(client, conn)
    r = client.get("/rv/search?text=song&downloadable=1")
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {s["file_id"]: s for s in body["songs"]}
    # zipped and mega.nz uploads are left out when only downloadable ones are asked for
    assert set(by_id) == {"aaa111", "bbb222", "eee555"}
    assert by_id["aaa111"]["downloaded"] is True and by_id["aaa111"]["in_library"] is True
    assert sorted(by_id["aaa111"]["games"]) == ["Deck", "Desktop"]
    assert by_id["bbb222"]["downloaded"] is False and by_id["bbb222"]["in_library"] is False
    assert by_id["eee555"]["host"] == "www.mediafire.com" and by_id["eee555"]["download"] is True
    assert body["page_size"] == 100
    # cached: the same search doesn't ask RhythmVerse again
    calls = len(fake_rv.calls)
    client.get("/rv/search?text=song&downloadable=1")
    assert len(fake_rv.calls) == calls
    form = fake_rv.calls[0].content.decode()
    assert "text=song" in form and "records=100" in form


def test_rv_download_makes_a_request(client, conn):
    pair_game(client, conn, "g1", "Desktop")
    sign_in(client, conn, channel_id="555")
    client.get("/rv/search?text=song")
    assert client.post("/rv/download", content=b'{"file_id":"aaa111"}').status_code == 415
    assert client.post("/rv/download", json={"file_id": "nope"}).status_code == 404
    assert client.post("/rv/download", json={"file_id": "ccc333"}).status_code == 409  # zipped
    r = client.post("/rv/download", json={"file_id": "aaa111", "update": False})
    assert r.status_code == 200 and r.text.startswith("Requested")
    row = conn.execute("SELECT * FROM requests").fetchone()
    assert row["user_id"] == "42" and row["channel_id"] == "555" and row["approved"] == 1
    # asked again while it's going: no second request
    assert client.post("/rv/download", json={"file_id": "aaa111"}).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1

    d = client.get("/rv/downloads").json()
    assert d["folder"] == ""
    entry = d["downloads"][0]
    assert entry["file_id"] == "aaa111" and entry["state"] == "queued" and entry["requested_by"] == "someone"
    assert entry["games"] == [{"name": "Desktop", "state": "waiting", "received": 0, "total": 0}]


def test_rv_download_per_user_limit(client, conn):
    sign_in(client, conn)
    client.get("/rv/search?text=song")
    for file_id in ("aaa111", "bbb222", "eee555"):
        assert client.post("/rv/download", json={"file_id": file_id}).status_code == 200
    for i in range(2):
        store.remember_songs(conn, [song(f"extra{i}", f"Extra {i}", song_id=0)])
    assert client.post("/rv/download", json={"file_id": "extra0"}).status_code == 429
    # an approver isn't limited
    sign_in(client, conn, approver=True)
    assert client.post("/rv/download", json={"file_id": "extra1"}).status_code == 200


def test_rv_download_already_everywhere(client, conn):
    _libraries(client, conn)
    sign_in(client, conn)
    client.get("/rv/search?text=song")
    r = client.post("/rv/download", json={"file_id": "aaa111"})
    assert r.status_code == 200 and r.text == "Already in the song folders"


def test_rv_downloads_merged_states(client, conn):
    desk = pair_game(client, conn, "g1", "Desktop")
    pair_game(client, conn, "g2", "Deck")
    rid = store.create_request(conn, song(), "1", "someone", approved=True)
    client.post("/api/v1/requests/1/status", headers=auth(desk),
                json={"state": "downloading", "received": 10, "total": 40})
    sign_in(client, conn)
    entry = client.get("/rv/downloads").json()["downloads"][0]
    assert entry["state"] == "downloading" and entry["received"] == 10 and entry["total"] == 40
    games = {g["name"]: g for g in entry["games"]}
    assert games["Desktop"]["state"] == "downloading" and games["Deck"]["state"] == "waiting"
    assert rid
