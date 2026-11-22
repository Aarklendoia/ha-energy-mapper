"""Tests of learning an appliance from a measured device (#14): the ground
truth of a power sensor (runs, kinds, coverage), the learned signature, the
evaluation of the curve-based recognition, the teacher in the add-on's
appliances and analysis, and the HTTP endpoints. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import random
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from api import route  # noqa: E402
from appliances import AppliancesError, analysis_options, validate  # noqa: E402
from energy_config import derive  # noqa: E402
from nilm_core import AnalysisConfig, analyze  # noqa: E402
from nilm_core.groundtruth import (  # noqa: E402
    CYCLING,
    MULTIPHASE,
    STEADY,
    Interval,
    Run,
    detections,
    evaluate,
    learn,
    merge_evaluations,
    runs_of,
)
from nilm_core.registry import Matcher, cycle_templates, update  # noqa: E402
from storage import summary_of  # noqa: E402
from synth import DAY0, HOUR, Cycling, House, Steady  # noqa: E402
from synth_cycles import dishwasher  # noqa: E402
from teaching import TeachingError, apply, day_truth, resolve, status  # noqa: E402
from test_addon import TODAY, FakeHA, make_analyzer, midnight, snapshot  # noqa: E402
from test_energy_config import energy, power  # noqa: E402

DAY = 24 * HOUR
GRID = "sensor.grid_power"
PLUG = "sensor.plug_power"
UTC_HOUR = lambda ts: int(ts // 3600) % 24  # noqa: E731


def series(load, start, end, dt=10.0, unavailable=()):
    """Readings of a plug measuring ``load`` (strings, as recorded)."""
    out, t = [], start
    while t < end:
        if any(a <= t < b for a, b in unavailable):
            out.append((t, None))
        else:
            out.append((t, round(load.power_at(t), 1)))
        t += dt
    return out


class Sum:
    def __init__(self, *loads):
        self.loads = loads

    def power_at(self, t):
        return sum(x.power_at(t) for x in self.loads)


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
class RunsTests(unittest.TestCase):
    def test_steady_run_and_coverage(self):
        kettle = Steady(DAY0 + HOUR, DAY0 + HOUR + 180, 2000)
        runs, covered = runs_of(series(kettle, DAY0 - HOUR, DAY0 + DAY), DAY0, DAY0 + DAY)
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual((r.kind, r.start, r.end), (STEADY, DAY0 + HOUR, DAY0 + HOUR + 180))
        self.assertEqual(round(r.power_w), 2000)
        self.assertAlmostEqual(r.energy_kwh, 0.1, places=3)
        self.assertEqual(covered, [(DAY0, DAY0 + DAY)])

    def test_state_holds_until_the_next_one(self):
        """Home Assistant records a state when it changes: two readings are a
        two-hour run."""
        points = [(DAY0 - HOUR, 0.0), (DAY0 + HOUR, 1500.0), (DAY0 + 3 * HOUR, 0.0)]
        runs, covered = runs_of(points, DAY0, DAY0 + DAY)
        self.assertEqual([(r.start, r.end) for r in runs], [(DAY0 + HOUR, DAY0 + 3 * HOUR)])
        self.assertAlmostEqual(runs[0].energy_kwh, 3.0)
        self.assertEqual(covered, [(DAY0, DAY0 + DAY)])

    def test_unavailable_is_not_covered(self):
        kettle = Steady(DAY0 + HOUR, DAY0 + HOUR + 180, 2000)
        gap = (DAY0 + 10 * HOUR, DAY0 + 12 * HOUR)
        _, covered = runs_of(series(kettle, DAY0, DAY0 + DAY, unavailable=[gap]), DAY0, DAY0 + DAY)
        self.assertEqual(covered, [(DAY0, gap[0]), (gap[1], DAY0 + DAY)])

    def test_standby_and_short_blips_are_not_runs(self):
        load = Sum(
            Steady(DAY0, DAY0 + DAY, 3),  # standby
            Steady(DAY0 + HOUR, DAY0 + HOUR + 30, 900),  # too short
            Steady(DAY0 + 2 * HOUR, DAY0 + 3 * HOUR, 35),  # never reaches min_use_w
        )
        runs, _ = runs_of(series(load, DAY0, DAY0 + DAY), DAY0, DAY0 + DAY)
        self.assertEqual(runs, [])

    def test_short_pauses_stay_in_one_run(self):
        load = Sum(
            Steady(DAY0 + HOUR, DAY0 + HOUR + 600, 2000), Steady(DAY0 + HOUR + 900, DAY0 + HOUR + 1500, 2000)
        )
        runs, _ = runs_of(series(load, DAY0, DAY0 + DAY), DAY0, DAY0 + DAY)
        self.assertEqual(len(runs), 1)
        far = Sum(
            Steady(DAY0 + HOUR, DAY0 + HOUR + 600, 2000), Steady(DAY0 + 3 * HOUR, DAY0 + 3 * HOUR + 600, 2000)
        )
        runs, _ = runs_of(series(far, DAY0, DAY0 + DAY), DAY0, DAY0 + DAY)
        self.assertEqual(len(runs), 2)

    def test_cycling_run(self):
        oven = Cycling(DAY0 + HOUR, DAY0 + HOUR + 2400, 2400, 120, 90)
        runs, _ = runs_of(series(oven, DAY0, DAY0 + DAY), DAY0, DAY0 + DAY)
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r.kind, CYCLING)
        self.assertEqual(round(r.power_w), 2400)
        self.assertAlmostEqual(r.on_s, 120, delta=10)
        self.assertAlmostEqual(r.period_s, 210, delta=10)
        self.assertAlmostEqual(r.duty, 0.57, delta=0.05)

    def test_multiphase_run(self):
        cycle = dishwasher(DAY0 + 10 * HOUR, random.Random(1))
        runs, _ = runs_of(series(cycle, DAY0, DAY0 + DAY), DAY0, DAY0 + DAY)
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r.kind, MULTIPHASE)
        self.assertEqual([p.kind for p in r.phases], ["motor", "heat", "motor", "heat", "motor"])
        self.assertAlmostEqual(r.energy_kwh, cycle.energy_kwh(), delta=0.02)

    def test_round_trip(self):
        cycle = dishwasher(DAY0 + 10 * HOUR, random.Random(1))
        runs, _ = runs_of(series(cycle, DAY0, DAY0 + DAY), DAY0, DAY0 + DAY)
        again = Run.from_dict(json.loads(json.dumps(runs[0].to_dict())))
        self.assertEqual(again.kind, MULTIPHASE)
        self.assertEqual(len(again.phases), len(runs[0].phases))


# ---------------------------------------------------------------------------
# learned signature
# ---------------------------------------------------------------------------
def run(kind, minutes, power, **extra):
    return Run(0.0, minutes * 60.0, power * minutes / 60000, power, power, kind, **extra)


class LearnTests(unittest.TestCase):
    def test_nothing(self):
        self.assertIsNone(learn([]))

    def test_steady_majority_band(self):
        runs = [run(STEADY, 40, 2400), run(STEADY, 45, 2350), run(STEADY, 50, 2450), run(STEADY, 3, 2400)]
        got = learn(runs)
        self.assertEqual((got.kind, got.band, round(got.power_w)), ("steady", "medium", 2400))
        self.assertEqual((got.uses, got.runs), (3, 4))
        self.assertTrue(got.recognisable)

    def test_below_the_step_threshold(self):
        got = learn([run(STEADY, 60, 120), run(STEADY, 50, 110)], step_threshold_w=300)
        self.assertFalse(got.recognisable)

    def test_cycling(self):
        runs = [run(CYCLING, 30, 1400, on_s=40, period_s=70, duty=0.57) for _ in range(3)] + [
            run(STEADY, 3, 2000)
        ]
        got = learn(runs)
        self.assertEqual((got.kind, got.band, got.on_s, got.period_s), ("cycling", "session", 40, 70))

    def test_multiphase_template(self):
        rnd = random.Random(2)
        runs = []
        for i in range(5):
            d0 = DAY0 + i * DAY
            cycle = dishwasher(d0 + 9 * HOUR, rnd)
            runs += runs_of(series(cycle, d0, d0 + DAY), d0, d0 + DAY)[0]
        got = learn(runs)
        self.assertEqual((got.kind, got.band, got.uses), ("multiphase", "cycle", 5))
        self.assertEqual([p.kind for p in got.phases], ["motor", "heat", "motor", "heat", "motor"])
        self.assertAlmostEqual(got.power_w, 2000, delta=200)
        self.assertGreater(got.cycle_kwh, 0.8)


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
class EvaluateTests(unittest.TestCase):
    def test_precision_recall(self):
        truth = [Interval(0, 100, 1.0), Interval(1000, 1100, 1.0), Interval(5000, 5100, 1.0)]
        detected = [
            Interval(10, 90, 0.8),  # found
            Interval(1050, 1300, 1.0),  # overlaps half of the shorter: found
            Interval(3000, 3100, 1.0),  # false
        ]
        got = evaluate(truth, detected)
        self.assertEqual((got["found"], got["missed"], got["correct"], got["false"]), (2, 1, 2, 1))
        self.assertAlmostEqual(got["precision"], 0.667, places=3)
        self.assertAlmostEqual(got["recall"], 0.667, places=3)
        self.assertAlmostEqual(got["f1"], 0.667, places=3)
        self.assertAlmostEqual(got["energy_ratio"], 0.933, places=3)

    def test_small_overlap_is_not_a_match(self):
        got = evaluate([Interval(0, 1000)], [Interval(900, 1200)])
        self.assertEqual((got["found"], got["false"]), (0, 1))

    def test_two_detections_of_one_use(self):
        got = evaluate([Interval(0, 1000)], [Interval(0, 400), Interval(500, 1000)])
        self.assertEqual((got["found"], got["correct"], got["precision"], got["recall"]), (1, 2, 1.0, 1.0))

    def test_uncovered_detections_are_left_out(self):
        got = evaluate(
            [Interval(0, 100)], [Interval(0, 100), Interval(5000, 5100)], covered_periods=[(0, 4000)]
        )
        self.assertEqual((got["detections"], got["precision"]), (1, 1.0))

    def test_empty_sides(self):
        got = evaluate([], [])
        self.assertEqual((got["precision"], got["recall"], got["f1"]), (None, None, None))
        got = evaluate([Interval(0, 100)], [])
        self.assertEqual((got["precision"], got["recall"]), (None, 0.0))

    def test_merge(self):
        a = evaluate([Interval(0, 100, 1)], [Interval(0, 100, 1)])
        b = evaluate([Interval(0, 100, 1)], [Interval(500, 600, 1)])
        got = merge_evaluations([a, b])
        self.assertEqual((got["uses"], got["found"], got["detections"], got["correct"]), (2, 1, 2, 1))
        self.assertEqual((got["precision"], got["recall"], got["f1"]), (0.5, 0.5, 0.5))
        self.assertEqual(got["energy_ratio"], 1.0)

    def test_detections_of_a_day(self):
        registry = {
            "power_tol": 0.12,
            "signatures": [
                {"id": "medium-2400w", "kind": "steady", "band": "medium", "power_w": 2400},
                {"id": "short-2000w", "kind": "steady", "band": "short", "power_w": 2000},
            ],
        }
        summary = {
            "activations": [
                {"start": 0, "end": 2400, "power_w": 2380},
                {"start": 5000, "end": 5180, "power_w": 2000},
            ],
            "cycles": [],
        }
        got = detections(summary, "medium-2400w", Matcher(registry).match)
        self.assertEqual([(d.start, d.end) for d in got], [(0, 2400)])
        self.assertAlmostEqual(got[0].energy_kwh, 2380 * 2400 / 3.6e6)


# ---------------------------------------------------------------------------
# end to end: a plug teaches an appliance, the curve recognises it
# ---------------------------------------------------------------------------
def teach_days(appliance_of, days=8, seed=3, templates=None):
    """Synthetic days with a taught appliance (``appliance_of(day0, rnd)``
    returns its load or None) and distractors: the summaries with their
    ground truth, as the add-on stores them (the plug is not subtracted)."""
    rnd = random.Random(seed)
    summaries = {}
    for i in range(days):
        d0 = DAY0 + i * DAY
        mine = appliance_of(d0, rnd)
        loads = [
            Cycling(d0, d0 + DAY, 110, 15 * 60, 30 * 60),  # fridge
            Steady(d0 + 1.5 * HOUR, d0 + 4 * HOUR, 3000),  # water heater
        ]
        for _ in range(rnd.randint(1, 3)):
            t = d0 + rnd.uniform(6, 22) * HOUR
            loads.append(Steady(t, t + rnd.uniform(2, 4) * 60, rnd.uniform(1900, 2100)))  # kettle
        if mine is not None:
            loads.append(mine)
        house = House(base_w=250, loads=loads, noise_w=10, seed=seed * 100 + i)
        hist = house.histories(d0 - 3 * HOUR, d0 + DAY)
        report = analyze(
            hist,
            AnalysisConfig(grid_entity=GRID, cycle_templates=templates or {}),
            d0,
            d0 + DAY,
            local_hour=UTC_HOUR,
        )
        plug = series(mine or Steady(0, 0, 0), d0 - 3 * HOUR, d0 + DAY)
        report["ground_truth"] = [day_truth("appliance:oven", PLUG, plug, d0, d0 + DAY)]
        summaries[(dt.date(2027, 1, 1) + dt.timedelta(days=i)).isoformat()] = summary_of(report)
    return summaries


def oven_of(d0, rnd):
    if rnd.random() < 0.25:
        return None
    t = d0 + rnd.uniform(11, 20) * HOUR
    return Steady(t, t + rnd.uniform(35, 50) * 60, rnd.uniform(2350, 2450))


def dishwasher_of(d0, rnd):
    return dishwasher(d0 + rnd.uniform(8, 18) * HOUR, rnd) if rnd.random() < 0.8 else None


OVEN = {
    "key": "appliance:oven",
    "name": "Oven",
    "sources": [{"type": "submeter", "entity_id": PLUG}],
    "teacher": {"entity_id": PLUG, "since": DAY0},
}


class EndToEndTests(unittest.TestCase):
    def test_steady_appliance(self):
        summaries = teach_days(oven_of)
        end = DAY0 + 8 * DAY
        registry = update(None, summaries, end, local_hour=UTC_HOUR)
        got = status(OVEN, summaries, registry, end)
        self.assertEqual(got["days"], 8)
        self.assertGreaterEqual(got["uses"], 4)
        learned = got["learned"]
        self.assertEqual((learned["kind"], learned["band"]), ("steady", "medium"))
        self.assertAlmostEqual(learned["power_w"], 2400, delta=60)
        sig = got["signature"]
        self.assertTrue(sig["in_registry"])
        self.assertEqual((sig["kind"], sig["band"]), ("steady", "medium"))
        ev = got["evaluation"]
        self.assertEqual(ev["uses"], got["uses"])
        self.assertGreaterEqual(ev["recall"], 0.9)
        self.assertGreaterEqual(ev["precision"], 0.9)
        self.assertAlmostEqual(ev["energy_ratio"], 1.0, delta=0.15)
        self.assertEqual(len(got["per_day"]), 8)
        self.assertIsNone(got["conflict"])
        # the signature to link is the registry's own
        reg = json.loads(json.dumps(registry))
        linked, created, _ = apply(OVEN, summaries, reg, end)
        self.assertFalse(created)
        self.assertEqual(linked["id"], sig["id"])

    def test_multiphase_appliance(self):
        summaries = teach_days(dishwasher_of, days=6)
        end = DAY0 + 6 * DAY
        registry = update(None, summaries, end, local_hour=UTC_HOUR)
        registry["signatures"] = [s for s in registry["signatures"] if s["kind"] != "multiphase"]
        got = status(OVEN, summaries, registry, end)
        self.assertEqual(got["learned"]["kind"], "multiphase")
        # nothing like it in the registry yet: evaluated as the expected signature
        self.assertFalse(got["signature"]["in_registry"])
        self.assertGreaterEqual(got["evaluation"]["recall"], 0.75)
        # applying it creates the signature with the learned template...
        sig, created, _ = apply(OVEN, summaries, registry, end)
        self.assertTrue(created)
        self.assertEqual(sig["kind"], "multiphase")
        self.assertEqual((sig["label"], sig["appliance"]), ("Oven", "appliance:oven"))
        self.assertTrue(sig["phases"])
        self.assertEqual(sig["taught"]["entity_id"], PLUG)
        # ... which the analysis recognises at once (labelled: validated)
        templates = cycle_templates(registry)
        self.assertIn(sig["id"], templates)
        again = teach_days(dishwasher_of, days=6, templates=templates)
        found = [a for s in again.values() for a in s["activations"] if a.get("multiphase")]
        self.assertTrue(found)
        got = status(OVEN, again, registry, end)
        self.assertTrue(got["signature"]["in_registry"])
        self.assertGreaterEqual(got["evaluation"]["recall"], 0.75)
        self.assertGreaterEqual(got["evaluation"]["precision"], 0.75)

    def test_not_learned_and_not_recognisable(self):
        summaries = teach_days(lambda d0, rnd: None, days=2)
        with self.assertRaises(TeachingError) as cm:
            apply(OVEN, summaries, {"signatures": []}, DAY0)
        self.assertEqual(cm.exception.code, "not_learned")
        got = status(OVEN, summaries, {"signatures": []}, DAY0)
        self.assertEqual((got["days"], got["uses"], got["learned"]), (2, 0, None))
        summaries = teach_days(lambda d0, rnd: Steady(d0 + 20 * HOUR, d0 + 22 * HOUR, 120), days=2)
        with self.assertRaises(TeachingError) as cm:
            apply(OVEN, summaries, {"signatures": []}, DAY0)
        self.assertEqual(cm.exception.code, "not_recognisable")

    def test_conflict(self):
        summaries = teach_days(oven_of, days=4)
        registry = update(None, summaries, DAY0 + 4 * DAY, local_hour=UTC_HOUR)
        sid = status(OVEN, summaries, registry, DAY0)["signature"]["id"]
        for s in registry["signatures"]:
            if s["id"] == sid:
                s.update(label="Dryer", appliance="appliance:dryer")
        self.assertEqual(status(OVEN, summaries, registry, DAY0)["conflict"]["appliance"], "appliance:dryer")
        with self.assertRaises(TeachingError) as cm:
            apply(OVEN, summaries, registry, DAY0)
        self.assertEqual((cm.exception.code, cm.exception.signature_id), ("conflict", sid))

    def test_other_sensor_days_are_not_counted(self):
        summaries = teach_days(oven_of, days=3)
        other = {**OVEN, "teacher": {"entity_id": "sensor.other_plug"}}
        self.assertEqual(status(other, summaries, {"signatures": []}, DAY0)["days"], 0)

    def test_resolve_multiphase_by_power_when_expected(self):
        from nilm_core.groundtruth import Learned

        learned = Learned("multiphase", "cycle", 2000, 5000, 3, 3)
        registry = {
            "signatures": [{"id": "cycle-2000w", "kind": "multiphase", "band": "cycle", "power_w": 2050}]
        }
        self.assertEqual(resolve(registry, learned)["id"], "cycle-2000w")
        self.assertIsNone(resolve({"signatures": []}, learned))


# ---------------------------------------------------------------------------
# the teacher in the add-on: appliances, derivation, analysis, API
# ---------------------------------------------------------------------------
class TeacherDeclarationTests(unittest.TestCase):
    def test_validate(self):
        got = validate({"appliances": [OVEN]})
        self.assertEqual(got[0]["teacher"], {"entity_id": PLUG, "since": DAY0})
        bare = {**OVEN, "teacher": {"entity_id": PLUG}}
        self.assertEqual(validate({"appliances": [bare]})[0]["teacher"], {"entity_id": PLUG})

    def test_rejects(self):
        for teacher in (
            {"entity_id": "switch.plug"},
            {"entity_id": PLUG, "x": 1},
            {"entity_id": PLUG, "since": "x"},
            "x",
        ):
            with self.assertRaises(AppliancesError, msg=teacher):
                validate({"appliances": [{**OVEN, "teacher": teacher}]})
        two = [
            OVEN,
            {"key": "appliance:dryer", "name": "Dryer", "sources": [], "teacher": {"entity_id": PLUG}},
        ]
        with self.assertRaises(AppliancesError):
            validate({"appliances": two})

    def test_never_subtracted(self):
        """Whether the plug is a sub-meter source of the appliance or a
        device of the Energy dashboard, it stays in the residual."""
        opts = analysis_options({}, [OVEN])
        self.assertEqual(opts["teachers"], [PLUG])
        snap = snapshot()
        snap["states"] += [energy("sensor.plug_energy"), power(PLUG)]
        snap["prefs"]["device_consumption"] = [{"stat_consumption": "sensor.plug_energy"}]
        derived = derive(snap["prefs"], snap["states"], snap["registry"], opts)
        self.assertNotIn(PLUG, [m.entity_id for m in derived.config.submeters])
        self.assertEqual([t.role for t in derived.tracked], ["teacher"])
        self.assertEqual(derived.teachers, [PLUG])
        derived = derive(
            snap["prefs"], snap["states"], snap["registry"], analysis_options({}, [{**OVEN, "teacher": None}])
        )
        self.assertIn(PLUG, [m.entity_id for m in derived.config.submeters])


class PlugHA(FakeHA):
    """The fake house plus a plug measuring its 2 kW load."""

    def history(self, entities, start, end):
        out = super().history(entities, start, end)
        if PLUG in entities and out:
            load = self.house.loads[0]
            out[PLUG] = [(t, f"{load.power_at(t):.1f}") for t, _ in out["sensor.grid_power"]]
        return out


class AnalyzerTeachingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ha = PlugHA(data_start=midnight(TODAY) - 3 * 86400)
        self.analyzer, self.store = make_analyzer(self._tmp.name, self.ha)

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, method, target, body=b""):
        return route(self.analyzer, self.store, method, target, body)

    def test_ground_truth_and_status(self):
        self.analyzer.set_appliances(validate({"appliances": [OVEN]}))
        day = TODAY - dt.timedelta(days=1)
        report = self.analyzer.analyze_day(day)
        entities, _, _ = self.ha.history_calls[-1]
        self.assertIn(PLUG, entities)
        (truth,) = report["ground_truth"]
        self.assertEqual((truth["key"], truth["entity_id"]), ("appliance:oven", PLUG))
        self.assertEqual(len(truth["runs"]), 1)
        self.assertAlmostEqual(truth["energy_kwh"], 4.0, delta=0.1)
        # the plug was not subtracted: the load is in the curve
        self.assertEqual(report["energy_kwh"]["submetered"], 0)
        self.assertTrue(report["activations"])
        status_code, body = self.call("GET", "/api/teaching")
        self.assertEqual(status_code, 200)
        (st,) = body["teachers"]
        self.assertEqual((st["days"], st["uses"], st["learned"]["kind"]), (1, 1, "steady"))
        self.assertEqual(st["evaluation"]["recall"], 1.0)
        status_code, body = self.call(
            "POST", "/api/teaching", json.dumps({"appliance": "appliance:oven"}).encode()
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["signature"]["kind"], "steady")
        self.assertIn("created", body)

    def test_without_teacher_nothing_is_stored(self):
        report = self.analyzer.analyze_day(TODAY - dt.timedelta(days=1))
        self.assertNotIn("ground_truth", report)
        self.assertEqual(self.call("GET", "/api/teaching"), (200, {"teachers": []}))

    def test_api_errors(self):
        self.assertEqual(self.call("POST", "/api/teaching", b"{")[0], 400)
        self.assertEqual(self.call("POST", "/api/teaching", b'{"x": 1}')[0], 400)
        status_code, body = self.call("POST", "/api/teaching", b'{"appliance": "nope"}')
        self.assertEqual((status_code, body["error"]), (404, "not_found"))
        self.analyzer.set_appliances(validate({"appliances": [OVEN]}))
        status_code, body = self.call("POST", "/api/teaching", b'{"appliance": "appliance:oven"}')
        self.assertEqual((status_code, body["error"]), (422, "not_learned"))
        self.assertEqual(self.call("PUT", "/api/teaching")[0], 405)


if __name__ == "__main__":
    unittest.main()
