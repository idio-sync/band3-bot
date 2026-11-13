"""RhythmVerse's (undocumented) API: search, and the songs' album art.

Ported from band3's src/Net/rhythmverse.cpp and tools/rv_import.py. Calls are
spaced PAUSE_S apart, and searches kept SEARCH_TTL_S, so many people on Discord
don't multiply the load on RhythmVerse.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from app import file_hosts

log = logging.getLogger(__name__)

SITE = "https://rhythmverse.co"
LIST_API = SITE + "/api/rb3xbox/songfiles/list"
SEARCH_API = SITE + "/api/rb3xbox/songfiles/search/live"
PAGE_SIZE = 25
DOWNLOADABLE_PAGE_SIZE = 100
PAUSE_S = 2.0
SEARCH_TTL_S = 600
MAX_ART_BYTES = 4 * 1024 * 1024
USER_AGENT = "band3-bot (+https://github.com/idio-sync/band3-bot)"

# RhythmVerse's difficulty fields, by the game's names for the parts. Its values
# are the game's tiers plus one: 1 Warmup to 7 Impossible; 0 and -1 are a part
# the song doesn't have.
PARTS = [
    ("diff_band", "band"), ("diff_guitar", "guitar"), ("diff_bass", "bass"),
    ("diff_drums", "drum"), ("diff_vocals", "vocals"), ("diff_keys", "keys"),
    ("diff_proguitar", "real_guitar"), ("diff_probass", "real_bass"),
    ("diff_prokeys", "real_keys"),
]
# the page's sorts, and RhythmVerse's field and order for each
SORTS = {
    "newest": ("release_date", "DESC"), "updated": ("update_date", "DESC"),
    "downloads": ("downloads", "DESC"), "title": ("title", "ASC"),
    "artist": ("artist", "ASC"), "length": ("length", "ASC"),
}
# RhythmVerse's name for each of the game's parts
INSTRUMENTS = {
    "guitar": "guitar", "bass": "bass", "drum": "drums", "vocals": "vocals", "keys": "keys",
    "real_guitar": "proguitar", "real_bass": "probass", "real_keys": "prokeys",
}

_FILE_ID = re.compile(r"[A-Za-z0-9.]{1,64}")
_AUTHOR_ID = re.compile(r"[a-z0-9_-]{1,64}")


def valid_file_id(file_id: str) -> bool:
    return bool(_FILE_ID.fullmatch(file_id or ""))


def _text(value: Any) -> str:
    if value is None or value is False or value is True:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _whole(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _field(file: dict, data: dict, file_key: str, data_key: str) -> Any:
    own = file.get(file_key)
    return data.get(data_key) if own is None or _text(own) == "" else own


def song_id_of(custom_id: Any) -> int:
    """rhythmverse.cpp's SongIdOf: a text song ID as band3 corrects it."""
    custom_id = _text(custom_id)
    if custom_id in ("", "0"):
        return 0
    if re.fullmatch(r"[+-]?\d+", custom_id):
        n = int(custom_id)
        return n if 0 < n <= 0x7FFFFFFF else 0
    return zlib.crc32(custom_id.encode("utf-8")) % 9999999 + 2130000000


def _escaped(url: str) -> str:
    # what a URL can't hold as it is, escaped; its own escapes left alone
    return quote(url, safe="".join(chr(c) for c in range(0x21, 0x7F) if chr(c) not in '"<>\\^`{|}'))


def absolute(url: str) -> str:
    return SITE + _escaped(url) if url.startswith("/") else _escaped(url)


def _host(url: str) -> str:
    if "://" not in url:
        return ""
    return url.split("://", 1)[1].split("/")[0].split("?")[0].split("#")[0]


