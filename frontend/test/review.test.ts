import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../src/i18n.ts";
import {
  blankForm,
  currentOf,
  exampleDrawing,
  filterCounts,
  filterOf,
  formForMerge,
  formForUse,
  nextAfter,
  reviewProgress,
  reviewQueue,
  shortcutFor,
  shortcutLetters,
  stepFrom,
  typingTarget,
  bestSuggestion,
  drivesLoad,
  entityChoices,
  exampleWindow,
  formatHours,
  hasReview,
  formForSuggestion,
  initialForm,
  linkable,
  reviewGroups,
  suggestionLabel,
  suggestionMode,
  usualHours,
  validateMessage,
} from "../src/review.ts";
import type { EntitySuggestion, ReviewListing, ReviewSignature } from "../src/types.ts";

function sig(id: string, state: ReviewSignature["review_state"]): ReviewSignature {
  return {
    id,
    kind: "steady",
    band: "short",
    label: null,
    power_w: 2000,
    duration_s: 150,
    on_s: null,
    period_s: null,
    duty: null,
    weight: 5,
    count: 8,
    days_seen: 4,
    per_week: 2,
    first_seen: "2027-03-01",
    last_seen: "2027-03-09",
    hours: Array(24).fill(0),
    energy_kwh: 0.7,
    review_state: state,
  };
}

function listing(...sigs: ReviewSignature[]): ReviewListing {
  return {
    criteria: { min_uses: 5, min_days: 3, wait_days: 14, wait_factor: 2 },
    now: 0,
    counts: {},
    signatures: sigs,
  };
}

test("review groups keep the add-on's order within a state", () => {
  const g = reviewGroups(
    listing(sig("a", "proposed"), sig("w", "waiting"), sig("b", "proposed"), sig("i", "ignored"), sig("v", "validated")),
  );
  assert.deepEqual(
    [g.proposed, g.waiting, g.ignored].map((x) => x.map((s) => s.id)),
    [["a", "b"], ["w"], ["i"]],
  );
  assert.equal(hasReview(g), true);
  assert.equal(hasReview(reviewGroups(null)), false);
  assert.equal(hasReview(reviewGroups(listing(sig("v", "validated")))), false);
});

test("usual hours: busiest ranges first, across midnight", () => {
  const h = Array(24).fill(0);
  h[7] = 4;
  h[8] = 3;
  h[19] = 5;
  h[12] = 1; // below half of the busiest hour
  assert.deepEqual(usualHours(h), [
    [7, 8],
    [19, 19],
  ]);
  assert.equal(formatHours(usualHours(h)), "7–9 h, 19 h");
  const night = Array(24).fill(0);
  night[23] = 2;
  night[0] = 2;
  night[1] = 1.5;
  assert.deepEqual(usualHours(night), [[23, 1]]);
  assert.equal(formatHours(usualHours(night)), "23–2 h");
  assert.deepEqual(usualHours(Array(24).fill(1)), [[0, 23]]);
  assert.deepEqual(usualHours(undefined), []);
  assert.deepEqual(usualHours(Array(24).fill(0)), []);
  // at most three ranges
  const many = Array(24).fill(0).map((_, i) => (i % 2 ? 1 : 0));
  assert.equal(usualHours(many).length, 3);
});

test("example window stays inside the day", () => {
  assert.deepEqual(exampleWindow({ day: "x", start: 1000, end: 1100, power_w: 1 }, 0, 86400), [0, 2300 * 1000]);
  assert.deepEqual(exampleWindow({ day: "x", start: 86000, end: 86000, power_w: 1 }, 0, 86400), [
    84800 * 1000,
    86400 * 1000,
  ]);
});

test("validate message: a name, and an entity of the right kind for a link", () => {
  const s = sig("cyc-1400w", "proposed");
  assert.deepEqual(validateMessage(s, "new", "  Hob ", ""), {
    msg: { type: "energy_mapper/validate", signature_id: "cyc-1400w", name: "Hob" },
  });
  assert.deepEqual(validateMessage(s, "new", " ", ""), { missing: "name" });
  assert.deepEqual(validateMessage(s, "switch", "Radiator", "switch.radiator"), {
    msg: { type: "energy_mapper/validate", signature_id: "cyc-1400w", name: "Radiator", entity_id: "switch.radiator" },
  });
  assert.deepEqual(validateMessage(s, "switch", "Radiator", "sensor.x"), { missing: "entity" });
  assert.deepEqual(validateMessage(s, "sensor", "Kettle", ""), { missing: "entity" });
  assert.equal("msg" in validateMessage(s, "sensor", "Kettle", "sensor.kettle_power"), true);
});

