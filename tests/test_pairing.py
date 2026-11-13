from __future__ import annotations

from app import store
from tests.conftest import auth, pair_game


def test_start_gives_a_four_digit_code(client):
    r = client.post("/api/v1/pair/start", json={"game_id": "g1", "name": "Desktop"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["code"]) == 4 and body["code"].isdigit()
    assert body["expires_in"] == 300
    assert body["pair_id"]


def test_start_needs_a_game_id(client):
    assert client.post("/api/v1/pair/start", json={"name": "x"}).status_code == 400
    assert client.post("/api/v1/pair/start", content=b"not json").status_code == 400


def test_three_codes_waiting_then_429(client):
    for _ in range(3):
        assert client.post("/api/v1/pair/start", json={"game_id": "g1", "name": "D"}).status_code == 200
    assert client.post("/api/v1/pair/start", json={"game_id": "g1", "name": "D"}).status_code == 429
    # another game isn't held up
    assert client.post("/api/v1/pair/start", json={"game_id": "g2", "name": "D"}).status_code == 200


def test_finish_waits_then_pairs(client, conn):
    started = client.post("/api/v1/pair/start", json={"game_id": "g1", "name": "Desktop"}).json()
    r = client.post("/api/v1/pair/finish", json={"pair_id": started["pair_id"]})
    assert r.status_code == 202 and r.json() == {"state": "waiting"}
    assert store.confirm_code(conn, started["code"]).ok
    r = client.post("/api/v1/pair/finish", json={"pair_id": started["pair_id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "paired" and body["name"] == "Test Bot" and body["bot_id"]
    assert len(bytes.fromhex(body["token"])) == 32
    # stored hashed, not as sent
    row = conn.execute("SELECT token_hash FROM games WHERE game_id = 'g1'").fetchone()
    assert row["token_hash"] != body["token"] and row["token_hash"] == store.hash_token(body["token"])
    # the pairing is used up
    assert client.post("/api/v1/pair/finish", json={"pair_id": started["pair_id"]}).status_code == 410


def test_code_expires_after_five_minutes(client, conn):
    started = store.start_pairing(conn, "g1", "Desktop", now=1000)
    assert store.finish_pairing(conn, started["pair_id"], "bot", "B", now=1299)[0] == 202
    assert store.finish_pairing(conn, started["pair_id"], "bot", "B", now=1301) == (410, {"state": "expired"})
    assert not store.confirm_code(conn, started["code"], now=1301).ok


def test_five_wrong_tries_expire_the_code(client, conn):
    started = client.post("/api/v1/pair/start", json={"game_id": "g1", "name": "Desktop"}).json()
    wrong = "0000" if started["code"] != "0000" else "1111"
    for _ in range(5):
        assert not store.confirm_code(conn, wrong).ok
    assert not store.confirm_code(conn, started["code"]).ok
    r = client.post("/api/v1/pair/finish", json={"pair_id": started["pair_id"]})
    assert r.status_code == 410 and r.json() == {"state": "expired"}


def test_unknown_pair_id_is_expired(client):
    assert client.post("/api/v1/pair/finish", json={"pair_id": "nope"}).status_code == 410


def test_repair_replaces_token_and_keeps_cursor(client, conn):
    token = pair_game(client, conn, "g1", "Desktop")
    conn.execute("UPDATE games SET cursor = 17")
    new = pair_game(client, conn, "g1", "Desktop 2")
    assert new != token
    row = conn.execute("SELECT * FROM games WHERE game_id = 'g1'").fetchone()
    assert row["cursor"] == 17 and row["name"] == "Desktop 2"
    assert client.get("/api/v1/requests", headers=auth(token)).status_code == 401
    assert client.get("/api/v1/requests", headers=auth(new)).status_code == 200
