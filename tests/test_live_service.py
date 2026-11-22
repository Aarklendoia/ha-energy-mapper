"""Tests of the live view service (#17): on-demand start, long-poll sequence
numbers, history replay, reconnection, idle stop, self-protection and the
kill switch, with a fake Home Assistant (a fake state subscription)."""

from __future__ import annotations

import json
import pathlib
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))

from api import route  # noqa: E402
from ha_client import HAError, entity_changes  # noqa: E402
from live_service import HISTORY_S, LiveService  # noqa: E402
from service import Analyzer  # noqa: E402
from storage import ReportStore  # noqa: E402
from ws_client import OP_TEXT, WebSocket, encode_frame  # noqa: E402

GRID = "sensor.grid_power"
T0 = 1_800_000_000.0
STEP_S = 2.0  # the fake meter reports every 2 s


def snapshot():
    states = [
        {
            "entity_id": "sensor.grid_energy",
            "state": "1234.5",
            "attributes": {"device_class": "energy", "unit_of_measurement": "kWh"},
        },
        {
            "entity_id": GRID,
            "state": "300",
            "attributes": {"device_class": "power", "unit_of_measurement": "W"},
        },
    ]
    registry = [
        {"entity_id": "sensor.grid_energy", "device_id": "meter", "platform": "test"},
        {"entity_id": GRID, "device_id": "meter", "platform": "test"},
    ]
    prefs = {
        "energy_sources": [{"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid_energy"}]}],
        "device_consumption": [],
    }
    return {"prefs": prefs, "states": states, "registry": registry, "config": {"time_zone": "UTC"}}


class Clock:
    def __init__(self, t: float):
        self.t = t
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.t

    def set(self, t: float) -> None:
        with self.lock:
            self.t = t


class House:
    """The whole-house power: 300 W and some appliances ``(on, off, watts)``."""

    def __init__(self, loads=()):
        self.loads = list(loads)

    def power(self, t: float) -> float:
        return 300.0 + sum(w for a, b, w in self.loads if a <= t < b)

    def readings(self, start: float, end: float) -> list[tuple[float, str]]:
        """What the recorder holds: every 2 s; the first moved to ``start``."""
        t = start - start % STEP_S
        out = [(start, str(self.power(start)))]
        t += STEP_S
        while t <= end:
            out.append((t, str(self.power(t))))
            t += STEP_S
        return out


class FakeClient:
    def __init__(self, house: House):
        self.house = house
        self.fail = False
        self.history_calls: list[tuple[float, float]] = []

    def snapshot(self):
        if self.fail:
            raise HAError("websocket: connection refused")
        return snapshot()

    def history(self, entities, start, end):
        self.history_calls.append((start, end))
        return {GRID: self.house.readings(start, end)} if GRID in entities else {}


