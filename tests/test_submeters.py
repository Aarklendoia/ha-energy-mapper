"""Sub-meters that do not report with the grid meter (#85): the steps their
misaligned reports leave in the residual load are explained, in the nightly
analysis and in the live view alike. Synthetic households only."""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import selftest  # noqa: E402
from nilm_core import AnalysisConfig, SubmeterConfig, analyze, detect_steps, parse_numeric  # noqa: E402
from nilm_core.live import LiveDetector, replay  # noqa: E402
from nilm_core.report import explained_steps, find_steps  # noqa: E402
from nilm_core.steps import CANCELLED, CONFIRMED, PROVISIONAL, Step  # noqa: E402
from nilm_core.submeters import LAG_S, SubmeterEchoes  # noqa: E402
from synth import DAY0, HOUR, House, Steady, cloudy_day, plug_history  # noqa: E402
from test_live import jitter, night_samples  # noqa: E402

GRID = "sensor.grid_power"
PLUG = "sensor.heater_plug_power"
NAME = "Heater"
UTC_HOUR = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731
START, END = DAY0, DAY0 + 24 * HOUR
T0 = DAY0 - 3 * HOUR  # the history starts with the context of the analysis

# the appliance on the plug: 1.5 kW, twelve uses of 12 to 23 min, two hours apart
HEATER = [
    Steady(DAY0 + 0.5 * HOUR + i * 2 * HOUR, DAY0 + 0.5 * HOUR + i * 2 * HOUR + 720 + 60 * i, 1500)
    for i in range(12)
]
SWITCHINGS = sorted([u.start for u in HEATER] + [u.end for u in HEATER])
# the rest of the house, away from the heater's switchings
OTHERS = (
    [Steady(DAY0 + 1.5 * HOUR + i * 4 * HOUR, DAY0 + 1.5 * HOUR + i * 4 * HOUR + 180, 2000) for i in range(6)]
    + [
        Steady(DAY0 + 3.2 * HOUR + i * 6 * HOUR, DAY0 + 3.2 * HOUR + i * 6 * HOUR + 1200, 800)
        for i in range(4)
    ]
    + [Steady(DAY0 + 11.3 * HOUR, DAY0 + 12.0 * HOUR, 1100)]
)


def heater_at(t: float) -> float:
    return sum(u.power_at(t) for u in HEATER)


def house(loads, **kwargs) -> dict:
    """The grid history of a house with ``loads`` (the same noise whatever they are)."""
    return House(base_w=250, loads=list(loads), noise_w=10, seed=4).histories(T0, END, dt=10, **kwargs)


def config(estimated: bool = False, **kwargs) -> AnalysisConfig:
    return AnalysisConfig(grid_entity=GRID, submeters=[SubmeterConfig(PLUG, NAME, estimated)], **kwargs)


REFERENCE = AnalysisConfig(grid_entity=GRID)  # the same house without the heater and its plug


def steps_of(hist, cfg):
    return find_steps(night_samples(hist, cfg), cfg)


def free(steps):
    return [s for s in steps if not s.explained_by]


def near(steps, times, within=90.0):
    return [s for s in steps if any(abs(s.ts - t) <= within for t in times)]


def key(steps):
    return [(s.ts, round(s.delta, 6), round(s.before, 6), round(s.after, 6), s.explained_by) for s in steps]


def uses(report):
    return [(a["start"], a["end"], a["cycling"]) for a in report["activations"]]


def stable(report: dict) -> dict:
    """A report without what changes from one run to the next."""
    out = dict(report)
    out.pop("generated_at")
    out["quality"] = {k: v for k, v in report["quality"].items() if k != "compute_s"}
    return out


def lagged(lag_s=(0.0, 25.0), every_s=20.0, seed=1, loads=None) -> dict:
    """The house with its heater, and the heater's plug reporting off the
    grid meter's clock."""
    hist = house([*HEATER, *(OTHERS if loads is None else loads)])
    hist[PLUG] = plug_history(heater_at, T0, END, every_s=every_s, lag_s=lag_s, seed=seed)
    return hist


LATE = {"lag_s": (0.0, 25.0), "every_s": 20.0}  # seen up to 45 s after the fact
EARLY = {"lag_s": (-45.0, -10.0), "every_s": 5.0}  # stamped before the grid meter shows it


