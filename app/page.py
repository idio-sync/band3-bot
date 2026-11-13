"""The browse page: band3's own library page (web/library.html), and the endpoints
it reads, answered from every paired game's library merged.

Only what the bot does is answered; the rest of band3's endpoints (/history/*,
/jump, /library/*, /rv/updates, /karaoke, /live/events...) are 404s, which the
page takes as "not here". Every page endpoint needs the browse cookie that the
signed link from /browse sets.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from app import signing, store
from app.rhythmverse import RhythmVerseError, parse_search_options, query_dict
from app.service import request_song
from app.signing import Viewer
from app.state import AppState

log = logging.getLogger(__name__)

TEXT = "text/plain; charset=utf-8"
ART_MAX_AGE = 3600

NOT_SIGNED_IN = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>band3-bot</title>
<style>body{font:16px system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;
color:#ddd;background:#111}code{background:#222;padding:.1em .3em;border-radius:3px}</style></head>
<body><h1>Song library</h1><p>{message}</p></body></html>"""


def _value(text: object) -> str:
    # a value on a line of its own: line breaks in it would start another key
    return str(text or "").replace("\r", " ").replace("\n", " ")


def format_song(song: dict, section: bool) -> str:
    """band3's FormatSong (RB3E's INI-style block)."""
    out = f"[{_value(song['shortname'])}]\r\n" if section else ""
    for key in ("shortname", "title", "artist", "album", "origin"):
        out += f"{key}={_value(song.get(key))}\r\n"
    return out + "\r\n"


def song_details(songs: list[dict]) -> dict:
    return {s["shortname"]: {"genre": s["genre"], "year": s["year"], "length_ms": s["length_ms"],
                             "vocal_parts": s["vocal_parts"], "origin": s["origin"], "tiers": s["tiers"],
                             "games": s["games"]} for s in songs}


