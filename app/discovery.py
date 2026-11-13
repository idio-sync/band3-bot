"""UDP discovery on 21072: band3 broadcasts {"band3": 1, "want": "requests", ...};
the bot answers the sender with where to find it."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Callable

log = logging.getLogger(__name__)

MAX_DATAGRAM = 2048


def local_address_for(peer: str) -> str:
    """The address replies to `peer` leave from (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def answer(data: bytes, peer: str, bot_id: str, name: str, advertise_url: str, port: int,
           local_address: Callable[[str], str] = local_address_for) -> bytes | None:
    """The reply to a datagram, or None for one that isn't band3's broadcast."""
    if len(data) > MAX_DATAGRAM:
        return None
    try:
        msg = json.loads(data)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("band3") != 1 or msg.get("want") != "requests":
        return None
    url = advertise_url or f"http://{local_address(peer)}:{port}"
    return json.dumps({"band3_bot": 1, "protocol": 1, "bot_id": bot_id, "name": name, "url": url}).encode()


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, reply: Callable[[bytes, str], bytes | None]):
        self.reply = reply
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            out = self.reply(data, addr[0])
        except Exception:
            log.exception("Discovery reply failed")
            return
        if out and self.transport:
            self.transport.sendto(out, addr)


async def start(port: int, reply: Callable[[bytes, str], bytes | None]) -> asyncio.DatagramTransport | None:
    loop = asyncio.get_running_loop()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", port))
        transport, _ = await loop.create_datagram_endpoint(lambda: _Responder(reply), sock=sock)
    except OSError as exc:
        log.error("Discovery is off: UDP port %d: %s", port, exc)
        return None
    log.info("Discovery: answering band3 on UDP %d", port)
    return transport
