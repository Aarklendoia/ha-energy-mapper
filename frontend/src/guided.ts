// The guided mode of the live view (#17), "I switch an appliance on": name it,
// switch it on, confirm the step Energy Mapper saw, switch it off, saved.
// A pure state machine, unit tested; the panel feeds it the live store and
// the clock of the stream, and saves when it reaches "save".

import { curveRise, type CurvePoint, type FeedEntry, type LiveEvent } from "./live.ts";
import { type LabelPlan, labelMessage } from "./live-feed.ts";

/** Seconds to wait for a step after "I'm switching it on". */
export const WAIT_S = 120;
/** Without a step after this long, a small rise of the curve means "too weak". */
export const WEAK_AFTER_S = 30;
/** Two switch-ons this close are "two at once": the user picks. */
export const TOGETHER_S = 10;
/** A step may be dated slightly before the click (clocks, sampling). */
export const LEAD_S = 3;
/** Still running after this long: saved with its switch-on only. */
export const MAX_RUN_S = 1800;
/** A rise of at least this share of the threshold, below it: too weak. */
export const WEAK_SHARE = 0.3;

export type GuidedStep =
  | "idle" // the card's intro, "Start"
  | "name" // which appliance?
  | "wait" // "switch it on now"
  | "confirm" // "Detected: +850 W at 14:32:05, is it?"
  | "pick" // two switch-ons at once: which one?
  | "weak" // a rise under the threshold: too weak for the curve
  | "none" // nothing seen in time
  | "running" // "switch it off whenever you like"
  | "save" // to save (the panel sends the label)
  | "saving"
  | "done"
  | "error";

export interface Guided {
  step: GuidedStep;
  name: string;
  /** the click on "I'm switching it on" (stream clock, s) */
  since?: number;
  deadline?: number;
  /** events the user said are not it */
  rejected?: number[];
  /** the event proposed, then followed */
  event?: number;
  /** two at once: the events to pick from */
  choices?: number[];
  /** too weak: the rise of the curve (W) */
  rise?: number;
  /** to save: the stop, null without it */
  t_off?: number | null;
  /** done: what the label did, and the use */
  effect?: string;
  power?: number;
  duration?: number | null;
  error?: string;
}

export const guidedIdle = (): Guided => ({ step: "idle", name: "" });

export const guidedStart = (name = ""): Guided => ({ step: "name", name });

export function guidedName(g: Guided, name: string): Guided {
  return g.step === "name" ? { ...g, name } : g;
}

/** "I'm switching it on": wait for a step from now on. */
export function guidedArm(g: Guided, now: number): Guided {
  if ((g.step !== "name" && g.step !== "weak" && g.step !== "none") || !g.name.trim()) return g;
  return { step: "wait", name: g.name.trim(), since: now, deadline: now + WAIT_S, rejected: [] };
}

const events = (entries: Iterable<FeedEntry>) => {
  const out = new Map<number, LiveEvent>();
  for (const { ev } of entries) out.set(ev.id, ev);
  return out;
};

/** The switch-ons since the click, oldest first, but those rejected. */
function freshOns(all: Map<number, LiveEvent>, g: Guided): LiveEvent[] {
  const since = (g.since ?? 0) - LEAD_S;
  const rejected = new Set(g.rejected ?? []);
  return [...all.values()]
    .filter((ev) => ev.delta_w > 0 && ev.state !== "cancelled" && ev.t_on >= since && !rejected.has(ev.id))
    .sort((a, b) => a.t_on - b.t_on || a.id - b.id);
}

/** The first switch-on and those within ``TOGETHER_S`` of it. */
function together(fresh: LiveEvent[]): LiveEvent[] {
  if (!fresh.length) return [];
  return fresh.filter((ev) => ev.t_on - fresh[0].t_on <= TOGETHER_S);
}

