"""Signing, discovery, RhythmVerse parsing, file hosts."""

from __future__ import annotations

import json

import pytest

from app import discovery, file_hosts, signing
from app.rhythmverse import parse_search_options, parse_song, search_request, song_id_of
from tests.conftest import rv_entry

SECRET = b"s" * 32


def test_signed_link_round_trip():
    token = signing.make(SECRET, "42", "someone", "99", can_request=False, approver=True, now=1000)
    v = signing.verify(SECRET, token, now=1001)
    assert v.user_id == "42" and v.name == "someone" and v.channel_id == "99"
    assert v.can_request is False and v.approver is True and v.expires == 1000 + 24 * 3600


def test_signed_link_expires_and_resists_tampering():
    token = signing.make(SECRET, "42", "someone", now=1000)
    assert signing.verify(SECRET, token, now=1000 + 24 * 3600) is None
    assert signing.verify(b"other" * 8, token, now=1001) is None
    payload, mac = token.split(".")
    forged = signing.sign(b"wrong", signing.Viewer("1", "x", 10**12))
    assert signing.verify(SECRET, forged.split(".")[0] + "." + mac, now=1001) is None
    assert signing.verify(SECRET, None) is None and signing.verify(SECRET, "garbage") is None


def test_discovery_answer():
    msg = json.dumps({"band3": 1, "want": "requests", "game_id": "g"}).encode()
    out = json.loads(discovery.answer(msg, "192.168.1.5", "bid", "Bot", "", 8080, lambda peer: "192.168.1.20"))
    assert out == {"band3_bot": 1, "protocol": 1, "bot_id": "bid", "name": "Bot", "url": "http://192.168.1.20:8080"}
    out = json.loads(discovery.answer(msg, "1.2.3.4", "bid", "Bot", "https://bot.example", 8080))
    assert out["url"] == "https://bot.example"
    assert discovery.answer(b"{}", "1.2.3.4", "b", "B", "", 1) is None
    assert discovery.answer(b"nonsense", "1.2.3.4", "b", "B", "", 1) is None
    assert discovery.answer(json.dumps({"band3": 1, "want": "other"}).encode(), "1.2.3.4", "b", "B", "", 1) is None


def test_song_id_of():
    assert song_id_of("1001") == 1001
    assert song_id_of("0") == 0 and song_id_of("") == 0 and song_id_of(None) == 0
    assert song_id_of("-5") == 0
    assert 2130000000 <= song_id_of("tester_second") < 2130000000 + 9999999


def test_parse_song():
    s = parse_song(rv_entry("aaa111", "First Song", custom_id="1001"))
    assert s.download_url.startswith("https://rhythmverse.co/download_file/u/aaa111/First%20Song")
    assert s.tiers == {"guitar": 2, "drum": 4}
    assert s.art_url == "https://rhythmverse.co/img/aaa111.jpg" and s.song_id == 1001
    zipped = parse_song(rv_entry("ccc333", "Z", zipped=True))
    assert not zipped.downloadable and zipped.host == "rhythmverse.co"
    zipped_text = parse_song(rv_entry("ccc334", "Z", zipped="1"))
    assert not zipped_text.downloadable
    mega = parse_song(rv_entry("ddd444", "M", url="https://mega.nz/file/x"))
    assert not mega.downloadable and mega.host == "mega.nz"
    assert parse_song({"file": {"file_id": "../x"}, "data": {}}) is None


@pytest.mark.parametrize("url,ok", [
    ("https://rhythmverse.co/download_file/x", True),
    ("https://www.mediafire.com/file/abc/song/file", True),
    ("https://www.mediafire.com/file/abc/song.zip/file", False),
    ("https://www.mediafire.com/?abc123", True),
    ("https://drive.google.com/file/d/1AbC_d-E/view", True),
    ("https://drive.google.com/open?id=1AbC", True),
    ("https://drive.google.com/drive/folders/1AbC", False),
    ("https://www.dropbox.com/s/abc/song?dl=0", True),
    ("https://www.dropbox.com/scl/fi/abc/song", True),
    ("https://dingorb.com/song?id=abc_1", True),
    ("https://bit.ly/abc", True),
    ("https://tinyurl.com/", False),
    ("https://mega.nz/file/abc", False),
    ("ftp://rhythmverse.co/x", False),
    ("https://example.com/song", False),
])
def test_file_hosts(url, ok):
    assert file_hosts.can_download(url) is ok


def test_search_request():
    o = parse_search_options({"text": "abc", "page": "2", "sort": "downloads", "downloadable": "1",
                              "has": "guitar,real_keys,bogus", "decade": "1990,1995", "cap": "drum:3",
                              "genre": "rock,Bad Genre", "author": "Bad Author"})
    url, form, size = search_request(o)
    assert url.endswith("/search/live") and size == 100
    d = [f"{k}={v}" for k, v in form]
    assert "page=2" in d and "text=abc" in d and "sort[0][sort_by]=downloads" in d
    assert "instrument[]=guitar" in d and "instrument[]=prokeys" in d
    assert "decade[]=1990" in d and "decade[]=1995" not in d
    assert "genre[]=rock" in d and len([x for x in d if x.startswith("genre")]) == 1
    assert [x for x in d if x.startswith("tier[]")] == ["tier[]=1", "tier[]=2", "tier[]=3", "tier[]=4"]
    assert not any(x.startswith("author") for x in d)
    url, form, size = search_request(parse_search_options({}))
    assert url.endswith("/list") and size == 25 and ("sort[0][sort_by]", "release_date") in form


def test_rhythmverse_spaces_calls_and_caches(tmp_path):
    import asyncio
    import time

    import httpx

    from app.rhythmverse import RhythmVerse, SearchOptions
    from tests.conftest import FakeRhythmVerse

    fake = FakeRhythmVerse()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            rv = RhythmVerse(http, str(tmp_path), pause_s=0.3)
            start = time.monotonic()
            await rv.search(SearchOptions(text="a"))
            await rv.search(SearchOptions(text="b"))
            await rv.search(SearchOptions(text="a"))  # cached
            return time.monotonic() - start

    elapsed = asyncio.run(run())
    # Windows' timer can wake a few ms early (15.6 ms ticks)
    assert len(fake.calls) == 2 and elapsed >= 0.25
