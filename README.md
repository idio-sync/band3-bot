# band3-bot

A Discord bot and small web server for [band3](https://github.com/idio-sync/band3_recomp),
the PC recompilation of Rock Band 3. People on your Discord server:

- **request** custom songs from [RhythmVerse](https://rhythmverse.co) with `/request`;
  every paired band3 downloads each request the next time it's online, and the request's
  message shows how far each game has got;
- **browse** the band's song library (every paired game's songs, merged) on a web page,
  with `/browse`, and request songs from the page too.

band3 finds the bot on the LAN by itself; pairing a game once takes a code typed in Discord.
The protocol between them is in [PROTOCOL.md](PROTOCOL.md).

## Install on Unraid

1. Create the Discord application (see [Discord setup](#discord-setup)) and copy its bot token.
2. Docker tab → **Add Container** → **Template**: paste
   `https://raw.githubusercontent.com/idio-sync/band3-bot/main/unraid/band3-bot.xml`
   in the template URL box (or copy the file to
   `/boot/config/plugins/dockerMan/templates-user/my-band3-bot.xml`).
3. Fill in **Discord token**, **Discord server ID** and **Public URL**, then Apply.

The template uses **br0** networking, which gives the container its own address on your
LAN. That matters: band3 looks for the bot with a broadcast on UDP 21072, and Docker's
default bridge network doesn't pass broadcasts into a container. **Host** networking works
too. On bridge, discovery doesn't work and each game needs `requests_server` set by hand.

Everything the bot keeps (`band3-bot.db` and `art/`) is in `/config`
(`/mnt/user/appdata/band3-bot`), so updating or recreating the container keeps it.
The container runs as `nobody:users` (99:100).

## Reverse proxy

People open the browse page from the link `/browse` gives them, so it has to be reachable
at `PUBLIC_URL`. Forward these paths to the bot's port 8080:

- `/browse`, and the page's endpoints: `/status`, `/list_songs`, `/song_details`,
  `/song_<id>`, `/album_art`, `/rv/search`, `/rv/downloads`, `/rv/download`
  (simplest: forward everything)
- `/api/v1` is optional: games on your LAN reach the bot directly. Forwarding it lets a
  game away from home (a Steam Deck at a friend's) keep getting requests, with
  `requests_server` set to the public address. Every `/api/v1` call needs the game's token.

The page and its endpoints answer only with the cookie the signed `/browse` link sets.

## Pairing a game

1. In band3: **Settings > Online**. The bot shows up there by its `BOT_NAME`; pick **Pair**.
2. band3 shows a 4-digit code for 5 minutes.
3. In Discord, someone with the approver role (or the server owner) types `/pair 4821`.
4. band3 says it's paired. `/games` lists paired games; `/unpair` removes one.

A game paired later still gets every earlier request.

## Discord commands

| Command | Does |
|---|---|
| `/request <text>` | searches RhythmVerse (by title or by artist; its search doesn't match both together) for songs band3 can download, shows up to 5 with art, parts and tiers, and whether a game has it; pick one from the dropdown |
| `/browse` | a link to the library page, for you only, valid 24 hours |
| `/queue` | requests not every game has yet |
| `/cancel <request>` | the requester or an approver cancels a request no game has started |
| `/pair <code>` | pairs the game showing that code (approver role or server owner) |
| `/games` | paired games and when each was last online |
| `/unpair <game>` | stops sending requests to a game (approver role or server owner) |

With `APPROVER_ROLE` set, a request waits for an approver to press **Approve** before games
see it. An approver's own requests don't wait.

## Settings

| Variable | Default | |
|---|---|---|
| `DISCORD_TOKEN` | | the bot's token |
| `DISCORD_GUILD_ID` | | your server's ID; commands are registered there (blank: every server the bot is in) |
| `PUBLIC_URL` | | the browse page's address through your proxy, for `/browse` links |
| `BOT_NAME` | `band3-bot` | what band3 shows in Settings > Online |
| `ADVERTISE_URL` | | what band3 is told to connect to; blank: `http://<the bot's LAN address>:<PORT>` |
| `PORT` | `8080` | the HTTP port |
| `REQUESTER_ROLE` | | only members with this role (name or ID) can request |
| `APPROVER_ROLE` | | requests wait for an approver; approvers pair and unpair games |
| `PER_USER_LIMIT` | `5` | requests per user per 24 hours; approvers aren't limited; `0` for no limit |
| `SHARE_ALBUM_ART` | `true` | ask games for album art for the page |
| `SECRET` | | signs `/browse` links; blank: generated once and kept in `/config` |
| `CONFIG_DIR` | `/config` | where the database and art go (for running outside Docker) |
| `LIBRARY_PAGE` | `/app/web/library.html` | band3's page file (for running outside Docker) |

## Discord setup

1. <https://discord.com/developers/applications> → **New Application** → **Bot** →
   **Reset Token**; that's `DISCORD_TOKEN`. No privileged intents are needed.
2. **OAuth2 > URL Generator**: scopes `bot` and `applications.commands`; permissions
   *Send Messages*, *Embed Links*, *Attach Files*. Open the URL to add the bot to your server.
3. Discord **Settings > Advanced > Developer Mode**, then right-click your server →
   **Copy Server ID**; that's `DISCORD_GUILD_ID`.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
ruff check . && python -m pytest -q
CONFIG_DIR=./config LIBRARY_PAGE=web/library.html python -m app
```

`scripts/fake_band3.py` stands in for a game: it discovers the bot, pairs (printing the
code to `/pair`), polls, "downloads" each request reporting each state, and uploads a small
library and its art.

`web/library.html` is band3's library page, copied here because band3's repo is private.
After the page changes in band3, refresh the copy and commit it:

```sh
python scripts/sync_page.py ../band3_recomp
```

Images are built by GitHub Actions and pushed to `ghcr.io/idio-sync/band3-bot`: `latest` and
`sha-<commit>` from `main`, and `1.2.3` and `1.2` from a `v1.2.3` tag.
