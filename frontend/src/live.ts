// The live view (#17): the messages of ``energy_mapper/live/subscribe`` and
// the feed the panel keeps from them. Pure functions, unit tested.
//
// The integration sends a ``snapshot`` (15 minutes of curve, the recent and
// running events), then ``update`` messages (new samples, the events that
// appeared or changed, whole, with a new ``seq``), or a ``status`` when the
// view cannot run. The panel keeps the events of the last hour while it is
// open, even after the integration forgot them.

/** Why a candidate is proposed: a signature of the registry... */
export interface SignatureReason {
  power_w: number;
  /** distance to the signature's power, in tolerances */
  distance: number;
  /** most of its uses are at this hour */
  usual_hour: boolean;
  per_week: number;
  /** an expected signature (created from a labelled use, not seen at night yet) */
  expected: boolean;
  pv_moved: boolean;
}

/** ... or a switch that changed at the same time (``seconds`` after the step). */
export interface SwitchReason {
  switch: string;
  seconds: number;
  pv_moved: boolean;
}

export type Confidence = "likely" | "maybe" | "unknown";

export interface LiveCandidate {
  source: "signature" | "switch";
  signature_id?: string;
  entity_id?: string;
  label: string | null;
  appliance: string | null;
  kind?: string | null;
  band?: string | null;
  power_w: number | null;
  score: number;
  confidence?: Confidence;
  reason: SignatureReason | SwitchReason;
}

/** A session: the same step on and off again and again (a hob regulating). */
export interface LiveSession {
  count: number;
  steps: number;
  on_s: number | null;
  period_s: number | null;
  duty: number | null;
  confirmed: boolean;
}

export interface LiveEvent {
  id: number;
  seq: number;
  t_on: number;
  /** the switch-on step (W, positive), or a negative off-step whose
   * switch-on was not seen */
  delta_w: number;
  state: "provisional" | "confirmed" | "cancelled" | string;
  running: boolean;
  t_off: number | null;
  off_w: number | null;
  duration_s: number | null;
  power_w: number;
  session: LiveSession | null;
  pv_moved: boolean;
  confidence: Confidence;
  candidates: LiveCandidate[];
  steps?: Array<[number, number]>;
}

export interface LiveSubmeter {
  entity_id: string;
  name: string;
  appliance: string | null;
  estimated: boolean;
  power_w: number | null;
}

export interface LiveSwitch {
  entity_id: string;
  name: string;
  appliance: string | null;
  on: boolean | null;
}

export interface LiveNow {
  ts?: number;
  house_w?: number | null;
  residual_w?: number | null;
  grid_w?: number | null;
  pv_w?: number | null;
  battery_w?: number | null;
  submetered_w?: number | null;
  submeters?: LiveSubmeter[];
  /** ids of the running events, newest first */
  running: number[];
  unidentified_w: number | null;
  switches?: LiveSwitch[];
}

/** One sample of the curve: [ts, house_w, residual_w]. */
export type CurvePoint = [number, number | null, number | null];

export type LiveReason =
  | "disabled"
  | "starting"
  | "overloaded"
  | "error"
  | "addon_unavailable"
  | "addon_too_old"
  | "not_loaded";

export interface LiveStatusMessage {
  type: "status";
  available: false;
  reason: LiveReason | string;
  retry_at?: number;
  error?: { code?: string; message?: string };
  min_version?: string;
  addon_version?: string | null;
}

export interface LiveSnapshotMessage {
  type: "snapshot";
  available: true;
  seq: number;
  session: number | null;
  history_s?: number;
  threshold_w?: number | null;
  time_zone?: string | null;
  curve: CurvePoint[];
  events: LiveEvent[];
  now: LiveNow | null;
}

export interface LiveUpdateMessage {
  type: "update";
  seq: number;
  curve: CurvePoint[];
  events: LiveEvent[];
  now: LiveNow | null;
}

export type LiveMessage = LiveStatusMessage | LiveSnapshotMessage | LiveUpdateMessage;

/** An event of the feed, with the live session (``stream``) it came from:
 * labelling it names the event of that session. */
export interface FeedEntry {
  ev: LiveEvent;
  stream: number | null;
}

export interface LiveStore {
  /** null: no message yet (connecting) */
  status: LiveStatusMessage | null;
  available: boolean;
  session: number | null;
  seq: number;
  history_s: number;
  threshold_w: number;
  curve: CurvePoint[];
  now: LiveNow | null;
  events: Map<number, FeedEntry>;
}

/** How long the feed keeps an event that ended (s): the last hour. */
export const FEED_KEEP_S = 3600;
export const DEFAULT_HISTORY_S = 900;
export const DEFAULT_THRESHOLD_W = 300;

export function emptyStore(): LiveStore {
  return {
    status: null,
    available: false,
    session: null,
    seq: 0,
    history_s: DEFAULT_HISTORY_S,
    threshold_w: DEFAULT_THRESHOLD_W,
    curve: [],
    now: null,
    events: new Map(),
  };
}

const ended = (ev: LiveEvent) => !ev.running;
const lastTime = (ev: LiveEvent) => ev.t_off ?? ev.t_on;

/** The events kept: the running ones, and those that ended within the
 * last ``FEED_KEEP_S`` before ``now``. */
