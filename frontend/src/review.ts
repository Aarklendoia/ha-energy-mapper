// The "To review" section of the panel (#10): pure helpers, unit tested.

import type {
  EntitySuggestion,
  ReviewListing,
  ReviewSignature,
  SignatureExample,
} from "./types.ts";

export interface ReviewGroups {
  proposed: ReviewSignature[];
  waiting: ReviewSignature[];
  ignored: ReviewSignature[];
}

/** The listing split by state; proposals with the most energy first (the
 * add-on's order is kept within a state). */
export function reviewGroups(listing: ReviewListing | null | undefined): ReviewGroups {
  const out: ReviewGroups = { proposed: [], waiting: [], ignored: [] };
  for (const s of listing?.signatures ?? []) {
    if (s.review_state === "proposed" || s.review_state === "waiting" || s.review_state === "ignored") {
      out[s.review_state].push(s);
    }
  }
  return out;
}

/** Whether the section has anything to show. */
export function hasReview(groups: ReviewGroups): boolean {
  return groups.proposed.length + groups.waiting.length + groups.ignored.length > 0;
}

/** Usual hours of a signature: the ranges of consecutive hours whose weight
 * is at least ``share`` of the busiest hour, the busiest ranges first (at
 * most ``max``). A range crossing midnight is one range ([22, 1]). */
export function usualHours(hours: number[] | undefined, share = 0.5, max = 3): Array<[number, number]> {
  const h = Array.from({ length: 24 }, (_, i) => Math.max(hours?.[i] ?? 0, 0));
  const peak = Math.max(...h);
  if (!(peak > 0)) return [];
  const on = h.map((v) => v >= share * peak);
  if (on.every(Boolean)) return [[0, 23]];
  // start the scan after an hour that is off, so that a range crossing midnight is not split
  const first = on.indexOf(false);
  const ranges: Array<{ from: number; to: number; weight: number }> = [];
  let cur: { from: number; to: number; weight: number } | null = null;
  for (let k = 1; k <= 24; k++) {
    const i = (first + k) % 24;
    if (on[i]) {
      if (cur) {
        cur.to = i;
        cur.weight += h[i];
      } else cur = { from: i, to: i, weight: h[i] };
    } else if (cur) {
      ranges.push(cur);
      cur = null;
    }
  }
  if (cur) ranges.push(cur);
  return ranges
    .sort((a, b) => b.weight - a.weight)
    .slice(0, max)
    .map((r) => [r.from, r.to]);
}

/** "7 h", "7–9 h", "22–1 h" (the hour after the last one is the end). */
export function formatHours(ranges: Array<[number, number]>): string {
  return ranges.map(([a, b]) => (a === b ? `${a} h` : `${a}–${(b + 1) % 24} h`)).join(", ");
}

/** The time window shown on the chart for an example (ms, with a margin,
 * inside the day). */
export function exampleWindow(example: SignatureExample, start: number, end: number, margin_s = 1200): [number, number] {
  const a = Math.max(start, example.start - margin_s);
  const b = Math.min(end, Math.max(example.end, example.start + 60) + margin_s);
  return [a * 1000, b * 1000];
}

/** What validating does: a new appliance (from the signature, or driven
 * by a switch, or measured by a power sensor), or a part of an existing
 * appliance (merge, #43). */
export type LinkMode = "new" | "switch" | "sensor" | "merge";

export const SWITCH_DOMAINS = [
  "switch",
  "input_boolean",
  "light",
  "fan",
  "binary_sensor",
  "climate",
  "humidifier",
  "water_heater",
  "media_player",
];
/** A sensor of this device class (an operation state) is linked like a switch. */
export const STATE_SENSOR_CLASS = "enum";

/** Entity domains offered for a link mode (sensors: power sensors for
 * "sensor", operation states for "switch"). */
export function linkDomains(mode: LinkMode): string[] {
  return mode === "sensor" ? ["sensor"] : mode === "switch" ? [...SWITCH_DOMAINS, "sensor"] : [];
}

/** Whether an entity can be linked in a mode. */
export function linkable(mode: LinkMode, entityId: string, attributes?: Record<string, unknown>): boolean {
  const domain = entityId.split(".")[0];
  if (!linkDomains(mode).includes(domain)) return false;
  if (domain !== "sensor") return true;
  const dc = attributes?.device_class;
  return mode === "sensor" ? dc !== STATE_SENSOR_CLASS : dc === STATE_SENSOR_CLASS;
}

/** The best entity suggested for a signature, if any. */
export function bestSuggestion(sig: Pick<ReviewSignature, "suggestions">): EntitySuggestion | undefined {
  return sig.suggestions?.[0];
}

