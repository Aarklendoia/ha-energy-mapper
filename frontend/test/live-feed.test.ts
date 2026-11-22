import assert from "node:assert/strict";
import { test } from "node:test";

import { localeInfo } from "../src/format.ts";
import { translator } from "../src/i18n.ts";
import type { FeedEntry, LiveCandidate, LiveEvent } from "../src/live.ts";
import {
  blankLabelForm,
  candidateName,
  candidateReason,
  feedRow,
  labelError,
  labelMessage,
  labelPlan,
  liveDuration,
  type NameContext,
  nowItems,
  nowSplit,
  planEffect,
  planMissing,
  resultEffect,
  statusView,
  stepText,
  whenText,
} from "../src/live-feed.ts";
import { event } from "./live-fixtures.ts";

// synthetic household (no real data)
const T0 = 1_800_000_000;
const fr = translator("fr");
const en = translator("en");
const info = localeInfo({ language: "fr", number_format: "language", time_format: "24", time_zone: "server" }, "fr", "Europe/Paris");
const infoEn = localeInfo({ language: "en", number_format: "language", time_format: "24", time_zone: "server" }, "en", "Europe/Paris");

const ctx = (t = fr, i = info): NameContext => ({
  t,
  info: i,
  appliances: [
    { key: "signature:short-2000w", name: "Bouilloire" },
    { key: "load:switch.water_heater", name: "Chauffe-eau" },
  ],
  signatures: new Map([
    ["short-2000w", { label: null, appliance: "signature:short-2000w", kind: "steady", power_w: 2000, duration_s: 120, count: 40 }],
    ["medium-2100w", { label: null, appliance: null, kind: "steady", power_w: 2100, duration_s: 900, count: 23, hours: Array(24).fill(1) }],
  ]),
  entityName: (id) => (id === "switch.water_heater" ? "Relais chauffe-eau" : undefined),
});

const sigCand = (id: string, score: number, extra: Partial<LiveCandidate> = {}): LiveCandidate => ({
  source: "signature",
  signature_id: id,
  label: null,
  appliance: null,
  kind: "steady",
  band: "medium",
  power_w: 2100,
  score,
  confidence: score >= 0.6 ? "likely" : score >= 0.3 ? "maybe" : "unknown",
  reason: { power_w: 2100, distance: 0.2, usual_hour: true, per_week: 3.5, expected: false, pv_moved: false },
  ...extra,
});

const entry = (ev: LiveEvent, stream: number | null = 4): FeedEntry => ({ ev, stream });

test("durations, steps and times in words", () => {
  assert.equal(liveDuration(40, info), "40 s");
  assert.equal(liveDuration(130, info), "2 min 10 s");
  assert.equal(liveDuration(120, info), "2 min");
  assert.equal(liveDuration(720, info), "12 min");
  assert.equal(stepText(850, info), "+850 W");
  assert.equal(stepText(-2080, info), "−2,1 kW");
  assert.equal(stepText(2080, infoEn), "+2.1 kW");
  assert.equal(whenText(T0, T0 + 20, fr, info), "il y a 20 s");
  assert.equal(whenText(T0, T0 + 360, fr, info), "il y a 6 min");
  assert.equal(whenText(T0, T0 + 20, en, infoEn), "20 s ago");
});

test("an unknown switch-on: Inconnu, nouveau, to label", () => {
  const row = feedRow(entry(event(1)), ctx(), T0 + 30, true);
  assert.equal(row.title, "Inconnu");
  assert.equal(row.tag, "new");
  assert.equal(row.todo, true);
  assert.equal(row.canLabel, true);
  assert.match(row.sub, /en marche depuis 20 s/);
  // a non-admin sees it without actions
  assert.equal(feedRow(entry(event(1)), ctx(), T0 + 30, false).canLabel, false);
});

test("an unnamed signature: Ressemble à …, déjà signalé, and it can be ignored", () => {
  const ev = event(2, { delta_w: 2080, power_w: 2080, candidates: [sigCand("medium-2100w", 0.82)] });
  const row = feedRow(entry(ev), ctx(), T0 + 100, true);
  assert.equal(row.tag, "seen");
  assert.match(row.title, /^Ressemble à « Appareil de 2,1 kW, ~15 min/);
  assert.match(row.sub, /ressemble à 82 %/);
  assert.equal(row.todo, true);
  assert.equal(row.ignorable, "medium-2100w");
});