def _zipped(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false")
    return bool(value)


@dataclass
class Song:
    file_id: str
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    author: str = ""
    author_id: str = ""
    year: int = 0
    length_s: int = 0
    vocal_parts: int = 0
    size: int = 0
    downloads: int = 0
    tiers: dict[str, int] = field(default_factory=dict)
    art_url: str = ""
    page_url: str = ""
    file_name: str = ""
    song_id: int = 0
    download_url: str = ""  # empty: band3 can't download it
    host: str = ""

    @property
    def downloadable(self) -> bool:
        return bool(self.download_url)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Song:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def parse_song(entry: dict) -> Song | None:
    """rhythmverse.cpp's ParseSong: one upload of a search reply."""
    file = entry.get("file") or {}
    data = entry.get("data") or {}
    if not isinstance(file, dict) or not isinstance(data, dict):
        return None
    file_id = _text(file.get("file_id"))
    if not valid_file_id(file_id):
        return None
    song = Song(file_id=file_id)
    song.title = _text(_field(file, data, "file_title", "title"))
    song.artist = _text(_field(file, data, "file_artist", "artist"))
    song.album = _text(_field(file, data, "file_album", "album"))
    song.genre = _text(_field(file, data, "file_genre", "genre"))
    song.year = _whole(_field(file, data, "file_year", "year"))
    song.length_s = _whole(_field(file, data, "song_length", "song_length"))
    song.vocal_parts = _whole(_field(file, data, "vocal_parts_authored", "vocal_parts"))
    song.size = _whole(file.get("size"))
    song.downloads = _whole(file.get("downloads"))
    author = file.get("author") if isinstance(file.get("author"), dict) else {}
    song.author = _text(author.get("name")) or _text(file.get("user"))
    author_id = _text(author.get("shortname")) or _text(file.get("user"))
    song.author_id = author_id if _AUTHOR_ID.fullmatch(author_id) else ""
    for key, part in PARTS:
        value = _whole(_field(file, data, key, key))
        if value >= 1:
            song.tiers[part] = min(value - 1, 6)
    art = _text(_field(file, data, "album_art", "album_art"))
    if art:
        song.art_url = absolute(art)
    song.page_url = absolute(_text(file.get("file_url")))
    song.file_name = _text(file.get("file_name"))
    song.song_id = song_id_of(file.get("custom_id"))

    url = _text(file.get("external_url")) or _text(file.get("download_url"))
    host = _host(url)
    hosted = bool(url) and host in ("", "rhythmverse.co")
    zipped = _zipped(file.get("zippata"))
    if hosted and not zipped:
        song.download_url = absolute(url)
    elif not hosted and file_hosts.can_download(url):
        song.download_url = url
        song.host = host
    else:
        song.host = "rhythmverse.co" if hosted else host
    return song


# ---- search options, as the page's /rv/search sends them ----

@dataclass
class SearchOptions:
    text: str = ""
    page: int = 1
    sort: str = ""
    downloadable_only: bool = False
    harmonies: bool = False
    has: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    decades: list[int] = field(default_factory=list)
    cap_part: str = ""
    cap_tier: int = -1
    author: str = ""


def _commas(text: str) -> list[str]:
    return [p for p in text.split(",") if p]


def _whole_of(text: str) -> int | None:
    return int(text) if re.fullmatch(r"[+-]?\d{1,9}", text or "") else None


def parse_search_options(query: dict[str, str]) -> SearchOptions:
    """rhythmverse.cpp's ParseSearchOptions, from the query's parameters."""
    o = SearchOptions()
    o.text = query.get("text", "")
    o.page = max(_whole_of(query.get("page", "")) or 1, 1)
    if query.get("sort", "") in SORTS:
        o.sort = query["sort"]
    o.downloadable_only = query.get("downloadable") == "1"
    o.harmonies = query.get("harmonies") == "1"
    o.has = [p for p in _commas(query.get("has", "")) if p in INSTRUMENTS]
    o.genres = [g for g in _commas(query.get("genre", "")) if re.fullmatch(r"[a-z_]{1,32}", g)]
    for decade in _commas(query.get("decade", "")):
        year = _whole_of(decade)
        if year is not None and 1900 <= year <= 2100 and year % 10 == 0:
            o.decades.append(year)
    part, colon, tier = query.get("cap", "").partition(":")
    if colon and part in INSTRUMENTS:
        t = _whole_of(tier)
        if t is not None and 0 <= t <= 6:
            o.cap_part, o.cap_tier = part, t
    if _AUTHOR_ID.fullmatch(query.get("author", "")):
        o.author = query["author"]
    return o


def search_request(o: SearchOptions) -> tuple[str, list[tuple[str, str]], int]:
    """rhythmverse.cpp's Search: (url, form fields, page size)."""
    page_size = DOWNLOADABLE_PAGE_SIZE if o.downloadable_only else PAGE_SIZE
    form = [("records", str(page_size)), ("page", str(max(o.page, 1))), ("data_type", "full")]
    if o.text:
        url = SEARCH_API
        form.append(("text", o.text))
    else:
        url = LIST_API
    sort = o.sort or ("" if o.text else "newest")
    if sort in SORTS:
        by, order = SORTS[sort]
        form += [("sort[0][sort_by]", by), ("sort[0][sort_order]", order)]
    for part in o.has:
        form.append(("instrument[]", INSTRUMENTS[part]))
    if o.harmonies:
        form += [("vocal_parts[]", "2"), ("vocal_parts[]", "3")]
    form += [("genre[]", g) for g in o.genres]
    form += [("decade[]", str(d)) for d in o.decades]
    if o.cap_part in INSTRUMENTS and 0 <= o.cap_tier <= 6:
        form.append(("tierinstrument[]", INSTRUMENTS[o.cap_part]))
        # RhythmVerse's tiers are the game's plus one, and match exactly
        form += [("tier[]", str(t + 1)) for t in range(o.cap_tier + 1)]
    if o.author:
        form.append(("author", o.author))
    return url, form, page_size


@dataclass
class SearchResult:
    total: int = 0
    page: int = 1
    page_size: int = PAGE_SIZE
    songs: list[Song] = field(default_factory=list)


def parse_search(body: Any) -> SearchResult | None:
    if not isinstance(body, dict) or body.get("status") != "success":
        return None
    data = body.get("data") or {}
    songs = data.get("songs")
    # a search that found nothing says "songs": false
    if not isinstance(songs, list):
        if songs is False:
            songs = []
        else:
            return None
    result = SearchResult(
        total=_whole((data.get("records") or {}).get("total_filtered")),
        page=max(_whole((data.get("pagination") or {}).get("page")), 1),
    )
    for entry in songs:
        if isinstance(entry, dict) and (song := parse_song(entry)):
            result.songs.append(song)
    return result


class RhythmVerseError(Exception):
    pass


class RhythmVerse:
    """The client: one call at a time, PAUSE_S apart, searches cached."""

    def __init__(self, http: httpx.AsyncClient, art_dir: str, pause_s: float = PAUSE_S):
        self.http = http
        self.art_dir = art_dir
        self.pause_s = pause_s
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict[tuple, tuple[float, SearchResult]] = {}

    async def _post(self, url: str, form: list[tuple[str, str]]) -> Any:
        async with self._lock:
            wait = self._last + self.pause_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                reply = await self.http.post(url, content=urlencode(form), timeout=30, headers={
                    "User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
            except httpx.HTTPError as exc:
                raise RhythmVerseError(f"Couldn't reach RhythmVerse: {exc}") from exc
            finally:
                self._last = time.monotonic()
        if reply.status_code != 200:
            raise RhythmVerseError(f"RhythmVerse answered {reply.status_code}")
        try:
            return reply.json()
        except ValueError as exc:
            raise RhythmVerseError("RhythmVerse's reply wasn't JSON") from exc

    async def search(self, options: SearchOptions) -> SearchResult:
        url, form, page_size = search_request(options)
        key = (url, tuple(form))
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < SEARCH_TTL_S:
            return hit[1]
        result = parse_search(await self._post(url, form))
        if result is None:
            raise RhythmVerseError("RhythmVerse's reply wasn't one band3-bot can read")
        result.page_size = page_size
        # RhythmVerse can't leave them out itself
        if options.downloadable_only:
            result.songs = [s for s in result.songs if s.downloadable]
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] < SEARCH_TTL_S}
        self._cache[key] = (now, result)
        return result

    def art_path(self, file_id: str) -> str:
        return os.path.join(self.art_dir, "rv", f"{file_id}.jpg")

    async def art(self, song: Song) -> str | None:
        """The song's album art on disk, fetched the first time; None without any."""
        if not valid_file_id(song.file_id):
            return None
        path = self.art_path(song.file_id)
        if os.path.exists(path):
            return path
        # only RhythmVerse's own files, never anywhere a reply points
        parts = urlsplit(song.art_url)
        if parts.scheme != "https" or parts.netloc.lower() not in ("rhythmverse.co", "www.rhythmverse.co"):
            return None
        try:
            reply = await self.http.get(song.art_url, headers={"User-Agent": USER_AGENT}, timeout=20)
        except httpx.HTTPError as exc:
            log.info("Album art for %s: %s", song.file_id, exc)
            return None
        if reply.status_code != 200 or not reply.content or len(reply.content) > MAX_ART_BYTES:
            return None
        if not reply.headers.get("content-type", "").startswith("image/"):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(reply.content)
        os.replace(tmp, path)
        return path


def query_dict(query: str) -> dict[str, str]:
    """A query string's parameters, the first of each, as band3's QueryParam reads them."""
    return {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
