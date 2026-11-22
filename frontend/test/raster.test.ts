import assert from "node:assert/strict";
import { test } from "node:test";

import { firstBlock, isWeekend, moveBlock, rasterLastDay, rasterRows, rasterTicks } from "../src/raster.ts";
import type { Occurrence } from "../src/types.ts";

const H = 3600;
/** Midnight UTC of a March 2027 day. */
const utc = (day: number) => Date.UTC(2027, 2, day) / 1000;
/** Midnight in Paris (UTC+1 in winter, +2 after the last Sunday of March). */
const paris = (month: number, day: number, offsetH: number) => Date.UTC(2027, month - 1, day) / 1000 - offsetH * H;

test("weekends", () => {
  assert.equal(isWeekend("2027-03-13"), true); // Saturday
  assert.equal(isWeekend("2027-03-14"), true);
  assert.equal(isWeekend("2027-03-15"), false);
});

test("one row per day, most recent first, empty days included", () => {
  const occ: Occurrence[] = [
    [utc(9) + 7 * H, 1800, 2000],
    [utc(6) + 12 * H, 3600, 1000],
  ];
  const rows = rasterRows(occ, "UTC", { lastDay: "2027-03-09", firstDay: "2027-03-05" });
  assert.deepEqual(
    rows.map((r) => r.day),
    ["2027-03-09", "2027-03-08", "2027-03-07", "2027-03-06", "2027-03-05"],
  );
  assert.equal(rows[1].blocks.length, 0);
  assert.equal(rows[0].blocks.length, 1);
  const b = rows[0].blocks[0];
  assert.equal(b.x, 7 / 24);
  assert.equal(b.w, 0.5 / 24);
  assert.equal(b.shade, 1); // the highest power
  assert.equal(rows[3].blocks[0].shade, 0); // the lowest
  assert.equal(rows[3].weekend, true); // 2027-03-06 is a Saturday
  assert.deepEqual([b.cut_before, b.cut_after], [false, false]);
});

test("the window and first_seen bound the rows; a later occurrence extends them", () => {
  const rows = rasterRows([], "UTC", { lastDay: "2027-03-09", firstDay: "2026-01-01", maxDays: 60 });
  assert.equal(rows.length, 60);
  assert.equal(rows[59].day, "2027-01-09");
  const later = rasterRows([[utc(10) + H, 60, 500]], "UTC", { lastDay: "2027-03-09", firstDay: "2027-03-08" });
  assert.deepEqual(
    later.map((r) => r.day),
    ["2027-03-10", "2027-03-09", "2027-03-08"],
  );
  // an occurrence before first_seen still gets its row; no first_seen: from the last day
  assert.equal(rasterRows([[utc(7), 60, 500]], "UTC", { lastDay: "2027-03-09", firstDay: "2027-03-08" }).length, 3);
  assert.equal(rasterRows([], "UTC", { lastDay: "2027-03-09" }).length, 1);
  assert.equal(rasterRows([], "UTC", { lastDay: "2027-03-09", firstDay: "2027-03-12" }).length, 1);
});

test("an occurrence crossing midnight is split in two blocks", () => {
  const rows = rasterRows([[utc(8) + 23 * H, 2 * H, 1500]], "UTC", { lastDay: "2027-03-09", firstDay: "2027-03-08" });
  const [today, yesterday] = rows;
  assert.equal(yesterday.blocks.length, 1);
  assert.equal(today.blocks.length, 1);
  assert.equal(yesterday.blocks[0].x, 23 / 24);
  assert.equal(yesterday.blocks[0].w, 1 / 24);
  assert.equal(yesterday.blocks[0].cut_after, true);
  assert.equal(today.blocks[0].x, 0);
  assert.equal(today.blocks[0].w, 1 / 24);
  assert.equal(today.blocks[0].cut_before, true);
  assert.equal(today.blocks[0].start, utc(9));
  assert.equal(today.blocks[0].from, utc(8) + 23 * H);
});

test("local days of the time zone, a 23-hour day scaled to the row", () => {
  // 00:30 local in Paris on March 10 (UTC+1) is March 9 23:30 UTC
  const t = paris(3, 10, 1) + 0.5 * H;
  const rows = rasterRows([[t, 600, 800]], "Europe/Paris", { lastDay: "2027-03-10", firstDay: "2027-03-09" });
  assert.equal(rows[0].day, "2027-03-10");
  assert.equal(rows[0].blocks.length, 1);
  assert.ok(Math.abs(rows[0].blocks[0].x - 0.5 / 24) < 1e-9);
  // 2027-03-28: 23 hours; noon UTC is 14:00 local, 13 hours after midnight
  const dst = paris(3, 28, 1) + 13 * H;
  const r2 = rasterRows([[dst, 0, 800]], "Europe/Paris", { lastDay: "2027-03-28" });
  assert.equal(r2[0].blocks.length, 1); // a zero-length occurrence is kept
  assert.ok(Math.abs(r2[0].blocks[0].x - 13 / 23) < 1e-9);
});

test("malformed occurrences are ignored", () => {
  const bad = [[1, 2], [Number.NaN, 1, 1], [utc(9), -5, 100], null] as unknown as Occurrence[];
  const rows = rasterRows(bad, "UTC", { lastDay: "2027-03-09" });
  assert.equal(rows[0].blocks.length, 0);
  assert.deepEqual(rasterRows(undefined, "UTC", { lastDay: "2027-03-09" })[0].blocks, []);
});

test("last day, ticks", () => {
  assert.equal(rasterLastDay(Date.UTC(2027, 2, 10, 6), "UTC"), "2027-03-09");
  assert.deepEqual(rasterTicks(false), [0, 3, 6, 9, 12, 15, 18, 21, 24]);
  assert.deepEqual(rasterTicks(true), [0, 6, 12, 18, 24]);
});

test("keyboard moves between blocks", () => {
  const occ: Occurrence[] = [
    [utc(9) + 7 * H, 600, 2000],
    [utc(9) + 19 * H, 600, 2000],
    [utc(7) + 18 * H, 600, 2000],
  ];
  const rows = rasterRows(occ, "UTC", { lastDay: "2027-03-09", firstDay: "2027-03-07" });
  assert.deepEqual(firstBlock(rows), [0, 0]);
  assert.deepEqual(moveBlock(rows, [0, 0], "ArrowRight"), [0, 1]);
  assert.equal(moveBlock(rows, [0, 1], "ArrowRight"), null);
  assert.deepEqual(moveBlock(rows, [0, 0], "ArrowLeft"), [2, 0]); // the day before, empty one skipped
  assert.deepEqual(moveBlock(rows, [2, 0], "ArrowUp"), [0, 1]); // nearest in time of day: 19 h
  assert.deepEqual(moveBlock(rows, [0, 0], "ArrowDown"), [2, 0]);
  assert.equal(moveBlock(rows, [2, 0], "ArrowDown"), null);
  assert.deepEqual(moveBlock(rows, [2, 0], "Home"), [0, 1]);
  assert.deepEqual(moveBlock(rows, [0, 1], "End"), [2, 0]);
  assert.equal(moveBlock(rows, [0, 0], "Enter"), null);
  assert.equal(firstBlock(rasterRows([], "UTC", { lastDay: "2027-03-09" })), null);
});

test("row labels", async () => {
  const { rasterDayLabel } = await import("../src/raster.ts");
  assert.match(rasterDayLabel("2027-03-09", "en"), /Tue/);
  assert.match(rasterDayLabel("2027-03-09", "fr"), /mar/);
  assert.match(rasterDayLabel("2027-03-09", "fr"), /9/);
});
