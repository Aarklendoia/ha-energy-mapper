"""The live view (#17): one long-poll of the add-on, streamed to every panel.

The panel subscribes with the websocket command ``energy_mapper/live/subscribe``.
While at least one panel is subscribed, the hub long-polls the add-on
(``GET /api/live``, which starts the add-on's live detection and keeps it
running) and keeps a merged copy of what it answered: the curve of the last
15 minutes, the events, the "now" summary. Each subscriber gets:

* ``{"type": "snapshot", "available": true, "seq", "session", "history_s",
  "threshold_w", "time_zone", "curve", "events", "now"}`` first, and again
  whenever the add-on starts a new session (``reset``): the panel replaces
  what it shows;
* ``{"type": "update", "seq", "curve", "events", "now"}`` then: the new
  samples and the events that appeared or changed (an event comes back
  whole, with a new ``seq``, when it is confirmed, cancelled or completed);
* ``{"type": "status", "available": false, "reason", ...}`` when the view
  cannot run: ``disabled`` (the add-on option ``live_view`` is off),
  ``addon_too_old`` (add-on before 0.12.0), ``addon_unavailable``,
  ``starting``, ``overloaded`` (the add-on's self-protection, until
  ``retry_at``), ``error`` (``error``: code and message) or ``not_loaded``.

When the last panel leaves, the poll stops; the add-on stops its detection
2 minutes later.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperConnectionError
from .const import LIVE_MIN_ADDON

_LOGGER = logging.getLogger(__name__)

# seconds before polling again after an answer without the view
RETRY_S = {
    "starting": 0.5,
    "disabled": 60.0,
    "addon_too_old": 300.0,
    "error": 30.0,
    "overloaded": 60.0,
}
UNAVAILABLE_RETRY_S = (2.0, 5.0, 10.0, 30.0)
OTHER_ERROR_RETRY_S = 10.0
DEFAULT_HISTORY_S = 900.0

type Sender = Callable[[dict[str, Any]], None]


class LiveHub:
    """The add-on's live view, polled while someone watches (one per entry)."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EnergyMapperClient,
        addon_version: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.hass = hass
        self.client = client
        self._addon_version = addon_version
        self._subscribers: dict[int, Sender] = {}
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None
        self._snapshot: dict[str, Any] | None = None  # the merged state, while available
        self._status: dict[str, Any] | None = None  # the last "not available" status

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    @callback
    def subscribe(self, send: Sender) -> Callable[[], None]:
        """Add a panel; returns the function that removes it."""
        key = next(self._ids)
        self._subscribers[key] = send
        if self._snapshot is not None:
            send(self._snapshot_message())
        elif self._status is not None:
            send(self._status)
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_background_task(self._run(), "energy_mapper live view")

        @callback
        def unsubscribe() -> None:
            self._subscribers.pop(key, None)
            if not self._subscribers:
                self._halt()

        return unsubscribe

    @callback
    def async_stop(self) -> None:
        """The entry unloads: tell the panels, stop polling."""
        message = {"type": "status", "available": False, "reason": "not_loaded"}
        for send in list(self._subscribers.values()):
            send(message)
        self._subscribers.clear()
        self._halt()

    @callback
    def _halt(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._snapshot = None
        self._status = None

    # -- polling ------------------------------------------------------------------
    async def _run(self) -> None:
        since: int | None = None
        failures = 0
        while self._subscribers:
            try:
                body = await self.client.live(since)
            except EnergyMapperConnectionError as err:
                delay = UNAVAILABLE_RETRY_S[min(failures, len(UNAVAILABLE_RETRY_S) - 1)]
                failures += 1
                self._unavailable("addon_unavailable", message=str(err))
                since = None
                await asyncio.sleep(delay)
                continue
            except EnergyMapperApiError as err:
                since = None
                if err.status == 404:
                    self._unavailable(
                        "addon_too_old", min_version=LIVE_MIN_ADDON, addon_version=self._addon_version()
                    )
                    await asyncio.sleep(RETRY_S["addon_too_old"])
                else:
                    self._unavailable("error", error={"code": err.code, "message": err.message})
                    await asyncio.sleep(OTHER_ERROR_RETRY_S)
                continue
            failures = 0
            if not body.get("available"):
                since = None
                reason = str(body.get("reason") or "error")
                extra = {k: body[k] for k in ("error", "retry_at") if body.get(k) is not None}
                self._unavailable(reason, **extra)
                await asyncio.sleep(RETRY_S.get(reason, OTHER_ERROR_RETRY_S))
                continue
            if body.get("reset") or self._snapshot is None or since is None:
                self._replace(body)
            else:
                self._merge(body)
            since = body.get("seq")

    @callback
    def _unavailable(self, reason: str, **extra: Any) -> None:
        status = {"type": "status", "available": False, "reason": reason, **extra}
        self._snapshot = None
        if status != self._status:
            self._status = status
            self._broadcast(status)

    @callback
    def _replace(self, body: dict[str, Any]) -> None:
        self._status = None
        self._snapshot = {
            "seq": body.get("seq"),
            "session": body.get("session"),
            "history_s": body.get("history_s") or DEFAULT_HISTORY_S,
            "threshold_w": body.get("threshold_w"),
            "time_zone": body.get("time_zone"),
            "curve": list(body.get("curve") or []),
            "events": {e["id"]: e for e in body.get("events") or [] if e.get("state") != "cancelled"},
            "now": body.get("now"),
        }
        self._broadcast(self._snapshot_message())

    @callback
    def _merge(self, body: dict[str, Any]) -> None:
        snap = self._snapshot
        assert snap is not None
        curve = body.get("curve") or []
        events = body.get("events") or []
        changed = curve or events or body.get("now") != snap["now"]
        snap["seq"] = body.get("seq")
        if not changed:
            return
        snap["curve"].extend(curve)
        snap["now"] = body.get("now")
        for e in events:
            if e.get("state") == "cancelled":
                snap["events"].pop(e["id"], None)
            else:
                snap["events"][e["id"]] = e
        self._trim(snap)
        self._broadcast(
            {"type": "update", "seq": snap["seq"], "curve": curve, "events": events, "now": snap["now"]}
        )

    @staticmethod
    def _trim(snap: dict[str, Any]) -> None:
        """Keep the last ``history_s`` of the curve, and the events that run
        or ended within it."""
        if not snap["curve"]:
            return
        cutoff = snap["curve"][-1][0] - snap["history_s"]
        if snap["curve"][0][0] < cutoff:
            snap["curve"] = [p for p in snap["curve"] if p[0] >= cutoff]
        for key in [
            k
            for k, e in snap["events"].items()
            if not e.get("running") and (e.get("t_off") or e.get("t_on") or 0) < cutoff
        ]:
            del snap["events"][key]

    def _snapshot_message(self) -> dict[str, Any]:
        snap = self._snapshot
        assert snap is not None
        return {
            "type": "snapshot",
            "available": True,
            **{k: v for k, v in snap.items() if k != "events"},
            "curve": list(snap["curve"]),
            "events": sorted(snap["events"].values(), key=lambda e: e.get("t_on") or 0),
        }

    @callback
    def _broadcast(self, message: dict[str, Any]) -> None:
        for send in list(self._subscribers.values()):
            try:
                send(message)
            except Exception:  # one closed connection must not stop the others
                _LOGGER.debug("live view: a subscriber could not be sent a message", exc_info=True)
