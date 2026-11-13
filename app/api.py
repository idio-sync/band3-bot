"""/api/v1: the band3 protocol (PROTOCOL.md). JSON, token auth but for pairing."""

from __future__ import annotations

import json
import os
import zlib
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import store
from app.state import AppState

# a library of tens of thousands of songs is a few MB of JSON
MAX_LIBRARY_BYTES = 64 * 1024 * 1024
MAX_ART_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _read(request: Request, limit: int) -> bytes | None:
    """The body, or None past `limit` bytes."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


async def _json(request: Request) -> Any:
    raw = await _read(request, MAX_JSON_BYTES)
    if raw is None:
        return None
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return None


def _token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def router(state: AppState) -> APIRouter:
    api = APIRouter(prefix="/api/v1")
    settings = state.settings

    def authed(request: Request, conn):
        return store.game_for_token(conn, _token(request))

    @api.post("/pair/start")
    async def pair_start(request: Request):
        body = await _json(request)
        if not isinstance(body, dict) or not store.valid_game_id(body.get("game_id")):
            return _error(400, "Send {\"game_id\", \"name\"}")
        with state.conn() as conn:
            try:
                answer = store.start_pairing(conn, body["game_id"], store.clean_name(body.get("name")))
            except store.TooManyCodes:
                return _error(429, "This game already has 3 codes waiting")
        return JSONResponse(answer)

    @api.post("/pair/finish")
    async def pair_finish(request: Request):
        body = await _json(request)
        pair_id = body.get("pair_id") if isinstance(body, dict) else None
        if not isinstance(pair_id, str):
            return _error(400, "Send {\"pair_id\"}")
        with state.conn() as conn:
            status, answer = store.finish_pairing(conn, pair_id, store.bot_id(conn), settings.bot_name)
        return JSONResponse(answer, status_code=status)

    @api.get("/requests")
    async def requests(request: Request):
        try:
            since = int(request.query_params.get("since", "0") or 0)
        except ValueError:
            return _error(400, "since must be a whole number")
        with state.conn() as conn:
            game = authed(request, conn)
            if game is None:
                return _error(401, "Not paired")
            cursor, items = store.requests_for_game(conn, game, since)
            want_library = store.want_library(game, request.headers.get("x-library-hash", "").strip())
            want_art = store.want_art(conn, game, settings.art_dir, settings.share_album_art)
        return JSONResponse({"cursor": cursor, "requests": items, "want_library": want_library,
                             "want_art": want_art})

    @api.post("/requests/{seq}/status")
    async def request_status(seq: int, request: Request):
        body = await _json(request)
        with state.conn() as conn:
            game = authed(request, conn)
            if game is None:
                return _error(401, "Not paired")
            if not isinstance(body, dict) or body.get("state") not in store.STATES:
                return _error(400, "state must be one of " + ", ".join(store.STATES))

            def whole(key: str) -> int:
                try:
                    return max(int(body.get(key) or 0), 0)
                except (TypeError, ValueError):
                    return 0

            error = body.get("error") if isinstance(body.get("error"), str) else ""
            request_id = store.set_status(conn, game, seq, body["state"], whole("received"), whole("total"),
                                          error)
        if request_id is None:
            return _error(404, "No such request")
        state.events.request_changed(request_id)
        return Response(status_code=204)

    @api.put("/library")
    async def library(request: Request):
        with state.conn() as conn:
            if authed(request, conn) is None:
                return _error(401, "Not paired")
        raw = await _read(request, MAX_LIBRARY_BYTES)
        if raw is None:
            return _error(413, "Too big")
        if "gzip" in request.headers.get("content-encoding", "").lower():
            try:
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw = decoder.decompress(raw, MAX_LIBRARY_BYTES)
                if decoder.unconsumed_tail:
                    return _error(413, "Too big")
            except zlib.error:
                return _error(400, "Not gzip")
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "Not JSON")
        if not isinstance(body, dict) or not isinstance(body.get("songs"), list):
            return _error(400, "Send {\"hash\", \"songs\": [...]}")
        with state.conn() as conn:
            game = authed(request, conn)
            if game is None:
                return _error(401, "Not paired")
            store.replace_library(conn, game, body)
        return Response(status_code=204)

    @api.put("/art/{shortname}")
    async def art(shortname: str, request: Request):
        with state.conn() as conn:
            if authed(request, conn) is None:
                return _error(401, "Not paired")
        if not store.valid_shortname(shortname):
            return _error(400, "Not a shortname")
        if not request.headers.get("content-type", "").startswith("image/jpeg"):
            return _error(415, "Send image/jpeg")
        raw = await _read(request, MAX_ART_BYTES)
        if raw is None:
            return _error(413, "Too big")
        if not raw.startswith(b"\xff\xd8"):
            return _error(400, "Not a JPEG")
        if settings.share_album_art:
            path = os.path.join(settings.art_dir, "game", f"{shortname}.jpg")
            # the first copy of each shortname stays
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(raw)
                os.replace(tmp, path)
        return Response(status_code=204)

    return api