/** One step of the machine: what the stream shows now. */
export function guidedTick(
  g: Guided,
  entries: Iterable<FeedEntry>,
  curve: CurvePoint[],
  threshold: number,
  now: number,
): Guided {
  const all = events(entries);
  switch (g.step) {
    case "wait":
    case "confirm":
    case "pick": {
      const group = together(freshOns(all, g));
      if (group.length >= 2) {
        const choices = group.map((ev) => ev.id);
        if (g.step === "pick" && same(choices, g.choices)) return g;
        return { ...g, step: "pick", choices, event: undefined };
      }
      if (group.length === 1) {
        if (g.step === "confirm" && g.event === group[0].id) return g;
        return { ...g, step: "confirm", event: group[0].id, choices: undefined };
      }
      // nothing (or the proposed step was cancelled): wait on
      const back: Guided = g.step === "wait" ? g : { ...g, step: "wait", event: undefined, choices: undefined };
      const since = back.since ?? now;
      const rise = curveRise(curve, since);
      const weak = rise !== null && rise >= WEAK_SHARE * threshold && rise < threshold;
      if (weak && now - since >= WEAK_AFTER_S) return { step: "weak", name: g.name, rise: Math.round(rise) };
      if (now >= (back.deadline ?? Infinity)) return { step: "none", name: g.name };
      return back;
    }
    case "running": {
      const ev = g.event !== undefined ? all.get(g.event) : undefined;
      if (!ev || ev.state === "cancelled") {
        return { ...g, step: "wait", event: undefined, deadline: Math.max(g.deadline ?? 0, now + 60) };
      }
      if (!ev.running && ev.t_off !== null) return { ...g, step: "save", t_off: ev.t_off };
      if (now - ev.t_on > MAX_RUN_S) return { ...g, step: "save", t_off: null };
      return g;
    }
    default:
      return g;
  }
}

const same = (a: number[], b: number[] | undefined) => !!b && a.length === b.length && a.every((x, i) => x === b[i]);

/** "Yes, it is" (with the event picked when two came at once). */
export function guidedYes(g: Guided, id?: number): Guided {
  if (g.step === "confirm" && g.event !== undefined) return { ...g, step: "running", choices: undefined };
  if (g.step === "pick" && id !== undefined && g.choices?.includes(id)) return { ...g, step: "running", event: id, choices: undefined };
  return g;
}

/** "Not it, keep waiting". */
export function guidedNo(g: Guided, now: number): Guided {
  if (g.step !== "confirm" && g.step !== "pick") return g;
  const ids = g.step === "confirm" ? [g.event!] : (g.choices ?? []);
  return {
    ...g,
    step: "wait",
    rejected: [...(g.rejected ?? []), ...ids],
    event: undefined,
    choices: undefined,
    deadline: Math.max(g.deadline ?? 0, now + 60),
  };
}

/** "Finish without the stop". */
export function guidedFinish(g: Guided): Guided {
  return g.step === "running" ? { ...g, step: "save", t_off: null } : g;
}

export const guidedSaving = (g: Guided): Guided => (g.step === "save" ? { ...g, step: "saving" } : g);

export function guidedDone(g: Guided, effect: string, ev: LiveEvent | undefined): Guided {
  const t_off = g.t_off ?? null;
  return {
    step: "done",
    name: g.name,
    since: g.since,
    event: g.event,
    effect,
    power: ev ? Math.round(ev.power_w) : undefined,
    duration: ev && t_off !== null ? Math.max(0, t_off - ev.t_on) : null,
  };
}

/** The save failed: the error, and "try again" goes back to saving. */
export const guidedFailed = (g: Guided, error: string): Guided => ({ ...g, step: "error", error });

export const guidedRetrySave = (g: Guided): Guided => (g.step === "error" ? { ...g, step: "save", error: undefined } : g);

/** The plan of the guided label: an existing appliance of that name gets
 * the example, else a new appliance recognised in the curve. */
export function guidedPlan(
  name: string,
  appliances: ReadonlyArray<{ key: string; name: string }>,
): Exclude<LabelPlan, { kind: "none" }> {
  const n = name.trim();
  const a = appliances.find((x) => x.name.trim().toLowerCase() === n.toLowerCase());
  return a ? { kind: "appliance", key: a.key, name: a.name } : { kind: "new", name: n, track: "curve", entity: "" };
}

/** The websocket message that saves the guided use. */
export function guidedMessage(
  g: Guided,
  entry: FeedEntry,
  appliances: ReadonlyArray<{ key: string; name: string }>,
): Record<string, unknown> {
  return labelMessage(guidedPlan(g.name, appliances), entry, "guided", g.t_off ?? null);
}