test("entity choices of the fallback picker", () => {
  const states = {
    "switch.b": { entity_id: "switch.b", attributes: { friendly_name: "B relay" } },
    "switch.a": { entity_id: "switch.a", attributes: { friendly_name: "A relay" } },
    "sensor.p": { entity_id: "sensor.p", attributes: { friendly_name: "Power", device_class: "power" } },
    "sensor.t": { entity_id: "sensor.t", attributes: { device_class: "temperature" } },
    "sensor.w": { entity_id: "sensor.w", attributes: { friendly_name: "Washer state", device_class: "enum" } },
    "automation.x": { entity_id: "automation.x" },
  };
  assert.deepEqual(
    entityChoices(states, "switch").map((e) => e.id),
    ["switch.a", "switch.b", "sensor.w"],
  );
  assert.deepEqual(entityChoices(states, "sensor"), [{ id: "sensor.p", name: "Power" }]);
  assert.deepEqual(entityChoices(states, "new"), []);
  assert.deepEqual(entityChoices(undefined, "switch"), []);
});

test("review strings exist in French", () => {
  const fr = translator("fr");
  assert.equal(fr("review"), "À examiner");
  assert.equal(fr("review_uses", { count: "8", days: "4" }), "8 utilisations sur 4 jours");
  for (const mode of ["new", "switch", "sensor", "merge"]) {
    assert.notEqual(fr(`rq_track_${mode}` as never), translator("en")(`rq_track_${mode}` as never));
  }
});

function suggestion(entity_id: string, score: number, on_states = ["on"]): EntitySuggestion {
  return {
    entity_id,
    score,
    on_share: score,
    off_share: score,
    explained: 0.9,
    matches: 11,
    steps: 12,
    days: 4,
    on_states,
    inverted: on_states[0] === "off",
  };
}

test("suggested entities pre-fill the validation form (#41)", () => {
  const plain = sig("a", "proposed");
  assert.equal(bestSuggestion(plain), undefined);
  assert.deepEqual(initialForm(plain), { id: "a", mode: "new", name: "", entity: "", appliance: "" });
  const s = { ...plain, suggestions: [suggestion("fan.attic", 0.92), suggestion("switch.pilot", 0.85, ["off"])] };
  assert.equal(bestSuggestion(s)?.entity_id, "fan.attic");
  assert.deepEqual(
    initialForm(s, (e) => (e === "fan.attic" ? "Attic fan" : undefined)),
    { id: "a", mode: "switch", name: "Attic fan", entity: "fan.attic", appliance: "" },
  );
  assert.deepEqual(initialForm(s), { id: "a", mode: "switch", name: "", entity: "fan.attic", appliance: "" });
});

test("validate message: the on-states of a suggested entity", () => {
  const washer = suggestion("sensor.washer_state", 0.9, ["rinsing", "running"]);
  const s = { ...sig("a", "proposed"), suggestions: [suggestion("fan.attic", 0.92), suggestion("switch.pilot", 0.85, ["off"]), washer] };
  // "on" is the default: not sent
  assert.deepEqual(validateMessage(s, "switch", "Fan", "fan.attic"), {
    msg: { type: "energy_mapper/validate", signature_id: "a", name: "Fan", entity_id: "fan.attic" },
  });
  assert.deepEqual(validateMessage(s, "switch", "Radiator", "switch.pilot"), {
    msg: { type: "energy_mapper/validate", signature_id: "a", name: "Radiator", entity_id: "switch.pilot", on_states: ["off"] },
  });
  // an operation-state sensor is linked like a switch, with its states
  assert.deepEqual(validateMessage(s, "switch", "Washer", "sensor.washer_state", { device_class: "enum" }), {
    msg: {
      type: "energy_mapper/validate",
      signature_id: "a",
      name: "Washer",
      entity_id: "sensor.washer_state",
      on_states: ["rinsing", "running"],
    },
  });
  // another entity: the add-on's automatic polarity
  assert.deepEqual(validateMessage(s, "switch", "Other", "light.other"), {
    msg: { type: "energy_mapper/validate", signature_id: "a", name: "Other", entity_id: "light.other" },
  });
});

