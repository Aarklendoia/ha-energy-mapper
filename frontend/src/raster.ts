// The day raster of a signature in the To review tab (#65): one row per day,
// 0–24 h, each occurrence drawn as a block (width: its duration, shade: its
// power). Pure helpers, unit tested.

import { addDays, dayOf, startOfDay } from "./format.ts";
import type { Occurrence } from "./types.ts";

/** One block of a row: an occurrence, or the part of it inside the day when
 * it crosses midnight. */
export interface RasterBlock {
  /** the part inside the day (unix seconds) */
  start: number;
  end: number;
  power_w: number;
  /** the whole occurrence (unix seconds) */
  from: number;
  to: number;
  /** position and width in the row, as fractions of the day (0..1) */
  x: number;
  w: number;
  /** 0..1: the power between the lowest and the highest of the signature */
  shade: number;
  /** the occurrence goes on before / after this day */
  cut_before: boolean;
  cut_after: boolean;
}

export interface RasterRow {
  day: string;
  weekend: boolean;
  blocks: RasterBlock[];
}

export interface RasterOptions {
  /** the most recent day (on top); a later occurrence extends it */
  lastDay: string;
  /** the first day shown (the signature's first_seen): rows stop there */
  firstDay?: string | null;
  /** at most this many rows (the add-on keeps 60 days) */
  maxDays?: number;
}

/** Whether a "YYYY-MM-DD" day is a Saturday or a Sunday. */
export function isWeekend(day: string): boolean {
  const [y, m, d] = day.split("-").map(Number);
  const wd = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  return wd === 0 || wd === 6;
}

/** Only the well-formed occurrences (add-on arrays of three numbers). */
function valid(occurrences: readonly Occurrence[] | undefined): Occurrence[] {
  return (occurrences ?? []).filter(
    (o) => Array.isArray(o) && o.length >= 3 && o.every((v) => Number.isFinite(v)) && o[1] >= 0,
  );
}

/** The rows of the raster, the most recent day first: every day from
 * ``lastDay`` back to ``firstDay`` (at most ``maxDays``), empty ones
 * included. An occurrence crossing midnight is split into one block per day
 * (the days of the local ``timeZone``; a 23- or 25-hour day is scaled to the
 * row). Blocks of a row are sorted by start. */
export function rasterRows(
  occurrences: readonly Occurrence[] | undefined,
  timeZone: string,
  options: RasterOptions,
): RasterRow[] {
  const occ = valid(occurrences);
  const maxDays = Math.max(options.maxDays ?? 60, 1);
  let last = options.lastDay;
  let oldest = options.firstDay || last;
  for (const [start, duration] of occ) {
    const d = dayOf((start + duration) * 1000, timeZone);
    if (d > last) last = d;
    const s = dayOf(start * 1000, timeZone);
    if (s < oldest) oldest = s;
  }
  const earliest = addDays(last, -(maxDays - 1));
  const first = oldest > last ? last : oldest > earliest ? oldest : earliest;
  const rows: RasterRow[] = [];
  const index = new Map<string, RasterRow>();
  for (let day = last; day >= first; day = addDays(day, -1)) {
    const row: RasterRow = { day, weekend: isWeekend(day), blocks: [] };
    rows.push(row);
    index.set(day, row);
  }
  const powers = occ.map((o) => o[2]);
  const lo = Math.min(...powers);
  const hi = Math.max(...powers);
  const bounds = new Map<string, [number, number]>();
  const boundsOf = (day: string): [number, number] => {
    let b = bounds.get(day);
    if (!b) {
      b = [startOfDay(day, timeZone) / 1000, startOfDay(addDays(day, 1), timeZone) / 1000];
      bounds.set(day, b);
    }
    return b;
  };
  for (const [from, duration, power] of occ) {
    const to = from + duration;
    const shade = hi > lo ? (power - lo) / (hi - lo) : 1;
    let day = dayOf(from * 1000, timeZone);
    // one block per day the occurrence touches (a few at most)
    for (let k = 0; k < 7; k++) {
      const [a, b] = boundsOf(day);
      const start = Math.max(from, a);
      const end = Math.min(to, b);
      const row = index.get(day);
      if (row && (end > start || (duration === 0 && k === 0))) {
        row.blocks.push({
          start,
          end,
          power_w: power,
          from,
          to,
          x: (start - a) / (b - a),
          w: (end - start) / (b - a),
          shade,
          cut_before: from < a,
          cut_after: to > b,
        });
      }
      if (to <= b) break;
      day = addDays(day, 1);
    }
  }
  for (const row of rows) row.blocks.sort((p, q) => p.start - q.start);
  return rows;
}

/** The most recent day of the raster when nothing later is known: the day
 * before ``now`` (the last day the add-on analysed). */
export function rasterLastDay(nowMs: number, timeZone: string): string {
  return addDays(dayOf(nowMs, timeZone), -1);
}

/** Hour ticks of the axis: every 3 h, or every 6 h on a phone. */
export function rasterTicks(narrow: boolean): number[] {
  const step = narrow ? 6 : 3;
  return Array.from({ length: 24 / step + 1 }, (_, i) => i * step);
}

/** Position of a block in the keyboard navigation: [row, block]. */
export type BlockPos = [number, number];

/** The block a key moves to from ``pos`` (null: stay). Left / Right: the
 * previous / next block in time (across rows); Up / Down: the nearest block
 * of the closest non-empty row above (more recent) / below; Home / End: the
 * most recent / oldest block. */
export function moveBlock(rows: readonly RasterRow[], pos: BlockPos, key: string): BlockPos | null {
  const flat: BlockPos[] = [];
  // in time order: the oldest row last, so walk the rows backwards
  for (let r = rows.length - 1; r >= 0; r--) rows[r].blocks.forEach((_, b) => flat.push([r, b]));
  if (!flat.length) return null;
  const at = flat.findIndex(([r, b]) => r === pos[0] && b === pos[1]);
  switch (key) {
    case "ArrowLeft":
      return at > 0 ? flat[at - 1] : null;
    case "ArrowRight":
      return at >= 0 && at < flat.length - 1 ? flat[at + 1] : null;
    case "Home":
      return flat[flat.length - 1];
    case "End":
      return flat[0];
    case "ArrowUp":
    case "ArrowDown": {
      const step = key === "ArrowUp" ? -1 : 1;
      const cur = rows[pos[0]]?.blocks[pos[1]];
      if (!cur) return null;
      const mid = cur.x + cur.w / 2;
      for (let r = pos[0] + step; r >= 0 && r < rows.length; r += step) {
        const blocks = rows[r].blocks;
        if (!blocks.length) continue;
        let best = 0;
        blocks.forEach((b, i) => {
          const d = Math.abs(b.x + b.w / 2 - mid);
          const bd = Math.abs(blocks[best].x + blocks[best].w / 2 - mid);
          if (d < bd) best = i;
        });
        return [r, best];
      }
      return null;
    }
    default:
      return null;
  }
}

/** The first block in reading order (top row first): the one that takes the
 * focus when tabbing into the raster. */
export function firstBlock(rows: readonly RasterRow[]): BlockPos | null {
  for (let r = 0; r < rows.length; r++) if (rows[r].blocks.length) return [r, 0];
  return null;
}

/** "Mon 9" / "lun. 9": the label of a row. */
export function rasterDayLabel(day: string, language: string | undefined): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Intl.DateTimeFormat(language || "en", { weekday: "short", day: "numeric", timeZone: "UTC" }).format(
    new Date(Date.UTC(y, m - 1, d, 12)),
  );
}
