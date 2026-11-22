"""Tests of the burst series (#87): the regular bursts of a thermostat are
one cycling use.

A heater regulating on its own thermostat switches on for some tens of
seconds every few minutes. Its steps are too far apart for a cycling
session, so each burst used to be one short activation: hundreds of uses of
a "40 s appliance". Synthetic households only."""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from nilm_core import AnalysisConfig, analyze, report  # noqa: E402
from nilm_core.activations import (  # noqa: E402
    BURSTS,
    Activation,
    BurstConfig,
    burst_series,
    find_cycling_sessions,
    group_bursts,
)
from nilm_core.correlation import CorrelationConfig, signature_edges, signature_uses  # noqa: E402
from nilm_core.live import CandidateRanker, LiveEvent, LivePairer, replay  # noqa: E402
from nilm_core.registry import update  # noqa: E402
from nilm_core.steps import CANCELLED, CONFIRMED, LiveStep, Step  # noqa: E402
from synth import DAY0, HOUR, Cycling, House, Steady  # noqa: E402
from synth_cycles import dishwasher, make_day  # noqa: E402
from test_live import night_steps  # noqa: E402

GRID = "sensor.grid_power"
CFG = AnalysisConfig(grid_entity=GRID)
UTC_HOUR = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731
MIN = 60.0


def day_report(loads, dt=5, **house):
    hist = House(base_w=250, loads=loads, **house).histories(DAY0 - 3 * HOUR, DAY0 + 24 * HOUR, dt=dt)
    return analyze(hist, CFG, DAY0, DAY0 + 24 * HOUR, local_hour=UTC_HOUR), hist


def thermostat(start=DAY0 + 6 * HOUR, hours=2.0, power=1000, on_s=40, period_s=240):
    """A heater on its thermostat: ``on_s`` every ``period_s``."""
    return Cycling(start, start + hours * HOUR, power, on_s, period_s - on_s)


def units(*starts, on_s=40.0, power=1000.0):
    return [(t, t + on_s, power) for t in starts]


def burst(start, on_s=40.0, power=1000.0):
    """One burst as the pairing gives it."""
    steps = [Step(start, power, 250, 250 + power), Step(start + on_s, -power, 250 + power, 250)]
    return Activation(start=start, end=start + on_s, power_w=power, steps=steps)


