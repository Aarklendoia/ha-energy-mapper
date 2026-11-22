"""The night threshold (#77): smaller steps are kept only while the solar
panels produce nothing. Synthetic days only (no real data)."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from nilm_core import AnalysisConfig, analyze, detect_steps  # noqa: E402
from nilm_core.live import LiveDetector  # noqa: E402
from nilm_core.report import find_steps  # noqa: E402
from nilm_core.steps import (  # noqa: E402
    CONFIRMED,
    PROVISIONAL,
    PvQuiet,
    StepStream,
    night_threshold,
    pv_quiet,
)
from settings import SettingsError, validate  # noqa: E402
from synth import DAY0, HOUR, House, Steady, solar_drift_day  # noqa: E402
from test_live import level, night_samples  # noqa: E402

GRID = "sensor.grid_power"
PV = "sensor.pv_power"
HOUR_OF = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731


def config(night=150.0, pv=True, step=300.0) -> AnalysisConfig:
    return AnalysisConfig(
        grid_entity=GRID,
        pv_entity=PV if pv else None,
        step_threshold_w=step,
        night_step_threshold_w=night,
    )


def steps_of(hist, cfg):
    return find_steps(night_samples(hist, cfg), cfg)


def hours(steps):
    return [(round((s.ts - DAY0) / HOUR, 2), round(s.delta)) for s in steps]


def stable(report: dict) -> dict:
    """A report without what changes from one run to the next."""
    out = dict(report)
    out.pop("generated_at")
    out["quality"] = {k: v for k, v in report["quality"].items() if k != "compute_s"}
    return out


class RuleTest(unittest.TestCase):
    def test_night_threshold_applies(self):
        self.assertEqual(night_threshold(300, 150, True), 150)
        self.assertIsNone(night_threshold(300, None, True))  # option unset
        self.assertIsNone(night_threshold(300, 150, False))  # no PV meter
        self.assertIsNone(night_threshold(300, 300, True))  # not lower
        self.assertEqual(config().low_step_threshold(), 150)
        self.assertIsNone(config(pv=False).low_step_threshold())
        self.assertEqual(config().smallest_step_w(), 150)
        self.assertEqual(config(pv=False).smallest_step_w(), 300)

    def test_pv_quiet(self):
        ts = [DAY0 + 10 * i for i in range(30)]
        self.assertTrue(pv_quiet(ts, [0.0] * 30, DAY0 + 150))
        # the median, not a single reading
        values = [0.0] * 30
        values[16] = 500.0
        self.assertTrue(pv_quiet(ts, values, DAY0 + 150))
        # producing after the step
        self.assertFalse(pv_quiet(ts, [0.0] * 15 + [40.0] * 15, DAY0 + 150))
        # unknown on one side
        self.assertFalse(pv_quiet(ts, [0.0] * 30, DAY0))
        self.assertFalse(PvQuiet([])(DAY0))


class NightAnalysisTest(unittest.TestCase):
    def test_midday_drift_is_not_kept(self):
        hist = solar_drift_day()
        # the test has teeth: at 150 W everywhere, the drift makes steps
        everywhere = steps_of(hist, config(night=None, step=150))
        self.assertGreater(sum(1 for s in everywhere if 11 * HOUR < s.ts - DAY0 < 14.2 * HOUR), 10)
        steps = steps_of(hist, config())
        daytime = [s for s in steps if 8 * HOUR <= s.ts - DAY0 <= 18 * HOUR]
        self.assertTrue(all(abs(s.delta) >= 300 for s in daytime), hours(daytime))

    def test_large_step_during_pv_is_kept(self):
        steps = steps_of(solar_drift_day(), config())
        big = [h for h in hours(steps) if abs(h[1]) > 1000]
        self.assertEqual([h[0] for h in big], [12.5, 12.83])

    def test_small_evening_load_is_found(self):
        hist = solar_drift_day()
        steps = steps_of(hist, config())
        small = [s for s in steps if abs(s.delta) < 300]
        self.assertEqual([round((s.ts - DAY0) / HOUR, 2) for s in small], [20.0, 20.42])
        self.assertAlmostEqual(small[0].delta, 170, delta=25)
        self.assertAlmostEqual(small[1].delta, -170, delta=25)
        # and the analysis makes it a ~170 W activation of 25 minutes
        report = analyze(hist, config(), DAY0, DAY0 + 24 * HOUR, local_hour=HOUR_OF)
        evening = [a for a in report["activations"] if a["start"] >= DAY0 + 19 * HOUR]
        self.assertEqual(len(evening), 1)
        self.assertAlmostEqual(evening[0]["power_w"], 170, delta=25)
        self.assertAlmostEqual(evening[0]["end"] - evening[0]["start"], 1500, delta=30)
        # without the option, it is not seen
        off = steps_of(hist, config(night=None))
        self.assertEqual([s for s in off if abs(s.delta) < 300], [])

    def test_daytime_steps_unchanged(self):
        # where PV produces, the steps are exactly those without the option
        hist = solar_drift_day()
        on = [s for s in steps_of(hist, config()) if 8 * HOUR <= s.ts - DAY0 <= 18 * HOUR]
        off = [s for s in steps_of(hist, config(night=None)) if 8 * HOUR <= s.ts - DAY0 <= 18 * HOUR]
        self.assertEqual(on, off)

    def test_off_is_identical(self):
        hist = solar_drift_day()
        span = (DAY0, DAY0 + 24 * HOUR)
        # option unset: the steps of the plain detector, as before #77
        cfg = config(night=None)
        points = [(s.ts, s.residual) for s in night_samples(hist, cfg) if s.residual is not None]
        self.assertEqual(steps_of(hist, cfg), detect_steps(points, threshold=300))
        # without a PV meter the option has no effect: the same report
        reference = analyze(hist, config(night=None, pv=False), *span, local_hour=HOUR_OF)
        with_option = analyze(hist, config(night=150, pv=False), *span, local_hour=HOUR_OF)
        self.assertEqual(stable(reference), stable(with_option))
        # with PV, the option unset or not lower than the step threshold: the same report
        reference = analyze(hist, config(night=None), *span, local_hour=HOUR_OF)
        self.assertEqual(
            stable(reference), stable(analyze(hist, config(night=300), *span, local_hour=HOUR_OF))
        )


class LiveTest(unittest.TestCase):
    def test_stream_small_step_at_night(self):
        # PV produces nothing: a 170 W step is confirmed, once the minute after it is known
        t_on = DAY0 + 600
        pts = level(DAY0, DAY0 + 1800, 10, lambda t: 420 if t >= t_on else 250)
        s = StepStream(interval_s=10, low_threshold=150, low_ok=lambda t: True)
        log = []
        for t, v in pts:
            log += [(t, st.state, st.small, st.ts) for st in s.push(t, v)]
        self.assertEqual([x[1] for x in log], [PROVISIONAL, CONFIRMED])
        self.assertFalse(log[0][2])  # not "too small"
        self.assertEqual(log[1][3], t_on)
        self.assertGreater(log[1][0] - t_on, 60)
        self.assertEqual([(st.ts, st.delta) for st in s.flush()], [])

    def test_stream_small_step_while_pv_produces(self):
        # PV produces: the same updates as without the option
        t_on = DAY0 + 600
        for power in (170, 250):
            pts = level(DAY0, DAY0 + 1800, 10, lambda t, p=power: 250 + p if t >= t_on else 250)

            def run(pts=pts, **kwargs):
                s = StepStream(interval_s=10, **kwargs)
                log = []
                for t, v in pts:
                    log += [(t, st.state, st.small, st.ts, st.delta) for st in s.push(t, v)]
                return log + [(None, st.state, st.small, st.ts, st.delta) for st in s.flush()]

            with self.subTest(power=power):
                log = run(low_threshold=150, low_ok=lambda t: False)
                self.assertNotIn(CONFIRMED, [x[1] for x in log])
                plain = run()
                # judged later (the minute of PV after it), else the same
                self.assertEqual([x[1:] for x in log], [x[1:] for x in plain])

    def test_detector_pv_quiet_now(self):
        house = House(base_w=250, loads=[Steady(DAY0 + 20 * HOUR, DAY0 + 20.5 * HOUR, 170)], pv_peak_w=3000)
        hist = house.histories(DAY0 + 11 * HOUR, DAY0 + 11.2 * HOUR, dt=10)
        det = LiveDetector(config())
        self.assertEqual(det.night_threshold_w, 150)
        self.assertIsNone(det.pv_quiet_now())  # nothing yet
        for e, rows in hist.items():
            for ts, v in rows:
                det.push(e, ts, v)
        det.flush()
        self.assertFalse(det.pv_quiet_now())  # midday
        det = LiveDetector(config())
        for e, rows in house.histories(DAY0 + 21 * HOUR, DAY0 + 21.2 * HOUR, dt=10).items():
            for ts, v in rows:
                det.push(e, ts, v)
        det.flush()
        self.assertTrue(det.pv_quiet_now())  # evening
        # off: no answer
        self.assertIsNone(LiveDetector(config(night=None)).pv_quiet_now())
        self.assertIsNone(LiveDetector(config(pv=False)).night_threshold_w)


class SettingsTest(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate({"night_step_threshold_w": 150}), {"night_step_threshold_w": 150.0})
        self.assertEqual(
            validate({"night_step_threshold_w": 300, "step_threshold_w": 300}),
            {"night_step_threshold_w": 300.0, "step_threshold_w": 300.0},
        )
        self.assertEqual(validate({"night_step_threshold_w": None}), {})
        for data, options in (
            ({"night_step_threshold_w": 40}, {}),
            ({"night_step_threshold_w": 350}, {}),  # above the default 300 W
            ({"night_step_threshold_w": 250, "step_threshold_w": 200}, {}),
            ({"night_step_threshold_w": 250}, {"step_threshold_w": 200}),  # the add-on option
            ({"night_step_threshold_w": "150"}, {}),
        ):
            with self.subTest(data=data), self.assertRaises(SettingsError):
                validate(data, options)
        # the add-on's step threshold allows it
        self.assertEqual(
            validate({"night_step_threshold_w": 350}, {"step_threshold_w": 500}),
            {"night_step_threshold_w": 350.0},
        )

    def test_service_smallest_step(self):
        from storage import ReportStore
        from test_addon import FakeHA, make_analyzer

        with tempfile.TemporaryDirectory() as tmp:
            analyzer, _ = make_analyzer(tmp, FakeHA(0), {"night_step_threshold_w": 150})
            self.assertEqual(analyzer.smallest_step_w, 300.0)  # no PV meter
            analyzer.prepare()
            self.assertEqual(analyzer.smallest_step_w, 300.0)
        with tempfile.TemporaryDirectory() as tmp:
            options = {"night_step_threshold_w": 150, "pv_entity": PV}
            analyzer, _ = make_analyzer(tmp, FakeHA(0), options)
            self.assertEqual(analyzer.smallest_step_w, 150.0)
            cfg = analyzer.prepare().derived.config
            self.assertEqual((cfg.pv_entity, cfg.night_step_threshold_w), (PV, 150.0))
            self.assertEqual(analyzer.smallest_step_w, 150.0)
            self.assertIsInstance(analyzer.store, ReportStore)


if __name__ == "__main__":
    unittest.main()
