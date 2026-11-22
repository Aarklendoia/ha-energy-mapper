import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applianceCards,
  type ApplianceInfo,
  applianceSeries,
  dailyEnergy,
  dayUsage,
  daysEnding,
  isEstimated,
  lastUse,
  statisticIds,
  statsRequest,
  trackingOf,
} from "../src/appliances.ts";
import { usageRows } from "../src/data.ts";
import type { Registry, Report } from "../src/types.ts";

// synthetic household (no real data)
function report(day = "2027-03-09"): Report {
  return {
    day,
    start: 1000,
    end: 1000 + 86400,
    energy_kwh: {
      house: 10,
      grid_import: 10,
      grid_export: 0,
      pv: 0,
      submetered: 0,
      base: 4,
      other: 0.5,
      known: { "Water heater": 2.4 },
      signatures: { "short-2000w": 0.6, "short-2050w": 0.2, "cyc-1500w": 0.3 },
    },
    max_house_w: 4200,
    peak_threshold_w: 3500,
    curve: [],
    peaks: [],
    signatures: [
      { id: "short-2000w", kind: "steady", band: "short", power_w: 2000, count: 2, duration_s: 120, energy_kwh: 0.6, registry_id: "short-2000w" },
      { id: "short-2050w", kind: "steady", band: "short", power_w: 2050, count: 1, duration_s: 120, energy_kwh: 0.2, registry_id: "short-2000w" },
      { id: "cyc-1500w", kind: "cycling", band: "session", power_w: 1500, count: 1, duration_s: 900, energy_kwh: 0.3, registry_id: "cyc-1500w" },
    ],
    activations: [
      { start: 2000, end: 2120, power_w: 2000, cycling: false, signature: "short-2000w" },
      { start: 5000, end: 5120, power_w: 2000, cycling: false, signature: "short-2000w" },
      { start: 9000, end: 9120, power_w: 2050, cycling: false, signature: "short-2050w" },
      { start: 7000, end: 7900, power_w: 1500, cycling: true, signature: "cyc-1500w" },
    ],
    known_loads: [
      { name: "Water heater", entity_id: "switch.water_heater", power_w: 2400, transitions: 2, matched: 2, inverted: false, on_s: 3600 },
    ],
  };
}

const sig = (id: string, last_seen: string) => ({
  id, kind: "steady", band: "short", label: null, power_w: 2000, duration_s: 120, on_s: null, period_s: null,
  duty: null, weight: 1, count: 3, days_seen: 3, per_week: 3, first_seen: "2027-03-01", last_seen, hours: [], energy_kwh: 1,
});

const registry: Registry = {
  signatures: [sig("short-2000w", "2027-03-09"), sig("cyc-1500w", "2027-03-07"), sig("long-900w", "2027-03-02")],
  switched_loads: [
    { entity_id: "switch.water_heater", name: "Water heater", power_w: 2400, inverted: false, days_seen: 9, last_seen: "2027-03-08" },
  ],
  peak: null,
};

const appliance = (key: string, name: string, sources: ApplianceInfo["sources"], statistic_id: string | null): ApplianceInfo => ({
  key, name, subentry_id: `sub-${key}`, statistic_id, power_w: null, sources,
});

const HEATER = appliance("load:switch.water_heater", "Water heater", [{ type: "switched_load", entity_id: "switch.water_heater" }], "s:heater");
const KETTLE = appliance("signature:short-2000w", "Kettle", [{ type: "signature", id: "short-2000w" }], "s:kettle");
const HOB = appliance(
  "signature:cyc-1500w",
  "Hob",
  [{ type: "signature", id: "cyc-1500w" }, { type: "switched_load", entity_id: "switch.hob" }],
  "s:hob",
);
const IRON = appliance("signature:long-900w", "Iron", [{ type: "signature", id: "long-900w" }], "s:iron");
const DRYER = appliance("submeter:sensor.dryer", "Dryer", [{ type: "submeter", entity_id: "sensor.dryer" }], null);
const ALL = [HEATER, KETTLE, HOB, IRON, DRYER];

test("how an appliance is tracked", () => {
  assert.deepEqual(trackingOf(HEATER), ["switch"]);
  assert.deepEqual(trackingOf(KETTLE), ["curve"]);
  // merged sources: one each, in a stable order
  assert.deepEqual(trackingOf(HOB), ["curve", "switch"]);
  assert.deepEqual(trackingOf(DRYER), ["sensor"]);
  assert.deepEqual(trackingOf({ sources: [{ type: "future" }] }), []);
  assert.equal(isEstimated(KETTLE), true);
  assert.equal(isEstimated(DRYER), false);
});

test("series of an appliance in the day's report", () => {
  const r = report();
  assert.deepEqual(applianceSeries(HEATER, r), ["known:Water heater"]);
  assert.deepEqual(applianceSeries(KETTLE, r), ["sig:short-2000w"]);
  // its switch is not in the report: only its signature
  assert.deepEqual(applianceSeries(HOB, r), ["sig:cyc-1500w"]);
  assert.deepEqual(applianceSeries(DRYER, r), []);
  assert.deepEqual(applianceSeries(KETTLE, null), []);
});

