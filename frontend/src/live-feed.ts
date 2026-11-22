// The live view (#17) in words: the lines of the feed (title, tag, when),
// why a candidate is proposed, the "Now" card, the labelling request and the
// sentences that say what a label does. Pure functions, unit tested.

import { formatDuration, formatNumber, formatPercent, formatPower, formatTime, type LocaleInfo } from "./format.ts";
import type { Translate } from "./i18n.ts";
import type {
  FeedEntry,
  LiveCandidate,
  LiveEvent,
  LiveNow,
  LiveStatusMessage,
  SignatureReason,
  SwitchReason,
} from "./live.ts";
import { provisionalName } from "./names.ts";
import { BASE_INDEX, HA_PALETTE, SERIES_INDEXES, SUBMETERED_INDEX, ESTIMATED_INDEX } from "./theme.ts";

/** What the names come from: the appliances and the registry. */
export interface NameContext {
  t: Translate;
  info: LocaleInfo;
  appliances: ReadonlyArray<{ key: string; name: string }>;
  signatures: ReadonlyMap<
    string,
    {
      label?: string | null;
      appliance?: string | null;
      kind?: string | null;
      power_w?: number | null;
      duration_s?: number | null;
      hours?: number[] | null;
      count?: number | null;
    }
  >;
  /** friendly name of an entity, if known */
  entityName?: (entityId: string) => string | undefined;
}

const applianceName = (ctx: NameContext, key: string | null | undefined) =>
  key ? ctx.appliances.find((a) => a.key === key)?.name : undefined;

/** Whether a candidate already has a name (an appliance, a label). */
export function candidateNamed(c: LiveCandidate, ctx: NameContext): boolean {
  if (c.appliance || c.label) return true;
  const sig = c.signature_id ? ctx.signatures.get(c.signature_id) : undefined;
  return Boolean(sig?.label || sig?.appliance);
}

/** The appliance a candidate belongs to (its key), if any. */
export function candidateAppliance(c: LiveCandidate, ctx: NameContext): string | null {
  const sig = c.signature_id ? ctx.signatures.get(c.signature_id) : undefined;
  return c.appliance ?? sig?.appliance ?? null;
}

/** "Kettle", or the provisional name of an unnamed signature ("2 kW
 * appliance, ~15 min, all day"), or the switch's name. */
export function candidateName(c: LiveCandidate, ctx: NameContext): string {
  const sig = c.signature_id ? ctx.signatures.get(c.signature_id) : undefined;
  const named = applianceName(ctx, c.appliance) ?? applianceName(ctx, sig?.appliance) ?? c.label ?? sig?.label;
  if (named) return named;
  if (c.source === "switch") {
    const id = c.entity_id ?? (c.reason as SwitchReason).switch;
    return ctx.entityName?.(id) ?? id;
  }
  return provisionalName(sig ?? { kind: c.kind, power_w: c.power_w }, ctx.t, ctx.info);
}

/** How many past uses an unnamed signature has (the registry's count). */
export function pastUses(c: LiveCandidate, ctx: NameContext): number | null {
  const sig = c.signature_id ? ctx.signatures.get(c.signature_id) : undefined;
  return typeof sig?.count === "number" ? sig.count : null;
}

