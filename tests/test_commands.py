"""The Discord commands' logic, without Discord."""

from __future__ import annotations

from dataclasses import replace

from app import commands, signing, store
from app.commands import Member
from app.service import request_song
from tests.conftest import auth, pair_game, song

USER = Member("1", "someone")
OTHER = Member("2", "other")
OWNER = Member("3", "owner", owner=True)
APPROVER = Member("4", "approver", roles=["DJs", "777"])


def test_roles(settings):
    s = replace(settings, approver_role="DJs", requester_role="Band")
    assert commands.is_approver(s, APPROVER) and commands.is_approver(s, OWNER)
    assert not commands.is_approver(s, USER)
    assert not commands.can_request(s, USER)
    assert commands.can_request(s, Member("5", "x", roles=["band"]))
    assert commands.can_request(s, APPROVER)
    assert commands.is_approver(replace(settings, approver_role="777"), APPROVER)


def test_request_needs_approval_when_approver_role_set(client, conn, settings):
    s = replace(settings, approver_role="DJs")
    out = request_song(conn, s, song(), USER.user_id, USER.name, can_request=True, approver=False)
    assert out.kind == "requested" and "approver" in out.message
    row = store.get_request(conn, out.request_id)
    assert row["approved"] == 0 and row["seq"] is None
    assert "waiting for approval" in commands.render_request(conn, row) or "no games" in \
        commands.render_request(conn, row)
    assert store.approve(conn, out.request_id, APPROVER.user_id)
    assert store.get_request(conn, out.request_id)["seq"] == 1
    assert not store.approve(conn, out.request_id, APPROVER.user_id)
    # an approver's own requests need no approval
    out = request_song(conn, s, song("bbb222", "Second"), "4", "approver", can_request=True, approver=True)
    assert store.get_request(conn, out.request_id)["approved"] == 1


def test_per_user_limit(client, conn, settings):
    for i in range(3):
        assert request_song(conn, settings, song(f"f{i}"), "1", "u", can_request=True, approver=False).kind == \
            "requested"
    assert request_song(conn, settings, song("f9"), "1", "u", can_request=True, approver=False).kind == "limit"
    assert request_song(conn, settings, song("f9"), "2", "v", can_request=True, approver=False).kind == "requested"
    # a day later the count starts over
    now = store.now_s() + 24 * 3600 + 1
    assert request_song(conn, settings, song("f8"), "1", "u", can_request=True, approver=False, now=now).kind == \
        "requested"


def test_not_downloadable_and_forbidden(client, conn, settings):
    out = request_song(conn, settings, song(downloadable=False), "1", "u", can_request=True, approver=False)
    assert out.kind == "not_downloadable"
    out = request_song(conn, settings, song(), "1", "u", can_request=False, approver=False)
    assert out.kind == "forbidden"


def test_render_request(client, conn):
    desk = pair_game(client, conn, "g1", "Desktop")
    pair_game(client, conn, "g2", "Deck")
    pair_game(client, conn, "g3", "Laptop")
    rid = store.create_request(conn, song(), "1", "someone", approved=True)
    client.post("/api/v1/requests/1/status", headers=auth(desk), json={"state": "in_library"})
    laptop = pair_game(client, conn, "g3", "Laptop")
    client.post("/api/v1/requests/1/status", headers=auth(laptop),
                json={"state": "downloading", "received": 42, "total": 100})
    text = commands.render_request(conn, store.get_request(conn, rid))
    lines = text.splitlines()
    assert lines[0].startswith("🎸 **First Song** — The Testers") and "<@1>" in lines[0]
    assert "Deck" in lines[1] and "waiting for the game" in lines[1]
    assert "Desktop" in lines[2] and "in library" in lines[2]
    assert "Laptop" in lines[3] and "42%" in lines[3]


def test_cancel(client, conn, settings):
    s = replace(settings, approver_role="DJs")
    token = pair_game(client, conn)
    rid = store.create_request(conn, song(), "1", "someone", approved=True)
    assert commands.cancel(conn, s, OTHER, str(rid))[1] is None
    assert [v for _, v in commands.cancel_choices(conn, s, USER, "")] == [str(rid)]
    assert commands.cancel_choices(conn, s, OTHER, "") == []
    text, cancelled = commands.cancel(conn, s, USER, f"#{rid}")
    assert cancelled == rid and "Cancelled" in text
    assert "cancelled" in commands.render_request(conn, store.get_request(conn, rid))
    # once a game has started, no one can cancel
    rid2 = store.create_request(conn, song("bbb222", "Second"), "1", "someone", approved=True)
    seq = store.get_request(conn, rid2)["seq"]
    client.post(f"/api/v1/requests/{seq}/status", headers=auth(token), json={"state": "queued"})
    assert commands.cancel(conn, s, APPROVER, str(rid2))[1] is None


def test_pair_and_unpair_need_an_approver(client, conn, settings):
    s = replace(settings, approver_role="DJs")
    started = store.start_pairing(conn, "g9", "Deck")
    assert "Only" in commands.pair(conn, s, USER, started["code"])
    assert "Paired" in commands.pair(conn, s, APPROVER, started["code"])
    status, body = store.finish_pairing(conn, started["pair_id"], "b", "B")
    assert status == 200
    assert "Deck" in commands.games_text(conn) and "online now" in commands.games_text(conn)
    assert commands.game_choices(conn, "de") == [("Deck", "g9")]
    assert "Only" in commands.unpair(conn, s, USER, "g9")
    assert "Unpaired" in commands.unpair(conn, s, OWNER, "g9")
    assert "No games" in commands.games_text(conn)


def test_queue_text(client, conn):
    token = pair_game(client, conn)
    assert "Nothing waiting" in commands.queue_text(conn)
    store.create_request(conn, song(), "1", "someone", approved=True)
    assert "First Song" in commands.queue_text(conn)
    client.post("/api/v1/requests/1/status", headers=auth(token), json={"state": "have"})
    assert "Nothing waiting" in commands.queue_text(conn)


def test_browse_link(settings):
    text = commands.browse_link(b"k" * 32, settings, APPROVER, "123", now=1000)
    token = text.split("/browse?t=")[1]
    assert text.split("\n")[1].startswith("https://bot.example/browse?t=")
    v = signing.verify(b"k" * 32, token, now=1001)
    assert v.user_id == "4" and v.channel_id == "123"
    assert "PUBLIC_URL" in commands.browse_link(b"k", replace(settings, public_url=""), USER, "1")


def test_song_embed(client, conn):
    from app.rhythmverse import parse_song
    from tests.conftest import rv_entry
    s = parse_song(rv_entry("aaa111", "First Song", custom_id="1001"))
    e = commands.song_embed(s, ["Desktop"], art_attached=True)
    assert e["title"] == "First Song — The Testers"
    assert e["thumbnail"]["url"] == "attachment://aaa111.jpg"
    assert "Desktop" in e["description"] and "Guitar" in e["description"]
    label, desc, value = commands.match_option(s)
    assert value == "aaa111" and len(label) <= 100