test("which entities can be linked", () => {
  assert.equal(linkable("switch", "media_player.tv"), true);
  assert.equal(linkable("switch", "sensor.w", { device_class: "enum" }), true);
  assert.equal(linkable("switch", "sensor.p", { device_class: "power" }), false);
  assert.equal(linkable("sensor", "sensor.p", { device_class: "power" }), true);
  assert.equal(linkable("sensor", "sensor.w", { device_class: "enum" }), false);
  assert.equal(linkable("new", "switch.a"), false);
  assert.equal(linkable("switch", "automation.x"), false);
});

test("suggestion strings exist in French", () => {
  const fr = translator("fr");
  assert.equal(fr("review_probably", { name: "Ventilateur", score: "92 %" }), "Probablement : Ventilateur (92 %)");
  assert.notEqual(fr("review_unrecorded", { domains: "x" }), translator("en")("review_unrecorded", { domains: "x" }));
});

function contained(entity_id: string, extra: Partial<EntitySuggestion> = {}): EntitySuggestion {
  return {
    entity_id,
    attribute: "hvac_action",
    match: "containment",
    score: 0.97,
    matches: 80,
    sessions: 80,
    time_share: 0.3,
    used: 0.8,
    chance: 1e-30,
    days: 20,
    on_states: ["heating"],
    inverted: false,
    ...extra,
  };
}

test("attribute and containment suggestions name an appliance or merge it (#43)", () => {
  const friendly = (e: string) => (e === "climate.room" ? "Room" : undefined);
  const attr = contained("climate.room");
  assert.equal(drivesLoad(attr), false);
  assert.equal(drivesLoad(suggestion("fan.attic", 0.9)), true);
  assert.equal(drivesLoad({ ...suggestion("switch.pilot", 0.9), match: "containment" }), false);
  assert.equal(suggestionLabel(attr, friendly), "Room (heating)");
  assert.equal(suggestionLabel(suggestion("fan.attic", 0.9), friendly), "fan.attic");
  // not linked to an appliance: a new appliance named after it
  assert.equal(suggestionMode(attr), "new");
  assert.deepEqual(initialForm({ id: "a", suggestions: [attr] }, friendly), {
    id: "a",
    mode: "new",
    name: "Room",
    entity: "",
    appliance: "",
  });
  // part of an appliance: merge into it
  const part = contained("climate.room", { appliance: { key: "load:switch.radiator", name: "Radiator", via: "device" } });
  assert.equal(suggestionMode(part), "merge");
  assert.deepEqual(formForSuggestion("a", part, friendly), {
    id: "a",
    mode: "merge",
    name: "Room",
    entity: "",
    appliance: "load:switch.radiator",
  });
  // an attribute is never sent as the on-states of a switch
  const s = { id: "a", suggestions: [contained("switch.pilot", { attribute: undefined, on_states: ["off"] })] };
  assert.deepEqual(validateMessage(s, "switch", "Pilot", "switch.pilot"), {
    msg: { type: "energy_mapper/validate", signature_id: "a", name: "Pilot", entity_id: "switch.pilot" },
  });
});

test("merge message (#43)", () => {
  const s = { id: "a", suggestions: [] };
  assert.deepEqual(validateMessage(s, "merge", "", "", undefined, "load:switch.radiator"), {
    msg: { type: "energy_mapper/merge_signature", signature_id: "a", appliance: "load:switch.radiator" },
  });
  assert.deepEqual(validateMessage(s, "merge", "x", "", undefined, ""), { missing: "appliance" });
  const fr = translator("fr");
  assert.equal(fr("rq_merge_hint", { name: "Radiateur" }), "Ajouter cette signature à Radiateur");
  assert.notEqual(fr("review_mode_merge_hint"), translator("en")("review_mode_merge_hint"));
});

// -- one signature at a time (#51) ----------------------------------------

function withEnergy(id: string, state: ReviewSignature["review_state"], energy: number, extra: Partial<ReviewSignature> = {}): ReviewSignature {
  return { ...sig(id, state), energy_kwh: energy, weighted_energy_kwh: energy, ...extra };
}

const ids = (list: Array<{ id: string }>) => list.map((s) => s.id);