/** Whether a suggestion can drive the appliance as a switched load: its
 * state changes at the signature's steps. An attribute, or a state that only
 * contains the uses (a thermostat heating for an hour while its radiator
 * cycles), is a hint for the name or a merge only. */
export function drivesLoad(c: EntitySuggestion): boolean {
  return !c.attribute && (c.match ?? "edge") === "edge";
}

/** What picking a suggestion does: merge into its appliance, link it as a
 * switch, or a new appliance named after it. */
export function suggestionMode(c: EntitySuggestion): LinkMode {
  return c.appliance ? "merge" : drivesLoad(c) ? "switch" : "new";
}

/** "Living room (heating)": the entity, and for an attribute or a
 * containment the states in which the signature runs. */
export function suggestionLabel(c: EntitySuggestion, friendlyName?: (entityId: string) => string | undefined): string {
  const name = friendlyName?.(c.entity_id) ?? c.entity_id;
  return drivesLoad(c) || !c.on_states.length ? name : `${name} (${c.on_states.join(", ")})`;
}

export interface ReviewForm {
  id: string;
  mode: LinkMode;
  name: string;
  entity: string;
  /** merge: the key of the appliance */
  appliance: string;
  missing?: string;
}

/** The form after picking a suggestion. */
export function formForSuggestion(
  sigId: string,
  c: EntitySuggestion,
  friendlyName?: (entityId: string) => string | undefined,
): ReviewForm {
  const mode = suggestionMode(c);
  return {
    id: sigId,
    mode,
    name: friendlyName?.(c.entity_id) ?? "",
    entity: mode === "switch" ? c.entity_id : "",
    appliance: c.appliance?.key ?? "",
  };
}

/** The validation form a signature opens with, from its best suggestion
 * (``formForSuggestion``), else a new appliance. */
export function initialForm(
  sig: Pick<ReviewSignature, "id" | "suggestions">,
  friendlyName?: (entityId: string) => string | undefined,
): ReviewForm {
  const best = bestSuggestion(sig);
  if (!best) return { id: sig.id, mode: "new", name: "", entity: "", appliance: "" };
  return formForSuggestion(sig.id, best, friendlyName);
}

/** The websocket message of a validation, or the key of the missing field.
 * An entity the add-on suggested is linked with the states in which its
 * appliance runs (unless they are the default "on"). */
export function validateMessage(
  sig: Pick<ReviewSignature, "id" | "suggestions">,
  mode: LinkMode,
  name: string,
  entityId: string,
  attributes?: Record<string, unknown>,
  appliance = "",
): { msg: Record<string, unknown> } | { missing: "name" | "entity" | "appliance" } {
  if (mode === "merge") {
    if (!appliance) return { missing: "appliance" };
    return { msg: { type: "energy_mapper/merge_signature", signature_id: sig.id, appliance } };
  }
  const n = name.trim();
  if (!n) return { missing: "name" };
  const msg: Record<string, unknown> = { type: "energy_mapper/validate", signature_id: sig.id, name: n };
  if (mode !== "new") {
    const e = entityId.trim();
    if (!e || !linkable(mode, e, attributes)) return { missing: "entity" };
    msg.entity_id = e;
    const states = sig.suggestions?.find((s) => s.entity_id === e && drivesLoad(s))?.on_states ?? [];
    if (mode === "switch" && states.length && !(states.length === 1 && states[0] === "on")) {
      msg.on_states = [...states];
    }
  }
  return { msg };
}

/** Entities offered by the fallback picker (when Home Assistant's own entity
 * picker is not loaded): those of the mode's domains, power sensors only for
 * a sub-meter, operation-state sensors only for a switch, sorted by name. */
