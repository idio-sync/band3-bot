"""The web app: band3's API, the browse page and its endpoints; Discord and UDP
discovery run alongside it in the same event loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app import api, config, db, discovery, page, store
from app.rhythmverse import RhythmVerse
from app.state import AppState

log = logging.getLogger(__name__)

# the page turns on band3's own endpoints (art, details, the RhythmVerse tab)
# only for a server that says it's band3 or band3-bot
SERVER_HEADER = "band3-bot"


def create_app(settings: config.Settings | None = None, run_background: bool = True,
               http: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or config.load()
    os.makedirs(os.path.join(settings.art_dir, "game"), exist_ok=True)
    os.makedirs(os.path.join(settings.art_dir, "rv"), exist_ok=True)
    state = AppState(settings)
    with state.conn() as conn:
        db.init(conn)
        state.secret = store.secret(conn, settings.secret)
        bot_id = store.bot_id(conn)
    if http is not None:
        state.rv = RhythmVerse(http, settings.art_dir)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        own_http = None
        if state.rv is None:
            own_http = httpx.AsyncClient(follow_redirects=True)
            state.rv = RhythmVerse(own_http, settings.art_dir)
        transport = None
        tasks: list[asyncio.Task] = []
        discord_bot = None
        if run_background:
            def reply(data: bytes, peer: str) -> bytes | None:
                return discovery.answer(data, peer, bot_id, settings.bot_name, settings.advertise_url, settings.port)

            transport = await discovery.start(settings.discovery_port, reply)
            if settings.discord_token:
                from app import bot

                discord_bot = bot.Band3Bot(state)
                state.events = bot.BotEvents(discord_bot)
                tasks.append(asyncio.create_task(bot.run(discord_bot, settings.discord_token)))
            else:
                log.warning("DISCORD_TOKEN isn't set: no Discord commands")
        yield
        if discord_bot is not None:
            await discord_bot.close()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if transport is not None:
            transport.close()
        if own_http is not None:
            await own_http.aclose()

    app = FastAPI(title="band3-bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bot_state = state

    @app.middleware("http")
    async def server_header(request: Request, call_next):
        response = await call_next(request)
        response.headers["Server"] = SERVER_HEADER
        return response

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    app.include_router(api.router(state))
    app.include_router(page.router(state))
    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
