"""Tests of the nightly self-test (#15): which measured devices are hidden and
in what order, the hidden analysis (found again, never stored), the stored
results and their summary (success rate, not enough data, trend), the
setting and the HTTP endpoint. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import selftest  # noqa: E402
import service  # noqa: E402
from api import route  # noqa: E402
from nilm_core import AnalysisConfig, SubmeterConfig  # noqa: E402
from settings import SettingsError, validate  # noqa: E402
from synth import Cycling, House, Steady  # noqa: E402
from test_addon import TODAY, FakeHA, make_analyzer, midnight  # noqa: E402

DAY = 86400
HEATER = "sensor.heater_power"
OVEN = "sensor.oven_power"
FRIDGE = "sensor.fridge_power"
GRID = "sensor.grid_power"


class MeteredHA(FakeHA):
    """A house whose water heater, oven and fridge each have a sub-meter."""

    def __init__(self, data_start: float, days: int = 10):
        super().__init__(data_start)
        first = midnight(TODAY) - days * DAY
        self.meters: dict[str, list] = {HEATER: [], OVEN: [], FRIDGE: []}
        for i in range(days):
            d0 = first + i * DAY
            self.meters[HEATER].append(Steady(d0 + 2 * 3600, d0 + 3.5 * 3600, 2500))
            self.meters[OVEN].append(Steady(d0 + 18 * 3600, d0 + 18.75 * 3600, 1800))
        self.meters[FRIDGE].append(Cycling(first, midnight(TODAY), 100, 15 * 60, 30 * 60))
        self.house = House(base_w=250, loads=[x for loads in self.meters.values() for x in loads], noise_w=5)

    def history(self, entities, start, end):
        out = super().history(entities, start, end)
        if not out:
            return out
        stamps = [t for t, _ in out[GRID]]
        for entity, loads in self.meters.items():
            if entity in entities:
                out[entity] = [(t, f"{sum(x.power_at(t) for x in loads):.1f}") for t in stamps]
        return out


OPTIONS = {"backfill_days": 1, "submeters_mode": "manual", "submeters": [FRIDGE, HEATER, OVEN]}


def entry(day, entity, status=selftest.TESTED, uses=1, found=1, detections=1, correct=1):
    return {
        "day": day,
        "entity_id": entity,
        "name": entity,
        "status": status,
        "runs": [],
        "evaluation": {
            "uses": uses,
            "found": found,
            "detections": detections,
            "correct": correct,
            "truth_kwh": float(uses),
            "detected_kwh": float(found),
        }
        if status == selftest.TESTED
        else None,
        "signature": {"id": "steady:medium:2500"} if status == selftest.TESTED else None,
    }


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
class PureTests(unittest.TestCase):
    def test_candidates_are_measuring_submeters(self):
        meters = [
            SubmeterConfig(OVEN, "Oven"),
            SubmeterConfig("sensor.estimate", "Estimate", estimated=True),
            SubmeterConfig(HEATER, "Heater"),
            SubmeterConfig(OVEN, "Oven again"),
        ]
        self.assertEqual(selftest.candidates(meters), [(HEATER, "Heater"), (OVEN, "Oven")])

    def test_pick_rotates(self):
        devices = [OVEN, HEATER, FRIDGE]
        data = selftest.empty()
        self.assertEqual(selftest.pick(devices, data, 1), [FRIDGE])  # never tested, by entity id
        self.assertEqual(selftest.pick(devices, data, 0), [])
        data["tests"] = [entry("2027-03-01", FRIDGE), entry("2027-03-02", HEATER)]
        self.assertEqual(selftest.pick(devices, data, 1), [OVEN])
        self.assertEqual(selftest.pick(devices, data, 2), [OVEN, FRIDGE])
        data["tests"].append(entry("2027-03-03", OVEN))
        self.assertEqual(selftest.pick(devices, data, 1), [FRIDGE])

    def test_hidden_config_is_a_copy(self):
        cfg = AnalysisConfig(
            grid_entity=GRID, submeters=[SubmeterConfig(HEATER, "H"), SubmeterConfig(OVEN, "O")]
        )
        hidden = selftest.hidden_config(cfg, HEATER)
        self.assertEqual([m.entity_id for m in hidden.submeters], [OVEN])
        self.assertEqual([m.entity_id for m in cfg.submeters], [HEATER, OVEN])

    def test_record_replaces_and_prunes(self):
        today = dt.date(2027, 3, 10)
        data = {"tests": [entry("2026-12-01", HEATER), entry("2027-03-08", HEATER, found=0)]}
        out = selftest.record(data, [entry("2027-03-08", HEATER), entry("2027-03-09", OVEN)], today)
        self.assertEqual(
            [(t["day"], t["entity_id"]) for t in out["tests"]], [("2027-03-08", HEATER), ("2027-03-09", OVEN)]
        )
        self.assertEqual(out["tests"][0]["evaluation"]["found"], 1)

    def test_not_enough_data_is_never_zero(self):
        s = selftest.device_summary(
            [entry("2027-03-01", HEATER, found=0), entry("2027-03-02", HEATER, found=0)]
        )
        self.assertEqual(s["state"], "not_enough_data")
        self.assertIsNone(s["rate"])
        self.assertEqual((s["tests"], s["uses"], s["found"]), (2, 2, 0))
        self.assertIsNone(s["trend"])

    def test_rate_and_trend(self):
        older = [entry(f"2027-02-0{i}", HEATER, uses=2, found=1) for i in range(1, 4)]
        newer = [entry(f"2027-02-1{i}", HEATER, uses=2, found=2) for i in range(1, 4)]
        s = selftest.device_summary(older + newer)
        self.assertEqual(s["state"], "ok")
        self.assertEqual(s["rate"], 0.75)
        self.assertEqual(s["trend"], "up")
        self.assertEqual(selftest.trend(newer + older), "down")
        self.assertEqual(selftest.trend(newer + newer), "stable")
        self.assertIsNone(selftest.trend(newer[:1] + newer[:1]))  # too few uses per half
        self.assertEqual(len(s["history"]), 6)
        self.assertEqual(s["precision"], 1.0)

    def test_states_without_evaluation(self):
        s = selftest.device_summary([entry("2027-03-01", FRIDGE, status=selftest.NOT_RECOGNISABLE)])
        self.assertEqual((s["state"], s["tests"], s["nights"], s["rate"]), ("not_recognisable", 0, 1, None))
        s = selftest.device_summary([entry("2027-03-01", OVEN, status=selftest.NO_USE)])
        self.assertEqual(s["state"], "no_use")

    def test_summary_window(self):
        data = {"tests": [entry("2026-01-01", OVEN), entry("2027-03-09", HEATER)]}
        out = selftest.summary(data, dt.date(2027, 3, 10), devices_per_night=0)
        self.assertFalse(out["enabled"])
        self.assertEqual([d["entity_id"] for d in out["devices"]], [HEATER])
        self.assertEqual(out["last_day"], "2027-03-09")
        self.assertEqual(selftest.summary(None, dt.date(2027, 3, 10))["devices"], [])

    def test_settings(self):
        self.assertEqual(validate({"self_test_devices": 0}), {"self_test_devices": 0})
        self.assertEqual(validate({"self_test_devices": 3}), {"self_test_devices": 3})
        for bad in (11, -1, 1.5, "2", True):
            with self.assertRaises(SettingsError):
                validate({"self_test_devices": bad})


# ---------------------------------------------------------------------------
# in the add-on
# ---------------------------------------------------------------------------
class AnalyzerSelfTestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ha = MeteredHA(data_start=midnight(TODAY) - 9 * DAY)
        self.analyzer, self.store = make_analyzer(self._tmp.name, self.ha, dict(OPTIONS))
        self.calls = 0
        real = service.analyze

        def counting(*args, **kwargs):
            self.calls += 1
            return real(*args, **kwargs)

        patcher = mock.patch.object(service, "analyze", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def run_day(self, back: int, self_test: bool = True) -> dict:
        ctx = self.analyzer.prepare()
        return self.analyzer._run_day(TODAY - dt.timedelta(days=back), ctx, self_test=self_test)

    def test_rotation_and_found_again(self):
        # night 1: the fridge first (by entity id), too small for the curve:
        # no hidden analysis
        self.run_day(8)
        self.assertEqual(self.calls, 1)
        (t,) = self.store.load_self_test()["tests"]
        self.assertEqual((t["entity_id"], t["status"]), (FRIDGE, selftest.NOT_RECOGNISABLE))
        # night 2: the water heater, hidden and found again
        self.calls = 0
        self.run_day(7)
        self.assertEqual(self.calls, 2)
        t = self.store.load_self_test()["tests"][-1]
        self.assertEqual((t["entity_id"], t["status"]), (HEATER, selftest.TESTED))
        self.assertEqual(len(t["runs"]), 1)
        self.assertEqual((t["evaluation"]["uses"], t["evaluation"]["found"]), (1, 1))
        self.assertEqual(t["evaluation"]["precision"], 1.0)
        self.assertAlmostEqual(t["evaluation"]["energy_ratio"], 1.0, delta=0.1)
        self.assertEqual((t["signature"]["kind"], t["signature"]["band"]), ("steady", "long"))
        self.assertFalse(t["signature"]["in_registry"])  # a subtracted device never reaches it
        # night 3: the oven; night 4: the fridge again
        self.run_day(6)
        self.run_day(5)
        order = [t["entity_id"] for t in self.store.load_self_test()["tests"]]
        self.assertEqual(order, [FRIDGE, HEATER, OVEN, FRIDGE])

    def test_the_real_report_and_registry_are_untouched(self):
        self.analyzer.maintain()
        plain = self.run_day(3, self_test=False)
        registry = self.store.registry_path.read_bytes()
        self.analyzer.set_settings({"self_test_devices": 3})
        tested = self.run_day(3)
        self.assertEqual(self.calls, 1 + 1 + 2)  # two real analyses, the heater and the oven hidden
        for key in ("energy_kwh", "activations", "signatures", "known_loads"):
            self.assertEqual(json.dumps(tested[key]), json.dumps(plain[key]))
        self.assertEqual(
            json.dumps(self.store.load(tested["day"])["activations"]), json.dumps(plain["activations"])
        )
        self.assertGreater(plain["energy_kwh"]["submetered"], 0)
        self.assertEqual(self.store.registry_path.read_bytes(), registry)
        tests = self.store.load_self_test()["tests"]
        self.assertEqual(sorted(t["entity_id"] for t in tests), [FRIDGE, HEATER, OVEN])
        # the same day again: nothing left to test
        self.calls = 0
        self.run_day(3)
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.store.load_self_test()["tests"]), 3)

    def test_disabled(self):
        self.analyzer.set_settings({"self_test_devices": 0})
        self.assertEqual(self.analyzer.self_test_devices, 0)
        self.run_day(2)
        self.assertIsNone(self.store.load_self_test())
        self.assertFalse(self.analyzer.self_test()["enabled"])

    def test_a_failure_only_costs_the_test(self):
        with (
            mock.patch.object(selftest, "hide_and_find", side_effect=RuntimeError("boom")),
            self.assertLogs("service", "ERROR"),
        ):
            report = self.run_day(2)
        self.assertTrue(self.store.has(report["day"]))
        self.assertIsNone(self.store.load_self_test())

    def test_catch_up_tests_the_newest_day_only(self):
        self.analyzer.options = {**OPTIONS, "backfill_days": 3}
        result = self.analyzer.catch_up()
        self.assertEqual(len(result["analysed"]), 3)
        (t,) = self.store.load_self_test()["tests"]
        self.assertEqual(t["day"], result["analysed"][0])

    def test_success_rate_over_nights_and_api(self):
        self.analyzer.set_settings({"self_test_devices": 3})
        for back in range(1, 7):
            self.run_day(back)
        status, body = route(self.analyzer, self.store, "GET", "/api/self_test")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        self.assertEqual(body["devices_per_night"], 3)
        by_entity = {d["entity_id"]: d for d in body["devices"]}
        heater = by_entity[HEATER]
        self.assertEqual((heater["state"], heater["tests"], heater["uses"]), ("ok", 6, 6))
        self.assertEqual(heater["rate"], 1.0)
        self.assertEqual(heater["trend"], "stable")
        self.assertEqual(by_entity[OVEN]["rate"], 1.0)
        self.assertEqual(by_entity[FRIDGE]["state"], "not_recognisable")
        self.assertIsNone(by_entity[FRIDGE]["rate"])
        self.assertEqual(route(self.analyzer, self.store, "PUT", "/api/self_test")[0], 405)


if __name__ == "__main__":
    unittest.main()