export function entityChoices(
  states: Record<string, { entity_id: string; attributes?: Record<string, unknown> }> | undefined,
  mode: LinkMode,
): Array<{ id: string; name: string }> {
  const out: Array<{ id: string; name: string }> = [];
  for (const [id, st] of Object.entries(states ?? {})) {
    if (!linkable(mode, id, st.attributes)) continue;
    if (mode === "sensor" && st.attributes?.device_class !== "power") continue;
    out.push({ id, name: String(st.attributes?.friendly_name ?? id) });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

// -- one signature at a time (#51) ----------------------------------------

/** A blank validation form: a new appliance, no name yet ("Use" on the
 * suggestion fills it). */
export function blankForm(id: string): ReviewForm {
  return { id, mode: "new", name: "", entity: "", appliance: "" };
}

/** The filters of the queue: the proposals with the most energy first (the
 * default), those with an entity suggestion, the waiting and the ignored
 * signatures. */
export type ReviewFilter = "energy" | "suggested" | "waiting" | "ignored";

export const REVIEW_FILTERS: readonly ReviewFilter[] = ["energy", "suggested", "waiting", "ignored"];

const energyOf = (s: Pick<ReviewSignature, "energy_kwh">): number =>
  Number.isFinite(s.energy_kwh) ? Math.max(s.energy_kwh, 0) : 0;

/** Most energy first (the energy shown), then the add-on's weighted energy,
 * then the id: a stable order. */
function byEnergy(a: ReviewSignature, b: ReviewSignature): number {
  return (
    energyOf(b) - energyOf(a) ||
    (b.weighted_energy_kwh ?? 0) - (a.weighted_energy_kwh ?? 0) ||
    (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
  );
}

/** The signatures a filter lists, in queue order. */
export function reviewQueue(listing: ReviewListing | null | undefined, filter: ReviewFilter): ReviewSignature[] {
  const groups = reviewGroups(listing);
  const list =
    filter === "waiting"
      ? groups.waiting
      : filter === "ignored"
        ? groups.ignored
        : filter === "suggested"
          ? groups.proposed.filter((s) => (s.suggestions?.length ?? 0) > 0)
          : groups.proposed;
  return [...list].sort(byEnergy);
}

/** The number of signatures of each filter (shown on its chip). */
export function filterCounts(listing: ReviewListing | null | undefined): Record<ReviewFilter, number> {
  const g = reviewGroups(listing);
  return {
    energy: g.proposed.length,
    suggested: g.proposed.filter((s) => (s.suggestions?.length ?? 0) > 0).length,
    waiting: g.waiting.length,
    ignored: g.ignored.length,
  };
}

/** The filter that lists a signature of the review (a proposal: the default
 * one), or null when it is not in the review (validated, immature). */
export function filterOf(sig: Pick<ReviewSignature, "review_state"> | undefined): ReviewFilter | null {
  switch (sig?.review_state) {
    case "proposed":
      return "energy";
    case "waiting":
      return "waiting";
    case "ignored":
      return "ignored";
    default:
      return null;
  }
}

/** The signature shown: the one wanted when the queue lists it, else the
 * first of the queue (null: empty queue). */
export function currentOf(queue: readonly Pick<ReviewSignature, "id">[], wanted: string | null): string | null {
  if (wanted && queue.some((s) => s.id === wanted)) return wanted;
  return queue[0]?.id ?? null;
}

/** The signature ``delta`` places away in the queue (no wrap: null past
 * either end). */
export function stepFrom(queue: readonly Pick<ReviewSignature, "id">[], current: string | null, delta: number): string | null {
  const i = queue.findIndex((s) => s.id === current);
  if (i < 0) return queue[0]?.id ?? null;
  return queue[i + delta]?.id ?? null;
}

/** The signature to show after some left the queue (validated, waiting,
 * ignored, restored): the next one still listed after the current, else the
 * one before it, else null. */
export function nextAfter(
  queue: readonly Pick<ReviewSignature, "id">[],
  current: string | null,
  removed: Iterable<string>,
): string | null {
  const gone = new Set(removed);
  const i = Math.max(
    queue.findIndex((s) => s.id === current),
    0,
  );
  if (current && !gone.has(current) && queue.some((s) => s.id === current)) return current;
  for (let k = i + 1; k < queue.length; k++) if (!gone.has(queue[k].id)) return queue[k].id;
  for (let k = i - 1; k >= 0; k--) if (!gone.has(queue[k].id)) return queue[k].id;
  return null;
}

export interface ReviewProgress {
  named: number;
  toReview: number;
  waiting: number;
  ignored: number;
  /** 0..1: the share of the signatures' energy already explained by a named
   * signature (``basis: "energy"``), else the share of the signatures
   * handled (named or ignored, ``basis: "count"``); null: nothing yet. */
  share: number | null;
  basis: "energy" | "count";
}

/** The progress of the review. The energy share needs the registry (the
 * named signatures are not in the review listing): named signatures' energy
 * over that plus the energy of the signatures still in the review. */
export function reviewProgress(
  listing: ReviewListing | null | undefined,
  registry?: { signatures: Array<Pick<ReviewSignature, "label" | "appliance" | "energy_kwh">> } | null,
): ReviewProgress {
  const g = reviewGroups(listing);
  const namedSigs = (registry?.signatures ?? []).filter((s) => s.label || s.appliance);
  const named = listing?.counts?.validated ?? namedSigs.length;
  const out: ReviewProgress = {
    named,
    toReview: g.proposed.length,
    waiting: g.waiting.length,
    ignored: g.ignored.length,
    share: null,
    basis: "count",
  };
  if (registry) {
    const explained = namedSigs.reduce((a, s) => a + energyOf(s), 0);
    const left = [...g.proposed, ...g.waiting, ...g.ignored].reduce((a, s) => a + energyOf(s), 0);
    if (explained + left > 0) {
      out.share = explained / (explained + left);
      out.basis = "energy";
      return out;
    }
  }
  const total = named + out.toReview + out.waiting + out.ignored;
  if (total > 0) out.share = (named + out.ignored) / total;
  return out;
}

/** What a keyboard shortcut of the review does. */
export type ReviewAction = "validate" | "wait" | "ignore" | "prev" | "next";

/** The letters shown for the shortcuts: V validate, I ignore, and for wait
 * W in English, A ("attendre") in French. Both W and A work in any
 * language. */
export function shortcutLetters(language: string | undefined): Record<"validate" | "wait" | "ignore", string> {
  return { validate: "V", wait: (language ?? "").toLowerCase().startsWith("fr") ? "A" : "W", ignore: "I" };
}

/** The action of a key press, or null (a modifier other than Shift, or a
 * key of no shortcut). */
export function shortcutFor(ev: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey">): ReviewAction | null {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return null;
  switch (ev.key) {
    case "ArrowLeft":
      return "prev";
    case "ArrowRight":
      return "next";
  }
  switch (ev.key.toLowerCase()) {
    case "v":
      return "validate";
    case "w":
    case "a":
      return "wait";
    case "i":
      return "ignore";
    default:
      return null;
  }
}

/** Whether a key press comes from a place where typing goes (a text field,
 * a select, an editable element, Home Assistant's pickers): no shortcut
 * there. ``path``: the event's composed path. */
export function typingTarget(path: readonly EventTarget[]): boolean {
  for (const el of path) {
    const e = el as Partial<HTMLElement> & { tagName?: string; type?: string };
    const tag = (e.tagName ?? "").toLowerCase();
    if (!tag) continue;
    if (tag === "textarea" || tag === "select" || tag.endsWith("-picker") || tag.endsWith("-textfield")) return true;
    if (tag === "input" && !["checkbox", "radio", "button", "submit", "reset"].includes(String(e.type ?? "text"))) {
      return true;
    }
    if (e.isContentEditable) return true;
    if ((e as { getAttribute?: (n: string) => string | null }).getAttribute?.("role") === "tab") return true;
  }
  return false;
}

/** A use drawn on the house curve: the curve around it as a path in a
 * ``width`` × ``height`` box, and where the use starts and ends (x). */
export interface ExampleDrawing {
  path: string;
  area: string;
  from: number;
  to: number;
}

/** The house curve around one use of a signature (the window of
 * ``exampleWindow``, with a margin of at least the use's duration), scaled to
 * the box; null without at least two points in the window. */
export function exampleDrawing(
  curve: ReadonlyArray<Pick<{ t: number; house: number }, "t" | "house">>,
  example: SignatureExample,
  start: number,
  end: number,
  width = 200,
  height = 56,
): ExampleDrawing | null {
  const margin = Math.max(1200, example.end - example.start);
  const [a, b] = exampleWindow(example, start, end, margin).map((ms) => ms / 1000);
  const pts = curve.filter((p) => p.t >= a && p.t <= b && Number.isFinite(p.house));
  if (pts.length < 2 || !(b > a)) return null;
  const max = Math.max(...pts.map((p) => p.house), example.power_w || 0, 1);
  const x = (t: number) => ((t - a) / (b - a)) * width;
  const y = (w: number) => height - (Math.max(w, 0) / max) * (height - 2);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.house).toFixed(1)}`).join("");
  const first = x(pts[0].t).toFixed(1);
  const last = x(pts[pts.length - 1].t).toFixed(1);
  return {
    path: line,
    area: `${line}L${last},${height}L${first},${height}Z`,
    from: Math.max(x(example.start), 0),
    to: Math.min(Math.max(x(example.end), x(example.start) + 2), width),
  };
}

/** The form after "Use" on a suggestion: the entity linked as a switch when
 * its state drives the load, else a new appliance named after it (merging
 * into its appliance is the separate "Merge"). */
export function formForUse(
  sigId: string,
  c: EntitySuggestion,
  friendlyName?: (entityId: string) => string | undefined,
): ReviewForm {
  const mode: LinkMode = drivesLoad(c) ? "switch" : "new";
  return { id: sigId, mode, name: friendlyName?.(c.entity_id) ?? "", entity: mode === "switch" ? c.entity_id : "", appliance: "" };
}

/** The form after "Merge" on a suggestion that belongs to an appliance. */
export function formForMerge(sigId: string, c: EntitySuggestion): ReviewForm | null {
  return c.appliance ? { id: sigId, mode: "merge", name: "", entity: "", appliance: c.appliance.key } : null;
}
