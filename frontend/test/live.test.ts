import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applyMessage,
  clockOffset,
  curveRise,
  emptyStore,
  FEED_KEEP_S,
  feedEntries,
  houseAt,
  type LiveSnapshotMessage,
  zoomDrawing,
} from "../src/live.ts";
import { event } from "./live-fixtures.ts";

// synthetic stream (no real data)
const T0 = 1_800_000_000;

function snapshot(extra: Partial<LiveSnapshotMessage> = {}): LiveSnapshotMessage {
  return {
    type: "snapshot",
    available: true,
    seq: 10,
    session: 3,
    history_s: 900,
    threshold_w: 300,
    curve: [
      [T0, 400, 300],
      [T0 + 5, 410, 310],
    ],
    events: [event(1), event(2, { state: "provisional" })],
    now: { ts: T0 + 5, house_w: 410, running: [2, 1], unidentified_w: 0 },
    ...extra,
  };
}

test("a snapshot replaces the curve and fills the feed, tagged with its session", () => {
  const s = applyMessage(emptyStore(), snapshot(), T0 + 5);
  assert.equal(s.available, true);
  assert.equal(s.session, 3);
  assert.equal(s.threshold_w, 300);
  assert.equal(s.curve.length, 2);
  assert.deepEqual(
    feedEntries(s).map((e) => [e.ev.id, e.stream]),
    [
      [2, 3],
      [1, 3],
    ],
  );
});

test("updates merge by seq: an event comes back whole, an older copy is ignored, cancelled ones go", () => {
  let s = applyMessage(emptyStore(), snapshot(), T0 + 5);
  s = applyMessage(s, { type: "update", seq: 11, curve: [[T0 + 10, 1300, 1200]], events: [event(1, { seq: 11, running: false, t_off: T0 + 60, duration_s: 50 })], now: null }, T0 + 10);
  assert.equal(s.events.get(1)?.ev.running, false);
  assert.equal(s.curve.length, 3);
  assert.equal(s.seq, 11);
  // an older copy does not win
  s = applyMessage(s, { type: "update", seq: 12, curve: [], events: [event(1, { seq: 9, running: true })], now: null }, T0 + 10);
  assert.equal(s.events.get(1)?.ev.running, false);
  // a cancelled provisional step disappears
  s = applyMessage(s, { type: "update", seq: 13, curve: [], events: [event(2, { seq: 13, state: "cancelled" })], now: null }, T0 + 10);
  assert.equal(s.events.has(2), false);
  // a new event takes the current session
  s = applyMessage(s, { type: "update", seq: 14, curve: [], events: [event(5, { seq: 14 })], now: null }, T0 + 10);
  assert.equal(s.events.get(5)?.stream, 3);
  // "now" is kept when an update has none
  assert.equal(s.now?.house_w, 410);
});

test("the feed keeps the last hour; a new snapshot drops what ran and is gone", () => {
  let s = applyMessage(emptyStore(), snapshot(), T0 + 5);
  s = applyMessage(s, { type: "update", seq: 11, curve: [], events: [event(1, { seq: 11, running: false, t_off: T0 + 60 })], now: null }, T0 + 60);
  // a new session: event 1 (ended) is kept, event 2 (provisional, absent) is dropped
  s = applyMessage(s, snapshot({ seq: 1, session: 7, events: [event(9)] }), T0 + 100);
  assert.deepEqual(
    feedEntries(s).map((e) => [e.ev.id, e.stream]),
    [
      [9, 7],
      [1, 3],
    ],
  );
  // an hour later, the ended event is pruned; the running one stays
  s = applyMessage(s, { type: "update", seq: 2, curve: [], events: [], now: null }, T0 + 61 + FEED_KEEP_S);
  assert.deepEqual([...s.events.keys()], [9]);
});

test("a status keeps the feed but forgets the curve and what runs", () => {
  let s = applyMessage(emptyStore(), snapshot(), T0 + 5);
  s = applyMessage(s, { type: "status", available: false, reason: "overloaded", retry_at: T0 + 300 }, T0 + 6);
  assert.equal(s.available, false);
  assert.equal(s.status?.reason, "overloaded");
  assert.equal(s.curve.length, 0);
  assert.equal(s.now, null);
  assert.equal(s.events.size, 2);
});

test("the curve is trimmed to the history", () => {
  let s = applyMessage(emptyStore(), snapshot({ history_s: 60 }), T0 + 5);
  s = applyMessage(s, { type: "update", seq: 11, curve: [[T0 + 100, 500, 500]], events: [], now: null }, T0 + 100);
  assert.deepEqual(
    s.curve.map((p) => p[0]),
    [T0 + 100],
  );
});

test("the clock offset, the house power at a time, the rise of the curve", () => {
  const s = applyMessage(emptyStore(), snapshot(), T0);
  assert.equal(clockOffset(s, T0 - 2), 7);
  assert.equal(clockOffset(emptyStore(), T0), null);
  const curve: Array<[number, number | null, number | null]> = [
    [T0, 400, 0],
    [T0 + 10, 410, 0],
    [T0 + 20, 590, 0],
    [T0 + 30, 600, 0],
  ];
  assert.equal(houseAt(curve, T0 + 15), 410);
  assert.equal(houseAt(curve, T0 - 5), 400);
  assert.equal(curveRise(curve, T0 + 15, 3), 595 - 405);
  assert.equal(curveRise(curve, T0 + 100), null);
});

test("the inline zoom: a step path and the event's span", () => {
  const curve: Array<[number, number | null, number | null]> = [
    [T0, 100, 0],
    [T0 + 50, 1000, 0],
    [T0 + 100, 100, 0],
  ];
  const d = zoomDrawing(curve, T0, T0 + 100, T0 + 50, T0 + 100, 100, 50);
  assert.ok(d);
  assert.match(d.path, /^M0\.0,/);
  assert.equal(d.x, 50);
  assert.equal(d.w, 50);
  assert.equal(zoomDrawing([], T0, T0 + 10, T0, T0, 10, 10), null);
});
