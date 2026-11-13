"""Browse links: HMAC-signed with the user's Discord ID and an expiry.

The token rides in the /browse link once, then lives in a cookie. It also says
where the user asked from (so a request made on the page is posted there) and
whether they could request and approve when the link was made.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

LINK_TTL_S = 24 * 3600
COOKIE = "band3_bot"


@dataclass(frozen=True)
class Viewer:
    user_id: str
    name: str
    expires: int
    channel_id: str = ""
    can_request: bool = True
    approver: bool = False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: bytes, viewer: Viewer) -> str:
    payload = _b64(json.dumps({
        "u": viewer.user_id, "n": viewer.name, "e": viewer.expires, "c": viewer.channel_id,
        "r": int(viewer.can_request), "a": int(viewer.approver),
    }, separators=(",", ":")).encode())
    mac = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{mac}"


def make(secret: bytes, user_id: str, name: str, channel_id: str = "", can_request: bool = True,
         approver: bool = False, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    return sign(secret, Viewer(str(user_id), name, now + LINK_TTL_S, str(channel_id), can_request, approver))


def verify(secret: bytes, token: str | None, now: int | None = None) -> Viewer | None:
    if not token or token.count(".") != 1:
        return None
    payload, mac = token.split(".")
    expected = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
        viewer = Viewer(str(data["u"]), str(data["n"]), int(data["e"]), str(data.get("c", "")),
                        bool(data.get("r", 1)), bool(data.get("a", 0)))
    except (ValueError, KeyError, TypeError):
        return None
    if viewer.expires <= (int(time.time()) if now is None else now):
        return None
    return viewer
