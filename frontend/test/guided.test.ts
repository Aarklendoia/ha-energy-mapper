import assert from "node:assert/strict";
import { test } from "node:test";

import {
  guidedArm,
  guidedDone,
  guidedFinish,
  guidedMessage,
  guidedName,
  guidedNo,
  guidedRetrySave,
  guidedSaving,
  guidedStart,
  guidedTick,
  guidedYes,
  MAX_RUN_S,
  WAIT_S,
  WEAK_AFTER_S,
} from "../src/guided.ts";
import type { CurvePoint, FeedEntry, LiveEvent } from "../src/live.ts";
import { event } from "./live-fixtures.ts";

// synthetic stream (no real data)
const T0 = 1_800_000_000;
const entries = (...evs: LiveEvent[]): FeedEntry[] => evs.map((ev) => ({ ev, stream: 2 }));
const flat: CurvePoint[] = Array.from({ length: 30 }, (_, i) => [T0 - 60 + i * 5, 400, 400]);
const APPLIANCES = [{ key: "signature:short-2000w", name: "Kettle" }];

function armed() {
  return guidedArm(guidedName(guidedStart(), "Toaster"), T0);
}

test("name, then switch it on: a name is needed", () => {
  assert.equal(guidedArm(guidedStart(), T0).step, "name");
  const g = armed();
  assert.equal(g.step, "wait");
  assert.equal(g.deadline, T0 + WAIT_S);
});

test("the next switch-on after the click is proposed; yes, then its stop saves it", () => {
  let g = armed();
  // an event from before the click is not it
  const old = event(1, { t_on: T0 - 30 });
  g = guidedTick(g, entries(old), flat, 300, T0 + 2);
  assert.equal(g.step, "wait");
  const toaster = event(2, { t_on: T0 + 3, delta_w: 850, power_w: 850 });
  g = guidedTick(g, entries(old, toaster), flat, 300, T0 + 8);
  assert.equal(g.step, "confirm");
  assert.equal(g.event, 2);
  g = guidedYes(g);
  assert.equal(g.step, "running");
  g = guidedTick(g, entries(toaster), flat, 300, T0 + 60);
  assert.equal(g.step, "running");
  const stopped = { ...toaster, running: false, t_off: T0 + 133, duration_s: 130 };
  g = guidedTick(g, entries(stopped), flat, 300, T0 + 140);
  assert.equal(g.step, "save");
  assert.equal(g.t_off, T0 + 133);
  const msg = guidedMessage(g, { ev: stopped, stream: 2 }, APPLIANCES);
  assert.deepEqual(msg, {
    type: "energy_mapper/live/label",
    event_id: 2,
    session: 2,
    source: "guided",
    t_on: T0 + 3,
    t_off: T0 + 133,
    power_w: 850,
    target: "new",
    name: "Toaster",
    track: "curve",
  });
  g = guidedSaving(g);
  assert.equal(g.step, "saving");
  const done = guidedDone(g, "created", stopped);
  assert.equal(done.step, "done");
  assert.equal(done.duration, 130);
  assert.equal(done.power, 850);
});

test("not it: keep waiting, that event is not proposed again", () => {
  let g = armed();
  const other = event(3, { t_on: T0 + 5 });
  g = guidedTick(g, entries(other), flat, 300, T0 + 10);
  assert.equal(g.step, "confirm");
  g = guidedNo(g, T0 + 100);
  assert.equal(g.step, "wait");
  // a "no" late in the wait leaves a minute at least
  assert.equal(g.deadline, T0 + 160);
  g = guidedTick(g, entries(other), flat, 300, T0 + 101);
  assert.equal(g.step, "wait");
  const late = guidedNo(guidedTick(g, entries(other, event(4, { t_on: T0 + 115 })), flat, 300, T0 + 118), T0 + 118);
  assert.equal(late.deadline, T0 + 178);
});

test("two switch-ons at once: the user picks one", () => {
  let g = armed();
  const a = event(5, { t_on: T0 + 4 });
  const b = event(6, { t_on: T0 + 9, delta_w: 1200, power_w: 1200 });
  g = guidedTick(g, entries(a), flat, 300, T0 + 6);
  assert.equal(g.step, "confirm");
  g = guidedTick(g, entries(a, b), flat, 300, T0 + 12);
  assert.equal(g.step, "pick");
  assert.deepEqual(g.choices, [5, 6]);
  assert.equal(guidedYes(g, 99).step, "pick");
  const picked = guidedYes(g, 6);
  assert.equal(picked.step, "running");
  assert.equal(picked.event, 6);
  // neither: both are rejected
  assert.deepEqual(guidedNo(g, T0 + 12).rejected, [5, 6]);
});

test("a proposed step that is cancelled goes back to waiting", () => {
  let g = armed();
  const a = event(7, { t_on: T0 + 4, state: "provisional" });
  g = guidedTick(g, entries(a), flat, 300, T0 + 6);
  assert.equal(g.step, "confirm");
  g = guidedTick(g, entries(), flat, 300, T0 + 30);
  assert.equal(g.step, "wait");
});

test("nothing in 2 minutes; a small rise of the curve is too weak", () => {
  let g = armed();
  g = guidedTick(g, entries(), flat, 300, T0 + WAIT_S);
  assert.equal(g.step, "none");
  // the curve rose by 180 W after the click, without a step
  const rising: CurvePoint[] = [...flat.filter((p) => p[0] < T0), ...Array.from({ length: 12 }, (_, i): CurvePoint => [T0 + 6 + i * 5, 580, 580])];
  g = guidedTick(armed(), entries(), rising, 300, T0 + WEAK_AFTER_S - 1);
  assert.equal(g.step, "wait");
  g = guidedTick(g, entries(), rising, 300, T0 + WEAK_AFTER_S);
  assert.equal(g.step, "weak");
  assert.equal(g.rise, 180);
  // try again: wait anew
  assert.equal(guidedArm(g, T0 + 60).step, "wait");
});

test("finish without the stop, the 30-minute limit, a failed save", () => {
  let g = guidedYes(guidedTick(armed(), entries(event(8, { t_on: T0 + 2 })), flat, 300, T0 + 5));
  assert.equal(g.step, "running");
  const f = guidedFinish(g);
  assert.equal(f.step, "save");
  assert.equal(f.t_off, null);
  const msg = guidedMessage(f, { ev: event(8, { t_on: T0 + 2 }), stream: 2 }, [{ key: "a", name: "toaster" }]);
  assert.equal(msg.target, "appliance");
  assert.equal(msg.appliance, "a");
  assert.equal("t_off" in msg, false);
  g = guidedTick(g, entries(event(8, { t_on: T0 + 2 })), flat, 300, T0 + 3 + MAX_RUN_S);
  assert.equal(g.step, "save");
  const failed = { ...guidedSaving(g), step: "error" as const, error: "boom" };
  assert.equal(guidedRetrySave(failed).step, "save");
});