# ---------------------------------------------------------------------------
class SeriesRuleTests(unittest.TestCase):
    """:func:`burst_series` on hand-made bursts."""

    def test_regular_bursts_are_one_series(self):
        series = burst_series(units(*(i * 240.0 for i in range(10))))
        self.assertEqual(series, [[[i] for i in range(10)]])

    def test_needs_four_blocks(self):
        self.assertEqual(burst_series(units(0, 240, 480)), [])
        self.assertEqual(len(burst_series(units(0, 240, 480, 720))), 1)

    def test_two_kettles_twenty_minutes_apart(self):
        self.assertEqual(burst_series(units(0, 1200, on_s=180, power=2000)), [])
        # and four of them: still too far apart
        self.assertEqual(burst_series(units(0, 1200, 2400, 3600, on_s=180, power=2000)), [])

    def test_irregular_short_uses_are_not_merged(self):
        # a microwave used six times in half an hour, at any intervals
        starts = (0, 200, 760, 1010, 1700, 1830)
        self.assertEqual(burst_series(units(*starts, on_s=90, power=1200)), [])

    def test_bursts_close_together_are_one_block(self):
        # a radiator bursting every 80 s while its relay allows it, the
        # relay opening every 10 minutes: blocks of three, two, then one burst
        starts = [0, 80, 160, 600, 680, 760, 1200, 1280, 1800, 1880, 2400, 3000]
        (series,) = burst_series(units(*starts))
        self.assertEqual([len(b) for b in series], [3, 3, 2, 2, 1, 1])

    def test_other_power_is_another_series(self):
        a = units(*(i * 240.0 for i in range(6)), power=1000)
        b = units(*(100 + i * 300.0 for i in range(6)), power=1500)
        both = sorted(a + b)
        series = burst_series(both)
        self.assertEqual(len(series), 2)
        for blocks in series:
            self.assertEqual(len({both[i][2] for b in blocks for i in b}), 1)
            self.assertEqual(len(blocks), 6)

    def test_period_must_stay_regular(self):
        # three regular bursts, then the fourth one comes much later: the
        # series never gets its four blocks
        self.assertEqual(burst_series(units(0, 240, 480, 1100)), [])
        # a slow drift is a thermostat: each period within 25 % of the last ones
        starts = [0.0]
        for i in range(12):
            starts.append(starts[-1] + 240 * 1.06**i)
        self.assertEqual(len(burst_series(units(*starts))[0]), 13)

    def test_longest_period(self):
        self.assertEqual(len(burst_series(units(*(i * 900.0 for i in range(5))))), 1)
        self.assertEqual(burst_series(units(*(i * 960.0 for i in range(5)))), [])

    def test_one_missed_burst_does_not_end_a_series(self):
        starts = [i * 240.0 for i in range(12) if i != 6]
        self.assertEqual(len(burst_series(units(*starts))[0]), 11)
        # two in a row do: the rest is a series of its own
        starts = [i * 240.0 for i in range(14) if i not in (6, 7)]
        self.assertEqual([len(s) for s in burst_series(units(*starts))], [6, 6])
        # and a series that is not one yet cannot miss any
        self.assertEqual(burst_series(units(0, 240, 480, 960)), [])

    def test_an_intruder_does_not_end_a_series(self):
        # another short use of the same power in the middle of a series: it
        # stays alone, and the next burst goes to the series (the longest)
        regular = units(*(i * 600.0 for i in range(10)))
        both = sorted([*regular, (3300.0, 3390.0, 1000.0)])
        (series,) = burst_series(both)
        self.assertEqual([both[i] for b in series for i in b], regular)
        # right after a burst, it is in its block
        both = sorted([*regular, (3100.0, 3190.0, 1000.0)])
        (series,) = burst_series(both)
        self.assertEqual([len(b) for b in series], [1, 1, 1, 1, 1, 2, 1, 1, 1, 1])

    def test_a_use_before_the_series_does_not_prevent_it(self):
        series = burst_series(units(0, 500, 740, 980, 1220, 1460))
        self.assertEqual(series, [[[1], [2], [3], [4], [5]]])

    def test_is_burst(self):
        self.assertTrue(BURSTS.is_burst(1000, 40))
        self.assertFalse(BURSTS.is_burst(1000, 300))  # a heating phase
        self.assertFalse(BURSTS.is_burst(400, 40))  # a motor
        self.assertTrue(BURSTS.is_burst(1400, 900, cycling=True))  # a session

    def test_group_bursts_builds_one_cycling_use(self):
        pairs = [burst(DAY0 + i * 240.0) for i in range(8)] + [burst(DAY0 + 50, on_s=180, power=2000)]
        sessions, rest = group_bursts([], pairs)
        self.assertEqual(len(sessions), 1)
        self.assertEqual([a.power_w for a in rest], [2000])
        use = sessions[0]
        self.assertTrue(use.cycling)
        self.assertEqual((use.start, use.end), (DAY0, DAY0 + 7 * 240 + 40))
        self.assertEqual((use.cycles, use.on_s, use.period_s), (8, 40.0, 240.0))
        self.assertAlmostEqual(use.duty, 8 * 40 / (7 * 240 + 40), places=3)
        self.assertAlmostEqual(use.energy_kwh, 8 * 40 * 1000 / 3.6e6, delta=0.001)
        self.assertEqual(len(use.blocks), 8)
        # its exact pattern: on during a burst, off between two
        self.assertEqual(use.power_at(DAY0 + 240 + 10), 1000)
        self.assertEqual(use.power_at(DAY0 + 240 + 100), 0.0)
        self.assertAlmostEqual(use.overlap_energy_wh(DAY0 + 60, DAY0 + 240), 0.0)
        self.assertAlmostEqual(use.overlap_energy_wh(DAY0 + 240, DAY0 + 300), 1000 * 40 / 3600)
        self.assertAlmostEqual(use.overlap_energy_wh(DAY0 - 100, DAY0 + 1e6), use.energy_kwh * 1000, delta=1)

    def test_nothing_to_group(self):
        pairs = [burst(DAY0, on_s=180, power=2000), burst(DAY0 + 1200, on_s=180, power=2000)]
        self.assertEqual(group_bursts([], pairs), ([], pairs))

    def test_a_session_ending_on_an_on_step(self):
        # the session of the first block ends on an on-step (its off-step
        # was not seen): it must not be on until the next block
        ups = [DAY0, DAY0 + 80, DAY0 + 160, DAY0 + 240]
        steps = []
        for t in ups[:3]:
            steps += [Step(t, 1000, 250, 1250), Step(t + 40, -1000, 1250, 250)]
        steps.append(Step(ups[3], 1000, 250, 1250))
        (session,) = find_cycling_sessions(steps)
        self.assertEqual(session.end, ups[3])
        pairs = [burst(DAY0 + i * 600.0) for i in range(1, 5)]
        (use,), rest = group_bursts([session], pairs)
        self.assertEqual(rest, [])
        self.assertEqual(use.cycles, 7)
        self.assertAlmostEqual(use.duty, 7 * 40 / (4 * 600 + 40), places=3)
        self.assertEqual(use.power_at(DAY0 + 400), 0.0)