def router(state: AppState) -> APIRouter:
    page = APIRouter()
    settings = state.settings

    def viewer(request: Request) -> Viewer | None:
        return signing.verify(state.secret, request.cookies.get(signing.COOKIE))

    def denied() -> PlainTextResponse:
        return PlainTextResponse("Open the page from the link /browse gives you in Discord", 401, media_type=TEXT)

    @page.get("/")
    def index():
        return RedirectResponse("/browse", status_code=303)

    @page.get("/browse")
    def browse(request: Request):
        token = request.query_params.get("t")
        if token is not None:
            v = signing.verify(state.secret, token)
            if v is None:
                return HTMLResponse(NOT_SIGNED_IN.replace(
                    "{message}", "That link has expired. Ask for a new one with <code>/browse</code> in Discord."),
                    status_code=401)
            # the token leaves the address bar (and the history) for a cookie
            response = RedirectResponse("/browse", status_code=303)
            response.set_cookie(signing.COOKIE, token, max_age=max(v.expires - store.now_s(), 0), httponly=True,
                                samesite="lax", secure=settings.cookie_secure, path="/")
            return response
        if viewer(request) is None:
            return HTMLResponse(NOT_SIGNED_IN.replace(
                "{message}", "Ask for a link with <code>/browse</code> in Discord."), status_code=401)
        if not os.path.isfile(settings.library_page):
            log.error("The library page isn't at %s (LIBRARY_PAGE)", settings.library_page)
            return PlainTextResponse("The library page is missing from this install", 500)
        return FileResponse(settings.library_page, media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"})

    @page.get("/status")
    def status(request: Request):
        if viewer(request) is None:
            return denied()
        with state.conn() as conn:
            games = [{"name": g["name"], "online": store.is_online(g)} for g in store.games(conn)]
        return JSONResponse({"host": "band3-bot", "name": settings.bot_name, "games": games},
                            headers={"Cache-Control": "no-store"})

    @page.get("/list_songs")
    def list_songs(request: Request):
        if viewer(request) is None:
            return denied()
        with state.conn() as conn:
            songs = store.merged_library(conn)
        return Response("".join(format_song(s, True) for s in songs), media_type=TEXT,
                        headers={"Cache-Control": "no-store"})

    @page.get("/song_{song_id:int}")
    def song(song_id: int, request: Request):
        if viewer(request) is None:
            return denied()
        with state.conn() as conn:
            row = conn.execute("SELECT * FROM library WHERE song_id = ? LIMIT 1", (song_id,)).fetchone() \
                if song_id else None
        if row is None:
            return PlainTextResponse("Not Found", 404, media_type=TEXT)
        return Response(format_song(dict(row), False), media_type=TEXT)

    @page.get("/song_details")
    def details(request: Request):
        if viewer(request) is None:
            return denied()
        with state.conn() as conn:
            songs = store.merged_library(conn)
        return JSONResponse(song_details(songs), headers={"Cache-Control": "no-store"})

    @page.get("/album_art")
    def album_art(request: Request, shortname: str = ""):
        if viewer(request) is None:
            return denied()
        path = os.path.join(settings.art_dir, "game", f"{shortname}.jpg")
        if not settings.share_album_art or not store.valid_shortname(shortname) or not os.path.isfile(path):
            return PlainTextResponse("No album art for that shortname", 404, media_type=TEXT)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": f"max-age={ART_MAX_AGE}"})

    @page.get("/rv/search")
    async def rv_search(request: Request):
        if viewer(request) is None:
            return denied()
        if state.rv is None:
            return PlainTextResponse("RhythmVerse isn't set up", 502, media_type=TEXT)
        options = parse_search_options(query_dict(urlsplit(str(request.url)).query))
        try:
            result = await state.rv.search(options)
        except RhythmVerseError as exc:
            return PlainTextResponse(str(exc), 502, media_type=TEXT)
        with state.conn() as conn:
            store.remember_songs(conn, result.songs)
            by_song_id = store.games_with_song_ids(conn)
            has_library = conn.execute("SELECT 1 FROM library LIMIT 1").fetchone() is not None
            songs = []
            for s in result.songs:
                in_library = bool(s.song_id and s.song_id in by_song_id) if has_library else None
                downloaded = bool(in_library) or bool(store.games_with_file(conn, s.file_id))
                songs.append({
                    "file_id": s.file_id, "title": s.title, "artist": s.artist, "album": s.album,
                    "genre": s.genre, "author": s.author, "author_id": s.author_id, "year": s.year,
                    "length_ms": s.length_s * 1000, "vocal_parts": s.vocal_parts, "size": s.size,
                    "downloads": s.downloads, "tiers": s.tiers, "art": s.art_url, "page": s.page_url,
                    "host": s.host, "download": s.downloadable, "downloaded": downloaded,
                    "song_id": s.song_id, "in_library": in_library, "update": "",
                    "games": sorted(set(by_song_id.get(s.song_id, [])), key=str.lower),
                })
        return JSONResponse({"total": result.total, "page": result.page, "page_size": result.page_size,
                             "songs": songs}, headers={"Cache-Control": "no-store"})

    @page.get("/rv/downloads")
    def rv_downloads(request: Request):
        if viewer(request) is None:
            return denied()
        out = []
        with state.conn() as conn:
            for r in store.open_requests(conn):
                states = store.game_states(conn, r)
                m = store.merged(states) if r["approved"] else {
                    "state": "queued", "received": 0, "total": 0, "error": "", "in_library": False}
                out.append({
                    "file_id": r["file_id"], "title": r["title"], "artist": r["artist"], "state": m["state"],
                    "received": m["received"], "total": m["total"], "error": m["error"], "song_id": r["song_id"],
                    "update": False, "in_library": m["in_library"], "requested_by": r["user_name"],
                    "approved": bool(r["approved"]),
                    "games": [{"name": s["name"], "state": s["state"] if r["approved"] else "approval",
                               "received": s["received"], "total": s["total"]} for s in states],
                })
        return JSONResponse({"folder": "", "downloads": out}, headers={"Cache-Control": "no-store"})

    @page.post("/rv/download")
    async def rv_download(request: Request):
        v = viewer(request)
        if v is None:
            return denied()
        # JSON only, as band3's: another site's page can't send one without a preflight
        if not request.headers.get("content-type", "").startswith("application/json"):
            return PlainTextResponse("Send the file ID as JSON", 415, media_type=TEXT)
        try:
            body = json.loads(await request.body())
        except ValueError:
            body = None
        file_id = body.get("file_id") if isinstance(body, dict) else None
        with state.conn() as conn:
            song = store.known_song(conn, file_id) if isinstance(file_id, str) else None
            if song is None:
                return PlainTextResponse("No search has found that song; search for it again", 404, media_type=TEXT)
            outcome = request_song(conn, settings, song, v.user_id, v.name, can_request=v.can_request,
                                   approver=v.approver, channel_id=v.channel_id)
        status = {"requested": 200, "have": 200, "duplicate": 200, "limit": 429,
                  "not_downloadable": 409, "forbidden": 403}[outcome.kind]
        if outcome.kind == "requested" and outcome.request_id is not None:
            state.events.request_created(outcome.request_id)
        return PlainTextResponse(outcome.message, status, media_type=TEXT)

    return page