class Sameness(unittest.TestCase):
    def assertSameSteps(self, steps, reference, delta=0.01):
        """The same steps, to the rounding of the recorded readings."""
        self.assertEqual([s.ts for s in steps], [s.ts for s in reference])
        for s, r in zip(steps, reference, strict=True):
            self.assertAlmostEqual(s.delta, r.delta, delta=delta)

    def assertSameUses(self, report, reference):
        """The same activations (their powers to a few watts)."""
        self.assertEqual(uses(report), uses(reference))
        for a, b in zip(report["activations"], reference["activations"], strict=True):
            self.assertAlmostEqual(a["power_w"], b["power_w"], delta=15)


# ---------------------------------------------------------------------------
class LaggedPlugTest(Sameness):
    """A sub-metered appliance reported with a random lag of up to 45 s, on
    a coarse reporting interval, leaves no step and no activation."""

    def check(self, hist):
        reference = steps_of(house(OTHERS), REFERENCE)
        steps = steps_of(hist, config())
        # the plain subtraction leaves the heater's switchings in the load
        raw = near(steps, SWITCHINGS)
        self.assertGreaterEqual(len(raw), 20)
        self.assertTrue(
            all(s.explained_by == NAME for s in raw), [key([s]) for s in raw if not s.explained_by]
        )
        # and nothing else is touched: the steps left are those of the house without the heater
        self.assertSameSteps(free(steps), reference)
        return steps

    def test_late_reports_leave_no_step(self):
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                self.check(lagged(seed=seed, **LATE))

    def test_early_reports_leave_no_step(self):
        for seed in (1, 2):
            with self.subTest(seed=seed):
                self.check(lagged(seed=seed, **EARLY))

    def test_irregular_grid_readings(self):
        hist = lagged(**LATE)
        ragged = {**jitter({GRID: hist[GRID]}, seed=9, sigma=2.0), PLUG: hist[PLUG]}
        steps = steps_of(ragged, config())
        left = near(free(steps), SWITCHINGS)
        raw = near(steps, SWITCHINGS)
        self.assertGreaterEqual(len(raw), 20)
        self.assertEqual(left, [])

    def test_no_activation_in_the_report(self):
        report = analyze(lagged(**LATE), config(), START, END, local_hour=UTC_HOUR)
        reference = analyze(house(OTHERS), REFERENCE, START, END, local_hour=UTC_HOUR)
        self.assertSameUses(report, reference)
        self.assertEqual(len(report["signatures"]), len(reference["signatures"]))
        self.assertEqual(report["quality"]["steps"], reference["quality"]["steps"])
        # the energy keeps the plain subtraction: the heater is in the sub-metered part
        self.assertAlmostEqual(
            report["energy_kwh"]["submetered"], 1500 * sum(u.end - u.start for u in HEATER) / 3.6e6, delta=0.1
        )
        (q,) = report["quality"]["submeters"]
        self.assertEqual((q["entity_id"], q["name"]), (PLUG, NAME))
        self.assertGreaterEqual(q["explained_steps"], 20)

    def test_without_the_explanation_the_day_is_wrong(self):
        # what the test above protects from: phantom uses of the heater's power
        with mock.patch.object(SubmeterEchoes, "for_config", return_value=None):
            report = analyze(lagged(**LATE), config(), START, END, local_hour=UTC_HOUR)
        reference = analyze(house(OTHERS), REFERENCE, START, END, local_hour=UTC_HOUR)
        self.assertGreaterEqual(len(report["activations"]), len(reference["activations"]) + 3)
        self.assertGreaterEqual(report["quality"]["steps"], reference["quality"]["steps"] + 20)

    def test_measured_lag(self):
        # a plug that reports a steady 30 s late: the lag of the report says so
        hist = lagged(lag_s=(30.0, 30.0), every_s=1.0)
        (q,) = analyze(hist, config(), START, END, local_hour=UTC_HOUR)["quality"]["submeters"]
        self.assertEqual(q["changes"], 2 * len(HEATER))
        self.assertEqual(q["located"], q["changes"])
        self.assertTrue(20 <= q["lag_s"] <= 31, q)
        self.assertTrue(20 <= q["lag_min_s"] <= q["lag_max_s"] <= 31, q)
        early = lagged(lag_s=(-30.0, -30.0), every_s=1.0)
        (q,) = analyze(early, config(), START, END, local_hour=UTC_HOUR)["quality"]["submeters"]
        self.assertTrue(-31 <= q["lag_s"] <= -18, q)