test("review queue: most energy first, per filter", () => {
  const l = listing(
    withEnergy("small", "proposed", 1),
    withEnergy("big", "proposed", 9, { suggestions: [suggestion("switch.x", 0.9)] }),
    withEnergy("mid", "proposed", 4),
    withEnergy("w1", "waiting", 2),
    withEnergy("w2", "waiting", 5),
    withEnergy("i1", "ignored", 3),
    withEnergy("v", "validated", 99),
  );
  assert.deepEqual(ids(reviewQueue(l, "energy")), ["big", "mid", "small"]);
  assert.deepEqual(ids(reviewQueue(l, "suggested")), ["big"]);
  assert.deepEqual(ids(reviewQueue(l, "waiting")), ["w2", "w1"]);
  assert.deepEqual(ids(reviewQueue(l, "ignored")), ["i1"]);
  assert.deepEqual(reviewQueue(null, "energy"), []);
  assert.deepEqual(filterCounts(l), { energy: 3, suggested: 1, waiting: 2, ignored: 1 });
});

test("review queue: ties are broken by weighted energy, then id", () => {
  const l = listing(
    withEnergy("b", "proposed", 2, { weighted_energy_kwh: 1 }),
    withEnergy("a", "proposed", 2, { weighted_energy_kwh: 1 }),
    withEnergy("c", "proposed", 2, { weighted_energy_kwh: 1.5 }),
  );
  assert.deepEqual(ids(reviewQueue(l, "energy")), ["c", "a", "b"]);
});

test("the filter that lists a signature", () => {
  assert.equal(filterOf(sig("a", "proposed")), "energy");
  assert.equal(filterOf(sig("a", "waiting")), "waiting");
  assert.equal(filterOf(sig("a", "ignored")), "ignored");
  assert.equal(filterOf(sig("a", "validated")), null);
  assert.equal(filterOf(sig("a", "immature")), null);
  assert.equal(filterOf(undefined), null);
});

test("current signature and the stepper", () => {
  const q = [{ id: "a" }, { id: "b" }, { id: "c" }];
  assert.equal(currentOf(q, "b"), "b");
  assert.equal(currentOf(q, "gone"), "a");
  assert.equal(currentOf(q, null), "a");
  assert.equal(currentOf([], "a"), null);
  assert.equal(stepFrom(q, "b", 1), "c");
  assert.equal(stepFrom(q, "b", -1), "a");
  assert.equal(stepFrom(q, "c", 1), null);
  assert.equal(stepFrom(q, "a", -1), null);
  assert.equal(stepFrom(q, "gone", 1), "a");
});

test("next selection after an action", () => {
  const q = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }];
  assert.equal(nextAfter(q, "b", ["b"]), "c");
  assert.equal(nextAfter(q, "d", ["d"]), "c");
  assert.equal(nextAfter(q, "b", ["b", "c"]), "d");
  assert.equal(nextAfter(q, "c", ["c", "d"]), "b");
  // a bulk action on other rows keeps the current one
  assert.equal(nextAfter(q, "a", ["c", "d"]), "a");
  assert.equal(nextAfter(q, "a", ["a", "b", "c", "d"]), null);
  assert.equal(nextAfter(q, null, ["a"]), "b");
});

test("review progress: the share of the energy explained, with the registry", () => {
  const l = { ...listing(withEnergy("p", "proposed", 3), withEnergy("w", "waiting", 1), withEnergy("i", "ignored", 2)), counts: { validated: 2 } };
  const registry = {
    signatures: [
      { label: "Oven", appliance: null, energy_kwh: 4 },
      { label: null, appliance: "load:switch.x", energy_kwh: 2 },
      { label: null, appliance: null, energy_kwh: 50 }, // immature: not counted
    ],
  };
  const p = reviewProgress(l, registry);
  assert.deepEqual({ ...p, share: Math.round((p.share ?? 0) * 100) }, {
    named: 2,
    toReview: 1,
    waiting: 1,
    ignored: 1,
    share: 50,
    basis: "energy",
  });
});

test("review progress: the share of the signatures handled, without the registry", () => {
  const l = { ...listing(sig("p", "proposed"), sig("w", "waiting"), sig("i", "ignored")), counts: { validated: 1 } };
  const p = reviewProgress(l, null);
  assert.equal(p.basis, "count");
  assert.equal(p.share, 0.5); // (1 named + 1 ignored) / 4
  assert.equal(reviewProgress(listing(), null).share, null);
  // no energy at all: falls back to the count
  assert.equal(reviewProgress({ ...listing(withEnergy("p", "proposed", 0)), counts: { validated: 0 } }, { signatures: [] }).basis, "count");
});