/** "40 s", "2 min 10 s", "12 min", "1 h 05". */
export function liveDuration(seconds: number, info: LocaleInfo): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60 || s >= 600) return formatDuration(s, info);
  const min = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${formatNumber(min, info)} min ${formatNumber(rest, info)} s` : `${formatNumber(min, info)} min`;
}

/** "+850 W", "−2.1 kW": the size of a step, signed. */
export function stepText(w: number, info: LocaleInfo): string {
  const a = Math.abs(w);
  const text = a >= 1000 ? `${formatNumber(a / 1000, info, 1)} kW` : `${formatNumber(Math.round(a), info)} W`;
  return `${w < 0 ? "−" : "+"}${text}`;
}

/** "3 s ago", "6 min ago", then the clock time after an hour. */
export function whenText(ts: number, now: number, t: Translate, info: LocaleInfo): string {
  const ago = Math.max(0, now - ts);
  if (ago < 60) return t("live_ago_s", { n: formatNumber(Math.round(ago), info) });
  if (ago < 3600) return t("live_ago_min", { n: formatNumber(Math.floor(ago / 60), info) });
  return formatTime(ts, info);
}

/** Why a candidate is proposed, from its structured reason. */
export function candidateReason(c: LiveCandidate, ev: Pick<LiveEvent, "power_w">, ctx: NameContext): string {
  const { t, info } = ctx;
  const parts: string[] = [];
  if (c.source === "switch") {
    const r = c.reason as SwitchReason;
    const name = ctx.entityName?.(r.switch) ?? c.label ?? r.switch;
    const n = formatNumber(Math.abs(Math.round(r.seconds)), info);
    parts.push(t(r.seconds < 0 ? "live_reason_switch_before" : "live_reason_switch_after", { name, n }));
    if (r.pv_moved) parts.push(t("live_pv_moved"));
    return parts.join(" · ");
  }
  const r = c.reason as SignatureReason;
  if (candidateNamed(c, ctx)) parts.push(t("live_reason_named"));
  else {
    const n = pastUses(c, ctx);
    parts.push(n ? t("live_reason_seen", { n: formatNumber(n, info) }) : t("live_reason_unnamed"));
  }
  if (r.expected) parts.push(t("live_reason_expected"));
  const power = formatPower(r.power_w ?? c.power_w ?? ev.power_w, info);
  parts.push(r.distance <= 0.5 ? t("live_reason_same_power", { power }) : t("live_reason_power", { power }));
  if (r.usual_hour) parts.push(t("live_reason_usual_hour"));
  if (r.per_week >= 0.5) parts.push(t("live_reason_per_week", { n: formatNumber(r.per_week, info, r.per_week < 10 ? 1 : 0) }));
  if (r.pv_moved) parts.push(t("live_pv_moved"));
  return parts.join(" · ");
}

export type FeedTag = "new" | "seen" | "known" | "labelled";

/** What the user labelled in this panel (the line keeps it). */
export interface Labelled {
  name: string;
  effect: string;
}

export interface FeedRow {
  id: number;
  entry: FeedEntry;
  /** the step (W, signed) */
  delta: number;
  title: string;
  tag: FeedTag;
  when: string;
  sub: string;
  provisional: boolean;
  /** an administrator can label it: a switch-on, not labelled yet */
  canLabel: boolean;
  /** it is still to label: new or seen before, and labellable */
  todo: boolean;
  /** the unnamed signature "Never propose again" would ignore, if any */
  ignorable: string | null;
}

/** The best candidate worth showing ("maybe" or better). */
export function bestCandidate(ev: LiveEvent): LiveCandidate | undefined {
  const c = ev.candidates?.[0];
  if (!c) return undefined;
  const confidence = c.confidence ?? (c.score >= 0.6 ? "likely" : c.score >= 0.3 ? "maybe" : "unknown");
  return confidence === "unknown" ? undefined : c;
}

/** One line of the feed. */
export function feedRow(
  entry: FeedEntry,
  ctx: NameContext,
  now: number,
  isAdmin: boolean,
  labelled?: Labelled,
): FeedRow {
  const { t, info } = ctx;
  const ev = entry.ev;
  const best = bestCandidate(ev);
  const named = best !== undefined && candidateNamed(best, ctx);
  const name = best ? candidateName(best, ctx) : null;
  const up = ev.delta_w > 0;
  let tag: FeedTag = labelled ? "labelled" : !best ? "new" : named ? "known" : "seen";
  let title: string;
  if (labelled) title = labelled.name;
  else if (!up) title = name && named ? t("live_stopped", { name }) : t("live_stopped_unknown");
  else if (!best || !name) title = t("live_unknown");
  else title = named ? name : t("live_looks_like", { name });
  if (!up && !labelled && !named) tag = "new";

  const sub: string[] = [];
  if (ev.state === "provisional") sub.push(t("live_provisional"));
  if (ev.session) {
    const span = (ev.running ? now : (ev.t_off ?? now)) - ev.t_on;
    sub.push(t("live_session", { n: formatNumber(ev.session.count, info), d: liveDuration(span, info) }));
  } else if (!up) sub.push(t("live_off_unseen"));
  else if (ev.running) sub.push(t("live_running_for", { d: liveDuration(now - ev.t_on, info) }));
  else if (ev.duration_s !== null && ev.duration_s !== undefined) sub.push(t("live_ran_for", { d: liveDuration(ev.duration_s, info) }));
  if (labelled) sub.push(t("live_example_saved"));
  else if (best && named) sub.push(t("live_recognised", { p: formatPercent(best.score, info) }));
  else if (best) sub.push(t("live_similar", { p: formatPercent(best.score, info) }));
  if (ev.pv_moved) sub.push(t("live_pv_moved"));

  const canLabel = isAdmin && up && !labelled;
  const unnamedSig = ev.candidates?.find((c) => c.source === "signature" && c.signature_id && !candidateNamed(c, ctx));
  return {
    id: ev.id,
    entry,
    delta: ev.delta_w,
    title,
    tag,
    when: whenText(ev.t_on, now, t, info),
    sub: sub.join(" · "),
    provisional: ev.state === "provisional",
    canLabel,
    todo: canLabel && (tag === "new" || tag === "seen"),
    ignorable: isAdmin && !labelled && best && unnamedSig === best ? (unnamedSig.signature_id ?? null) : null,
  };
}

// -- the "Now" card ----------------------------------------------------------

export interface NowItem {
  key: string;
  name: string;
  sub: string;
  /** W; null: unknown (a switch that is on) */
  power: number | null;
  color: string;
  /** not identified yet: drawn dashed */
  dashed: boolean;
  eventId?: number;
}

const paletteColor = (i: number) => `var(--graph-color-${i + 1}, ${HA_PALETTE[i]})`;

/** The colour of a running event: stable for its id. */
export function eventColor(id: number): string {
  return paletteColor(SERIES_INDEXES[Math.abs(id) % SERIES_INDEXES.length]);
}

/** What runs now: the running events (newest first), the measured devices
 * and the switches that are on, then the unidentified rest. */
export function nowItems(
  now: LiveNow | null,
  events: ReadonlyMap<number, FeedEntry>,
  ctx: NameContext,
  labelled: ReadonlyMap<number, Labelled> = new Map(),
): NowItem[] {
  if (!now) return [];
  const { t, info } = ctx;
  const items: NowItem[] = [];
  for (const id of now.running ?? []) {
    const ev = events.get(id)?.ev;
    if (!ev) continue;
    const best = bestCandidate(ev);
    const named = best !== undefined && candidateNamed(best, ctx);
    const mine = labelled.get(id);
    let name: string;
    let sub: string;
    if (mine) {
      name = mine.name;
      sub = t("live_now_labelled");
    } else if (best && named) {
      name = candidateName(best, ctx);
      sub = t("live_now_known", { d: liveDuration(Math.max(0, (now.ts ?? ev.t_on) - ev.t_on), info) });
    } else if (best) {
      name = candidateName(best, ctx);
      sub = t("live_now_seen", { p: formatPercent(best.score, info) });
    } else {
      name = t("live_now_unknown", { power: stepText(ev.delta_w, info) });
      sub = t("live_now_new");
    }
    items.push({ key: `ev-${id}`, name, sub, power: ev.power_w, color: eventColor(id), dashed: !mine && !named, eventId: id });
  }
  for (const sm of now.submeters ?? []) {
    if (!sm.power_w || sm.power_w < 1) continue;
    items.push({
      key: `sm-${sm.entity_id}`,
      name: applianceName(ctx, sm.appliance) ?? sm.name,
      sub: t(sm.estimated ? "live_now_estimated" : "live_now_measured"),
      power: sm.power_w,
      color: paletteColor(sm.estimated ? ESTIMATED_INDEX : SUBMETERED_INDEX),
      dashed: false,
    });
  }
  for (const sw of now.switches ?? []) {
    if (!sw.on) continue;
    items.push({
      key: `sw-${sw.entity_id}`,
      name: applianceName(ctx, sw.appliance) ?? sw.name,
      sub: t("live_now_switch"),
      power: null,
      color: paletteColor(SUBMETERED_INDEX),
      dashed: false,
    });
  }
  if (now.unidentified_w !== null && now.unidentified_w !== undefined) {
    items.push({
      key: "rest",
      name: t("live_now_rest"),
      sub: t("live_now_rest_hint"),
      power: now.unidentified_w,
      color: paletteColor(BASE_INDEX),
      dashed: false,
    });
  }
  return items;
}

/** The split bar: each item with a power, as a share of their sum. */
export function nowSplit(items: NowItem[]): Array<{ key: string; color: string; share: number }> {
  const parts = items.filter((i) => i.power !== null && i.power > 0);
  const total = parts.reduce((a, i) => a + (i.power ?? 0), 0);
  if (!(total > 0)) return [];
  return parts.map((i) => ({ key: i.key, color: i.color, share: (i.power ?? 0) / total }));
}

// -- labelling -----------------------------------------------------------------

export type Track = "curve" | "switch" | "sensor";

/** The inline labelling form of one event. */
export interface LabelForm {
  /** the candidate picked (index), null: none */
  pick: number | null;
  name: string;
  track: Track;
  entity: string;
}

export const blankLabelForm = (): LabelForm => ({ pick: null, name: "", track: "curve", entity: "" });

/** What saving the form would do. */
export type LabelPlan =
  | { kind: "none"; needName?: boolean }
  | { kind: "appliance"; key: string; name: string; signature_id?: string }
  | { kind: "signature"; signature_id: string; signature: string; name: string; past_uses: number | null }
  | { kind: "new"; name: string; track: Track; entity: string };

const sameName = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/** The form preselected for an event: its best candidate when it already
 * has a name or is a likely unnamed signature. */
export function initialForm(ev: LiveEvent, ctx: NameContext): LabelForm {
  const best = bestCandidate(ev);
  const form = blankLabelForm();
  if (best && (candidateNamed(best, ctx) || (best.confidence ?? "") === "likely" || best.score >= 0.6)) form.pick = 0;
  return form;
}

/** The plan of a form: a picked candidate with a name is its appliance; an
 * unnamed signature picked is named with the name typed; else the name
 * typed is an existing appliance (same name) or a new one. */
export function labelPlan(ev: LiveEvent, form: LabelForm, ctx: NameContext): LabelPlan {
  const name = form.name.trim();
  const c = form.pick !== null ? ev.candidates?.[form.pick] : undefined;
  const existing = name ? ctx.appliances.find((a) => sameName(a.name, name)) : undefined;
  if (c) {
    const key = candidateAppliance(c, ctx);
    if (key) {
      return { kind: "appliance", key, name: applianceName(ctx, key) ?? candidateName(c, ctx), signature_id: c.signature_id };
    }
    if (c.source === "signature" && c.signature_id) {
      const sig = ctx.signatures.get(c.signature_id);
      const label = c.label ?? sig?.label ?? null;
      const given = name || label || "";
      if (existing) return { kind: "appliance", key: existing.key, name: existing.name, signature_id: c.signature_id };
      if (!given) return { kind: "none", needName: true };
      return {
        kind: "signature",
        signature_id: c.signature_id,
        signature: candidateName({ ...c, label: null }, { ...ctx, appliances: [] }),
        name: given,
        past_uses: pastUses(c, ctx),
      };
    }
    // a switch that belongs to no appliance: a new one, tracked by it
    const entity = c.entity_id ?? (c.reason as SwitchReason).switch;
    const given = name || candidateName(c, ctx);
    if (existing) return { kind: "appliance", key: existing.key, name: existing.name };
    return { kind: "new", name: given, track: "switch", entity };
  }
  if (!name) return { kind: "none" };
  if (existing) return { kind: "appliance", key: existing.key, name: existing.name };
  return { kind: "new", name, track: form.track, entity: form.entity.trim() };
}

/** What is still missing before saving (a translation key), or null. */
export function planMissing(plan: LabelPlan): "live_need_choice" | "live_need_name" | "live_need_entity" | null {
  if (plan.kind === "none") return plan.needName ? "live_need_name" : "live_need_choice";
  if (plan.kind === "new" && plan.track !== "curve" && !plan.entity) return "live_need_entity";
  return null;
}

/** The websocket message ``energy_mapper/live/label`` of a plan, for an
 * event of the feed. The event's times are sent too: they are used if the
 * live session has ended since. */
export function labelMessage(
  plan: Exclude<LabelPlan, { kind: "none" }>,
  entry: FeedEntry,
  source: "live" | "guided" = "live",
  t_off?: number | null,
): Record<string, unknown> {
  const ev = entry.ev;
  const msg: Record<string, unknown> = { type: "energy_mapper/live/label", event_id: ev.id, source, t_on: ev.t_on, power_w: Math.max(1, Math.round(ev.power_w)) };
  if (entry.stream !== null) msg.session = entry.stream;
  const off = t_off !== undefined ? t_off : ev.running ? null : ev.t_off;
  if (off !== null && off !== undefined && off > ev.t_on) msg.t_off = off;
  switch (plan.kind) {
    case "appliance":
      msg.target = "appliance";
      msg.appliance = plan.key;
      if (plan.signature_id) msg.signature_id = plan.signature_id;
      break;
    case "signature":
      msg.target = "signature";
      msg.signature_id = plan.signature_id;
      msg.name = plan.name;
      break;
    case "new":
      msg.target = "new";
      msg.name = plan.name;
      msg.track = plan.track;
      if (plan.track !== "curve") msg.entity_id = plan.entity;
      break;
  }
  return msg;
}

/** The sentence before saving: what the label will do. */
export function planEffect(plan: LabelPlan, ev: LiveEvent, ctx: NameContext): string {
  const { t, info } = ctx;
  switch (plan.kind) {
    case "none":
      return t(plan.needName ? "live_effect_need_name" : "live_effect_choose");
    case "appliance":
      return t("live_effect_appliance", { name: plan.name });
    case "signature":
      return plan.past_uses
        ? t("live_effect_signature", { signature: plan.signature, name: plan.name, n: formatNumber(plan.past_uses, info) })
        : t("live_effect_signature_nocount", { signature: plan.signature, name: plan.name });
    case "new": {
      if (plan.track === "switch") return t("live_effect_new_switch", { name: plan.name, entity: plan.entity ? (ctx.entityName?.(plan.entity) ?? plan.entity) : "…" });
      if (plan.track === "sensor") return t("live_effect_new_sensor", { name: plan.name, entity: plan.entity ? (ctx.entityName?.(plan.entity) ?? plan.entity) : "…" });
      return ev.running
        ? t("live_effect_new_open", { name: plan.name })
        : t("live_effect_new_whole", { name: plan.name, power: formatPower(ev.power_w, info) });
    }
  }
}

/** The text of the save button. */
export function saveLabel(plan: LabelPlan, t: Translate): string {
  if (plan.kind === "new") return t("live_save_new", { name: plan.name });
  if (plan.kind === "signature") return t("live_save_signature");
  return t("live_save");
}

/** The answer of ``energy_mapper/live/label``. */
export interface LabelResult {
  effect: "appliance_created" | "signature_named" | "signature_merged" | "example_added" | "signature_ignored" | string;
  appliance: { key: string; name: string } | null;
  signature: { id: string; past_uses: number; created: boolean } | null;
  example: { t_on: number; t_off: number | null; power_w: number } | null;
  open: boolean;
  confirmed: boolean;
  other_appliance: { key: string; name: string } | null;
}

/** The sentence after saving, from the answer's ``effect``. */
export function resultEffect(r: LabelResult, t: Translate, info: LocaleInfo): string {
  const name = r.appliance?.name ?? "";
  const n = formatNumber(r.signature?.past_uses ?? 0, info);
  const open = r.open ? ` ${t("live_done_open")}` : "";
  switch (r.effect) {
    case "appliance_created":
      return `${t("live_done_created", { name })}${r.signature?.created ? ` ${t("live_done_signature_ready")}` : open}`;
    case "signature_named":
      return `${t("live_done_named", { name, n })}${open}`;
    case "signature_merged":
      return `${t("live_done_merged", { name, n })}${open}`;
    case "signature_ignored":
      return t("live_done_ignored");
    default:
      if (r.confirmed) return `${t("live_done_confirmed", { name })}${open}`;
      if (r.other_appliance) return `${t("live_done_other", { name, other: r.other_appliance.name })}${open}`;
      return `${t("live_done_example", { name })}${open}`;
  }
}

/** A labelling error in words. */
export function labelError(err: unknown, t: Translate): string {
  const e = (err ?? {}) as { code?: string; message?: string };
  switch (e.code) {
    case "unauthorized":
      return t("live_err_unauthorized");
    case "addon_unavailable":
      return t("live_err_addon");
    case "already_linked":
      return t("live_err_linked", { message: e.message ?? "" });
    case "conflict":
      return t("live_err_conflict", { message: e.message ?? "" });
    case "invalid_event":
      return t("live_err_event");
    default:
      return e.message || String(err);
  }
}

// -- the status of the view ---------------------------------------------------

export interface StatusView {
  type: "info" | "warning" | "error";
  title: string;
  text: string;
}

/** Why the view does not run, in words (null: it runs). */
export function statusView(status: LiveStatusMessage | null, connecting: boolean, t: Translate, info: LocaleInfo): StatusView | null {
  if (!status) return connecting ? { type: "info", title: t("live_connecting"), text: t("live_connecting_text") } : null;
  switch (status.reason) {
    case "disabled":
      return { type: "info", title: t("live_disabled"), text: t("live_disabled_text") };
    case "starting":
      return { type: "info", title: t("live_starting"), text: t("live_starting_text") };
    case "overloaded":
      return {
        type: "warning",
        title: t("live_overloaded"),
        text: status.retry_at ? t("live_overloaded_text", { time: formatTime(status.retry_at, info) }) : t("live_overloaded_soon"),
      };
    case "addon_unavailable":
      return { type: "error", title: t("live_addon_unavailable"), text: t("live_addon_unavailable_text") };
    case "addon_too_old":
      return {
        type: "warning",
        title: t("live_addon_too_old"),
        text: t("live_addon_too_old_text", { min: status.min_version ?? "0.12.0", version: status.addon_version ?? "?" }),
      };
    case "not_loaded":
      return { type: "error", title: t("not_loaded"), text: t("live_not_loaded_text") };
    default:
      return {
        type: "error",
        title: t("live_error"),
        text: t("live_error_text", { message: status.error?.message ?? status.error?.code ?? status.reason }),
      };
  }
}