# ---------------------------------------------------------------------------
class NightTests(unittest.TestCase):
    """The daily analysis of synthetic households."""

    def test_thermostat_is_one_cycling_use(self):
        # a 1 kW heater: 40 s every 4 minutes for 2 hours
        r, _ = day_report([thermostat()])
        (a,) = r["activations"]
        self.assertTrue(a["cycling"])
        self.assertAlmostEqual(a["power_w"], 1000, delta=30)
        self.assertAlmostEqual(a["start"], DAY0 + 6 * HOUR, delta=10)
        self.assertAlmostEqual(a["end"], DAY0 + 8 * HOUR - 200, delta=10)
        self.assertAlmostEqual(a["on_s"], 40, delta=5)
        self.assertAlmostEqual(a["period_s"], 240, delta=5)
        self.assertAlmostEqual(a["duty"], 40 / 240, delta=0.02)
        self.assertEqual(len(a["blocks"]), 30)
        truth = 30 * 40 * 1000 / 3.6e6  # 0.333 kWh
        energy = a["power_w"] * a["duty"] * (a["end"] - a["start"]) / 3.6e6
        self.assertAlmostEqual(energy, truth, delta=0.03)
        (sig,) = r["signatures"]
        self.assertEqual((sig["id"], sig["kind"], sig["count"]), ("cyc-1000w", "cycling", 1))
        self.assertAlmostEqual(sig["energy_kwh"], truth, delta=0.03)
        # the curve gives it its energy, in the minutes of its bursts only
        self.assertAlmostEqual(r["energy_kwh"]["signatures"]["cyc-1000w"], truth, delta=0.04)
        rows = [row for row in r["curve"] if DAY0 + 6 * HOUR <= row["t"] < DAY0 + 8 * HOUR - 200]
        on = [row for row in rows if row["signatures"]]
        self.assertLess(len(on), 0.6 * len(rows))

    def test_slower_meter(self):
        r, _ = day_report([thermostat()], dt=10)
        (a,) = r["activations"]
        self.assertTrue(a["cycling"])
        self.assertEqual(len(a["blocks"]), 30)

    def test_kettles_are_unchanged(self):
        kettles = [
            Steady(DAY0 + 7 * HOUR, DAY0 + 7 * HOUR + 180, 2000),
            Steady(DAY0 + 7 * HOUR + 20 * MIN, DAY0 + 7 * HOUR + 20 * MIN + 200, 2000),
            Steady(DAY0 + 16 * HOUR, DAY0 + 16 * HOUR + 150, 2000),
        ]
        r, _ = day_report(kettles)
        self.assertEqual(len(r["activations"]), 3)
        self.assertFalse(any(a["cycling"] or "blocks" in a for a in r["activations"]))
        self.assertEqual([s["id"] for s in r["signatures"]], ["short-2000w"])

    def test_unrelated_short_uses_are_not_merged(self):
        # a 1.2 kW microwave used six times around a meal, and a toaster twice
        t = DAY0 + 12 * HOUR
        uses = [Steady(t + s, t + s + d, 1200) for s, d in ((0, 90), (200, 60), (760, 120), (1010, 45))]
        uses += [Steady(t + s, t + s + d, 1200) for s, d in ((1700, 90), (1900, 60))]
        uses += [Steady(t + 400, t + 520, 800), Steady(t + 1200, t + 1320, 800)]
        r, _ = day_report(uses)
        self.assertEqual(len(r["activations"]), 8)
        self.assertFalse(any(a["cycling"] for a in r["activations"]))
        self.assertEqual(sorted(s["count"] for s in r["signatures"]), [2, 6])

    def test_hob_session_is_unchanged(self):
        hob = Cycling(DAY0 + 19 * HOUR, DAY0 + 19.5 * HOUR, 1400, 45, 25)
        r, hist = day_report([hob])
        (a,) = r["activations"]
        (night,) = find_cycling_sessions(night_steps(hist, CFG))
        self.assertNotIn("blocks", a)
        self.assertEqual(
            (a["start"], a["end"], a["power_w"], a["duty"], a["on_s"], a["period_s"]),
            (night.start, night.end, round(night.power_w), night.duty, night.on_s, night.period_s),
        )
        self.assertAlmostEqual(r["energy_kwh"]["signatures"]["cyc-1400w"], night.energy_kwh, delta=0.02)

    def test_heater_next_to_kettles_and_a_hob(self):
        loads = [
            thermostat(),
            Steady(DAY0 + 6.5 * HOUR + 70, DAY0 + 6.5 * HOUR + 190, 2000),  # between two bursts
            Steady(DAY0 + 7 * HOUR + 60, DAY0 + 7 * HOUR + 300, 2000),  # during one
            Cycling(DAY0 + 12 * HOUR, DAY0 + 12 * HOUR + 9 * 70, 1500, 45, 25),  # a hob
        ]
        r, _ = day_report(loads)
        by = {s["id"]: s for s in r["signatures"]}
        self.assertEqual(sorted(by), ["cyc-1000w", "cyc-1500w", "short-2000w"])
        self.assertEqual([by[k]["count"] for k in sorted(by)], [1, 1, 2])
        (heater,) = [a for a in r["activations"] if a["signature"] == "cyc-1000w"]
        self.assertEqual(len(heater["blocks"]), 30)
        (hob,) = [a for a in r["activations"] if a["signature"] == "cyc-1500w"]
        self.assertNotIn("blocks", hob)

    def test_radiator_behind_a_relay(self):
        # 40 s every 80 s while its pilot-wire relay allows it; the relay
        # opens every 10 minutes, for less and less time as the room warms
        t0 = DAY0 + 6 * HOUR
        loads = []
        for i, allowed in enumerate((330, 330, 250, 250, 170, 170, 90, 90, 90, 90)):
            loads.append(Cycling(t0 + i * 600, t0 + i * 600 + allowed, 930, 40, 40))
        r, _ = day_report(loads)
        (a,) = r["activations"]
        self.assertTrue(a["cycling"])
        self.assertEqual(len(a["blocks"]), 10)
        bursts = 2 * 4 + 2 * 3 + 2 * 2 + 4 * 1
        truth = bursts * 40 * 930 / 3.6e6
        self.assertAlmostEqual(a["power_w"] * a["duty"] * (a["end"] - a["start"]) / 3.6e6, truth, delta=0.02)
        self.assertEqual(r["signatures"][0]["id"], "cyc-950w")

    def test_joins_the_registry_signature_of_its_sessions(self):
        # the days before, the radiator was seen as short sessions and
        # single bursts: the series keeps the cycling signature's identity
        import datetime as dt

        from test_registry import D0, act, midnight, summary

        sums = {}
        for i in range(5):
            d = D0 + dt.timedelta(days=i)
            sums[d.isoformat()] = summary(
                d, [act(d, 6 + k / 6, 930, 240, cycling=True, duty=0.5) for k in range(4)]
            )
        reg = update(None, sums, midnight(D0 + dt.timedelta(days=5)))
        (before,) = reg["signatures"]
        d = D0 + dt.timedelta(days=5)
        series = act(d, 6, 925, 7200, cycling=True, duty=0.17)
        series["blocks"] = [[series["start"] + k * 600, series["start"] + k * 600 + 40] for k in range(12)]
        sums[d.isoformat()] = summary(d, [series])
        reg = update(reg, sums, midnight(D0 + dt.timedelta(days=6)))
        (after,) = reg["signatures"]
        self.assertEqual((after["id"], after["kind"], after["band"]), (before["id"], "cycling", "session"))
        self.assertEqual(after["count"], before["count"] + 1)


