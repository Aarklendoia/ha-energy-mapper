"""The live view service (#17): live detection, only while someone watches.

Nothing runs until a request comes. The first ``poll`` starts the ``live``
thread, which:

1. derives the configuration of the night (``Analyzer.live_config``: the
   meters, sub-meters and switched loads) and builds a
   :class:`~nilm_core.live.LiveDetector` with the signature registry;
2. subscribes to the state changes of the detector's entities only
   (``subscribe_entities``, ``HAClient.subscribe_states``);
3. replays the last 15 minutes of recorder history, so that the curve is
   full and an appliance switched on just before is seen;
4. then feeds each state change, builds the samples a few seconds behind the
   clock (PV and grid are not reported together), and publishes the new
   samples and the events that appeared or changed under a new **sequence
   number**.

It reconnects when Home Assistant goes away and fills the gap from the
history; it ranks the candidates again with a new registry (the nightly
update, a review decision: ``ReportStore.registry_version``); it starts
again when the configuration changes (settings, appliances' sources). It
stops after 2 minutes without a request and forgets everything (no cost when
nobody looks), and stops for 5 minutes when it falls behind by more than 30
seconds (self-protection). The nightly analysis runs in its own thread and is
never affected. The add-on option ``live_view`` (on by default) is the kill
switch.

``poll(since, wait)`` is the long-poll of ``GET /api/live``: with no
``since`` (or one from before the running session), a snapshot of the last
15 minutes (``reset: true``); else what changed after ``since``, as soon as
there is something, or after ``wait`` seconds with nothing new.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable

from ha_client import HAError
from nilm_core.live import LiveDetector, LiveEvent
from nilm_core.steps import CANCELLED
from service import AnalysisError, Analyzer

_LOGGER = logging.getLogger(__name__)

HISTORY_S = 900.0  # the curve and the replay at start: 15 minutes
IDLE_S = 120.0  # stops after this long without a request
WAIT_S = 25.0  # longest long-poll
DELAY_S = 5.0  # samples are built this far behind the clock
MAX_LAG_S = 30.0  # self-protection: stops when processing falls this far behind
OVERLOAD_PAUSE_S = 300.0  # then refuses to start again for this long
ERROR_PAUSE_S = 30.0  # after a failed start (Home Assistant down, no grid meter)
TICK_S = 1.0  # samples built and published at most this often
RERANK_S = 10.0  # running events ranked again, configuration checked
PING_S = 30.0
SILENCE_S = 75.0  # no message (not even a pong) for this long: reconnect
RECONNECT_S = (1.0, 2.0, 5.0, 10.0, 30.0)
TIMINGS_KEPT = 1000

# why the view is not available
DISABLED = "disabled"  # the live_view option is off
STARTING = "starting"  # the first snapshot is not ready yet
OVERLOADED = "overloaded"  # stopped by the self-protection
ERROR = "error"  # could not start (``error``: code and message)


def _r(value: float | None) -> int | None:
    return None if value is None else round(value)


class _Restart(Exception):
    """The configuration changed: start a new session."""


class LiveService:
    def __init__(
        self,
        analyzer: Analyzer,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
        subscribe: Callable | None = None,
        idle_s: float = IDLE_S,
        tick_s: float = TICK_S,
        rerank_s: float = RERANK_S,
        reconnect_s: tuple[float, ...] = RECONNECT_S,
    ):
        self.analyzer = analyzer
        self.store = analyzer.store
        self.enabled = enabled
        self.clock = clock
        self._subscribe = subscribe or analyzer.client.subscribe_states
        self.idle_s, self.tick_s, self.rerank_s = idle_s, tick_s, rerank_s
        self.reconnect_s = reconnect_s
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # increasing across sessions and add-on restarts (milliseconds at
        # start): a ``since`` from another process is always older than the
        # running session, so it gets a snapshot
        self._seq = int(clock() * 1000)
        self._session: int | None = None  # the seq the running session started at
        self._state = "idle"  # idle, starting, running, overloaded, error
        self._error: dict | None = None
        self._retry_at: float | None = None
        self._last_request: float | None = None
        self._next_event_id = 1
        self._reset_published()
        self._stats_reset()

    # -- published state (under self._cond) -----------------------------------
    def _reset_published(self) -> None:
        self._curve: deque[tuple[int, float, int | None, int | None]] = deque()
        self._events: dict[int, dict] = {}
        self._now: dict | None = None
        self._meta: dict = {}

    def _stats_reset(self) -> None:
        self._started_at: float | None = None
        self._readings = 0
        self._samples = 0
        self._steps = 0
        self._reconnects = 0
        self._lag = 0.0
        self._max_lag = 0.0
        self._timings: deque[float] = deque(maxlen=TIMINGS_KEPT)
        self._entities: list[str] = []
        self._interval: float | None = None
        self._buffered = 0

    # -- requests ---------------------------------------------------------------
    def poll(self, since: int | None = None, wait: float = WAIT_S) -> dict:
        """The long-poll of ``GET /api/live`` (also the "someone watches" signal)."""
        deadline = time.monotonic() + max(0.0, min(wait, WAIT_S))
        with self._cond:
            now = self.clock()
            self._last_request = now
            if not self.enabled:
                return self._unavailable(DISABLED)
            if self._state in (OVERLOADED, ERROR) and self._retry_at is not None and now < self._retry_at:
                return self._unavailable(self._state)
            if self._thread is None or not self._thread.is_alive():
                self._start()
            while True:
                if self._state == "idle":
                    self._start()  # the previous session ended while waiting
                if self._state == "running":
                    if since is None or self._session is None or not self._session <= since <= self._seq:
                        return self._answer(None)
                    if self._seq > since:
                        return self._answer(since)
                elif self._state in (OVERLOADED, ERROR):
                    return self._unavailable(self._state)
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            if self._state == "running":
                return self._answer(since)
            return self._unavailable(STARTING)

    def stats(self) -> dict:
        """``GET /api/live/stats`` (not a request: it does not start the view)."""
        with self._cond:
            now = self.clock()
            timings = sorted(self._timings)
            uptime = now - self._started_at if self._started_at else None
            return {
                "enabled": self.enabled,
                "state": self._state if self.enabled else DISABLED,
                "error": self._error,
                "retry_at": self._retry_at,
                "seq": self._seq,
                "session": self._session,
                "started_at": self._started_at,
                "last_request": self._last_request,
                "entities": list(self._entities),
                "interval_s": self._interval,
                "readings": self._readings,
                "readings_per_s": round(self._readings / uptime, 2) if uptime else None,
                "samples": self._samples,
                "steps": self._steps,
                "events": len(self._events),
                "reconnects": self._reconnects,
                "lag_s": round(self._lag, 1),
                "max_lag_s": round(self._max_lag, 1),
                "process_ms": {
                    "median": round(statistics.median(timings), 3) if timings else None,
                    "p99": round(timings[int(0.99 * (len(timings) - 1))], 3) if timings else None,
                    "max": round(timings[-1], 3) if timings else None,
                },
                "buffers": {
                    "curve": len(self._curve),
                    "events": len(self._events),
                    "readings": self._buffered,
                },
            }

    def stop(self) -> None:
        """Stop the live thread (add-on shutdown)."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def _unavailable(self, reason: str) -> dict:
        out = {"seq": self._seq, "available": False, "reason": reason}
        if reason == ERROR:
            out["error"] = self._error
        if reason in (OVERLOADED, ERROR):
            out["retry_at"] = self._retry_at
        return out

    def _answer(self, since: int | None) -> dict:
        """A snapshot (``since`` None) or what changed after ``since``."""
        if since is None:
            curve = [[ts, h, r] for _, ts, h, r in self._curve]
            events = sorted(
                (e for e in self._events.values() if e["state"] != CANCELLED), key=lambda e: e["t_on"]
            )
        else:
            curve = [[ts, h, r] for s, ts, h, r in self._curve if s > since]
            events = sorted((e for e in self._events.values() if e["seq"] > since), key=lambda e: e["t_on"])
        return {
            "seq": self._seq,
            "session": self._session,
            "available": True,
            "reset": since is None,
            **self._meta,
            "curve": curve,
            "events": events,
            "now": self._now,
        }

    def _start(self) -> None:
        self._state, self._error, self._retry_at = "starting", None, None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live", daemon=True)
        self._thread.start()

    # -- the live thread --------------------------------------------------------
    def _idle(self) -> bool:
        with self._cond:
            last = self._last_request
        return last is None or self.clock() - last > self.idle_s

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._session_loop()
                    break
                except _Restart:
                    _LOGGER.info("live view: the configuration changed, starting again")
                    with self._cond:
                        self._state = "starting"
        except (HAError, AnalysisError) as err:
            code = getattr(err, "code", "ha_error")
            message = getattr(err, "message", None) or str(err)
            _LOGGER.warning("live view not started: %s", message)
            self._end(ERROR, {"code": code, "message": message}, ERROR_PAUSE_S)
        except _Overloaded as err:
            _LOGGER.warning("live view stopped: %s (again in %d s at the earliest)", err, OVERLOAD_PAUSE_S)
            self._end(OVERLOADED, {"code": OVERLOADED, "message": str(err)}, OVERLOAD_PAUSE_S)
        except Exception as err:
            _LOGGER.exception("live view failed")
            self._end(ERROR, {"code": "internal", "message": str(err)}, ERROR_PAUSE_S)
        else:
            self._end("idle", None, None)

    def _end(self, state: str, error: dict | None, pause: float | None) -> None:
        with self._cond:
            self._state, self._error = state, error
            self._retry_at = self.clock() + pause if pause else None
            self._session = None
            self._reset_published()
            self._buffered = 0
            self._cond.notify_all()

    def _session_loop(self) -> None:
        session = _Session(self)
        try:
            session.open()
            while not self._stop.is_set():
                if self._idle():
                    _LOGGER.info("live view stopped: no request for %d s", self.idle_s)
                    return
                session.step()
        except _Stopped:
            _LOGGER.info("live view stopped while reconnecting")
        finally:
            session.close()

    # -- publishing (called by the session) -------------------------------------
    def _begin(self, det: LiveDetector, meta: dict) -> None:
        with self._cond:
            self._reset_published()
            self._stats_reset()
            self._started_at = self.clock()
            self._entities = det.entities
            self._meta = meta
            self._seq += 1
            self._session = self._seq

    def _running(self) -> None:
        with self._cond:
            self._state = "running"
            self._cond.notify_all()

    def _publish(self, samples: list, events: list[LiveEvent], det: LiveDetector, now: dict | None) -> None:
        with self._cond:
            self._samples += len(samples)
            alive = det.events
            dropped = [i for i in self._events if i not in alive]
            changed = bool(samples or events or dropped) or now != self._now
            if not changed or self._session is None:
                return
            self._seq += 1
            seq = self._seq
            for s in samples:
                self._curve.append((seq, round(s.ts, 3), _r(s.house), _r(s.residual)))
            if self._curve:
                cutoff = self._curve[-1][1] - HISTORY_S
                while self._curve and self._curve[0][1] < cutoff:
                    self._curve.popleft()
            for i in dropped:
                del self._events[i]
            for ev in events:
                if ev.id in alive:
                    self._events[ev.id] = {**_event(ev), "seq": seq}
            self._now = now
            self._interval = det.stream.interval_s
            self._cond.notify_all()


