from __future__ import annotations

import gzip
import json
import os

from app import store
from tests.conftest import JPEG, auth, pair_game, song


def _request(conn, file_id: str, approved: bool = True, user: str = "1") -> int:
    return store.create_request(conn, song(file_id, f"Song {file_id}"), user, "someone", approved=approved)


def test_unauthorized_calls_are_401(client):
    bad = auth("00" * 32)
    for headers in ({}, bad, {"Authorization": "Basic abc"}):
        assert client.get("/api/v1/requests", headers=headers).status_code == 401
        assert client.post("/api/v1/requests/1/status", json={"state": "queued"}, headers=headers).status_code == 401
        assert client.put("/api/v1/library", json={"hash": "", "songs": []}, headers=headers).status_code == 401
        assert client.put("/api/v1/art/song", content=JPEG, headers={**headers, "Content-Type": "image/jpeg"}
                          ).status_code == 401


def test_unpaired_game_gets_401(client, conn):
    token = pair_game(client, conn)
    store.unpair(conn, "game-1")
    assert client.get("/api/v1/requests", headers=auth(token)).status_code == 401


def test_new_game_gets_every_request_fifty_at_a_time(client, conn):
    for i in range(120):
        _request(conn, f"f{i}")
    token = pair_game(client, conn)  # paired after the requests
    r = client.get("/api/v1/requests?since=0", headers=auth(token)).json()
    assert len(r["requests"]) == 50 and r["cursor"] == 50
    assert [x["id"] for x in r["requests"]] == list(range(1, 51))
    first = r["requests"][0]
    assert set(first) == {"id", "file_id", "title", "artist", "requested_by", "requested_at"}
    r = client.get("/api/v1/requests?since=50", headers=auth(token)).json()
    assert len(r["requests"]) == 50 and r["cursor"] == 100
    r = client.get("/api/v1/requests?since=100", headers=auth(token)).json()
    assert len(r["requests"]) == 20 and r["cursor"] == 120
    r = client.get("/api/v1/requests?since=120", headers=auth(token)).json()
    assert r["requests"] == [] and r["cursor"] == 120
    assert conn.execute("SELECT cursor FROM games").fetchone()[0] == 120


def test_unapproved_and_cancelled_requests_are_left_out(client, conn):
    token = pair_game(client, conn)
    waiting = _request(conn, "w1", approved=False)
    shown = _request(conn, "s1")
    gone = _request(conn, "c1")
    assert store.cancel(conn, gone)
    r = client.get("/api/v1/requests?since=0", headers=auth(token)).json()
    assert [x["file_id"] for x in r["requests"]] == ["s1"]
    # approved later, it comes after the cursor the game already has
    assert store.approve(conn, waiting, "approver")
    r2 = client.get(f"/api/v1/requests?since={r['cursor']}", headers=auth(token)).json()
    assert [x["file_id"] for x in r2["requests"]] == ["w1"]
    assert shown


def test_status_is_kept_per_game_and_merged(client, conn):
    desk = pair_game(client, conn, "g1", "Desktop")
    deck = pair_game(client, conn, "g2", "Deck")
    rid = _request(conn, "aaa111")
    seq = store.get_request(conn, rid)["seq"]
    row = store.get_request(conn, rid)
    assert store.merged(store.game_states(conn, row))["state"] == "queued"

    r = client.post(f"/api/v1/requests/{seq}/status", headers=auth(desk),
                    json={"state": "downloading", "received": 50, "total": 100, "error": ""})
    assert r.status_code == 204
    states = {s["name"]: s for s in store.game_states(conn, row)}
    assert states["Desktop"]["state"] == "downloading" and states["Deck"]["state"] == "waiting"
    m = store.merged(list(states.values()))
    assert m["state"] == "downloading" and m["received"] == 50

    client.post(f"/api/v1/requests/{seq}/status", headers=auth(desk), json={"state": "in_library"})
    assert store.merged(store.game_states(conn, row))["state"] == "queued"  # Deck still waiting
    client.post(f"/api/v1/requests/{seq}/status", headers=auth(deck), json={"state": "have"})
    m = store.merged(store.game_states(conn, row))
    assert m["state"] == "done" and m["in_library"] is True

    assert client.post(f"/api/v1/requests/{seq}/status", headers=auth(deck),
                       json={"state": "exploded"}).status_code == 400
    assert client.post("/api/v1/requests/999/status", headers=auth(deck),
                       json={"state": "queued"}).status_code == 404


def test_merged_failure(conn, client):
    states = [{"name": "Desktop", "state": "done", "received": 0, "total": 0, "error": ""},
              {"name": "Deck", "state": "not_hosted", "received": 0, "total": 0, "error": ""}]
    m = store.merged(states)
    assert m["state"] == "failed" and m["error"].startswith("Deck:")


