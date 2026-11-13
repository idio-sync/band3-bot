"""A stand-in for band3, for checking a bot end to end without the game.

Discovers the bot on the LAN (or takes --server), pairs (prints the code to type
as /pair in Discord), then polls like band3: each new request is "downloaded"
(queued, downloading, done, in_library), the library is uploaded when the bot
wants it, and the art it asks for is sent.

  python scripts/fake_band3.py                    discover, pair, poll every 60 s
  python scripts/fake_band3.py --server http://127.0.0.1:8080 --interval 5
  python scripts/fake_band3.py --once             one poll, then stop

The token and cursor are kept in --state (fake_band3.json), so a second run
doesn't pair again. Only the standard library is needed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid

PORT = 21072
# a small JPEG-shaped placeholder (starts with the JPEG marker the bot checks for)
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a"
    "1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f"
    "0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d010203"
    "00041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a343536373839"
    "3a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa"
    "ffda0008010100003f00fbd3ffd9")

LIBRARY = [
    {"shortname": "fakesong1", "song_id": 1001, "title": "Fake Song One", "artist": "The Fakes",
     "album": "Pretend", "origin": "ugc_plus", "genre": "rock", "year": 2001, "length_ms": 201000,
     "vocal_parts": 1, "tiers": {"band": 3, "guitar": 2, "bass": 1, "drum": 4, "vocals": 2}},
    {"shortname": "fakesong2", "song_id": 1002, "title": "Fake Song Two", "artist": "The Fakes",
     "album": "Pretend", "origin": "ugc_plus", "genre": "metal", "year": 2005, "length_ms": 245000,
     "vocal_parts": 3, "tiers": {"band": 5, "guitar": 6, "drum": 5, "vocals": 4, "real_keys": 3}},
]


def discover(timeout: float = 3.0) -> dict | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    msg = json.dumps({"band3": 1, "want": "requests", "game_id": "discovery"}).encode()
    for addr in ("255.255.255.255", "127.0.0.1"):
        try:
            s.sendto(msg, (addr, PORT))
        except OSError:
            pass
    try:
        data, sender = s.recvfrom(4096)
    except TimeoutError:
        return None
    finally:
        s.close()
    reply = json.loads(data)
    print(f"Found {reply.get('name')!r} at {reply.get('url')} (from {sender[0]})")
    return reply


def call(method: str, url: str, token: str = "", body=None, headers: dict | None = None):
    headers = dict(headers or {})
    data = None
    if isinstance(body, bytes):
        data = body
    elif body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def pair(server: str, game_id: str, name: str) -> str:
    status, body = call("POST", f"{server}/api/v1/pair/start", body={"game_id": game_id, "name": name})
    if status != 200:
        sys.exit(f"pair/start answered {status}: {body}")
    print(f"\n  Type in Discord:  /pair {body['code']}\n  (valid {body['expires_in']} s)\n")
    while True:
        time.sleep(2)
        status, answer = call("POST", f"{server}/api/v1/pair/finish", body={"pair_id": body["pair_id"]})
        if status == 200:
            print(f"Paired with {answer['name']!r} (bot {answer['bot_id']})")
            return answer["token"]
        if status != 202:
            sys.exit(f"Pairing ended: {status} {answer}")


def library_hash() -> str:
    return hashlib.sha1(json.dumps(LIBRARY, sort_keys=True).encode()).hexdigest()


def poll(server: str, state: dict, save) -> bool:
    """One poll; False when the bot no longer knows this game."""
    token = state["token"]
    while True:
        status, body = call("GET", f"{server}/api/v1/requests?since={state['cursor']}", token,
                            headers={"X-Library-Hash": state.get("library_hash", "")})
        if status == 401:
            print("The bot answered 401: unpaired. Run again to pair.")
            state.pop("token", None)
            save()
            return False
        if status != 200:
            print(f"Poll answered {status}: {body}")
            return True
        for r in body["requests"]:
            print(f"Request #{r['id']}: {r['title']} by {r['artist']} (from {r['requested_by']})")
            url = f"{server}/api/v1/requests/{r['id']}/status"
            call("POST", url, token, {"state": "queued", "received": 0, "total": 0, "error": ""})
            for got in (0, 500_000, 1_000_000):
                call("POST", url, token, {"state": "downloading", "received": got, "total": 1_000_000, "error": ""})
                time.sleep(0.5)
            call("POST", url, token, {"state": "done", "received": 1_000_000, "total": 1_000_000, "error": ""})
            call("POST", url, token, {"state": "in_library", "received": 0, "total": 0, "error": ""})
            state["cursor"] = r["id"]
            save()
        state["cursor"] = max(state["cursor"], body["cursor"])
        save()
        if body.get("want_library"):
            payload = gzip.compress(json.dumps({"hash": library_hash(), "songs": LIBRARY}).encode())
            status, _ = call("PUT", f"{server}/api/v1/library", token, payload,
                             {"Content-Type": "application/json", "Content-Encoding": "gzip"})
            print(f"Library uploaded: {status}")
            if status == 204:
                state["library_hash"] = library_hash()
                save()
        for shortname in body.get("want_art", []):
            status, _ = call("PUT", f"{server}/api/v1/art/{shortname}", token, TINY_JPEG,
                             {"Content-Type": "image/jpeg"})
            print(f"Art for {shortname}: {status}")
        if len(body["requests"]) < 50:
            return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--server", help="the bot's URL; default: discover it on the LAN")
    p.add_argument("--name", default="Fake band3")
    p.add_argument("--state", default="fake_band3.json")
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    state = {"game_id": str(uuid.uuid4()), "cursor": 0}
    if os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as f:
            state.update(json.load(f))

    def save():
        with open(args.state, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    server = args.server or state.get("server")
    if not server:
        found = discover()
        if not found:
            sys.exit("No bot answered on UDP 21072; pass --server")
        server = found["url"]
    server = server.rstrip("/")
    state["server"] = server
    save()

    while True:
        if not state.get("token"):
            state["token"] = pair(server, state["game_id"], args.name)
            save()
        alive = poll(server, state, save)
        if args.once or not alive:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