test("a named appliance: its name, connu, not to label; a stop completes the line", () => {
  const ev = event(3, {
    delta_w: 1950,
    power_w: 1950,
    running: false,
    t_off: T0 + 70,
    duration_s: 40,
    candidates: [sigCand("short-2000w", 0.96, { appliance: "signature:short-2000w" })],
  });
  const row = feedRow(entry(ev), ctx(), T0 + 400, true);
  assert.equal(row.title, "Bouilloire");
  assert.equal(row.tag, "known");
  assert.equal(row.todo, false);
  assert.equal(row.ignorable, null);
  assert.match(row.sub, /a fonctionné 40 s · reconnu à 96 %/);
  // an off-step whose switch-on was not seen
  const off = feedRow(entry(event(4, { delta_w: -1950, running: false, candidates: ev.candidates })), ctx(), T0 + 400, true);
  assert.equal(off.title, "Bouilloire : arrêt");
  assert.equal(off.canLabel, false);
  // labelled by the user
  const mine = feedRow(entry(event(1)), ctx(), T0 + 30, true, { name: "Grille-pain", effect: "…" });
  assert.equal(mine.title, "Grille-pain");
  assert.equal(mine.tag, "labelled");
  assert.equal(mine.canLabel, false);
});

test("a provisional step, a session and the solar flag", () => {
  const row = feedRow(
    entry(event(5, { state: "provisional", pv_moved: true, session: { count: 8, steps: 16, on_s: 30, period_s: 45, duty: 0.6, confirmed: true } })),
    ctx(),
    T0 + 410,
    true,
  );
  assert.equal(row.provisional, true);
  assert.match(row.sub, /provisoire · s'allume et s'éteint \(8 fois en 6 min/);
  assert.match(row.sub, /le solaire a bougé en même temps/);
});

test("the structured reasons of the candidates, translated", () => {
  const ev = event(2, { power_w: 2080 });
  assert.equal(
    candidateReason(sigCand("medium-2100w", 0.82), ev, ctx()),
    "Déjà signalé 23 fois, sans nom · même puissance (2,10 kW) · d'habitude à cette heure · 3,5 fois par semaine",
  );
  const far = sigCand("medium-2100w", 0.4, { reason: { power_w: 1100, distance: 1.4, usual_hour: false, per_week: 0, expected: true, pv_moved: true } });
  assert.equal(candidateReason(far, ev, ctx(en, infoEn)), "Seen 23 times, without a name · not recognised at night yet · usually 1.10 kW · the solar output moved at the same time");
  const sw: LiveCandidate = {
    source: "switch",
    entity_id: "switch.water_heater",
    label: "Water heater relay",
    appliance: "load:switch.water_heater",
    power_w: 2400,
    score: 0.95,
    reason: { switch: "switch.water_heater", seconds: -4, pv_moved: false },
  };
  assert.equal(candidateReason(sw, ev, ctx()), "Relais chauffe-eau a changé 4 s avant");
  assert.equal(candidateName(sw, ctx()), "Chauffe-eau");
});

test("the Now card: running events, measured devices, switches, the rest; the split", () => {
  const events = new Map([
    [1, entry(event(1))],
    [2, entry(event(2, { power_w: 2080, candidates: [sigCand("medium-2100w", 0.82)] }))],
  ]);
  const items = nowItems(
    {
      ts: T0 + 60,
      house_w: 3300,
      running: [1, 2],
      unidentified_w: 300,
      submeters: [
        { entity_id: "sensor.fridge_power", name: "Fridge", appliance: null, estimated: false, power_w: 70 },
        { entity_id: "sensor.tv_power", name: "TV", appliance: null, estimated: false, power_w: 0 },
      ],
      switches: [
        { entity_id: "switch.water_heater", name: "Water heater", appliance: "load:switch.water_heater", on: true },
        { entity_id: "switch.pump", name: "Pump", appliance: null, on: false },
      ],
    },
    events,
    ctx(),
  );
  assert.deepEqual(
    items.map((i) => [i.name, i.power, i.dashed]),
    [
      ["Inconnu, +850 W", 850, true],
      ["Appareil de 2,1 kW, ~15 min, toute la journée", 2080, true],
      ["Fridge", 70, false],
      ["Chauffe-eau", null, false],
      ["Fond et non identifié", 300, false],
    ],
  );
  const split = nowSplit(items);
  assert.equal(split.length, 4);
  assert.ok(Math.abs(split.reduce((a, p) => a + p.share, 0) - 1) < 1e-9);
  assert.deepEqual(nowItems(null, events, ctx()), []);
});

test("the label plan: a named candidate, an unnamed signature with a name, a new or existing name", () => {
  const ev = event(2, {
    power_w: 2080,
    running: false,
    t_off: T0 + 600,
    candidates: [sigCand("medium-2100w", 0.82), sigCand("short-2000w", 0.3, { appliance: "signature:short-2000w" })],
  });
  // nothing chosen
  const none = labelPlan(ev, blankLabelForm(), ctx());
  assert.equal(none.kind, "none");
  assert.equal(planMissing(none), "live_need_choice");
  assert.equal(planEffect(none, ev, ctx()), "Choisissez une proposition ou tapez un nom.");
  // a named candidate: its appliance
  const named = labelPlan(ev, { ...blankLabelForm(), pick: 1 }, ctx());
  assert.deepEqual(named, { kind: "appliance", key: "signature:short-2000w", name: "Bouilloire", signature_id: "short-2000w" });
  // an unnamed signature: it needs a name, then it is named
  const unnamed = labelPlan(ev, { ...blankLabelForm(), pick: 0 }, ctx());
  assert.equal(unnamed.kind, "none");
  assert.equal(planMissing(unnamed), "live_need_name");
  assert.match(planEffect(unnamed, ev, ctx()), /^Donnez un nom à cette signature/);
  const sig = labelPlan(ev, { ...blankLabelForm(), pick: 0, name: "Four" }, ctx());
  assert.equal(sig.kind, "signature");
  assert.match(planEffect(sig, ev, ctx()), /devient « Four » : ses 23 utilisations passées/);
  assert.deepEqual(labelMessage(sig as Exclude<typeof sig, { kind: "none" }>, entry(ev)), {
    type: "energy_mapper/live/label",
    target: "signature",
    signature_id: "medium-2100w",
    name: "Four",
    event_id: 2,
    session: 4,
    source: "live",
    t_on: ev.t_on,
    t_off: T0 + 600,
    power_w: 2080,
  });
  // a name that exists: that appliance
  assert.deepEqual(labelPlan(ev, { ...blankLabelForm(), name: " bouilloire " }, ctx()), {
    kind: "appliance",
    key: "signature:short-2000w",
    name: "Bouilloire",
  });
  // a new name, tracked by a switch: the entity is required
  const fresh = labelPlan(ev, { ...blankLabelForm(), name: "Grille-pain", track: "switch" }, ctx());
  assert.equal(planMissing(fresh), "live_need_entity");
  const curve = labelPlan(ev, { ...blankLabelForm(), name: "Grille-pain" }, ctx());
  assert.match(planEffect(curve, ev, ctx()), /crée l'appareil « Grille-pain » et sa signature/);
  const running = event(1);
  assert.match(planEffect(curve, running, ctx()), /Sa signature suit à l'arrêt, ou cette nuit/);
  const msg = labelMessage(curve as Exclude<typeof curve, { kind: "none" }>, entry(running, null));
  assert.equal(msg.target, "new");
  assert.equal(msg.track, "curve");
  assert.equal("t_off" in msg, false);
  assert.equal("session" in msg, false);
});

test("the sentence after saving, from the effect", () => {
  const base = { appliance: { key: "k", name: "Four" }, signature: null, example: null, open: false, confirmed: false, other_appliance: null };
  assert.equal(
    resultEffect({ ...base, effect: "appliance_created", signature: { id: "x", past_uses: 0, created: true } }, fr, info),
    "Appareil « Four » créé. Sa signature est prête : il est reconnu dès la prochaine analyse.",
  );
  assert.equal(resultEffect({ ...base, effect: "appliance_created", open: true }, fr, info), "Appareil « Four » créé. L'exemple sera complété à l'arrêt, ou cette nuit.");
  assert.equal(
    resultEffect({ ...base, effect: "signature_named", signature: { id: "x", past_uses: 23, created: false } }, fr, info),
    "Signature nommée « Four » : 23 utilisations passées rattachées.",
  );
  assert.equal(resultEffect({ ...base, effect: "example_added", confirmed: true }, en, infoEn), "Confirmed: example added to “Four”.");
  assert.match(resultEffect({ ...base, effect: "example_added", other_appliance: { key: "o", name: "Hob" } }, en, infoEn), /thought it was “Hob”/);
  assert.equal(resultEffect({ ...base, appliance: null, effect: "signature_ignored" }, fr, info), "Ce type de variation ne vous sera plus proposé.");
  assert.equal(labelError({ code: "unauthorized", message: "x" }, fr), "Seul un administrateur peut étiqueter.");
  assert.equal(labelError({ code: "whatever", message: "boom" }, fr), "boom");
});

test("every status reason has its message", () => {
  for (const reason of ["disabled", "starting", "overloaded", "error", "addon_unavailable", "addon_too_old", "not_loaded"]) {
    const v = statusView({ type: "status", available: false, reason, retry_at: T0, min_version: "0.12.0", addon_version: "0.11.0", error: { message: "boom" } }, false, fr, info);
    assert.ok(v && v.title && v.text, reason);
    assert.doesNotMatch(v.text, /\{/, reason);
  }
  assert.match(statusView({ type: "status", available: false, reason: "addon_too_old", min_version: "0.12.0", addon_version: "0.11.0" }, false, en, infoEn)!.text, /0\.12\.0 or later \(installed: 0\.11\.0\)/);
  assert.equal(statusView(null, true, fr, info)?.title, "Connexion…");
  assert.equal(statusView(null, false, fr, info), null);
});