function prune(events: Map<number, FeedEntry>, now: number): void {
  for (const [id, { ev }] of events) {
    if (ended(ev) && lastTime(ev) < now - FEED_KEEP_S) events.delete(id);
  }
}

function trimCurve(curve: CurvePoint[], history: number): CurvePoint[] {
  if (!curve.length) return curve;
  const cutoff = curve[curve.length - 1][0] - history;
  if (curve[0][0] >= cutoff) return curve;
  return curve.filter((p) => p[0] >= cutoff);
}

/** The store after one message (a new object; the old one is not changed).
 * ``now``: the current time (s), for pruning the feed. */
export function applyMessage(store: LiveStore, msg: LiveMessage, now: number): LiveStore {
  const events = new Map(store.events);
  if (msg.type === "status") {
    // the feed stays (it can still be labelled by its times); what runs is unknown
    return { ...store, status: msg, available: false, now: null, curve: [], events };
  }
  if (msg.type === "snapshot") {
    const fresh = new Set<number>();
    for (const ev of msg.events ?? []) {
      if (ev.state === "cancelled") {
        events.delete(ev.id);
        continue;
      }
      fresh.add(ev.id);
      events.set(ev.id, { ev, stream: msg.session ?? null });
    }
    // what ran or was provisional and is not in the new snapshot is gone
    for (const [id, { ev }] of events) {
      if (!fresh.has(id) && (ev.running || ev.state === "provisional")) events.delete(id);
    }
    prune(events, now);
    const history = msg.history_s || DEFAULT_HISTORY_S;
    return {
      status: null,
      available: true,
      session: msg.session ?? null,
      seq: msg.seq,
      history_s: history,
      threshold_w: msg.threshold_w || DEFAULT_THRESHOLD_W,
      curve: trimCurve([...(msg.curve ?? [])], history),
      now: msg.now ?? null,
      events,
    };
  }
  // update: the events come back whole; a newer seq wins
  for (const ev of msg.events ?? []) {
    const old = events.get(ev.id);
    if (old && old.ev.seq > ev.seq) continue;
    if (ev.state === "cancelled") events.delete(ev.id);
    else events.set(ev.id, { ev, stream: old?.stream ?? store.session });
  }
  prune(events, now);
  const curve = msg.curve?.length ? trimCurve([...store.curve, ...msg.curve], store.history_s) : store.curve;
  return { ...store, seq: Math.max(store.seq, msg.seq ?? 0), curve, now: msg.now ?? store.now, events };
}

/** The events of the feed, newest first. */
export function feedEntries(store: LiveStore): FeedEntry[] {
  return [...store.events.values()].sort((a, b) => b.ev.t_on - a.ev.t_on || b.ev.id - a.ev.id);
}

/** The offset (s) to add to the local clock to get the clock of the stream:
 * the newest sample is about "now" for the add-on. */
export function clockOffset(store: LiveStore, localNow: number): number | null {
  const ts = store.now?.ts ?? store.curve[store.curve.length - 1]?.[0];
  return typeof ts === "number" ? ts - localNow : null;
}

/** The house power at ``ts``: the last sample at or before it (else the first). */
export function houseAt(curve: CurvePoint[], ts: number): number | null {
  let out: number | null = null;
  for (const [t, house] of curve) {
    if (t > ts) {
      if (out === null) out = house;
      break;
    }
    if (house !== null) out = house;
  }
  return out;
}

const median = (xs: number[]) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

/** How much the house power rose after ``since`` (W): the median of the
 * samples after it minus the median of the ``before_s`` before it; null
 * without samples. The guided mode's hint for a use too small to make a step. */
export function curveRise(curve: CurvePoint[], since: number, settle_s = 5, before_s = 15): number | null {
  const before = median(curve.filter(([t, h]) => h !== null && t < since && t >= since - before_s).map(([, h]) => h!));
  const after = median(curve.filter(([t, h]) => h !== null && t >= since + settle_s).map(([, h]) => h!));
  if (before === null || after === null) return null;
  return after - before;
}

/** A small drawing of the curve around an event (the inline zoom): the path
 * of the house curve in a ``width`` x ``height`` box, and the event's span.
 * Null without samples in the window. */
export function zoomDrawing(
  curve: CurvePoint[],
  from: number,
  to: number,
  spanFrom: number,
  spanTo: number,
  width: number,
  height: number,
): { path: string; x: number; w: number } | null {
  const pts = curve.filter(([t, h]) => h !== null && t >= from && t <= to) as Array<[number, number, number | null]>;
  if (pts.length < 2 || to <= from) return null;
  const top = Math.max(...pts.map((p) => p[1]), 1) * 1.1;
  const x = (t: number) => ((t - from) / (to - from)) * width;
  const y = (w: number) => height - (Math.max(0, w) / top) * height;
  // a step curve: each sample holds until the next one
  let path = `M${x(pts[0][0]).toFixed(1)},${y(pts[0][1]).toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    path += `H${x(pts[i][0]).toFixed(1)}V${y(pts[i][1]).toFixed(1)}`;
  }
  path += `H${width}`;
  const a = Math.max(0, x(spanFrom));
  const b = Math.min(width, x(spanTo));
  return { path, x: a, w: Math.max(1, b - a) };
}