test("day usage from the rows of What used energy", () => {
  const r = report();
  const rows = usageRows(r, registry);
  const kettle = dayUsage(KETTLE, r, rows)!;
  // both daily clusters of the registry signature, all their runs
  assert.ok(Math.abs(kettle.energy_kwh - 0.8) < 1e-9);
  assert.equal(kettle.runs, 3);
  assert.equal(kettle.series, "sig:short-2000w");
  assert.equal(dayUsage(HOB, r, rows)!.runs, 1);
  assert.equal(dayUsage(IRON, r, rows), null);
  assert.equal(dayUsage(DRYER, r, rows), null);
});

test("last use from the registry", () => {
  assert.equal(lastUse(KETTLE, registry), "2027-03-09");
  assert.equal(lastUse(HEATER, registry), "2027-03-08");
  assert.equal(lastUse(HOB, registry), "2027-03-07");
  assert.equal(lastUse(DRYER, registry), null);
  assert.equal(lastUse(KETTLE, null), null);
});

test("statistics request and daily energy", () => {
  assert.deepEqual(daysEnding("2027-03-02", 3), ["2027-02-28", "2027-03-01", "2027-03-02"]);
  const req = statsRequest(["s:a"], "2027-03-09", "Europe/Paris", 7);
  assert.equal(req.type, "recorder/statistics_during_period");
  assert.equal(req.start_time, "2027-03-02T23:00:00.000Z");
  assert.equal(req.end_time, "2027-03-09T23:00:00.000Z");
  assert.equal(req.period, "day");
  assert.deepEqual(req.types, ["change"]);
  assert.deepEqual(statisticIds(ALL), ["s:heater", "s:hob", "s:iron", "s:kettle"]);
  const energy = dailyEnergy(
    {
      "s:a": [
        { start: Date.parse("2027-03-07T23:00:00Z"), change: 1.5 },
        // an ISO string (older Home Assistant), a negative change (a reset)
        { start: "2027-03-08T23:00:00+00:00", change: -0.2 },
        { start: Date.parse("2027-03-06T23:00:00Z"), change: null },
      ],
    },
    "Europe/Paris",
  );
  assert.deepEqual(energy, { "s:a": { "2027-03-08": 1.5, "2027-03-09": 0 } });
  assert.deepEqual(dailyEnergy(null, "UTC"), {});
});

const STATS = {
  "s:heater": { "2027-03-03": 2, "2027-03-08": 3, "2027-03-09": 2.5 },
  "s:kettle": { "2027-03-09": 0.7, "2027-02-05": 5 },
  "s:hob": {},
};

test("cards of the day: report first, statistics otherwise, by energy", () => {
  const r = report();
  const cards = applianceCards(ALL, { day: r.day, period: "day", report: r, rows: usageRows(r, registry), registry, stats: STATS });
  assert.deepEqual(
    cards.map((c) => [c.name, c.energy_kwh === null ? null : Math.round(c.energy_kwh * 100) / 100, c.runs]),
    [
      ["Water heater", 2.4, null], // runs from the curve: none here
      ["Kettle", 0.8, 3],
      ["Hob", 0.3, 1],
      // analysed, not in the report: not used
      ["Iron", 0, null],
      // a power sensor: not in the report
      ["Dryer", null, null],
    ],
  );
  const kettle = cards[1];
  assert.deepEqual(kettle.tracking, ["curve"]);
  assert.equal(kettle.series, "sig:short-2000w");
  assert.equal(kettle.last_use, "2027-03-09");
  assert.deepEqual(kettle.bars.map((b) => b.day), daysEnding("2027-03-09", 7));
  assert.equal(kettle.bars[6].ratio, 1);
  assert.equal(cards[0].bars[0].energy_kwh, 2); // 2027-03-03
  assert.deepEqual(cards[4].bars, []);
  assert.equal(cards[4].series, null);
});

test("cards without report or statistics", () => {
  // no report of that day: the statistics give the day
  const cards = applianceCards(ALL, { day: "2027-03-08", period: "day", report: report(), rows: [], registry, stats: STATS });
  const heater = cards.find((c) => c.name === "Water heater")!;
  assert.equal(heater.energy_kwh, 3);
  assert.equal(heater.series, null);
  assert.equal(cards.find((c) => c.name === "Kettle")!.energy_kwh, null);
  // no statistics either: unknown, no bars
  const bare = applianceCards(ALL, { day: "2027-03-08", period: "day", report: null, rows: [], registry, stats: null });
  assert.ok(bare.every((c) => c.energy_kwh === null && c.bars.length === 0));
  assert.deepEqual(
    bare.map((c) => c.name),
    ["Dryer", "Hob", "Iron", "Kettle", "Water heater"],
  );
});

test("cards of 7 and 30 days", () => {
  const r = report();
  const input = { day: r.day, report: r, rows: usageRows(r, registry), registry, stats: STATS };
  const week = applianceCards(ALL, { ...input, period: "week" });
  const heater = week.find((c) => c.name === "Water heater")!;
  assert.equal(heater.energy_kwh, 7.5);
  assert.equal(heater.used_days, 3);
  assert.equal(heater.runs, null);
  assert.equal(heater.bars.length, 7);
  assert.equal(week.find((c) => c.name === "Hob")!.energy_kwh, 0);
  assert.equal(week.find((c) => c.name === "Dryer")!.energy_kwh, null);
  const month = applianceCards(ALL, { ...input, period: "month" });
  const kettle = month.find((c) => c.name === "Kettle")!;
  // 2027-02-05 is outside the 30 days ending on 2027-03-09
  assert.equal(kettle.energy_kwh, 0.7);
  assert.equal(kettle.bars.length, 30);
  assert.equal(kettle.bars[0].day, "2027-02-08");
});
