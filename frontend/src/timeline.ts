// Pure logic of the timeline of the Today tab (#49): one row per appliance of
// the day with its running periods on a shared time axis, the house power as
// a strip above. No DOM: the panel draws it with plain markup.

import { type Namer, peakWindow, seriesPeriods, stackSeries, usageRows, type UsageRow } from "./data.ts";
import type { CurvePoint, Peak, Registry, Report } from "./types.ts";

/** One running period of an appliance (s). */
export interface RunPeriod {
  start: number;
  end: number;
  /** a cycling session (one block for its cycles) */
  cycling: boolean;
  /** power of the activation (W), when known */
  power_w: number | null;
}

/** One row of the timeline: a row of "What used energy" with its periods. */
export interface TimelineRow extends UsageRow {
  periods: RunPeriod[];
}

/** The rows of the timeline: the rows of "What used energy" (same order,
 * same ids) without the base load and the unexplained rest, which run all
 * day. Signatures run in their activations (the curve when the report has
 * none for them); switched loads and sub-meters where they have some power
 * in the per-minute curve. */
export function timelineRows(report: Report, registry: Registry | null, namer?: Namer): TimelineRow[] {
  const stack = new Map(stackSeries(report, registry, namer).map((s) => [s.id, s]));
  const curve = report.curve ?? [];
  return usageRows(report, registry, namer)
    .filter((r) => r.kind !== "base" && r.kind !== "other")
    .map((r) => {
      const series = stack.get(r.id);
      let periods: RunPeriod[] = [];
      if (series?.kind === "signature") {
        const keys = new Set(series.keys);
        periods = (report.activations ?? [])
          .filter((a) => keys.has(a.signature))
          .map((a) => ({ start: a.start, end: a.end, cycling: Boolean(a.cycling), power_w: a.power_w ?? null }));
      }
      if (!periods.length && series) {
        periods = seriesPeriods(curve, series).map(([start, end]) => ({ start, end, cycling: false, power_w: null }));
      }
      periods.sort((a, b) => a.start - b.start);
      return { ...r, periods };
    });
}

/** A block of a row inside the visible window, as fractions of its width. */
export interface Block {
  start: number;
  end: number;
  cycling: boolean;
  power_w: number | null;
  /** left edge and width (0..1 of the window) */
  left: number;
  width: number;
}

/** The blocks of a row in the window [a, b] (s): periods clipped to it, the
 * ones closer than ``gap_s`` merged (they would touch on screen), each at
 * least ``min_width`` wide so a 1-minute run stays visible. */
export function visibleBlocks(
  periods: RunPeriod[],
  window: [number, number],
  gap_s = 0,
  min_width = 0.004,
): Block[] {
  const [a, b] = window;
  const span = b - a;
  if (!(span > 0)) return [];
  const merged: RunPeriod[] = [];
  for (const p of [...periods].sort((x, y) => x.start - y.start)) {
    if (p.end <= a || p.start >= b) continue;
    const last = merged[merged.length - 1];
    if (last && p.start - last.end <= gap_s) {
      last.end = Math.max(last.end, p.end);
      last.cycling = last.cycling || p.cycling;
      last.power_w = Math.max(last.power_w ?? 0, p.power_w ?? 0) || null;
    } else merged.push({ ...p });
  }
  return merged.map((p) => {
    const start = Math.max(p.start, a);
    const end = Math.min(Math.max(p.end, p.start), b);
    const width = Math.max((end - start) / span, min_width);
    const left = Math.min((start - a) / span, 1 - width);
    return { start: p.start, end: p.end, cycling: p.cycling, power_w: p.power_w, left: Math.max(left, 0), width };
  });
}

/** The house power strip: the highest house power (W) of each of ``n``
 * equal slices of the window (null: no point in it). */
export function houseBuckets(curve: CurvePoint[], window: [number, number], n: number): (number | null)[] {
  const [a, b] = window;
  const out: (number | null)[] = Array.from({ length: Math.max(n, 0) }, () => null);
  const span = b - a;
  if (!(span > 0) || n <= 0) return out;
  for (const p of curve) {
    if (p.t < a || p.t >= b) continue;
    const i = Math.min(Math.floor(((p.t - a) / span) * n), n - 1);
    const w = Math.max(p.house ?? 0, 0);
    if (out[i] === null || w > out[i]!) out[i] = w;
  }
  return out;
}

/** Number of slices of the strip: one per curve point in the window, at most ``max``. */
export function bucketCount(curve: CurvePoint[], window: [number, number], max = 144): number {
  let n = 0;
  for (const p of curve) if (p.t >= window[0] && p.t < window[1]) n++;
  return Math.max(1, Math.min(n, max));
}

const TICK_STEPS_MIN = [5, 10, 15, 30, 60, 120, 180, 360, 720];

/** Times (s) of the axis labels of the window: a round step from the start
 * of the day (5 min .. 12 h) giving at most ``max_labels`` labels. */
export function axisTicks(window: [number, number], dayStart: number, max_labels: number): number[] {
  const [a, b] = window;
  if (!(b > a) || max_labels < 2) return [];
  const step = (TICK_STEPS_MIN.find((m) => (b - a) / (m * 60) + 1 <= max_labels) ?? TICK_STEPS_MIN.at(-1)!) * 60;
  const out: number[] = [];
  for (let t = dayStart + Math.ceil((a - dayStart) / step) * step; t <= b + 0.5; t += step) out.push(t);
  return out;
}

/** The notable peaks of the day, in time order (the chips under the strip). */
export function notablePeaks(report: Report): Peak[] {
  return (report.peaks ?? []).filter((p) => p.notable).sort((a, b) => a.start - b.start);
}

/** The window (s) to show for a peak: the peak with ``margin_s`` on each
 * side, inside the day (the chart's peak zoom, in seconds). */
export function peakZoom(peak: Peak, report: Pick<Report, "start" | "end">, margin_s = 1800): [number, number] {
  const [a, b] = peakWindow(peak, report.start, report.end, margin_s);
  return [a / 1000, b / 1000];
}

/** Whether ``window`` shows the whole day (no zoom). */
export function isWholeDay(window: [number, number] | null, report: Pick<Report, "start" | "end">): boolean {
  return !window || (window[0] <= report.start && window[1] >= report.end);
}
