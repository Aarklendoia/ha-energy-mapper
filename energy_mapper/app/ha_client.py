"""Read-only access to Home Assistant: websocket commands, recorder history
and, for the live view, a subscription to the state changes of a few entities.

Under the Supervisor the add-on talks to Home Assistant through the
Supervisor proxy (``http://supervisor/core``) with ``SUPERVISOR_TOKEN``.
Anywhere else, ``HA_URL`` and ``HA_TOKEN`` (a long-lived access token) are
used instead.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from ws_client import WebSocket, WebSocketError

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
# entity ids per history request: Home Assistant refuses request lines over 8 KiB
MAX_FILTER_CHARS = 3000


class HAError(Exception):
    """Home Assistant could not be reached or refused a request."""


@dataclass
class Endpoint:
    rest: str  # base of the REST API, e.g. http://supervisor/core/api
    websocket: str  # e.g. ws://supervisor/core/websocket
    token: str
    supervised: bool

    def __repr__(self) -> str:  # never show the token
        return f"Endpoint(rest={self.rest!r}, websocket={self.websocket!r}, supervised={self.supervised})"


def endpoint_from_env(env: dict[str, str] | None = None) -> Endpoint:
    env = dict(os.environ) if env is None else env
    if env.get("HA_URL") and env.get("HA_TOKEN"):
        base = env["HA_URL"].rstrip("/")
        ws = "wss" + base[5:] if base.startswith("https") else "ws" + base[4:]
        return Endpoint(base + "/api", ws + "/api/websocket", env["HA_TOKEN"], supervised=False)
    if env.get("SUPERVISOR_TOKEN"):
        return Endpoint(
            SUPERVISOR_URL + "/core/api",
            "ws://supervisor/core/websocket",
            env["SUPERVISOR_TOKEN"],
            supervised=True,
        )
    raise HAError("no credentials: set HA_URL and HA_TOKEN, or run under the Supervisor")


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return dt.datetime.fromisoformat(value).timestamp()


def parse_history(payload: list, window_start: float | None = None) -> dict[str, list[tuple[float, str]]]:
    """Convert a REST ``/api/history/period`` answer to ``{entity_id: [(ts, state)]}``.

    The answer is one list per entity. With ``minimal_response`` only the
    first item carries ``entity_id`` (and attributes unless ``no_attributes``);
    later items only have ``state`` and ``last_changed``. The first item is the
    state at the start of the window and may be older than it: it is moved to
    ``window_start``.
    """
    out: dict[str, list[tuple[float, str]]] = {}
    for rows in payload or []:
        if not rows:
            continue
        entity_id = rows[0].get("entity_id")
        if not entity_id:
            continue
        points = out.setdefault(entity_id, [])
        for row in rows:
            stamp = row.get("last_changed") or row.get("last_updated")
            if stamp is None or "state" not in row:
                continue
            ts = _ts(stamp)
            if window_start is not None and ts < window_start:
                ts = window_start
            points.append((ts, str(row["state"])))
        points.sort(key=lambda p: p[0])
    return out


def parse_attribute_history(
    payload: list, wanted: dict[str, tuple[str, ...]], window_start: float | None = None
) -> dict[str, list[tuple[float, str]]]:
    """Convert a full REST ``/api/history/period`` answer (with attributes,
    not minimal) to ``{"<entity_id>::<attribute>": [(ts, value)]}`` for the
    attributes ``wanted`` of each entity. An attribute change keeps the
    state's ``last_changed``: ``last_updated`` is its time. A missing
    attribute (an unavailable entity) is ``unknown``. The first item may be
    older than the window: it is moved to ``window_start``."""
    out: dict[str, list[tuple[float, str]]] = {}
    for rows in payload or []:
        entity_id = None
        for row in rows or []:
            entity_id = row.get("entity_id") or entity_id
            attrs = wanted.get(entity_id or "")
            stamp = row.get("last_updated") or row.get("last_changed")
            if not attrs or stamp is None:
                continue
            ts = _ts(stamp)
            if window_start is not None and ts < window_start:
                ts = window_start
            values = row.get("attributes") or {}
            for attr in attrs:
                value = values.get(attr)
                out.setdefault(f"{entity_id}::{attr}", []).append(
                    (ts, "unknown" if value is None else str(value))
                )
    for points in out.values():
        points.sort(key=lambda p: p[0])
    return out


class HAClient:
    def __init__(self, endpoint: Endpoint, timeout: float = 120.0):
        self.endpoint = endpoint
        self.timeout = timeout

    # --- websocket ---------------------------------------------------------
    def _connect(self, timeout: float) -> WebSocket:
        """An authenticated websocket connection (raises HAError)."""
        ws = WebSocket(self.endpoint.websocket, timeout=timeout)
        try:
            msg = json.loads(ws.recv_text())
            if msg.get("type") != "auth_required":
                raise HAError(f"unexpected websocket greeting: {msg.get('type')}")
            ws.send_text(json.dumps({"type": "auth", "access_token": self.endpoint.token}))
            msg = json.loads(ws.recv_text())
            if msg.get("type") != "auth_ok":
                raise HAError("websocket authentication failed")
        except BaseException:
            ws.close()
            raise
        return ws

    def ws_commands(self, commands: list[dict]) -> list:
        """Run commands on one websocket connection. A command that fails
        returns None (e.g. ``energy/get_prefs`` without Energy dashboard)."""
        results: list = []
        try:
            with self._connect(self.timeout) as ws:
                for i, cmd in enumerate(commands, start=1):
                    ws.send_text(json.dumps({**cmd, "id": i}))
                    while True:
                        msg = json.loads(ws.recv_text())
                        if msg.get("id") == i and msg.get("type") == "result":
                            break
                    if msg.get("success"):
                        results.append(msg.get("result"))
                    else:
                        err = msg.get("error") or {}
                        _LOGGER.info("%s failed: %s", cmd.get("type"), err.get("message") or err.get("code"))
                        results.append(None)
        except (OSError, WebSocketError, ValueError) as err:
            raise HAError(f"websocket: {err}") from err
        return results

    def subscribe_states(self, entities: list[str], timeout: float = 30.0) -> StateSubscription:
        """Subscribe to the state changes of ``entities`` (the live view, #17):
        ``subscribe_entities`` filtered on them. Raises HAError."""
        try:
            ws = self._connect(timeout)
        except (OSError, WebSocketError, ValueError) as err:
            raise HAError(f"websocket: {err}") from err
        try:
            ws.send_text(json.dumps({"id": 1, "type": "subscribe_entities", "entity_ids": list(entities)}))
            while True:
                msg = json.loads(ws.recv_text())
                if msg.get("id") == 1 and msg.get("type") == "result":
                    break
            if not msg.get("success"):
                err = msg.get("error") or {}
                raise HAError(f"subscribe_entities: {err.get('message') or err.get('code')}")
        except (OSError, WebSocketError, ValueError) as err:
            ws.close()
            raise HAError(f"websocket: {err}") from err
        except HAError:
            ws.close()
            raise
        return StateSubscription(ws, 1)

    def snapshot(self) -> dict:
        """Everything the configuration is derived from."""
        prefs, states, registry, config = self.ws_commands(
            [
                {"type": "energy/get_prefs"},
                {"type": "get_states"},
                {"type": "config/entity_registry/list"},
                {"type": "get_config"},
            ]
        )
        if states is None or registry is None:
            raise HAError("could not read states or the entity registry")
        return {"prefs": prefs, "states": states, "registry": registry, "config": config or {}}

    # --- REST --------------------------------------------------------------
    def _get(self, path: str):
        req = urllib.request.Request(
            self.endpoint.rest + path,
            headers={"Authorization": f"Bearer {self.endpoint.token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            raise HAError(f"GET {path.split('?')[0]}: HTTP {err.code}") from err
        except (OSError, ValueError) as err:
            raise HAError(f"GET {path.split('?')[0]}: {err}") from err

    def history(self, entities: list[str], start: float, end: float) -> dict[str, list[tuple[float, str]]]:
        """Every recorded state of ``entities`` in [start, end], plus the
        state at ``start`` (several requests for a long list of entities)."""
        out: dict[str, list[tuple[float, str]]] = {}
        for chunk in chunks(entities, MAX_FILTER_CHARS):
            out.update(self._history(chunk, start, end))
        return out

    def _history(self, entities: list[str], start: float, end: float) -> dict[str, list[tuple[float, str]]]:
        iso = lambda ts: dt.datetime.fromtimestamp(ts, dt.UTC).isoformat()  # noqa: E731
        query = urlencode(
            {
                "end_time": iso(end),
                "filter_entity_id": ",".join(entities),
                "significant_changes_only": "0",
            }
        )
        path = f"/history/period/{quote(iso(start))}?{query}&minimal_response&no_attributes"
        return parse_history(self._get(path), window_start=start)

    def history_attributes(
        self, wanted: dict[str, tuple[str, ...]], start: float, end: float
    ) -> dict[str, list[tuple[float, str]]]:
        """The values of some attributes of some entities in [start, end],
        plus those at ``start`` (``parse_attribute_history``). Heavier than
        ``history``: every row carries the entity's attributes, so the
        caller keeps the list short."""
        iso = lambda ts: dt.datetime.fromtimestamp(ts, dt.UTC).isoformat()  # noqa: E731
        out: dict[str, list[tuple[float, str]]] = {}
        for chunk in chunks(list(wanted), MAX_FILTER_CHARS):
            query = urlencode(
                {
                    "end_time": iso(end),
                    "filter_entity_id": ",".join(chunk),
                    "significant_changes_only": "0",
                }
            )
            payload = self._get(f"/history/period/{quote(iso(start))}?{query}")
            out.update(parse_attribute_history(payload, {e: wanted[e] for e in chunk}, window_start=start))
        return out


def entity_changes(event: dict) -> list[tuple[str, float | None, str]]:
    """The states in a ``subscribe_entities`` event: ``(entity_id, ts, state)``.

    The first event lists every entity (``"a"``: ``{"s": state, "lc": last
    changed, ...}``), later ones the changes (``"c"``: ``{"+": {"s", "lc"}}``).
    Only state changes count, as in the recorder history the night reads: an
    attribute change keeps the state. ``ts`` is None when HA gave no time."""
    out: list[tuple[str, float | None, str]] = []

    def stamp(item: dict) -> float | None:
        value = item.get("lc", item.get("lu"))
        return float(value) if isinstance(value, int | float) else None

    for entity_id, item in (event.get("a") or {}).items():
        if isinstance(item, dict) and "s" in item:
            out.append((entity_id, stamp(item), str(item["s"])))
    for entity_id, diff in (event.get("c") or {}).items():
        plus = diff.get("+") if isinstance(diff, dict) else None
        if isinstance(plus, dict) and "s" in plus:
            out.append((entity_id, stamp(plus), str(plus["s"])))
    return out


class StateSubscription:
    """An open ``subscribe_entities`` subscription (``HAClient.subscribe_states``)."""

    def __init__(self, ws: WebSocket, sub_id: int):
        self._ws = ws
        self._id = sub_id
        self._next_id = sub_id + 1
        self.last_message = time.monotonic()  # for a silent (dead) connection

    def next(self, timeout: float) -> list[tuple[str, float | None, str]] | None:
        """The states of the next message (``entity_changes``), [] for another
        message (a pong), None when nothing came within ``timeout``. Raises
        HAError when the connection is lost."""
        try:
            text = self._ws.recv_text(timeout)
            if text is None:
                return None
            msg = json.loads(text)
        except (OSError, WebSocketError, ValueError) as err:
            raise HAError(f"websocket: {err}") from err
        self.last_message = time.monotonic()
        if msg.get("type") == "event" and msg.get("id") == self._id:
            return entity_changes(msg.get("event") or {})
        return []

    def ping(self) -> None:
        """Ask for a pong: a connection that stays silent is dead."""
        try:
            self._ws.send_text(json.dumps({"id": self._next_id, "type": "ping"}))
        except OSError as err:
            raise HAError(f"websocket: {err}") from err
        self._next_id += 1

    def close(self) -> None:
        self._ws.close()


def chunks(entities: list[str], max_chars: int) -> list[list[str]]:
    """``entities`` (duplicates removed) split into lists whose
    comma-separated length stays under ``max_chars`` (one entity at least)."""
    out: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for e in dict.fromkeys(entities):
        if cur and size + len(e) + 1 > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += len(e) + 1
    if cur:
        out.append(cur)
    return out


def post_discovery(token: str, host: str, port: int, timeout: float = 10.0) -> None:
    """Announce the add-on to Home Assistant (Supervisor discovery)."""
    body = json.dumps({"service": "energy_mapper", "config": {"host": host, "port": port}}).encode()
    req = urllib.request.Request(
        SUPERVISOR_URL + "/discovery",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as err:
        raise HAError(f"discovery: HTTP {err.code}") from err
    except OSError as err:
        raise HAError(f"discovery: {err}") from err