test("keyboard shortcuts of the review", () => {
  const k = (key: string, mods: Partial<KeyboardEvent> = {}) =>
    shortcutFor({ key, ctrlKey: false, metaKey: false, altKey: false, ...mods } as KeyboardEvent);
  assert.equal(k("v"), "validate");
  assert.equal(k("V"), "validate");
  assert.equal(k("w"), "wait");
  assert.equal(k("a"), "wait");
  assert.equal(k("i"), "ignore");
  assert.equal(k("ArrowLeft"), "prev");
  assert.equal(k("ArrowRight"), "next");
  assert.equal(k("x"), null);
  assert.equal(k("v", { ctrlKey: true }), null);
  assert.equal(k("a", { metaKey: true }), null);
  assert.deepEqual(shortcutLetters("en"), { validate: "V", wait: "W", ignore: "I" });
  assert.deepEqual(shortcutLetters("fr-CA"), { validate: "V", wait: "A", ignore: "I" });
});

test("no shortcut while typing", () => {
  const el = (tagName: string, extra: Record<string, unknown> = {}) => ({ tagName, getAttribute: () => null, ...extra }) as unknown as EventTarget;
  assert.equal(typingTarget([el("INPUT", { type: "text" }), el("DIV")]), true);
  assert.equal(typingTarget([el("INPUT", { type: "checkbox" }), el("DIV")]), false);
  assert.equal(typingTarget([el("SELECT")]), true);
  assert.equal(typingTarget([el("TEXTAREA")]), true);
  assert.equal(typingTarget([el("DIV"), el("HA-ENTITY-PICKER")]), true);
  assert.equal(typingTarget([el("DIV", { isContentEditable: true })]), true);
  assert.equal(typingTarget([el("BUTTON", { getAttribute: (n: string) => (n === "role" ? "tab" : null) })]), true);
  assert.equal(typingTarget([el("BUTTON"), el("SECTION")]), false);
  assert.equal(typingTarget([{} as EventTarget]), false); // window, document: no tag
});

test("Use and Merge on a suggestion (#51)", () => {
  const edge = suggestion("switch.oven_plug", 0.92);
  assert.deepEqual(formForUse("a", edge, () => "Oven plug"), {
    id: "a",
    mode: "switch",
    name: "Oven plug",
    entity: "switch.oven_plug",
    appliance: "",
  });
  const contained: EntitySuggestion = {
    ...suggestion("climate.room", 0.97, ["heating"]),
    attribute: "hvac_action",
    match: "containment",
    appliance: { key: "load:switch.radiator", name: "Radiator", via: "device" },
  };
  assert.deepEqual(formForUse("a", contained, () => "Room"), { id: "a", mode: "new", name: "Room", entity: "", appliance: "" });
  assert.deepEqual(formForMerge("a", contained), { id: "a", mode: "merge", name: "", entity: "", appliance: "load:switch.radiator" });
  assert.equal(formForMerge("a", edge), null);
  assert.deepEqual(blankForm("a"), { id: "a", mode: "new", name: "", entity: "", appliance: "" });
});

test("an example drawn on the house curve", () => {
  const curve = Array.from({ length: 200 }, (_, i) => ({ t: 1000 + i * 60, house: i >= 100 && i < 110 ? 2500 : 300 }));
  const ex = { day: "2027-03-09", start: 1000 + 100 * 60, end: 1000 + 110 * 60, power_w: 2200 };
  const d = exampleDrawing(curve, ex, 1000, 1000 + 200 * 60, 200, 56);
  assert.ok(d);
  assert.ok(d.path.startsWith("M"));
  assert.ok(d.area.endsWith("Z"));
  assert.ok(d.from > 0 && d.to > d.from && d.to < 200);
  // the use is in the middle of its window (margins of 20 min on both sides)
  assert.ok(Math.abs(d.from + d.to - 200) < 2);
  assert.equal(exampleDrawing([], ex, 1000, 13000), null);
});

test("queue strings exist in French", () => {
  const fr = translator("fr");
  const en = translator("en");
  for (const key of ["rq_filter_energy", "rq_validate_next", "rq_how", "rq_track_new", "rq_step", "rq_key_wait"] as const) {
    assert.notEqual(fr(key), en(key));
  }
  assert.equal(fr("rq_step", { pos: "1", count: "34" }), "1 sur 34");
});
