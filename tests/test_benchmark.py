"""Tests of the real-data benchmark tool (#84), on synthetic households: the
days it finds in an export, the meters' figures, each measured device hidden
and scored (above and below the step threshold), the comparison of two runs
and the command line."""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import gzip
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import benchmark  # noqa: E402
from nilm_core import AnalysisConfig, SubmeterConfig  # noqa: E402
from synth import DAY0, HOUR, Cycling, House, Steady  # noqa: E402

DAY = 86400.0
TZ = dt.timezone(dt.timedelta(hours=-8))  # DAY0 is midnight there
GRID = "sensor.grid_power"
HEATER = "sensor.heater_power"
FRIDGE = "sensor.fridge_power"
IDLE = "sensor.spare_plug_power"
FIRST = DAY0 - DAY  # the export: one day of context, three whole days, one partial
LAST = DAY0 + 3 * DAY + 6 * HOUR


def export(pv_peak_w: float = 0.0) -> dict:
    """A house whose water heater and fridge have a sub-meter, and a plug
    with nothing on it."""
    heater = [Steady(DAY0 + i * DAY + 2 * HOUR, DAY0 + i * DAY + 3.5 * HOUR, 2500) for i in range(-1, 4)]
    fridge = [Cycling(FIRST, LAST, 100, 15 * 60, 30 * 60)]
    house = House(base_w=250, loads=[*heater, *fridge], noise_w=5, pv_peak_w=pv_peak_w)
    hist = house.histories(FIRST, LAST)
    stamps = [t for t, _ in hist[GRID]]
    for entity, loads in ((HEATER, heater), (FRIDGE, fridge), (IDLE, [])):
        hist[entity] = [(t, f"{sum(x.power_at(t) for x in loads):.1f}") for t in stamps]
    return hist


def config(**kwargs) -> AnalysisConfig:
    return AnalysisConfig(
        grid_entity=GRID,
        submeters=[
            SubmeterConfig(HEATER, "Water heater"),
            SubmeterConfig(FRIDGE, "Fridge"),
            SubmeterConfig(IDLE, "Spare plug"),
        ],
        **kwargs,
    )


class InputTest(unittest.TestCase):
    def test_whole_days_of_an_export(self):
        days = benchmark.days_of([FIRST, LAST], TZ)
        self.assertEqual([d for d, _, _ in days], ["2027-01-15", "2027-01-16", "2027-01-17"])
        self.assertEqual((days[0][1], days[0][2]), (DAY0, DAY0 + DAY))
        self.assertEqual(benchmark.days_of([], TZ), [])
        self.assertEqual(benchmark.days_of([DAY0 + 10, DAY0 + 20], TZ), [])

    def test_window_keeps_the_state_before_it(self):
        hist = {"switch.x": [(0.0, "off"), (10.0, "on"), (50.0, "off"), (90.0, "on")]}
        self.assertEqual(benchmark.window(hist, 20, 60), {"switch.x": [(10.0, "on"), (50.0, "off")]})
        self.assertEqual(benchmark.window(hist, 50, 90)["switch.x"][0], (10.0, "on"))
        self.assertEqual(benchmark.window(hist, -5, 5), {"switch.x": [(0.0, "off")]})

    def test_load_a_csv_and_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "export.csv")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["entity_id", "state", "last_updated_ts"])  # a header is skipped
                w.writerow([GRID, "250.0", "20.0"])
                w.writerow([GRID, "unavailable", "10.0"])
            self.assertEqual(benchmark.load(str(path)), {GRID: [(10.0, "unavailable"), (20.0, "250.0")]})
            folder = pathlib.Path(tmp, "days")
            folder.mkdir()
            pathlib.Path(folder, "a.json").write_text(json.dumps({GRID: [[10.0, "1"], [20.0, "2"]]}))
            with gzip.open(pathlib.Path(folder, "b.json.gz"), "wt") as f:
                json.dump({GRID: [[20.0, "2"], [30.0, "3"]]}, f)  # overlaps the first file
            pathlib.Path(folder, "notes.txt").write_text("ignored")
            self.assertEqual(benchmark.load(str(folder)), {GRID: [(10.0, "1"), (20.0, "2"), (30.0, "3")]})


class RunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hist = export()
        cls.result = benchmark.run(cls.hist, config(), TZ)
        cls.devices = {d["entity_id"]: d for d in cls.result["devices"]}

    def test_days_and_meters(self):
        r = self.result
        self.assertEqual(r["days"], ["2027-01-15", "2027-01-16", "2027-01-17"])
        self.assertEqual(r["thresholds"], {"step_w": 300.0, "night_step_w": None})
        self.assertEqual(r["meters"]["grid"], {"median_s": 10.0, "p90_s": 10.0})
        self.assertEqual(r["meters"]["solar"], {"median_s": None, "p90_s": None})
        # the readings have a noise of 5 W
        self.assertTrue(2 <= r["meters"]["noise_w"]["no_solar"] <= 10, r["meters"]["noise_w"])
        self.assertIsNone(r["meters"]["noise_w"]["solar"])
        self.assertEqual(len(r["meters"]["per_day"]), 3)

    def test_energy_shares(self):
        e = self.result["energy"]
        self.assertAlmostEqual(sum(e["shares"].values()), 1.0, delta=0.02)
        # 250 W of base load, a 2.5 kW heater for 1.5 h and a fridge, all day
        self.assertAlmostEqual(e["house"] / 3, 0.25 * 24 + 3.75 + 0.8, delta=0.3)
        self.assertGreater(e["shares"]["submetered"], 0.3)

    def test_a_device_above_the_threshold_is_found_again(self):
        d = self.devices[HEATER]
        self.assertEqual((d["status"], d["kind"], d["band"]), ("tested", "steady", "long"))
        self.assertAlmostEqual(d["power_w"], 2500, delta=50)
        self.assertEqual((d["days_tested"], d["uses"], d["found"]), (3, 3, 3))
        self.assertEqual((d["recall"], d["precision"]), (1.0, 1.0))
        self.assertAlmostEqual(d["energy_ratio"], 1.0, delta=0.1)
        self.assertEqual([p["day"] for p in d["per_day"]], self.result["days"])

    def test_a_device_below_the_threshold_keeps_its_uses(self):
        d = self.devices[FRIDGE]
        self.assertEqual(d["status"], benchmark.BELOW_THRESHOLD)
        self.assertAlmostEqual(d["power_w"], 100, delta=5)
        self.assertEqual(d["uses"], 3 * 32)  # one run every 45 minutes
        self.assertEqual((d["found"], d["missed"], d["detections"], d["recall"]), (0, 96, 0, 0.0))
        self.assertAlmostEqual(d["truth_kwh"], 3 * 0.8, delta=0.1)
        self.assertEqual(d["energy_ratio"], 0.0)

    def test_a_device_that_never_ran(self):
        d = self.devices[IDLE]
        self.assertEqual((d["status"], d["uses"], d["recall"], d["power_w"]), ("no_use", 0, None, None))

    def test_a_lower_threshold_shows_its_gain(self):
        low = benchmark.run(self.hist, config(step_threshold_w=60.0), TZ, devices=[FRIDGE])
        self.assertEqual([d["entity_id"] for d in low["devices"]], [FRIDGE])
        d = low["devices"][0]
        self.assertEqual(d["status"], "tested")
        self.assertGreaterEqual(d["recall"], 0.9)
        self.assertGreaterEqual(d["precision"], 0.9)
        text = benchmark.compare(self.result, low)
        self.assertIn("Fridge", text)
        self.assertIn("0% -> ", text)
        self.assertNotIn("Water heater", text)

    def test_estimates_are_never_hidden(self):
        cfg = config()
        cfg.submeters[1].estimated = True
        r = benchmark.run(self.hist, cfg, TZ, devices=[FRIDGE, HEATER])
        self.assertEqual([d["entity_id"] for d in r["devices"]], [HEATER])

    def test_solar_meter_and_noise_with_solar(self):
        hist = export(pv_peak_w=3000)
        cfg = config(pv_entity="sensor.pv_power", night_step_threshold_w=150.0)
        r = benchmark.run(hist, cfg, TZ, devices=[HEATER])
        self.assertEqual(r["thresholds"], {"step_w": 300.0, "night_step_w": 150.0})
        self.assertEqual(r["meters"]["solar"], {"median_s": 10.0, "p90_s": 10.0})
        self.assertIsNotNone(r["meters"]["noise_w"]["solar"])
        self.assertIsNotNone(r["meters"]["noise_w"]["no_solar"])
        self.assertEqual(r["devices"][0]["recall"], 1.0)

    def test_table(self):
        text = benchmark.table(self.result)
        self.assertIn("3 day(s): 2027-01-15 .. 2027-01-17", text)
        self.assertIn("thresholds: 300 W", text)
        self.assertIn("grid meter: every 10 s", text)
        for name in ("Water heater", "Fridge", "Spare plug", "below_threshold", "no_use"):
            self.assertIn(name, text)

    def test_no_whole_day(self):
        hist = {GRID: [(DAY0 + 10.0, "250"), (DAY0 + 20.0, "250")]}
        r = benchmark.run(hist, config(), TZ)
        self.assertEqual((r["days"], r["steps_per_day"]), ([], None))
        self.assertIn("no whole day", benchmark.table(r))