def test_library_upload_gzip_and_hash(client, conn):
    token = pair_game(client, conn)
    r = client.get("/api/v1/requests", headers={**auth(token), "X-Library-Hash": ""}).json()
    assert r["want_library"] is True
    library = {"hash": "abc123", "songs": [
        {"shortname": "firstsong", "song_id": 1001, "title": "First Song", "artist": "The Testers",
         "album": "Demo", "origin": "ugc_plus", "genre": "rock", "year": 2001, "length_ms": 200000,
         "vocal_parts": 1, "tiers": {"guitar": 3, "drum": 5}},
        {"shortname": "../evil", "song_id": 1},
    ]}
    r = client.put("/api/v1/library", content=gzip.compress(json.dumps(library).encode()),
                   headers={**auth(token), "Content-Encoding": "gzip", "Content-Type": "application/json"})
    assert r.status_code == 204, r.text
    rows = conn.execute("SELECT * FROM library").fetchall()
    assert [r["shortname"] for r in rows] == ["firstsong"]
    assert json.loads(rows[0]["tiers"]) == {"guitar": 3, "drum": 5}
    r = client.get("/api/v1/requests", headers={**auth(token), "X-Library-Hash": "abc123"}).json()
    assert r["want_library"] is False
    r = client.get("/api/v1/requests", headers={**auth(token), "X-Library-Hash": "changed"}).json()
    assert r["want_library"] is True
    # plain JSON works too, and replaces the library
    r = client.put("/api/v1/library", json={"hash": "h2", "songs": []}, headers=auth(token))
    assert r.status_code == 204
    assert conn.execute("SELECT COUNT(*) FROM library").fetchone()[0] == 0


def test_library_rejects_bad_bodies(client, conn):
    token = pair_game(client, conn)
    h = {**auth(token), "Content-Encoding": "gzip"}
    assert client.put("/api/v1/library", content=b"not gzip", headers=h).status_code == 400
    assert client.put("/api/v1/library", content=b"{", headers=auth(token)).status_code == 400
    assert client.put("/api/v1/library", json={"songs": 3}, headers=auth(token)).status_code == 400


def _upload(client, token, n):
    songs = [{"shortname": f"s{i:03d}", "song_id": i + 1, "title": f"T{i}"} for i in range(n)]
    client.put("/api/v1/library", json={"hash": "h", "songs": songs}, headers=auth(token))


def test_want_art(client, conn, settings):
    token = pair_game(client, conn)
    _upload(client, token, 25)
    r = client.get("/api/v1/requests", headers=auth(token)).json()
    assert len(r["want_art"]) == 20 and r["want_art"][0] == "s000"
    # those asked aren't asked again at once; the rest are
    r = client.get("/api/v1/requests", headers=auth(token)).json()
    assert r["want_art"] == ["s020", "s021", "s022", "s023", "s024"]
    r = client.put("/api/v1/art/s000", content=JPEG, headers={**auth(token), "Content-Type": "image/jpeg"})
    assert r.status_code == 204
    path = os.path.join(settings.art_dir, "game", "s000.jpg")
    assert open(path, "rb").read() == JPEG
    # the first copy stays
    client.put("/api/v1/art/s000", content=JPEG + b"x", headers={**auth(token), "Content-Type": "image/jpeg"})
    assert open(path, "rb").read() == JPEG
    # after a day, those without art are asked again, those with it aren't
    conn.execute("UPDATE art_asked SET asked_at = 0")
    r = client.get("/api/v1/requests", headers=auth(token)).json()
    assert "s000" not in r["want_art"] and "s001" in r["want_art"]


def test_art_validation(client, conn):
    token = pair_game(client, conn)
    h = {**auth(token), "Content-Type": "image/jpeg"}
    assert client.put("/api/v1/art/ok", content=b"PNG", headers=h).status_code == 400
    assert client.put("/api/v1/art/ok", content=JPEG, headers={**auth(token), "Content-Type": "image/png"}
                      ).status_code == 415
    assert client.put("/api/v1/art/..bad", content=JPEG, headers=h).status_code == 400


def test_want_art_empty_when_sharing_is_off(settings, fake_rv, tmp_path):
    from dataclasses import replace

    from tests.conftest import make_client
    off = replace(settings, share_album_art=False)
    import contextlib

    from app import db
    with make_client(off, fake_rv) as c, contextlib.closing(db.connect(off.db_path)) as conn:
        token = pair_game(c, conn)
        _upload(c, token, 3)
        assert c.get("/api/v1/requests", headers=auth(token)).json()["want_art"] == []
