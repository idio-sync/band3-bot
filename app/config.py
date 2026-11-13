"""Settings, all from environment variables (Docker/Unraid friendly)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    config_dir: str = "/config"
    discord_token: str = ""
    discord_guild_id: int = 0
    public_url: str = ""
    bot_name: str = "band3-bot"
    advertise_url: str = ""
    port: int = 8080
    requester_role: str = ""
    approver_role: str = ""
    per_user_limit: int = 5
    share_album_art: bool = True
    secret: str = ""  # empty: generated once and kept in the database
    library_page: str = "/app/web/library.html"
    discovery_port: int = 21072

    @property
    def db_path(self) -> str:
        return os.path.join(self.config_dir, "band3-bot.db")

    @property
    def art_dir(self) -> str:
        return os.path.join(self.config_dir, "art")

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.startswith("https://")


def load() -> Settings:
    return Settings(
        config_dir=_env("CONFIG_DIR", "/config"),
        discord_token=_env("DISCORD_TOKEN"),
        discord_guild_id=_int("DISCORD_GUILD_ID", 0),
        public_url=_env("PUBLIC_URL").rstrip("/"),
        bot_name=_env("BOT_NAME", "band3-bot") or "band3-bot",
        advertise_url=_env("ADVERTISE_URL").rstrip("/"),
        port=_int("PORT", 8080),
        requester_role=_env("REQUESTER_ROLE"),
        approver_role=_env("APPROVER_ROLE"),
        per_user_limit=max(0, _int("PER_USER_LIMIT", 5)),
        share_album_art=_bool("SHARE_ALBUM_ART", True),
        secret=_env("SECRET"),
        library_page=_env("LIBRARY_PAGE", "/app/web/library.html"),
    )