# ---------------------------------------------------------------------------
class MultiPhaseTests(unittest.TestCase):
    """The multi-phase cycles (#26) keep their heating phases."""

    def test_synthetic_cycle_days_are_unchanged(self):
        # dishwashers, washing machines, tumble dryers (a heater cycling on
        # its thermostat, on for minutes), kettles, ovens, hobs: no series,
        # and the reports are those of the analysis without the grouping
        for index in range(8):
            house, day0, _ = make_day(index)
            hist = house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR, dt=10)
            with_ = analyze(hist, CFG, day0, day0 + 24 * HOUR, local_hour=UTC_HOUR)
            with mock.patch.object(report, "group_bursts", lambda sessions, pairs: (sessions, pairs)):
                without = analyze(hist, CFG, day0, day0 + 24 * HOUR, local_hour=UTC_HOUR)
            self.assertFalse(any("blocks" in a for a in with_["activations"]), index)
            self.assertEqual(with_["activations"], without["activations"], index)
            self.assertEqual(with_["cycles"], without["cycles"], index)

    def test_cycle_next_to_a_thermostat(self):
        # a dishwasher runs while a heater regulates: the candidate cycle
        # keeps its phases and does not take the heater's energy
        import random

        cycle = dishwasher(DAY0 + 10 * HOUR, random.Random(3))
        alone, _ = day_report([cycle], dt=10)
        # (its bursts a minute away from the dishwasher's own steps: two
        # loads switching together are one step, another matter)
        heater = thermostat(DAY0 + 9 * HOUR + 100, hours=4)
        both, _ = day_report([cycle, heater], dt=10)
        (c0,), (c1,) = alone["cycles"], both["cycles"]
        self.assertEqual((c1["start"], c1["end"], c1["members"]), (c0["start"], c0["end"], c0["members"]))
        self.assertEqual([p["kind"] for p in c1["phases"]], [p["kind"] for p in c0["phases"]])
        self.assertAlmostEqual(c1["energy_kwh"], c0["energy_kwh"], delta=0.03 * c0["energy_kwh"])
        (use,) = [a for a in both["activations"] if a["cycling"]]
        self.assertEqual(len(use["blocks"]), 60)
        self.assertEqual(len(both["activations"]), len(alone["activations"]) + 1)


