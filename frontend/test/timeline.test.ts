import assert from "node:assert/strict";
import { test } from "node:test";

import {
  axisTicks,
  bucketCount,
  houseBuckets,
  isWholeDay,
  notablePeaks,
  peakZoom,
  type RunPeriod,
  timelineRows,
  visibleBlocks,
} from "../src/timeline.ts";
import type { CurvePoint, Peak, Registry, Report } from "../src/types.ts";

const T0 = Date.UTC(2027, 2, 9, 0, 0) / 1000;

function point(i: number, values: Partial<CurvePoint> = {}): CurvePoint {
  return {
    t: T0 + 8 * 3600 + i * 60,
    house: 1000,
    grid: 1000,
    pv: 0,
    submetered: 0,
    base: 170,
    other: 30,
    known: {},
    signatures: {},
    ...values,
  };
}

/** A day with a switched load on twice, a sub-meter, two signatures (one
 * with activations, one only in the curve) and two peaks. */
function report(): Report {
  const heater = [2400, 2400, 0, 0, 2400, 0];
  const oven = [0, 0, 0, 1800, 1800, 0];
  return {
    day: "2027-03-09",
    start: T0,
    end: T0 + 86400,
    energy_kwh: {
      house: 10,
      grid_import: 10,
      grid_export: 0,
      pv: 0,
      submetered: 0.5,
      base: 4,
      other: 0.5,
      base_w: 170,
      known: { "Water heater": 2.4 },
      signatures: { "short-2000w": 0.6, "short-2050w": 0.2, "oven-1800w": 0.1 },
    },
    max_house_w: 4000,
    peak_threshold_w: 3500,
    curve: heater.map((w, i) =>
      point(i, {
        house: 1000 + w + oven[i],
        submetered: i < 3 ? 500 : 0,
        known: { "Water heater": w },
        signatures: { "oven-1800w": oven[i] },
      }),
    ),
    peaks: [
      { start: T0 + 20000, end: T0 + 20300, max_ts: T0 + 20100, house_w: 3600, grid_w: 0, pv_w: 0, parts: [], unexplained_w: 0, notable: true },
      { start: T0 + 9000, end: T0 + 9100, max_ts: T0 + 9050, house_w: 3000, grid_w: 0, pv_w: 0, parts: [], unexplained_w: 0, notable: false },
      { start: T0 + 300, end: T0 + 400, max_ts: T0 + 350, house_w: 3900, grid_w: 0, pv_w: 0, parts: [], unexplained_w: 0, notable: true },
    ],
    signatures: [
      { id: "short-2000w", kind: "steady", band: "short", power_w: 2000, count: 1, duration_s: 60, energy_kwh: 0.6, registry_id: "short-2000w" },
      // a second daily cluster of the same registry signature
      { id: "short-2050w", kind: "steady", band: "short", power_w: 2050, count: 1, duration_s: 30, energy_kwh: 0.2, registry_id: "short-2000w" },
      { id: "oven-1800w", kind: "steady", band: "medium", power_w: 1800, count: 1, duration_s: 120, energy_kwh: 0.1, registry_id: null },
    ],
    activations: [
      { start: T0 + 50000, end: T0 + 50060, power_w: 2050, cycling: false, signature: "short-2050w", registry_id: "short-2000w" },
      { start: T0 + 40000, end: T0 + 43600, power_w: 2000, cycling: true, signature: "short-2000w", registry_id: "short-2000w" },
    ],
    known_loads: [
      { name: "Water heater", entity_id: "switch.heater", power_w: 2400, transitions: 4, matched: 4, inverted: false, on_s: 180 },
    ],
  };
}

const registry: Registry = {
  signatures: [
    {
      id: "short-2000w", kind: "steady", band: "short", label: "Kettle", power_w: 2000, duration_s: 120,
      on_s: null, period_s: null, duty: null, weight: 3, count: 3, days_seen: 3, per_week: 3,
      first_seen: "2027-03-01", last_seen: "2027-03-09", hours: [], energy_kwh: 1,
    },
  ],
  switched_loads: [],
  peak: null,
};

test("timeline rows: the rows of the list without base load and unexplained, same order", () => {
  const rows = timelineRows(report(), registry);
  assert.deepEqual(
    rows.map((r) => r.id),
    ["known:Water heater", "sig:short-2000w", "submetered", "sig:oven-1800w"],
  );
  assert.equal(rows.find((r) => r.id === "sig:oven-1800w")!.unnamed, true);
  assert.equal(rows.find((r) => r.id === "sig:short-2000w")!.unnamed, false);
});

