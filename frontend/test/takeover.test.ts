import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../src/i18n.ts";
import { energyState, pickList, preselected, type TrackingListing } from "../src/takeover.ts";

// synthetic household (no real data)
function listing(): TrackingListing {
  return {
    devices: [
      { stat_consumption: "sensor.fridge_energy", name: "Fridge", included_in_stat: null },
      { stat_consumption: "sensor.boiler_estimate_energy", name: "Boiler", included_in_stat: null },
      { stat_consumption: "sensor.attic_energy", name: "Attic", included_in_stat: null },
    ],
    appliances: [
      {
        key: "load:switch.water_heater",
        statistic_id: "energy_mapper:load_switch_water_heater_energy",
        in_dashboard: false,
        taken_over: null,
        suggested: [{ stat_consumption: "sensor.boiler_estimate_energy", reason: "estimate" }],
      },
      {
        key: "signature:short-2000w",
        statistic_id: "energy_mapper:signature_short_2000w_energy",
        in_dashboard: false,
        taken_over: null,
        suggested: [],
      },
      {
        key: "load:switch.radiator",
        statistic_id: "energy_mapper:load_switch_radiator_energy",
        in_dashboard: true,
        taken_over: { stat_consumption: "sensor.radiator_estimate_energy", name: "Radiator estimate" },
        suggested: [],
      },
      {
        key: "load:switch.pump",
        statistic_id: "energy_mapper:load_switch_pump_energy",
        in_dashboard: true,
        taken_over: null,
        suggested: [],
      },
    ],
  };
}

test("the state of each card", () => {
  const l = listing();
  assert.deepEqual(energyState(l, "load:switch.water_heater"), { kind: "suggested", device: l.devices[1] });
  assert.deepEqual(energyState(l, "signature:short-2000w"), { kind: "available" });
  assert.deepEqual(energyState(l, "load:switch.radiator"), { kind: "taken_over", device: "Radiator estimate" });
  // added to the dashboard by hand: nothing to offer, nothing to undo
  assert.deepEqual(energyState(l, "load:switch.pump"), { kind: "tracked" });
  // a sensor-only appliance (not listed), no listing (not an admin, older integration)
  assert.deepEqual(energyState(l, "submeter:sensor.plug_power"), { kind: "none" });
  assert.deepEqual(energyState(null, "load:switch.water_heater"), { kind: "none" });
  // nothing to take over
  assert.deepEqual(energyState({ ...l, devices: [] }, "signature:short-2000w"), { kind: "none" });
  // a suggestion whose device is gone falls back to picking one
  const stale = { ...l, devices: l.devices.filter((d) => d.name !== "Boiler") };
  assert.deepEqual(energyState(stale, "load:switch.water_heater"), { kind: "available" });
});

test("the picker lists the suggestions first, then the others by name", () => {
  const l = listing();
  assert.deepEqual(
    pickList(l, "load:switch.water_heater").map((x) => [x.device.name, x.suggested]),
    [
      ["Boiler", true],
      ["Attic", false],
      ["Fridge", false],
    ],
  );
  assert.equal(preselected(l, "load:switch.water_heater"), "sensor.boiler_estimate_energy");
  // without a suggestion nothing is preselected: the user picks
  assert.equal(preselected(l, "signature:short-2000w"), "");
  assert.deepEqual(pickList(null, "x"), []);
});

test("the confirmation names both sides, in English and French", () => {
  const en = translator("en");
  const fr = translator("fr");
  const params = { device: "Boiler", name: "Water heater" };
  assert.match(en("takeover_confirm", params), /Replace Boiler with Water heater/);
  assert.match(fr("takeover_confirm", params), /Remplacer Boiler par Water heater/);
  assert.notEqual(en("takeover_action"), fr("takeover_action"));
});