# ---------------------------------------------------------------------------
class SuggestionTests(unittest.TestCase):
    """The entity suggestions (#41, #43) see when a series was on."""

    def setUp(self):
        t0 = DAY0 + 6 * HOUR
        self.blocks = [[t0 + i * 600, t0 + i * 600 + 200] for i in range(12)]
        series = {
            "start": t0,
            "end": self.blocks[-1][1],
            "power_w": 930,
            "cycling": True,
            "duty": 0.17,
            "on_s": 40.0,
            "period_s": 80.0,
            "blocks": self.blocks,
        }
        hob = {
            "start": t0 + 5 * HOUR,
            "end": t0 + 5 * HOUR + 900,
            "power_w": 930,
            "cycling": True,
            "duty": 0.6,
        }
        self.sums = {
            "d": {
                "start": DAY0,
                "end": DAY0 + 24 * HOUR,
                "quality": {"samples": 8640},
                "activations": [series, hob],
            }
        }
        self.reg = {
            "power_tol": 0.12,
            "signatures": [{"id": "cyc-950w", "kind": "cycling", "band": "session", "power_w": 930}],
        }

    def test_uses_are_the_blocks(self):
        uses = signature_uses(self.sums, self.reg, ["cyc-950w"])["cyc-950w"]["d"]
        self.assertEqual(uses[:12], [tuple(b) for b in self.blocks])
        self.assertEqual(uses[12], (DAY0 + 11 * HOUR, DAY0 + 11 * HOUR + 900))  # a session: its span

    def test_edges_are_those_of_the_blocks(self):
        edges = signature_edges(self.sums, self.reg, ["cyc-950w"], CorrelationConfig())["cyc-950w"]["d"]
        self.assertEqual(len(edges), 2 * 12 + 2)
        self.assertEqual(
            (edges[0].ts, edges[0].on, edges[1].ts, edges[1].on),
            (*self.blocks[0], True)[:1] + (True, self.blocks[0][1], False),
        )


