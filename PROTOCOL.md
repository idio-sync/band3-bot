# band3 ↔ band3-bot protocol, v1

The contract between band3 (`src/Net/song_requests.*`) and this bot. JSON bodies, UTF-8.
Every `/api/v1` call but the two pairing ones carries `Authorization: Bearer <token>`; a
**401** means the bot no longer knows the game (it was unpaired), and band3 clears
`requests_token`.

## Discovery (UDP 21072)

band3 sends to each of its broadcast addresses and 255.255.255.255:

```json
{"band3": 1, "want": "requests", "game_id": "<requests_game_id>"}
```

The bot answers to the sender's address and port:

```json
{"band3_bot": 1, "protocol": 1, "bot_id": "<uuid>", "name": "<BOT_NAME>", "url": "http://192.168.1.20:8080"}
```

`url` is `ADVERTISE_URL`, or else `http://<the address the reply leaves from>:<PORT>`.
`bot_id` is made once and kept in the database.

## Pairing

| Call | Body | Answers |
|---|---|---|
| `POST /api/v1/pair/start` | `{"game_id", "name"}` | 200 `{"pair_id", "code", "expires_in": 300}`; 429 when the game already has 3 codes waiting; 400 without a `game_id` (letters, digits and `-`, at most 64) |
| `POST /api/v1/pair/finish` | `{"pair_id"}` | 202 `{"state": "waiting"}`; 200 `{"state": "paired", "token", "bot_id", "name"}`; 410 `{"state": "expired"}` |

- `code` is 4 digits, kept 5 minutes. Someone types `/pair <code>` in Discord.
- A `/pair` with a code no game is showing counts as a wrong try against **every** code
  waiting; after 5, they expire (410). So a code can't be found by guessing.
- `finish` gives the token once; the pairing is then gone (a later `finish` is 410).
  An unknown `pair_id` is 410 too.
- The token is 32 random bytes, hex (64 characters). The bot keeps only its SHA-256.
- Pairing a `game_id` the bot already has replaces its token and keeps its cursor and
  states.

## Requests

`GET /api/v1/requests?since=<n>`, header `X-Library-Hash: <hash or empty>`:

```json
{
  "cursor": 42,
  "requests": [
    {"id": 41, "file_id": "abc123", "title": "...", "artist": "...",
     "requested_by": "someone", "requested_at": 1763049600}
  ],
  "want_library": true,
  "want_art": ["shortname1", "shortname2"]
}
```

- `since` is the highest request `id` band3 has handled (0 at first: every request).
- Only approved, uncancelled requests, in `id` order, at most 50 a call; band3 asks again
  at once while it gets 50. `cursor` is the last `id` listed, or `since` when none are.
- A request's `id` is given when games may first see it (at once, or when an approver
  approves it), so a request approved after a later one still comes after the cursor.
- `want_library`: the bot has no library from this game with that hash (an empty header
  always wants one).
- `want_art`: up to 20 shortnames from this game's library the bot has no art for. A
  shortname asked for isn't asked of the same game again for 24 hours (a song without art
  would otherwise be asked for at every poll). Empty with `SHARE_ALBUM_ART` off.

`POST /api/v1/requests/<id>/status`, body `{"state", "received", "total", "error"}`: 204;
400 for another `state`; 404 for an unknown `id`. `state` is one of `queued`,
`downloading`, `done`, `in_library`, `have`, `failed`, `not_hosted`, `no_folder`.

## Library and art

`PUT /api/v1/library`: 204. Body, gzipped with `Content-Encoding: gzip` or plain:

```json
{"hash": "<sha1 of the song list>", "songs": [
  {"shortname", "song_id", "title", "artist", "album", "origin", "genre", "year",
   "length_ms", "vocal_parts", "tiers": {"guitar": 3, "...": 0}}
]}
```

It replaces the game's library. Songs whose shortname isn't letters, digits, `_`, `.` and
`-` (at most 64, no `..`) are left out. 400 for a body that isn't (gzipped) JSON of that
shape; 413 past 64 MB.

`PUT /api/v1/art/<shortname>`, `Content-Type: image/jpeg`, the bytes `/album_art` serves:
204. The first copy of each shortname stays. 415 for another type, 400 for bytes that
don't start as a JPEG, 413 past 2 MB.

## The browse page's endpoints

The bot serves band3's own page (`web/library.html`) at `/browse` and answers its
endpoints from every paired game's library, merged by shortname. Every reply has the
header `Server: band3-bot`. All of them need the cookie the signed `/browse?t=` link sets
(401 without).

- `/status`: `{"host": "band3-bot", "name": "<BOT_NAME>", "games": [{"name", "online"}]}`
  (`online`: polled in the last 3 minutes).
- `/list_songs`, `/song_<song_id>`: band3's (RB3E's) INI-style blocks.
- `/song_details`: band3's, plus `games`, the names of the paired games that have each song.
- `/album_art?shortname=`: the art games uploaded.
- `/rv/search`: band3's, with `downloaded` true when any paired game has the song (by song
  ID in its library, or by reporting a request for it `done`, `in_library` or `have`),
  `in_library` true when a game's library has its song ID (null before any library), and
  `games`.
- `/rv/downloads`: `folder` is empty; each request with a state merged across games
  (`done` once every paired game is `done`, `in_library` or `have`; `downloading` while
  any is; `queued` while any hasn't finished; else `failed` with the first failure's
  `error`), plus `requested_by`, `approved` and `games`: `[{"name", "state", "received",
  "total"}]`, where a game's `state` is a protocol state, `waiting` (not reported yet) or
  `approval` (the request waits for an approver).
- `POST /rv/download` `{"file_id"}`: a request from the signed-in user. 200 with the text to
  show; 401 without the cookie; 415 if not JSON; 404 for a song no search has shown;
  403 without the requester role; 409 for a song band3 can't download; 429 past
  `PER_USER_LIMIT`.
- Everything else band3 serves (`/history/*`, `/jump`, `/library/*`, `/rv/updates`,
  `/rv/check`, `/karaoke`, `/live/events`, ...) is 404.
