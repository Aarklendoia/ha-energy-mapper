"""Minimal synchronous websocket client (RFC 6455), standard library only.

Enough for the Home Assistant websocket API: text messages, fragmentation,
ping/pong and close. No extensions (no compression) are negotiated.
``recv_text(timeout)`` returns None when no message came in time, without
losing a partly received frame (the live view waits for states that way).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 64 * 1024 * 1024  # a whole get_states of a large installation fits


class WebSocketError(Exception):
    pass


def encode_frame(opcode: int, payload: bytes, mask: bytes | None = None) -> bytes:
    """A single final frame. Clients must mask what they send."""
    mask = os.urandom(4) if mask is None else mask
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(0x80 | n)
    elif n < 1 << 16:
        head.append(0x80 | 126)
        head += struct.pack("!H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack("!Q", n)
    head += mask
    return bytes(head) + _apply_mask(payload, mask)


def _apply_mask(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return b""
    key = int.from_bytes((mask * (len(payload) // 4 + 1))[: len(payload)], "big")
    return (int.from_bytes(payload, "big") ^ key).to_bytes(len(payload), "big")


def read_frame(recv_exact) -> tuple[bool, int, bytes]:
    """Read one frame with ``recv_exact(n) -> bytes``: (fin, opcode, payload)."""
    b0, b1 = recv_exact(2)
    fin, opcode = bool(b0 & 0x80), b0 & 0x0F
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", recv_exact(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", recv_exact(8))
    mask = recv_exact(4) if b1 & 0x80 else None
    payload = recv_exact(n) if n else b""
    if mask:
        payload = _apply_mask(payload, mask)
    return fin, opcode, payload


def parse_frame(buf) -> tuple[bool, int, bytes, int] | None:
    """The frame at the start of ``buf``: (fin, opcode, payload, its size in
    ``buf``), or None while it is incomplete. Nothing is consumed."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    n, pos = b1 & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        (n,) = struct.unpack_from("!H", buf, 2)
        pos = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        (n,) = struct.unpack_from("!Q", buf, 2)
        pos = 10
    if n > MAX_MESSAGE:
        raise WebSocketError(f"frame too large: {n} bytes")
    mask = None
    if b1 & 0x80:
        if len(buf) < pos + 4:
            return None
        mask = bytes(buf[pos : pos + 4])
        pos += 4
    if len(buf) < pos + n:
        return None
    payload = bytes(buf[pos : pos + n])
    if mask:
        payload = _apply_mask(payload, mask)
    return bool(b0 & 0x80), b0 & 0x0F, payload, pos + n


class WebSocket:
    def __init__(self, url: str, timeout: float = 60.0, headers: dict[str, str] | None = None):
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme: {parts.scheme}")
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        if parts.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        self._sock = sock
        self._timeout = timeout
        self._buf = bytearray()
        self._parts: list[bytes] = []  # fragments of the message being received
        key = base64.b64encode(os.urandom(16)).decode()
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {parts.netloc}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            *(f"{k}: {v}" for k, v in (headers or {}).items()),
        ]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n").decode("latin-1")
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            self.close()
            raise WebSocketError(f"handshake refused: {status}")
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if accept.lower() not in head.lower():
            self.close()
            raise WebSocketError("handshake: bad Sec-WebSocket-Accept")

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise WebSocketError("connection closed")
        self._buf += chunk

    def _read_until(self, marker: bytes) -> bytes:
        while (i := self._buf.find(marker)) < 0:
            if len(self._buf) > 65536:
                raise WebSocketError("handshake response too long")
            self._fill()
        out = bytes(self._buf[: i + len(marker)])
        del self._buf[: i + len(marker)]
        return out

    def send_text(self, text: str) -> None:
        self._sock.sendall(encode_frame(OP_TEXT, text.encode()))

    def recv_text(self, timeout: float | None = None) -> str | None:
        """Next complete text message (control frames are handled here).

        Without ``timeout`` it waits as long as the connection's timeout
        (then raises). With one, it returns None when no complete message
        came within it; a frame received in part is kept for the next call."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            frame = parse_frame(self._buf)
            if frame is None:
                if deadline is None:
                    self._fill()
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._sock.settimeout(left)
                try:
                    self._fill()
                except TimeoutError:
                    return None
                finally:
                    self._sock.settimeout(self._timeout)
                continue
            fin, opcode, payload, size = frame
            del self._buf[:size]
            if opcode == OP_PING:
                self._sock.sendall(encode_frame(OP_PONG, payload))
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise WebSocketError("closed by server")
            if opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                self._parts.append(payload)
                if fin:
                    data, self._parts = b"".join(self._parts), []
                    return data.decode()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.sendall(encode_frame(OP_CLOSE, struct.pack("!H", 1000)))
        with contextlib.suppress(OSError):
            self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
