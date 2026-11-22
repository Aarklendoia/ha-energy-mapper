# Changelog

## 0.11.0

- Findings (#34): each finding keeps its stable `kind` and gets structured
  `params` (the device `name`, the `source` of a missing power sensor:
  `grid`, `solar` or `device`, the `declared_kwh` and `measured_kwh` of a
  tracking gap), so the panel (integration 0.9.0) writes it in the user's
  language. `detail` stays, in English, as the fallback for older panels.
- Learn an appliance from a measured device (#14): an appliance pushed with a
  `teacher` (`{"entity_id": <power sensor>, "since"}`, a temporary smart plug)
  is learned from it. The sensor is never subtracted while it teaches (the
  appliance stays in the whole-house curve, as after the plug is gone; an
  Energy dashboard device gets the role `teacher`), and each analysed day keeps
  its runs in a new report field `ground_truth`: start, end, energy, power,
  kind (`steady`, `cycling` or `multiphase`, with the phases) and the periods
  the sensor reported.
- From these runs the add-on learns one signature of the existing kinds
  (steady power and duration band, cycling on-time and period, or the phase
  template of the most frequent programme), finds the registry signature it is
  and measures the recognition from the curve against the plug over the
  learning days: precision, recall, F1 and energy ratio, per day and in all.
  New `GET /api/teaching` (status) and `POST /api/teaching` (the signature to
  link, created as an expected signature when the registry has none; a
  multi-phase one with the learned template, so its cycles are recognised at
  once).
- The ground truth and the evaluation are a pure module of the engine
  (`nilm_core/groundtruth.py`), for the nightly self-test (#15) too.

## 0.10.0

- Settings (#33): `submeters_mode` `manual` makes `submeters` the exact list
  of sub-meters, possibly empty: "no sub-meters" now subtracts nothing
  instead of falling back to the Energy dashboard (it also overrides the
  add-on option). A `submeters` list alone is stored with the mode.
- Settings (#33): new `excluded_submeters`, automatic sub-meters (Energy
  dashboard or add-on option) that are not subtracted; their power stays in
  the residual and the tracked device gets the role `excluded`. Not accepted
  together with manual sub-meters.
- Integration 0.8.0 uses them: the options show which sub-meters and switched
  loads come from the Energy dashboard and which are user changes, and
  removing an automatic one from a list excludes it.
- Multi-phase appliances, first version (#26): dishwashers, washing machines
  and tumble dryers. Around each heating-like activation (steady, at least
  1 kW, 5 min to 2 h), the minutes where the load stays at least 50 W above
  the base (a local threshold far below the step threshold) are grouped into
  a candidate cycle and split into phases (heating, motor). Reports list them
  in a new `cycles` field.
- Signature registry: recurring candidate cycles (at least 3, clustered on a
  distance between their phase sequences) become signatures of kind
  `multiphase` (band `cycle`, id `cycle-<heating power>w`) with their
  template in `phases` and the typical energy of one cycle in `cycle_kwh`.
  They are proposed in the review like the other signatures.
- Once such a signature belongs to an appliance (named, or validated in the
  review), the nightly analysis recognises its cycles: each one becomes one
  activation (`multiphase: true`, its phases, its exact per-minute power)
  that replaces the activations inside it (its heating phases), so its
  energy, low-power phases included, is counted once. Before that, nothing
  changes in the attribution.
- An appliance can declare a `multiphase` signature as a source.

## 0.9.0

- Signature registry: each signature keeps its `occurrences` of the last 60
  days, `[start, duration_s, power_w]` (unix time, seconds, watts, integers),
  sorted by start, at most 600 (the most recent ones). They feed the day
  raster of the To review tab of the panel (#65) and come with each
  signature in `GET /api/review` and `GET /api/signatures`. Merged clusters
  union their occurrences; a signature carried forward without stored
  activations keeps its own, pruned to the window.
- A registry written by an older version is updated at the next start,
  from the stored day summaries (about 200 days are kept), so the existing
  signatures get their occurrences at once.

## 0.8.1

- Reports: new `estimated`, the part of `submetered` that comes from
  estimated devices (an estimation integration's power sensor driven by a
  light or another entity), in the curve points, the day totals and the peaks
  (part `estimated`). They are still subtracted before the analysis like the
  sub-meters; the panel shows them apart (#44).

## 0.8.0

- Entity suggestions compare attributes too (#43): `hvac_action` of climate
  entities and `action` of humidifiers, read as if they were states (a
  thermostat stays `heat` all winter; `hvac_action` tells when it heats).
  Their history is read in a separate, bounded request with attributes.
- Containment scoring (#43): an entity that stays in a state for long
  periods while the load cycles inside them is suggested when the signature
  only runs in that state (`match: "containment"`: share of its on-time
  inside, sessions inside, chance level, share of the entity's on periods
  used). Random entities and entities "on" most of the time stay rejected.
- The switched loads and the entities linked to an appliance are candidates:
  a suggestion that belongs to an appliance (one of its sources, or the same
  Home Assistant device) carries `appliance`, and the panel of the
  integration 0.6.0 proposes to merge the signature into it.
- Changes files version 2 (with the state at midnight and the attributes);
  the files of the last 30 days written before are read again once.
- `GET /api/review`: `attribute_candidates` in `suggest`; suggestions have
  `match`, `attribute` and `appliance`.

## 0.7.0

- Entity suggestions for the signatures to review (#41), with the integration
  0.5.0: every night the signatures proposed (and waiting) are compared with
  the state changes of the Home Assistant entities that may tell when an
  appliance runs (lights, switches, input booleans, fans, climate, water
  heaters, media players, binary sensors, `enum` sensors). An entity whose
  state changes at 80 % of a signature's starts and stops (at least 5 times on
  3 days), and whose own changes mostly happen then, is suggested, with the
  states in which the appliance runs. `GET /api/review` gives `suggestions`
  per signature and `suggest` (with the candidate domains that are not
  recorded).
- The changes of the candidate entities are read once per day from the
  recorder and kept 31 days in `/data/changes`; the suggestions in
  `/data/suggestions.json`.
- New setting `review_suggest_score` (80).
- History requests for many entities are split, so that a request stays
  short.

## 0.6.0

- Review of the signatures in the Energy Mapper panel (#10), with the
  integration 0.4.0: new endpoints `GET /api/review` (the signatures seen
  often enough to be an appliance, waiting and ignored, with their
  `review_state`) and `POST /api/review` (`validate`, `wait`, `ignore`,
  `restore`). Decisions are stored in the registry (`review` of a signature)
  and follow its id; a waiting signature comes back after twice as many uses
  or 14 days; an ignored one is kept after it fades out (the 100 most recent)
  so that it is not proposed again.
- New settings `review_min_uses` (5), `review_min_days` (3),
  `review_wait_days` (14) and `review_wait_factor` (2).

## 0.5.0

- The user's appliances, managed in Home Assistant by the Energy Mapper
  integration 0.3.0 (one config subentry each, #11): new endpoints
  `GET /api/appliances` and `PUT /api/appliances`, stored in
  `/data/appliances.json`. Signature sources give their signature its name
  (`label`, and the appliance's key as `appliance`; labels no appliance
  declares are cleared, and a declared signature lost from the registry is
  recreated); switched-load sources are analysed as switched loads named after
  the appliance; sub-meter sources are subtracted before the analysis.
- New endpoint `POST /api/signatures`: an appliance declared before it is seen
  ("Kettle, 2000 W"): an *expected* signature, named and never forgotten, whose
  id is taken by the first cluster of its group within 12 % of its power.
- New setting `excluded_switched_loads` (#33): switches of the Energy dashboard
  that are not measured as switched loads (their estimate is subtracted like
  any other). The integration adds the switch of a deleted appliance.
- Tracking gaps compare a switched load by entity, so a renamed one is still
  checked.

## 0.4.1

- Automatic configuration: Energy Mapper's own statistics
  (`energy_mapper:load_…` / `energy_mapper:signature_…`) in the Energy
  dashboard are recognised (role `energy_mapper`). An appliance whose
  estimate was replaced by its Energy Mapper statistic is no longer reported
  as unavailable, is never subtracted, and its relay stays a switched load
  with its learned polarity, without manual settings (#36). The mapping
  statistic id -> appliance is kept in `/data/statistic_ids.json`; a
  statistic the add-on cannot map gets the finding `unknown_statistic`.

## 0.4.0

- New endpoint `GET /api/hourly/YYYY-MM-DD`: the energy of each appliance per
  UTC hour of a day (switched loads by entity id, signatures by registry id),
  computed from the report's per-minute curve. The Energy Mapper integration
  0.2.0 imports it into Home Assistant's long-term statistics.
- `GET /api/days` also answers `generated`: when each full report was made,
  so the integration sees new and re-analysed days.

## 0.3.0

- Settings pushed by the Energy Mapper integration: new endpoints
  `GET /api/settings` and `PUT /api/settings`, stored in
  `/data/settings.json`. They take precedence over the add-on options; with
  no settings pushed, the add-on options work as before.

## 0.2.0

- Signature registry learned across days, with progressive forgetting
  (`half_life_days`, 30 by default) and stable signature ids; new endpoint
  `GET /api/signatures`; daily reports served with registry ids.
- Switched loads: learned power and polarity kept across days.
- Peaks: every report lists the day's top episodes (relative to the day's
  maximum) and flags as `notable` those above 80 % of the maximum of the last
  `peak_window_days` days (or above `peak_threshold_w` when it is set).
- Bounded storage: full reports gzip-compressed and kept
  `report_retention_days` (90) days, day summaries kept while they still
  count; existing reports are compressed on start.

## 0.1.0

- First version: nightly analysis of the previous day, configured from the
  Energy dashboard (manual overrides in the options).
- Backfill of the past days still held by the recorder on first start.
- Reports stored as JSON in `/data/reports` (included in Home Assistant backups).
- Internal HTTP API for the Energy Mapper integration, announced through
  Supervisor discovery.