class CommandLineTest(unittest.TestCase):
    def test_run_write_and_compare(self):
        hist = export()
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp, "hist")
            folder.mkdir()
            with gzip.open(pathlib.Path(folder, "all.json.gz"), "wt") as f:
                json.dump(hist, f)
            before = pathlib.Path(tmp, "before.json")
            args = [str(folder), "--grid", GRID, "--tz", "Etc/GMT+8", "--submeter", f"{HEATER}=Water heater"]
            args += ["--submeter", FRIDGE]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = benchmark.main([*args, "--json", str(before)])
            self.assertEqual(code, 0)
            self.assertIn("Water heater", out.getvalue())
            self.assertIn(FRIDGE, out.getvalue())  # without a name: the entity id
            self.assertIn("2027-01-15: analysed", err.getvalue())
            saved = json.loads(before.read_text())
            self.assertEqual((saved["version"], len(saved["days"]), len(saved["devices"])), (1, 3, 2))

            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = benchmark.main([*args, "--threshold", "60", "--compare", str(before)])
            self.assertEqual(code, 0)
            self.assertIn("thresholds: 60 W", out.getvalue())
            self.assertIn("signatures: ", out.getvalue())
            self.assertIn("0% -> ", out.getvalue())

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = benchmark.main([str(folder), "--grid", "sensor.missing"])
            self.assertEqual(code, 1)
            self.assertIn("no reading of sensor.missing", err.getvalue())

    def test_build_config(self):
        ns = argparse.Namespace(
            grid=GRID, grid_export="sensor.export", pv="sensor.pv", submeter=["sensor.a=Oven"],
            estimated=["sensor.b"], switch=["switch.heater", "switch.radiator=off"],
            scale=["sensor.pv=1000"], threshold=300.0, night_threshold=150.0,
        )  # fmt: skip
        cfg = benchmark.build_config(ns)
        self.assertEqual([(m.entity_id, m.name, m.estimated) for m in cfg.submeters],
                         [("sensor.a", "Oven", False), ("sensor.b", "sensor.b", True)])  # fmt: skip
        self.assertEqual([(s.entity_id, s.on_states) for s in cfg.switched_loads],
                         [("switch.heater", ("on",)), ("switch.radiator", ("off",))])  # fmt: skip
        self.assertEqual(cfg.scales, {"sensor.pv": 1000.0})
        self.assertEqual(
            (cfg.grid_export_entity, cfg.pv_entity, cfg.low_step_threshold()),
            ("sensor.export", "sensor.pv", 150.0),
        )


if __name__ == "__main__":
    unittest.main()