class _Overloaded(Exception):
    pass


def _event(ev: LiveEvent) -> dict:
    out = ev.to_dict()
    out["t_on"] = round(ev.t_on, 3)
    out["t_off"] = None if ev.t_off is None else round(ev.t_off, 3)
    out["steps"] = [[round(t, 3), round(d)] for t, d in ev.steps]
    return out


class _Session:
    """One subscription: the detector, the connection, the readings."""

    def __init__(self, service: LiveService):
        self.svc = service
        self.det: LiveDetector | None = None
        self.sub = None
        self.last_ts: dict[str, float] = {}  # the last reading pushed per entity
        self.opened_at = 0.0
        self.base_delay: float | None = None  # the smallest delay seen: clock offset
        self.batch_lags: list[float] = []
        self.next_tick = 0.0
        self.next_rerank = 0.0
        self.next_ping = 0.0
        self.registry_version = 0
        self.options: dict = {}
        self.appliance_of: dict[str, str] = {}

    # -- start ------------------------------------------------------------------
    def open(self) -> None:
        svc, analyzer = self.svc, self.svc.analyzer
        self.options = analyzer.live_options()
        config, tz = analyzer.live_config()
        self.registry_version = svc.store.registry_version
        det = LiveDetector(
            config,
            svc.store.load_registry(),
            local_hour=lambda ts: dt.datetime.fromtimestamp(ts, tz).hour,
            first_event_id=svc._next_event_id,
        )
        self.det = det
        self.appliance_of = {
            s["entity_id"]: a["key"]
            for a in analyzer.appliances
            for s in a.get("sources") or ()
            if s.get("entity_id")
        }
        svc._begin(
            det, {"history_s": HISTORY_S, "threshold_w": config.step_threshold_w, "time_zone": str(tz)}
        )
        self.sub = svc._subscribe(det.entities)
        now = svc.clock()
        self.opened_at = now
        self._fill(now - HISTORY_S, now)
        _LOGGER.info(
            "live view started: %d entit%s, %d reading(s) replayed",
            len(det.entities),
            "y" if len(det.entities) == 1 else "ies",
            svc._readings,
        )
        self._tick(now, rerank=True)
        svc._running()
        mono = time.monotonic()
        self.next_tick = mono + svc.tick_s
        self.next_rerank = mono + svc.rerank_s
        self.next_ping = mono + PING_S

    def _fill(self, start: float, end: float) -> None:
        """Push the recorder history of [start, end] (newer readings only)."""
        hist = self.svc.analyzer.client.history(self.det.entities, start, end)
        readings = sorted(
            ((ts, e, state) for e, points in hist.items() for ts, state in points), key=lambda r: r[0]
        )
        for ts, e, state in readings:
            self._push(e, ts, state, live=False)

    def _push(self, entity_id: str, ts: float | None, state: str, live: bool = True) -> None:
        now = self.svc.clock()
        if ts is None:
            ts = now
        if ts <= self.last_ts.get(entity_id, float("-inf")):
            return  # already pushed (the history and the subscription overlap)
        self.last_ts[entity_id] = ts
        self.det.push(entity_id, ts, state)
        self.svc._readings += 1
        if live and ts >= self.opened_at:
            delay = now - ts
            self.base_delay = delay if self.base_delay is None else min(self.base_delay, delay)
            self.batch_lags.append(delay - self.base_delay)

    # -- loop ---------------------------------------------------------------------
    def step(self) -> None:
        svc = self.svc
        mono = time.monotonic()
        if mono >= self.next_tick:
            rerank = mono >= self.next_rerank
            if rerank:
                self.next_rerank = mono + svc.rerank_s
                if svc.analyzer.live_options() != self.options:
                    raise _Restart
            self._tick(svc.clock(), rerank)
            self.next_tick = time.monotonic() + svc.tick_s
        if mono >= self.next_ping:
            self.next_ping = mono + PING_S
            self._guard(self.sub.ping)
        try:
            batch = self.sub.next(max(0.0, self.next_tick - time.monotonic()))
        except HAError as err:
            self._reconnect(err)
            return
        if batch is None:
            if time.monotonic() - self.sub.last_message > SILENCE_S:
                self._reconnect(HAError("no message from Home Assistant"))
            return
        for entity_id, ts, state in batch:
            self._push(entity_id, ts, state)

    def _guard(self, call) -> None:
        try:
            call()
        except HAError as err:
            self._reconnect(err)

    def _tick(self, now: float, rerank: bool = False) -> None:
        svc, det = self.svc, self.det
        t0 = time.perf_counter()
        update = det.advance(now - DELAY_S)
        changed = {ev.id: ev for ev in update.events}
        version = svc.store.registry_version
        if version != self.registry_version:
            self.registry_version = version
            det.set_registry(svc.store.load_registry())
            rerank = True
        if rerank:
            for ev in det.rerank(now):
                changed[ev.id] = ev
        det.pairer.prune(now - HISTORY_S)
        svc._next_event_id = det.pairer._next_id
        svc._steps += len(update.steps)
        summary = self._summary(update.samples)
        svc._publish(update.samples, list(changed.values()), det, summary)
        elapsed = time.perf_counter() - t0
        svc._timings.append(elapsed * 1000.0)
        svc._buffered = len(det.builder.grid.out.points) + sum(len(s.points) for s in det.builder.subs)
        lag = max(min(self.batch_lags, default=0.0), elapsed)
        self.batch_lags = []
        svc._lag = lag
        svc._max_lag = max(svc._max_lag, lag)
        if lag > MAX_LAG_S:
            raise _Overloaded(f"processing is {lag:.0f} s behind (limit {MAX_LAG_S:.0f} s)")

    def _summary(self, samples: list) -> dict | None:
        """The "now" card: the last sample, what runs."""
        det = self.det
        last = samples[-1] if samples else None
        prev = self.svc._now
        if last is None and prev is None:
            return None
        running = sorted((e for e in det.events.values() if e.running), key=lambda e: -e.t_on)
        out = dict(prev or {})
        if last is not None:
            ts = last.ts
            out.update(
                {
                    "ts": round(ts, 3),
                    "house_w": _r(last.house),
                    "residual_w": _r(last.residual),
                    "grid_w": _r(last.grid),
                    "pv_w": _r(last.pv),
                    "battery_w": _r(last.battery),
                    "submetered_w": _r(last.submetered),
                    "submeters": [
                        {
                            "entity_id": sm.entity_id,
                            "name": sm.name,
                            "appliance": self.appliance_of.get(sm.entity_id),
                            "estimated": sm.estimated,
                            "power_w": _r(series.value_at(ts)),
                        }
                        for sm, series in zip(det.config.submeters, det.builder.subs, strict=True)
                    ],
                }
            )
        out["running"] = [e.id for e in running]
        residual = out.get("residual_w")
        out["unidentified_w"] = (
            None if residual is None else max(0, round(residual - sum(e.power_w for e in running)))
        )
        out["switches"] = [
            {
                "entity_id": sl.entity_id,
                "name": sl.name,
                "appliance": self.appliance_of.get(sl.entity_id),
                "on": det._switch_on.get(sl.entity_id),
            }
            for sl in det.config.switched_loads
        ]
        return out

    def _reconnect(self, err: HAError) -> None:
        """Connect again, then fill the gap from the history."""
        svc = self.svc
        _LOGGER.warning("live view: %s; reconnecting", err)
        if self.sub is not None:
            self.sub.close()
            self.sub = None
        for attempt in itertools.count():
            if svc._stop.is_set() or svc._idle():
                raise _Stopped
            delay = svc.reconnect_s[min(attempt, len(svc.reconnect_s) - 1)]
            if svc._stop.wait(delay):
                raise _Stopped
            try:
                self.sub = svc._subscribe(self.det.entities)
                now = svc.clock()
                newest = max(self.last_ts.values(), default=now - HISTORY_S)
                self._fill(max(newest, now - HISTORY_S), now)
            except HAError as again:
                if self.sub is not None:
                    self.sub.close()
                    self.sub = None
                _LOGGER.debug("live view: reconnection failed: %s", again)
                continue
            svc._reconnects += 1
            self.opened_at = now
            self.base_delay = None
            self.next_ping = time.monotonic() + PING_S
            _LOGGER.info("live view: reconnected")
            return

    def close(self) -> None:
        if self.sub is not None:
            self.sub.close()
            self.sub = None


class _Stopped(Exception):
    """Stopped while reconnecting (idle, or the add-on stops)."""