class FakeSubscription:
    """Batches ``(clock time, [(entity, ts, state)])``, or an exception."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.queue: queue.Queue = queue.Queue()
        self.closed = False
        self.pings = 0
        self.last_message = time.monotonic()

    def next(self, timeout):
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, Exception):
            raise item
        at, batch = item
        self.clock.set(at)
        self.last_message = time.monotonic()
        return batch

    def ping(self):
        self.pings += 1

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, test: unittest.TestCase, house: House, enabled=True, t=T0, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.clock = Clock(t)
        self.client = FakeClient(house)
        self.house = house
        self.store = ReportStore(pathlib.Path(tmp.name))
        self.analyzer = Analyzer(self.client, self.store, {"backfill_days": 0}, clock=self.clock)
        self.subs: list[FakeSubscription] = []
        self.subscribe_entities: list[list[str]] = []
        self.refuse = 0  # subscriptions to refuse first
        kwargs.setdefault("tick_s", 0.01)
        kwargs.setdefault("rerank_s", 0.05)
        kwargs.setdefault("reconnect_s", (0.01,))
        self.svc = LiveService(
            self.analyzer, enabled=enabled, clock=self.clock, subscribe=self.subscribe, **kwargs
        )
        test.addCleanup(self.svc.stop)

    def subscribe(self, entities):
        self.subscribe_entities.append(list(entities))
        if self.refuse:
            self.refuse -= 1
            raise HAError("websocket: connection refused")
        sub = FakeSubscription(self.clock)
        self.subs.append(sub)
        return sub

    @property
    def sub(self) -> FakeSubscription:
        return self.subs[-1]

    def feed(self, start: float, end: float, pace: float = 0.0) -> None:
        """Live readings of (start, end], each delivered at its own time;
        with ``pace``, one every ``pace`` real seconds (in the background), so
        that the service sees them come one by one as from a real meter."""
        sub = self.sub
        items = []
        t = start - start % STEP_S + STEP_S
        while t <= end:
            items.append((t, [(GRID, t, str(self.house.power(t)))]))
            t += STEP_S

        def put():
            for item in items:
                sub.queue.put(item)
                if pace:
                    time.sleep(pace)

        if pace:
            threading.Thread(target=put, daemon=True).start()
        else:
            put()

    def until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.005)
        raise AssertionError("condition not met in time")

    def poll_until(self, since, predicate, timeout=5.0):
        """Long-poll from ``since`` until an answer satisfies ``predicate``;
        returns the answers in order."""
        answers = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            answer = self.svc.poll(since, wait=0.5)
            answers.append(answer)
            since = answer["seq"]
            try:
                if predicate(answer):
                    return answers
            except KeyError:
                raise AssertionError(f"unexpected answer: {answer}") from None
        raise AssertionError(f"no matching answer: {answers[-1:]}")


class LiveServiceTest(unittest.TestCase):
    def test_first_request_starts_and_replays_15_minutes(self):
        # a 2 kW appliance switched on 5 minutes before the tab was opened
        h = Harness(self, House([(T0 - 300, T0 + 3600, 2000)]))
        self.assertEqual(h.svc.stats()["state"], "idle")  # nothing runs before a request
        self.assertEqual(h.subscribe_entities, [])
        answer = h.svc.poll(None, wait=5)
        self.assertTrue(answer["available"])
        self.assertTrue(answer["reset"])
        self.assertEqual(answer["session"], answer["seq"] - 1)  # began, then the replay
        self.assertEqual(h.subscribe_entities, [[GRID]])
        self.assertEqual(h.client.history_calls[0], (T0 - HISTORY_S, T0))
        self.assertEqual(answer["history_s"], HISTORY_S)
        self.assertEqual(answer["threshold_w"], 300.0)
        curve = answer["curve"]
        self.assertGreater(len(curve), 400)  # 15 min of a 2 s meter, minus the delay
        self.assertLessEqual(curve[0][0], T0 - HISTORY_S + STEP_S)
        self.assertEqual(curve[0][1:], [300, 300])  # [ts, house W, residual W]
        self.assertEqual(curve[-1][1], 2300)
        [ev] = answer["events"]
        self.assertEqual(ev["state"], "confirmed")
        self.assertTrue(ev["running"])
        self.assertAlmostEqual(ev["t_on"], T0 - 300, delta=STEP_S)
        self.assertEqual(ev["delta_w"], 2000)
        self.assertEqual(ev["seq"], answer["seq"])
        self.assertEqual(ev["steps"], [[ev["t_on"], 2000]])
        now = answer["now"]
        self.assertEqual(now["running"], [ev["id"]])
        self.assertEqual(now["residual_w"], 2300)
        self.assertEqual(now["unidentified_w"], 300)
        self.assertEqual(now["submeters"], [])
        self.assertEqual(now["switches"], [])

    def test_long_poll_sequence_numbers(self):
        # the fake clock runs faster than the requests come: no idle stop here
        h = Harness(self, House([(T0 + 60, T0 + 180, 2000)]), idle_s=10_000)
        snap = h.svc.poll(None, wait=5)
        seq = snap["seq"]
        self.assertEqual(snap["events"], [])
        # nothing new: the long-poll waits, then answers the same seq, empty
        t = time.monotonic()
        quiet = h.svc.poll(seq, wait=0.2)
        self.assertGreaterEqual(time.monotonic() - t, 0.19)
        self.assertEqual(
            (quiet["seq"], quiet["reset"], quiet["curve"], quiet["events"]), (seq, False, [], [])
        )
        # live readings: the switch-on, then the off-step completes the same line
        h.feed(T0, T0 + 260, pace=0.02)
        answers = h.poll_until(seq, lambda a: any(e["t_off"] for e in a["events"]), timeout=10)
        seqs = [a["seq"] for a in answers]
        self.assertEqual(seqs, sorted(seqs))
        self.assertTrue(all(not a["reset"] for a in answers))
        curve = [p for a in answers for p in a["curve"]]
        stamps = [p[0] for p in curve]
        self.assertEqual(stamps, sorted(set(stamps)))  # each sample once, in order
        self.assertGreater(stamps[0], snap["curve"][-1][0])
        lines = [e for a in answers for e in a["events"]]
        self.assertEqual({e["id"] for e in lines}, {lines[0]["id"]})  # one event, updated
        states = [e["state"] for e in lines]
        self.assertEqual(states[0], "provisional")  # greyed first
        self.assertIn("confirmed", states)
        last = lines[-1]
        self.assertFalse(last["running"])
        self.assertAlmostEqual(last["duration_s"], 120, delta=2 * STEP_S)
        self.assertEqual(last["off_w"], 2000)
        for a in answers:  # an event comes back with the seq of its last change
            for e in a["events"]:
                self.assertGreater(e["seq"], seqs[0] - 1 if a is answers[0] else 0)
                self.assertLessEqual(e["seq"], a["seq"])
        # a client that missed nothing gets nothing twice; an old one a diff
        rest = h.poll_until(answers[-1]["seq"], lambda a: a["now"]["ts"] >= T0 + 254)
        curve += [p for a in rest for p in a["curve"]]
        final = rest[-1]["seq"]
        again = h.svc.poll(final, wait=0.05)
        self.assertEqual(again["events"], [])
        diff = h.svc.poll(seq, wait=0)
        self.assertFalse(diff["reset"])
        self.assertEqual([e["id"] for e in diff["events"]], [last["id"]])
        self.assertEqual(len(diff["curve"]), len(curve))
        # a seq from the future (another add-on process): a snapshot
        self.assertTrue(h.svc.poll(final + 10, wait=0)["reset"])

    def test_idle_stop_frees_everything(self):
        h = Harness(self, House(), idle_s=120)
        first = h.svc.poll(None, wait=5)
        sub = h.sub
        h.clock.set(T0 + 100)
        time.sleep(0.05)
        self.assertEqual(h.svc.stats()["state"], "running")  # 100 s: still watched
        h.clock.set(T0 + 221)  # 2 min without a request
        h.until(lambda: h.svc.stats()["state"] == "idle")
        self.assertTrue(sub.closed)
        stats = h.svc.stats()
        self.assertEqual(stats["buffers"], {"curve": 0, "events": 0, "readings": 0})
        self.assertIsNone(stats["session"])
        # the next request starts a new session: an old seq gets a snapshot
        again = h.svc.poll(first["seq"], wait=5)
        self.assertTrue(again["reset"])
        self.assertGreater(again["session"], first["seq"])
        self.assertEqual(len(h.subs), 2)

    def test_restart_replays_the_history(self):
        house = House([(T0 - 200, T0 + 3600, 1500)])
        h = Harness(self, house)
        before = h.svc.poll(None, wait=5)
        h.svc.stop()
        # the add-on restarts 30 s later: the panel's seq is from the old process
        h2 = Harness(self, house, t=T0 + 30)
        after = h2.svc.poll(before["seq"], wait=5)
        self.assertTrue(after["reset"])
        self.assertGreater(after["session"], before["seq"])
        [ev] = after["events"]  # seen again from the history
        self.assertEqual(ev["delta_w"], 1500)
        self.assertTrue(ev["running"])
        self.assertEqual(h2.client.history_calls[0], (T0 + 30 - HISTORY_S, T0 + 30))

    def test_reconnects_and_fills_the_gap(self):
        house = House([(T0 + 30, T0 + 3600, 2000)])
        h = Harness(self, house)
        snap = h.svc.poll(None, wait=5)
        h.feed(T0, T0 + 10)
        h.poll_until(snap["seq"], lambda a: a["curve"] and a["curve"][-1][0] >= T0 + 4)
        first = h.sub
        # Home Assistant restarts: the connection drops, the gap is in the history
        first.queue.put(HAError("websocket: connection closed"))
        h.refuse = 1  # the first attempt fails
        h.clock.set(T0 + 90)
        h.until(lambda: h.svc.stats()["reconnects"] == 1)
        self.assertTrue(first.closed)
        self.assertEqual(len(h.subscribe_entities), 3)
        start, end = h.client.history_calls[-1]
        self.assertEqual((start, end), (T0 + 10, T0 + 90))  # from the last reading
        answers = h.poll_until(snap["seq"], lambda a: any(e["delta_w"] == 2000 for e in a["events"]))
        stamps = [p[0] for a in answers for p in a["curve"]]
        self.assertEqual(stamps, sorted(set(stamps)))  # no duplicate around the gap
        self.assertTrue(any(T0 + 10 < ts < T0 + 80 for ts in stamps))

    def test_kill_switch(self):
        h = Harness(self, House(), enabled=False)
        answer = h.svc.poll(None, wait=5)
        self.assertEqual(answer["available"], False)
        self.assertEqual(answer["reason"], "disabled")
        self.assertEqual(h.subscribe_entities, [])
        self.assertEqual(h.svc.stats()["state"], "disabled")
        status, body = route(h.analyzer, h.store, "GET", "/api/live", live=h.svc)
        self.assertEqual((status, body["reason"]), (200, "disabled"))
        status, body = route(h.analyzer, h.store, "GET", "/api/live/stats", live=h.svc)
        self.assertEqual((status, body["enabled"]), (200, False))

    def test_self_protection(self):
        h = Harness(self, House())
        snap = h.svc.poll(None, wait=5)
        readings = h.svc.stats()["readings"]
        h.sub.queue.put((T0 + 2, [(GRID, T0 + 2, "300")]))
        h.until(lambda: h.svc.stats()["readings"] > readings)
        time.sleep(0.1)  # a few ticks: the next reading is judged on its own
        h.sub.queue.put((T0 + 50, [(GRID, T0 + 4, "300")]))  # handled 46 s late
        answers = h.poll_until(snap["seq"], lambda a: not a["available"])
        self.assertEqual(answers[-1]["reason"], "overloaded")
        self.assertEqual(answers[-1]["retry_at"], T0 + 50 + 300)
        stats = h.svc.stats()
        self.assertEqual(stats["state"], "overloaded")
        self.assertGreater(stats["max_lag_s"], 30)
        # not again before the pause, then again
        self.assertEqual(h.svc.poll(None, wait=0)["reason"], "overloaded")
        self.assertEqual(len(h.subs), 1)
        h.clock.set(T0 + 351)
        self.assertTrue(h.svc.poll(None, wait=5)["available"])
        self.assertEqual(len(h.subs), 2)

    def test_start_failure_is_reported(self):
        h = Harness(self, House())
        h.client.fail = True
        answer = h.svc.poll(None, wait=5)
        self.assertEqual(answer["reason"], "error")
        self.assertEqual(answer["error"]["code"], "ha_error")
        self.assertEqual(answer["retry_at"], T0 + 30)
        h.client.fail = False
        self.assertEqual(h.svc.poll(None, wait=0)["reason"], "error")  # paused
        h.clock.set(T0 + 31)
        self.assertTrue(h.svc.poll(None, wait=5)["available"])

    def test_new_registry_ranks_again(self):
        h = Harness(self, House([(T0 - 120, T0 + 3600, 2000)]))
        snap = h.svc.poll(None, wait=5)
        [ev] = snap["events"]
        self.assertEqual(ev["candidates"], [])
        # the nightly update (or a review decision) writes the registry
        h.store.save_registry(
            {
                "power_tol": 0.12,
                "signatures": [
                    {
                        "id": "long-2000w",
                        "kind": "steady",
                        "band": "long",
                        "power_w": 2000,
                        "label": "Heater",
                        "hours": [0.0] * 24,
                        "per_week": 7,
                    }
                ],
            }
        )
        answers = h.poll_until(snap["seq"], lambda a: a["events"])
        [updated] = answers[-1]["events"]
        self.assertEqual(updated["id"], ev["id"])
        self.assertEqual(updated["candidates"][0]["signature_id"], "long-2000w")
        self.assertEqual(updated["confidence"], "likely")

    def test_configuration_change_starts_again(self):
        h = Harness(self, House())
        snap = h.svc.poll(None, wait=5)
        h.analyzer.set_settings({"step_threshold_w": 500.0})
        answers = h.poll_until(snap["seq"], lambda a: a.get("reset"))
        self.assertEqual(answers[-1]["threshold_w"], 500.0)
        self.assertEqual(len(h.subs), 2)
        self.assertTrue(h.subs[0].closed)

    def test_query_validation(self):
        h = Harness(self, House())
        for target in ("/api/live?since=x", "/api/live?since=-1", "/api/live?wait=abc"):
            status, body = route(h.analyzer, h.store, "GET", target, live=h.svc)
            self.assertEqual((status, body["error"]), (400, "invalid_query"))
        self.assertEqual(route(h.analyzer, h.store, "POST", "/api/live", live=h.svc)[0], 405)
        self.assertEqual(h.subscribe_entities, [])


class SubscriptionTest(unittest.TestCase):
    def test_entity_changes(self):
        first = {"a": {GRID: {"s": "300", "a": {"unit_of_measurement": "W"}, "lc": T0, "lu": T0 + 1}}}
        self.assertEqual(entity_changes(first), [(GRID, T0, "300")])
        change = {
            "c": {
                GRID: {"+": {"s": "2300", "lc": T0 + 5, "lu": T0 + 5}},
                "switch.relay": {"+": {"lu": T0 + 6, "a": {"x": 1}}},  # attributes only
                "sensor.other": {"-": {"a": ["x"]}},
            }
        }
        self.assertEqual(entity_changes(change), [(GRID, T0 + 5, "2300")])
        self.assertEqual(entity_changes({"c": {GRID: {"+": {"s": "1"}}}}), [(GRID, None, "1")])
        self.assertEqual(entity_changes({}), [])

    def test_recv_text_timeout_keeps_a_partial_frame(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        ws = WebSocket.__new__(WebSocket)
        ws._sock, ws._timeout, ws._buf, ws._parts = a, 5.0, bytearray(), []
        payload = json.dumps({"type": "event", "id": 1, "event": {"c": {}}}).encode()
        frame = bytes([0x81, len(payload)]) + payload  # a server frame: not masked
        self.assertIsNone(ws.recv_text(0.01))
        b.sendall(frame[:5])
        self.assertIsNone(ws.recv_text(0.05))  # incomplete: nothing lost
        b.sendall(frame[5:] + bytes([0x89, 0]))  # the rest, then a ping
        self.assertEqual(json.loads(ws.recv_text(1.0))["type"], "event")
        self.assertIsNone(ws.recv_text(0.05))  # the ping was answered
        self.assertEqual(b.recv(16)[:2], encode_frame(0xA, b"", mask=b"\0\0\0\0")[:2])
        self.assertEqual(OP_TEXT, 1)


if __name__ == "__main__":
    unittest.main()