# ---------------------------------------------------------------------------
FREEZER = "sensor.freezer_plug_power"
CYCLES = [DAY0 - 2 * HOUR + 1300 + i * 2400 for i in range(38)]  # on for 10 min every 40 min


def freezer_day() -> dict:
    """A freezer (70 W) whose compressor draws 500 W for 3 s when it starts.
    The grid meter sees the peak for one reading at most; the plug reports it
    at once, then every 40 s: it says 500 W for 40 s."""
    loads = list(OTHERS)
    plug = [(T0, "0.0")]
    for t in CYCLES:
        loads += [Steady(t, t + 600, 70), Steady(t, t + 3, 430)]
        plug += [(t + 1.0, "500.0"), (t + 41.0, "70.0"), (t + 602.0, "0.0")]
    hist = house(loads)
    hist[FREEZER] = plug
    return hist


FREEZER_CFG = AnalysisConfig(grid_entity=GRID, submeters=[SubmeterConfig(FREEZER, "Freezer")])


class ShortPeakTest(Sameness):
    """A start-up peak seen for 3 s by the grid meter and for 40 s by the
    plug: the hole the subtraction digs leaves no step and no activation."""

    def test_the_hole_leaves_no_step(self):
        steps = steps_of(freezer_day(), FREEZER_CFG)
        holes = near(steps, CYCLES, within=60)
        self.assertGreaterEqual(len(holes), 2 * len(CYCLES) - 4)  # two per start, without the fix
        self.assertTrue(all(s.explained_by == "Freezer" for s in holes))
        # (a reading of the 70 W freezer taken 2 s off its plug's clock moves a median a little)
        self.assertSameSteps(free(steps), steps_of(house(OTHERS), REFERENCE), delta=40)

    def test_no_activation_in_the_report(self):
        report = analyze(freezer_day(), FREEZER_CFG, START, END, local_hour=UTC_HOUR)
        reference = analyze(house(OTHERS), REFERENCE, START, END, local_hour=UTC_HOUR)
        self.assertSameUses(report, reference)
        with mock.patch.object(SubmeterEchoes, "for_config", return_value=None):
            before = analyze(freezer_day(), FREEZER_CFG, START, END, local_hour=UTC_HOUR)
        self.assertEqual(report["quality"]["steps"], reference["quality"]["steps"])
        self.assertGreaterEqual(before["quality"]["steps"], reference["quality"]["steps"] + 60)


# ---------------------------------------------------------------------------
HOB = "sensor.hob_plug_power"
SESSION = (DAY0 + 19 * HOUR, DAY0 + 19 * HOUR + 900)