# ---------------------------------------------------------------------------
class LiveTests(unittest.TestCase):
    """The live view follows the night's rule."""

    def test_same_series_as_the_night(self):
        heater = thermostat()
        hist = House(base_w=250, loads=[heater]).histories(DAY0 + 5 * HOUR, DAY0 + 9 * HOUR, dt=5)
        night = analyze(hist, CFG, DAY0 + 5 * HOUR, DAY0 + 9 * HOUR, local_hour=UTC_HOUR)
        (a,) = night["activations"]
        live = replay(hist, CFG, local_hour=UTC_HOUR)
        (ev,) = [e for e in live.events if e.state == CONFIRMED]
        info = ev.session()
        self.assertTrue(ev.series and info["confirmed"])
        self.assertEqual((ev.t_on, ev.t_off), (a["start"], a["end"]))
        self.assertEqual(info["count"], 30)
        self.assertEqual(
            (info["on_s"], info["period_s"], info["duty"]), (a["on_s"], a["period_s"], a["duty"])
        )
        self.assertAlmostEqual(ev.power_w, a["power_w"], delta=5)
        self.assertFalse(ev.running)
        # the lines of its other bursts left the feed
        self.assertEqual(sum(1 for e in live.events if e.state == CANCELLED), 29)

    def test_a_series_starts_at_its_fourth_burst(self):
        p = LivePairer()
        changed = []
        for i in range(5):
            t = DAY0 + i * 240.0
            p.feed(LiveStep(2 * i + 1, t, 1000, 250, 1250, state=CONFIRMED))
            changed = p.feed(LiveStep(2 * i + 2, t + 40, -1000, 1250, 250, state=CONFIRMED))
            alive = [e for e in p.events.values() if e.state == CONFIRMED]
            if i < 3:  # one line per burst, as the night would say so far
                self.assertEqual(len(alive), i + 1)
                self.assertFalse(any(e.series for e in alive))
            else:
                self.assertEqual(len(alive), 1)
                self.assertEqual((alive[0].id, alive[0].series, alive[0].cycles), (1, True, i + 1))
        # the last burst: its own line is cancelled, the series updated
        self.assertEqual([(e.id, e.state) for e in changed], [(5, CANCELLED), (1, CONFIRMED)])

    def test_kettles_stay_two_lines(self):
        loads = [Steady(DAY0 + 600, DAY0 + 780, 2000), Steady(DAY0 + 1800, DAY0 + 1980, 2000)]
        hist = House(base_w=250, loads=loads).histories(DAY0, DAY0 + 3600, dt=10)
        live = replay(hist, CFG, local_hour=UTC_HOUR)
        self.assertEqual([e.series for e in live.events if e.state == CONFIRMED], [False, False])

    def test_slow_thermostat_with_pruning(self):
        # one burst every 10 minutes, the service forgetting what ended more
        # than 15 minutes ago: the series still takes its first bursts
        p = LivePairer()
        for i in range(6):
            t = DAY0 + i * 600.0
            p.feed(LiveStep(2 * i + 1, t, 930, 250, 1180, state=CONFIRMED))
            p.feed(LiveStep(2 * i + 2, t + 35, -930, 1180, 250, state=CONFIRMED))
            p.prune(t + 60 - 900)
        (ev,) = [e for e in p.events.values() if e.state == CONFIRMED]
        self.assertTrue(ev.series)
        self.assertEqual((ev.t_on, ev.cycles, ev.t_off), (DAY0, 6, DAY0 + 5 * 600 + 35))
        # kept while its next burst may come, then forgotten
        p.prune(ev.t_off + 600 - 900)
        self.assertIn(ev.id, p.events)
        p.prune(ev.t_off + 2000)
        self.assertEqual(p.events, {})

    def test_a_burst_may_be_a_cycling_signature(self):
        registry = {
            "power_tol": 0.12,
            "signatures": [
                {
                    "id": "cyc-1000w",
                    "kind": "cycling",
                    "band": "session",
                    "power_w": 1000,
                    "label": "Radiator",
                },
                {"id": "cyc-2000w", "kind": "cycling", "band": "session", "power_w": 2000},
            ],
        }
        r = CandidateRanker(registry, local_hour=UTC_HOUR)
        ev = LiveEvent(1, DAY0, 1000, CONFIRMED, t_off=DAY0 + 40, off_w=1000)
        ev.steps = [(DAY0, 1000), (DAY0 + 40, -1000)]
        self.assertEqual([c["signature_id"] for c in r.rank(ev, DAY0 + 40)], ["cyc-1000w"])
        # still on after 3 minutes: it may be a burst (up to 5 minutes)
        ev = LiveEvent(1, DAY0, 1000, CONFIRMED, steps=[(DAY0, 1000)])
        self.assertEqual([c["signature_id"] for c in r.rank(ev, DAY0 + 180)], ["cyc-1000w"])
        self.assertEqual(r.rank(ev, DAY0 + 400), [])
        # a kettle closed after 6 minutes is not a burst
        ev = LiveEvent(1, DAY0, 2000, CONFIRMED, t_off=DAY0 + 360, off_w=2000)
        ev.steps = [(DAY0, 2000), (DAY0 + 360, -2000)]
        self.assertEqual(r.rank(ev, DAY0 + 360), [])

    def test_config_is_shared(self):
        self.assertIs(LivePairer().bursts, BURSTS)
        self.assertEqual(BurstConfig().max_period_s, 900.0)


if __name__ == "__main__":
    unittest.main()