test("timeline rows: signatures run in their activations, all daily clusters, in time order", () => {
  const kettle = timelineRows(report(), registry).find((r) => r.id === "sig:short-2000w")!;
  assert.deepEqual(kettle.periods, [
    { start: T0 + 40000, end: T0 + 43600, cycling: true, power_w: 2000 },
    { start: T0 + 50000, end: T0 + 50060, cycling: false, power_w: 2050 },
  ]);
});

test("timeline rows: switched loads, sub-meters and signatures without activations from the curve", () => {
  const rows = timelineRows(report(), registry);
  const at = (i: number) => T0 + 8 * 3600 + i * 60;
  const periods = (id: string) => rows.find((r) => r.id === id)!.periods.map((p) => [p.start, p.end]);
  // on at points 0-1 and 4: two periods, each point lasting one step
  assert.deepEqual(periods("known:Water heater"), [
    [at(0), at(2)],
    [at(4), at(5)],
  ]);
  assert.deepEqual(periods("submetered"), [[at(0), at(3)]]);
  assert.deepEqual(periods("sig:oven-1800w"), [[at(3), at(5)]]);
});

test("timeline rows: a pruned curve leaves switched loads without periods", () => {
  const r = report();
  r.curve = [];
  const heater = timelineRows(r, registry).find((x) => x.id === "known:Water heater")!;
  assert.deepEqual(heater.periods, []);
});

const run = (start: number, end: number, cycling = false): RunPeriod => ({ start, end, cycling, power_w: null });

test("visible blocks: positions as fractions of the window, clipped to it", () => {
  const blocks = visibleBlocks([run(0, 100), run(250, 300), run(900, 1200), run(1300, 1400)], [0, 1000]);
  assert.deepEqual(
    blocks.map((b) => [b.left, b.width]),
    [
      [0, 0.1],
      [0.25, 0.05],
      [0.9, 0.1],
    ],
  );
  // the block keeps the real times of its period (for its tooltip)
  assert.equal(blocks[2].end, 1200);
});

test("visible blocks: close periods merge, short ones keep a minimum width", () => {
  const blocks = visibleBlocks([run(100, 110, true), run(115, 130), run(500, 501)], [0, 1000], 10, 0.01);
  assert.equal(blocks.length, 2);
  assert.deepEqual([blocks[0].start, blocks[0].end, blocks[0].cycling], [100, 130, true]);
  assert.equal(blocks[1].width, 0.01);
  // a short run at the very end stays inside the track
  const [last] = visibleBlocks([run(999, 1000)], [0, 1000], 0, 0.01);
  assert.equal(last.left + last.width, 1);
  assert.deepEqual(visibleBlocks([run(0, 10)], [5, 5]), []);
});

test("house strip: highest power per slice, null where there is no point", () => {
  const curve = [0, 1, 2, 3].map((i) => point(i, { house: [500, 900, 300, 700][i] }));
  const a = curve[0].t;
  assert.deepEqual(houseBuckets(curve, [a, a + 240], 2), [900, 700]);
  assert.deepEqual(houseBuckets(curve, [a, a + 480], 4), [900, 700, null, null]);
  assert.equal(bucketCount(curve, [a, a + 240]), 4);
  assert.equal(bucketCount(curve, [a, a + 240], 3), 3);
  assert.equal(bucketCount([], [a, a + 240]), 1);
});

test("axis ticks: round steps from the start of the day, thinned to fit", () => {
  const day: [number, number] = [T0, T0 + 86400];
  assert.equal(axisTicks(day, T0, 9).length, 9); // every 3 h
  assert.deepEqual(
    axisTicks(day, T0, 5).map((t) => (t - T0) / 3600),
    [0, 6, 12, 18, 24],
  );
  // a zoom of 70 min from 18:14: every 10 min, on round times
  const zoom: [number, number] = [T0 + 18 * 3600 + 14 * 60, T0 + 19 * 3600 + 24 * 60];
  assert.deepEqual(
    axisTicks(zoom, T0, 9).map((t) => (t - T0) / 60 - 18 * 60),
    [20, 30, 40, 50, 60, 70, 80],
  );
  assert.deepEqual(axisTicks([T0, T0], T0, 9), []);
});

test("notable peaks in time order; the zoom window around a peak, inside the day", () => {
  const r = report();
  assert.deepEqual(
    notablePeaks(r).map((p) => p.max_ts - T0),
    [350, 20100],
  );
  const early: Peak = r.peaks[2];
  assert.deepEqual(peakZoom(early, r), [T0, T0 + 400 + 1800]);
  assert.deepEqual(peakZoom(r.peaks[0], r, 600), [T0 + 19400, T0 + 20900]);
});

test("whole day: no window, or a window covering the day", () => {
  const r = report();
  assert.equal(isWholeDay(null, r), true);
  assert.equal(isWholeDay([r.start, r.end], r), true);
  assert.equal(isWholeDay([r.start + 60, r.end], r), false);
});