def hob_at(t: float) -> float:
    """An induction hob: it pulses between two power levels every few
    seconds (7 s high, 6 s low), and the cook changes the setting every
    two and a half minutes."""
    if not SESSION[0] <= t < SESSION[1]:
        return 0.0
    low, high = ((0.0, 1600.0), (1600.0, 2400.0), (0.0, 800.0), (800.0, 2400.0))[
        int((t - SESSION[0]) // 150) % 4
    ]
    return high if (t - SESSION[0]) % 13 < 7 else low


class Hob:
    power_at = staticmethod(hob_at)


def hob_day(synchronous: bool = False, extra=()) -> dict:
    hist = house([Hob(), *OTHERS, *extra])
    if synchronous:
        hist[HOB] = [(t, f"{hob_at(t):.1f}") for t, _ in hist[GRID]]
    else:  # read every 5 s, reported 1 to 4 s later
        hist[HOB] = plug_history(hob_at, T0, END, every_s=5.0, lag_s=(1.0, 4.0), seed=2)
    return hist


HOB_CFG = AnalysisConfig(grid_entity=GRID, submeters=[SubmeterConfig(HOB, "Hob")])
# a kettle used while the hob runs
KETTLE = Steady(SESSION[0] + 300, SESSION[0] + 480, 2000)


class ModulationTest(Sameness):
    """A sub-metered appliance that modulates faster than the meters report."""

    def in_session(self, steps):
        return [s for s in steps if SESSION[0] - 60 <= s.ts <= SESSION[1] + 60]

    def test_modulation_leaves_almost_no_step(self):
        steps = steps_of(hob_day(), HOB_CFG)
        raw = self.in_session(steps)
        self.assertGreaterEqual(len(raw), 10)
        self.assertLessEqual(len(free(raw)), len(raw) // 4)
        outside = [s for s in steps if s not in raw]
        self.assertTrue(all(not s.explained_by for s in outside))

    def test_a_synchronous_plug_explains_nothing(self):
        # the plug reports with the grid meter: the subtraction is right, and
        # appliances used during the session keep their steps, whatever the
        # hob does at the reading where they switch
        for shift in range(0, 130, 10):
            with self.subTest(shift=shift):
                extra = [
                    Steady(SESSION[0] + 300 + shift, SESSION[0] + 480 + shift, 2000),
                    Steady(SESSION[0] + 60 + shift, SESSION[0] + 90 + shift, 1600),
                    Steady(SESSION[0] + 600 + shift, SESSION[0] + 800 + shift, 800),
                ]
                steps = steps_of(hob_day(synchronous=True, extra=extra), HOB_CFG)
                self.assertEqual([s for s in steps if s.explained_by], [])
                self.assertSameSteps(steps, steps_of(house([*OTHERS, *extra]), REFERENCE))


# ---------------------------------------------------------------------------
class SynchronousTest(Sameness):
    """Sub-meters that report with the grid meter: nothing changes."""

    def setUp(self):
        # another 1.5 kW appliance switching within 45 s of the heater, both ways
        self.twin = [
            Steady(HEATER[3].start + 20, HEATER[3].end - 20, 1500),
            Steady(HEATER[5].end + 30, HEATER[5].end + 400, 1500),
        ]
        self.hist = house([*HEATER, *OTHERS, *self.twin])
        self.hist[PLUG] = [(t, f"{heater_at(t):.1f}") for t, _ in self.hist[GRID]]

    def test_no_step_is_explained(self):
        cfg = config()
        samples = night_samples(self.hist, cfg)
        steps = find_steps(samples, cfg)
        self.assertEqual([s for s in steps if s.explained_by], [])
        points = [(s.ts, s.residual) for s in samples]
        self.assertEqual(key(steps), key(detect_steps(points, threshold=300)))
        self.assertEqual(len(near(steps, [u.start for u in self.twin] + [u.end for u in self.twin], 15)), 4)

    def test_the_report_is_unchanged(self):
        report = analyze(self.hist, config(), START, END, local_hour=UTC_HOUR)
        with mock.patch.object(SubmeterEchoes, "for_config", return_value=None):
            before = analyze(self.hist, config(), START, END, local_hour=UTC_HOUR)
        (q,) = report["quality"]["submeters"]
        self.assertEqual((q["explained_steps"], q["changes"], q["lag_s"]), (0, 2 * len(HEATER), 0.0))
        self.assertEqual(before["quality"]["submeters"], [])
        for r in (report, before):
            r["quality"].pop("submeters")
        self.assertEqual(stable(report), stable(before))

    def test_no_sub_meter_no_explanation(self):
        self.assertIsNone(SubmeterEchoes.for_config(REFERENCE))
        report = analyze(house(OTHERS), REFERENCE, START, END, local_hour=UTC_HOUR)
        self.assertEqual(report["quality"]["submeters"], [])

    def test_samples_keep_what_each_sub_meter_took(self):
        cfg = AnalysisConfig(
            grid_entity=GRID,
            submeters=[SubmeterConfig(PLUG, NAME), SubmeterConfig("sensor.light_power", "Light", True)],
        )
        hist = {**self.hist, "sensor.light_power": [(T0, "40")]}
        for s in night_samples(hist, cfg)[::500]:
            self.assertEqual(len(s.subs), 2)
            self.assertAlmostEqual(sum(s.subs), s.submetered)
            self.assertEqual(s.subs[1], s.estimated)


# ---------------------------------------------------------------------------
class HiddenAndEstimatedTest(unittest.TestCase):
    def test_an_estimate_explains_nothing(self):
        # an estimation integration's power is not a measurement: the steps
        # its subtraction leaves stay (and can be seen in the Live tab)
        hist = lagged(**LATE)
        self.assertIsNone(SubmeterEchoes.for_config(config(estimated=True)))
        steps = steps_of(hist, config(estimated=True))
        self.assertEqual([s for s in steps if s.explained_by], [])
        self.assertGreaterEqual(len(near(steps, SWITCHINGS)), 20)

    def test_a_hidden_device_keeps_its_steps(self):
        # the self-test (#15) and teaching (#14) leave the device in the
        # curve: its own steps are not explained by its own readings
        hist = lagged(**LATE)
        hidden = selftest.hidden_config(config(), PLUG)
        steps = steps_of(hist, hidden)
        self.assertEqual([s for s in steps if s.explained_by], [])
        mine = [s for s in near(steps, SWITCHINGS, 15) if abs(abs(s.delta) - 1500) < 100]
        self.assertEqual(len(mine), 2 * len(HEATER))

    def test_the_self_test_still_finds_a_hidden_device(self):
        hist = lagged(**LATE)
        cfg = config()
        hidden = selftest.hidden_config(cfg, PLUG)
        test = selftest.hide_and_find(
            "day",
            PLUG,
            NAME,
            parse_numeric(hist[PLUG]),
            START,
            END,
            lambda: analyze(hist, hidden, START, END, local_hour=UTC_HOUR),
            None,
            [],
            END,
        )
        self.assertEqual(test["status"], selftest.TESTED)
        ev = test["evaluation"]
        self.assertEqual(ev["uses"], len(HEATER))
        self.assertEqual(ev["found"], ev["uses"])

    def test_another_sub_meter_does_not_take_them(self):
        # two plugs: the hidden one's steps are not explained by the other one
        hist = lagged(**LATE)
        hist[FREEZER] = [(T0, "0.0")]
        cfg = AnalysisConfig(
            grid_entity=GRID, submeters=[SubmeterConfig(FREEZER, "Freezer"), SubmeterConfig(PLUG, NAME)]
        )
        self.assertTrue(all(s.explained_by == NAME for s in steps_of(hist, cfg) if s.explained_by))
        hidden = selftest.hidden_config(cfg, PLUG)
        self.assertEqual([s for s in steps_of(hist, hidden) if s.explained_by], [])


# ---------------------------------------------------------------------------
class LiveTest(unittest.TestCase):
    """The live view explains the same steps as the night."""

    def assertEquivalent(self, hist, cfg, **kwargs):
        night = steps_of(hist, cfg)
        live = replay(hist, cfg, local_hour=UTC_HOUR, **kwargs)
        self.assertEqual(key(night), key(live.steps))
        return night, live

    def test_live_equals_night(self):
        cases = {
            "late": (lagged(**LATE), config()),
            "late, another seed": (lagged(seed=7, **LATE), config()),
            "early": (lagged(**EARLY), config()),
            "short peak": (freezer_day(), FREEZER_CFG),
            "modulation": (hob_day(), HOB_CFG),
            "synchronous": (hob_day(synchronous=True, extra=[KETTLE]), HOB_CFG),
        }
        for name, (hist, cfg) in cases.items():
            with self.subTest(day=name):
                night, _ = self.assertEquivalent(hist, cfg)
                if name != "synchronous":
                    self.assertGreaterEqual(sum(1 for s in night if s.explained_by), 10)

    def test_irregular_readings_and_estimated_interval(self):
        hist = lagged(**LATE)
        ragged = {**jitter({GRID: hist[GRID]}, seed=9, sigma=2.0), PLUG: hist[PLUG]}
        self.assertEquivalent(ragged, config())
        self.assertEquivalent(lagged(**LATE), config(), interval_s=None)

    def test_with_the_night_threshold(self):
        hist = lagged(**LATE)
        hist["sensor.pv_power"] = [(T0, "0")]
        cfg = config(pv_entity="sensor.pv_power", night_step_threshold_w=150)
        self.assertEquivalent(hist, cfg)

    def test_the_feed_shows_no_use_of_the_heater(self):
        live = replay(lagged(**LATE), config(), local_hour=UTC_HOUR)
        reference = replay(house(OTHERS), REFERENCE, local_hour=UTC_HOUR)
        shown = [(e.t_on, e.t_off, round(e.delta_w, -1)) for e in live.events if e.state == CONFIRMED]
        expected = [(e.t_on, e.t_off, round(e.delta_w, -1)) for e in reference.events if e.state == CONFIRMED]
        self.assertEqual(shown, expected)
        # an explained step comes out cancelled, with the sub-meter's name
        explained = [u for u in live.updates if u.explained_by]
        self.assertGreaterEqual(len(explained), 20)
        self.assertTrue(all(u.state == CANCELLED and u.explained_by == NAME for u in explained))

    def test_a_step_waits_for_the_sub_meters(self):
        # a step of the house itself: confirmed once no plug can explain it
        # any more (45 s and the merge delay), still provisional before
        t_on = DAY0 + 600
        det = LiveDetector(config(), interval_s=10)
        log = []
        for i in range(120):
            t = DAY0 + 10 * i
            det.push(GRID, t, "2250" if t >= t_on else "250")
            det.push(PLUG, t, "0")
            for st in det.advance(t + 1).steps:
                log.append((t, st.state))
        self.assertEqual([s for _, s in log], [PROVISIONAL, CONFIRMED])
        reach = det.echoes.reach(det.stream.merge_s)
        self.assertGreater(log[1][0] - t_on, reach)
        self.assertLessEqual(log[1][0] - t_on, reach + 2 * 10)

    def test_no_wait_without_a_sub_meter_that_measures(self):
        for cfg in (REFERENCE, config(estimated=True)):
            t_on = DAY0 + 600
            det = LiveDetector(cfg, interval_s=10)
            self.assertIsNone(det.echoes)
            confirmed = None
            for i in range(120):
                t = DAY0 + 10 * i
                det.push(GRID, t, "2250" if t >= t_on else "250")
                for st in det.advance(t + 1).steps:
                    if st.state == CONFIRMED and confirmed is None:
                        confirmed = t
            self.assertLessEqual(confirmed - t_on, det.stream.window_s + det.stream.merge_s + 10)

    def test_memory_is_bounded(self):
        det = LiveDetector(config(), interval_s=10)
        for i in range(5000):
            t = DAY0 + 10 * i
            det.push(GRID, t, "250")
            det.push(PLUG, t, "0")
            det.advance(t + 1)
        self.assertLess(len(det.echoes._ts), 1000)


# ---------------------------------------------------------------------------
PV = "sensor.pv_power"
# a heater on a plug that bursts like a thermostat: 90 s every 5 minutes
BURSTS = [Steady(DAY0 + 6 * HOUR + i * 300, DAY0 + 6 * HOUR + i * 300 + 90, 1500) for i in range(16)]


def bursts_at(t: float) -> float:
    return sum(u.power_at(t) for u in BURSTS)


class WithTheOtherFixesTest(Sameness):
    """With the solar doubt (#86) and the burst series (#87) at work too."""

    def cloudy(self, seed, lag, night):
        """A day of clouds seen late by the solar meter, the heater on its
        lagging plug: ``(histories, config, reference histories and config)``."""
        hist, _ = cloudy_day(seed=seed, loads=(*HEATER, *OTHERS))
        hist[PLUG] = plug_history(heater_at, T0, END, seed=seed, **lag)
        ref, _ = cloudy_day(seed=seed, loads=tuple(OTHERS))
        cfg = config(pv_entity=PV, night_step_threshold_w=night)
        return hist, cfg, ref, AnalysisConfig(grid_entity=GRID, pv_entity=PV, night_step_threshold_w=night)

    def test_cloudy_solar_and_lagged_plug_live_equals_night(self):
        for seed in (1, 2, 3):
            for lag in (LATE, EARLY):
                for night in (None, 150.0):
                    with self.subTest(seed=seed, lag=lag["lag_s"], night=night):
                        hist, cfg, _, _ = self.cloudy(seed, lag, night)
                        held: list[float] = []
                        steps, _, _ = explained_steps(night_samples(hist, cfg), cfg, held)
                        # both at work: readings set aside by the solar doubt, steps explained
                        self.assertGreater(len(held), 100)
                        explained = [s for s in steps if s.explained_by]
                        self.assertGreaterEqual(len(explained), 20)
                        live = replay(hist, cfg, local_hour=UTC_HOUR)
                        self.assertEqual(key(steps), key(live.steps))

    def test_cloudy_solar_the_plug_still_leaves_no_step(self):
        # the sub-meters are read against the points the detector read: the
        # steps left are those of the same cloudy day without the heater
        for seed, lag in ((2, LATE), (2, EARLY), (3, LATE), (3, EARLY), (1, LATE)):
            with self.subTest(seed=seed, lag=lag["lag_s"]):
                hist, cfg, ref, ref_cfg = self.cloudy(seed, lag, None)
                steps = steps_of(hist, cfg)
                self.assertTrue(all(near([s], SWITCHINGS) for s in steps if s.explained_by))
                self.assertSameSteps(free(steps), steps_of(ref, ref_cfg))
        # a known limit: the solar doubt dates a step of the house up to a
        # minute late, out of the reach of the plug's early report
        hist, cfg, ref, ref_cfg = self.cloudy(1, EARLY, None)
        steps = steps_of(hist, cfg)
        self.assertEqual(len(free(steps)), len(steps_of(ref, ref_cfg)) + 1)
        self.assertEqual(len(near(free(steps), SWITCHINGS)), 1)

    def test_explained_steps_do_not_make_a_burst_series(self):
        # reported a steady 30 s late, each burst leaves a 30 s use of
        # 1.5 kW every 5 minutes: a thermostat that does not exist
        hist = house([*BURSTS, *OTHERS])
        hist[PLUG] = plug_history(bursts_at, T0, END, every_s=1.0, lag_s=(30.0, 30.0))
        reference = analyze(house(OTHERS), REFERENCE, START, END, local_hour=UTC_HOUR)
        with mock.patch.object(SubmeterEchoes, "for_config", return_value=None):
            before = analyze(hist, config(), START, END, local_hour=UTC_HOUR)
        phantom = [a for a in before["activations"] if a.get("blocks")]
        self.assertEqual(len(phantom), 1)
        self.assertGreaterEqual(len(phantom[0]["blocks"]), 4)
        report = analyze(hist, config(), START, END, local_hour=UTC_HOUR)
        self.assertEqual([a for a in report["activations"] if a.get("blocks")], [])
        self.assertSameUses(report, reference)
        # live: no line for the bursts, and the night's steps
        live = replay(hist, config(), local_hour=UTC_HOUR)
        self.assertEqual(key(steps_of(hist, config())), key(live.steps))
        hour = [
            e
            for e in live.events
            if e.state == CONFIRMED and BURSTS[0].start - 60 <= e.t_on <= BURSTS[-1].end + 60
        ]
        self.assertEqual(hour, [])


# ---------------------------------------------------------------------------
def series(rows, dt=10.0):
    """``rows``: (house, plug) per grid reading; the residual and the plug's part."""
    return [(DAY0 + i * dt, house - plug, plug) for i, (house, plug) in enumerate(rows)]


class RuleTest(unittest.TestCase):
    """The rules, on hand-made readings (a 10 s meter: window 31 s, merge 15 s)."""

    def explain(self, rows, deltas):
        """Feed the steps ``{index: delta}`` of the readings ``rows``."""
        echoes = SubmeterEchoes(["Plug"])
        for t, r, x in series(rows):
            echoes.add(t, r, [x])
        steps = [Step(DAY0 + 10 * i, d, 0.0, d) for i, d in sorted(deltas.items())]
        for s in steps:
            echoes.feed(s, 31.0, 15.0)
        return steps, echoes

    def test_late_report(self):
        # the house steps at reading 10, the plug says so at reading 14
        rows = [(250, 0)] * 10 + [(1750, 0)] * 4 + [(1750, 1500)] * 10
        steps, echoes = self.explain(rows, {10: 1500, 14: -1500})
        self.assertEqual([s.explained_by for s in steps], ["Plug", "Plug"])
        (echo,) = echoes.echoes
        self.assertEqual((echo.ts, echo.partner, echo.change), (DAY0 + 140, DAY0 + 100, 1500))

    def test_early_report_and_halved_step(self):
        # the plug first; the detector's medians halved the house's step
        rows = [(250, 0)] * 10 + [(250, 1500)] * 3 + [(1750, 1500)] * 10
        steps, _ = self.explain(rows, {10: -1500, 13: 750})
        self.assertEqual([s.explained_by for s in steps], ["Plug", "Plug"])

    def test_a_step_of_the_house_stays(self):
        # another appliance switches on 20 s after the plug's own (synchronous) step
        rows = [(250, 0)] * 10 + [(1750, 1500)] * 2 + [(3250, 1500)] * 10
        steps, echoes = self.explain(rows, {12: 1500})
        self.assertEqual([s.explained_by for s in steps], [None])
        self.assertEqual(echoes.echoes, [])

    def test_partner_too_far_or_of_another_size(self):
        rows = [(250, 0)] * 10 + [(1750, 0)] * 8 + [(1750, 1500)] * 10
        steps, _ = self.explain(rows, {10: 1500, 18: -1500})  # 80 s apart
        self.assertEqual([s.explained_by for s in steps], [None, "Plug"])
        # the house moved by 700 W only: not the plug's change
        rows = [(250, 0)] * 10 + [(950, 0)] * 4 + [(950, 1500)] * 10
        steps, _ = self.explain(rows, {10: 700, 14: -1500})
        self.assertEqual([s.explained_by for s in steps], [None, "Plug"])

    def test_one_partner_per_echo(self):
        # two steps of the plug's size before its report: the nearest one is its own
        rows = [(250, 0)] * 10 + [(1750, 0)] * 2 + [(3250, 0)] * 2 + [(3250, 1500)] * 10
        steps, _ = self.explain(rows, {10: 1500, 12: 1500, 14: -1500})
        self.assertEqual([s.explained_by for s in steps], [None, "Plug", "Plug"])

    def test_a_peak_of_one_reading_is_no_change(self):
        # found on a real installation: a pump starts (its meter reads a 3 kW
        # inrush for one reading) right after another appliance switched off
        rows = [(1550, 0)] * 10 + [(250, 0)] + [(1225, 3100)] + [(1225, 975)] * 10
        steps, echoes = self.explain(rows, {10: -1300})
        self.assertEqual([s.explained_by for s in steps], [None])
        self.assertEqual(echoes.echoes, [])

    def test_modulated(self):
        # the plug pulses every other reading, the house one reading before it
        pulse = [0, 0, 1600, 1600] * 4
        rows = (
            [(250, 0)] * 6 + [(250 + a, b) for a, b in zip(pulse[1:], pulse, strict=False)] + [(250, 0)] * 6
        )
        steps, echoes = self.explain(rows, {12: 1600, 15: -800})
        self.assertEqual([s.explained_by for s in steps], ["Plug", "Plug"])
        self.assertTrue(all(e.modulated and e.partner is None for e in echoes.echoes))
        # read late, a plug that pulses by 1600 W is wrong by up to twice that; not more
        steps, _ = self.explain(rows, {12: 3000})
        self.assertEqual([s.explained_by for s in steps], ["Plug"])
        steps, _ = self.explain(rows, {12: 4000})
        self.assertEqual([s.explained_by for s in steps], [None])
        # the same pulses, followed by the house: nothing to explain
        rows = [(250, 0)] * 6 + [(250 + a, a) for a in pulse] + [(250, 0)] * 6
        steps, _ = self.explain(rows, {12: 1600})
        self.assertEqual([s.explained_by for s in steps], [None])

    def test_the_house_follows_a_plug_that_pulses(self):
        # a plug that reports with the grid meter and pulses at every other
        # reading; another 1.6 kW appliance switches off at the very reading
        # where the plug goes up: at that reading alone it looks like a late
        # report, but around it the house follows the plug
        pulse = [0, 0, 1600, 1600] * 5
        rows = [(250 + a, a) for a in pulse]
        rows = [(h + 1600, x) for h, x in rows[:10]] + rows[10:]
        steps, echoes = self.explain(rows, {10: -1600})
        self.assertEqual([s.explained_by for s in steps], [None])
        self.assertEqual(echoes.echoes, [])

    def test_reach(self):
        self.assertEqual(SubmeterEchoes(["Plug"]).reach(15.0), LAG_S + 15.0)


if __name__ == "__main__":
    unittest.main()
