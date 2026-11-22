"""Tests of the multi-phase appliances (#26): candidate cycles, phases,
template distance, the ``multiphase`` signatures of the registry, the
attribution of recognised cycles, and an evaluation on synthetic cycles
(precision and recall). Synthetic data only.

Run ``python3 tests/test_cycles.py --eval [days] [seed]`` to print the
evaluation."""

from __future__ import annotations

import datetime as dt
import pathlib
import statistics
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from appliances import validate  # noqa: E402
from nilm_core import AnalysisConfig, analyze  # noqa: E402
from nilm_core.cycles import (  # noqa: E402
    HEAT,
    MOTOR,
    Phase,
    distance,
    segment,
)
from nilm_core.hourly import hourly_breakdown  # noqa: E402
from nilm_core.registry import (  # noqa: E402
    Matcher,
    annotate_report,
    cycle_templates,
    update,
)
from synth import House, Steady  # noqa: E402
from synth_cycles import HOUR, MIN, dishwasher, make_day, washing_machine  # noqa: E402

UTC_HOUR = lambda ts: int(ts // 3600) % 24  # noqa: E731
GRID = "sensor.grid_power"


def run(house: House, day0: float, templates=None) -> dict:
    cfg = AnalysisConfig(grid_entity=GRID, cycle_templates=templates or {})
    hist = house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR)
    return analyze(hist, cfg, day0, day0 + 24 * HOUR, local_hour=UTC_HOUR)


def phases(*spec) -> list[Phase]:
    """Back-to-back phases from ``(power W, minutes)``."""
    out, t = [], 0.0
    for p, minutes in spec:
        out.append(Phase(HEAT if p >= 800 else MOTOR, t, minutes * 60, p))
        t += minutes * 60
    return out


def day_key(i: int) -> str:
    return (dt.date(2027, 1, 1) + dt.timedelta(days=i)).isoformat()


_DATASETS: dict[tuple[int, int], dict] = {}


def dataset(days: int = 40, seed: int = 7) -> dict:
    """Synthetic days analysed without templates, the registry learned from
    them, then the days analysed again with all its templates (cached)."""
    key = (days, seed)
    if key in _DATASETS:
        return _DATASETS[key]
    made = [make_day(i, seed) for i in range(days)]
    t0 = time.perf_counter()
    summaries = {}
    for i, (house, day0, _) in enumerate(made):
        report = run(house, day0)
        report.pop("curve")
        summaries[day_key(i)] = report
    t1 = time.perf_counter()
    end = made[-1][1] + 24 * HOUR
    registry = update(None, summaries, end, local_hour=UTC_HOUR)
    t2 = time.perf_counter()
    templates = cycle_templates(registry, validated_only=False)
    second = [run(house, day0, templates) for house, day0, _ in made]
    out = {
        "made": made,
        "summaries": summaries,
        "registry": registry,
        "templates": templates,
        "reports": second,
        "time": {"analyze_s": t1 - t0, "registry_s": t2 - t1},
    }
    _DATASETS[key] = out
    return out


def evaluate(days: int = 40, seed: int = 7) -> dict:
    """Precision and recall of the recognised cycles against the generated
    ones. A recognised cycle is right when it covers more than half of a
    generated cycle and its template belongs to that appliance (the
    appliance most of the template's cycles overlap)."""
    data = dataset(days, seed)

    def overlap(a, c) -> float:
        return max(0.0, min(a["end"], c.end) - max(a["start"], c.start))

    found, truths = [], []
    for i, ((_, _, cycles), report) in enumerate(zip(data["made"], data["reports"], strict=True)):
        found += [(i, a) for a in report["activations"] if a.get("multiphase")]
        truths += [(i, c) for c in cycles]
    votes: dict[str, dict[str, int]] = {}
    for i, a in found:
        best = max((c for j, c in truths if j == i), key=lambda c: overlap(a, c), default=None)
        if best is not None and overlap(a, best) > 0:
            v = votes.setdefault(a["signature"], {})
            v[best.appliance] = v.get(best.appliance, 0) + 1
    owner = {t: max(v, key=lambda k: (v[k], k)) for t, v in votes.items()}
    hits: set[int] = set()
    energy: dict[str, list[float]] = {}
    heating: dict[str, list[float]] = {}
    for i, a in found:
        best = max((c for j, c in truths if j == i), key=lambda c: overlap(a, c), default=None)
        if (
            best is None
            or id(best) in hits
            or overlap(a, best) <= 0.5 * (best.end - best.start)
            or owner.get(a["signature"]) != best.appliance
        ):
            continue
        hits.add(id(best))
        e = a["power_w"] * (a["end"] - a["start"]) / 3.6e6
        energy.setdefault(best.appliance, []).append((e - best.energy_kwh()) / best.energy_kwh())
        h = sum(p["power_w"] * p["duration_s"] for p in a["phases"] if p["kind"] == HEAT) / 3.6e6
        true_h = best.phase_energy_kwh("drying" if best.appliance == "tumble_dryer" else "heat")
        heating.setdefault(best.appliance, []).append((h - true_h) / true_h)
    alone = [
        c
        for j, c in truths
        if not any(k == j and o is not c and min(o.end, c.end) > max(o.start, c.start) for k, o in truths)
    ]
    return {
        "days": days,
        "cycles": len(truths),
        "isolated": len(alone),
        "recognised": len(found),
        "correct": len(hits),
        "precision": len(hits) / len(found) if found else 0.0,
        "recall": len(hits) / len(truths) if truths else 0.0,
        "recall_isolated": sum(id(c) in hits for c in alone) / len(alone) if alone else 0.0,
        "templates": len(data["templates"]),
        "energy_error": {k: statistics.median(abs(x) for x in v) for k, v in sorted(energy.items())},
        "energy_bias": {k: statistics.mean(v) for k, v in sorted(energy.items())},
        "heating_error": {k: statistics.median(abs(x) for x in v) for k, v in sorted(heating.items())},
        "time": data["time"],
    }


class TestPhases(unittest.TestCase):
    def test_segment(self):
        # pump, heating, pump with a 2-minute pause, heating, pump
        profile = [90] * 8 + [2100] * 20 + [100] * 12 + [0] * 2 + [100] * 14 + [2050] * 10 + [120] * 12
        got = segment(profile, 60.0)
        self.assertEqual([p.kind for p in got], [MOTOR, HEAT, MOTOR, HEAT, MOTOR])
        self.assertEqual([round(p.duration_s / 60) for p in got], [8, 20, 28, 10, 12])
        self.assertAlmostEqual(got[1].power_w, 2100)
        self.assertEqual(got[1].offset_s, 8 * 60)

    def test_blips_and_idle(self):
        profile = [0] * 5 + [2000] * 15 + [2000, 50, 2000] + [300] * 10 + [5000] + [300] * 10 + [0] * 20
        got = segment(profile, 60.0)
        self.assertEqual([p.kind for p in got], [HEAT, MOTOR])  # idle is not a phase
        self.assertEqual(got[0].offset_s, 5 * 60)

    def test_distance(self):
        dw = phases((90, 8), (2100, 20), (100, 30), (2050, 10), (110, 14))
        self.assertEqual(distance(dw, dw), 0.0)
        # the same program, a bit longer, with a fridge starting just before
        dw2 = phases((110, 12), (90, 7), (2000, 23), (95, 28), (2100, 11), (120, 15))
        wm = phases((1900, 20), (280, 60))
        dryer = phases((2200, 70), (230, 12))
        self.assertLess(distance(dw, dw2), 0.6)
        self.assertGreater(distance(dw, wm), 0.6)
        self.assertGreater(distance(dw, dryer), 0.6)
        self.assertGreater(distance(wm, dryer), 0.6)
        self.assertAlmostEqual(distance(dw, wm), distance(wm, dw), places=6)
        self.assertEqual(distance([], dw), float("inf"))


class TestCandidates(unittest.TestCase):
    def test_dishwasher_cycle_found(self):
        import random

        day0 = 1_800_000_000.0
        c = dishwasher(day0 + 10 * HOUR, random.Random(3))
        house = House(base_w=250, loads=[c, Steady(day0 + 15 * HOUR, day0 + 15 * HOUR + 180, 2000)])
        report = run(house, day0)
        self.assertEqual(len(report["cycles"]), 1)
        cyc = report["cycles"][0]
        self.assertLessEqual(abs(cyc["start"] - c.start), 120)
        self.assertLessEqual(abs(cyc["end"] - c.end), 120)
        self.assertEqual([p["kind"] for p in cyc["phases"]], [MOTOR, HEAT, MOTOR, HEAT, MOTOR])
        self.assertAlmostEqual(cyc["energy_kwh"], c.energy_kwh(), delta=0.05 * c.energy_kwh())
        # no template: candidates are only listed, activations are unchanged
        self.assertFalse(any(a.get("multiphase") for a in report["activations"]))
        self.assertEqual(sum(1 for a in report["activations"] if a["power_w"] > 1500), 3)

    def test_single_phase_loads_make_no_cycle(self):
        day0 = 1_800_000_000.0
        loads = [
            Steady(day0 + 7 * HOUR, day0 + 7 * HOUR + 180, 2000),  # kettle
            Steady(day0 + 1 * HOUR, day0 + 4 * HOUR, 3000),  # water heater
            Steady(day0 + 12 * HOUR, day0 + 12.6 * HOUR, 2600),  # oven, nothing around
            Steady(day0 + 20 * HOUR, day0 + 22 * HOUR, 120),  # TV
        ]
        house = House(base_w=250, loads=loads)
        plain = run(house, day0)
        self.assertEqual(plain["cycles"], [])
        # templates do not change a day without cycles
        templates = {"cycle-2000w": [p.to_dict() for p in phases((90, 8), (2100, 20), (100, 30))]}
        with_t = run(house, day0, templates)
        for key in ("activations", "signatures", "peaks"):
            self.assertEqual(plain[key], with_t[key])
        self.assertEqual(plain["energy_kwh"], with_t["energy_kwh"])


class TestRegistryAndAttribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = dataset()

    def test_multiphase_signatures_learned(self):
        sigs = [s for s in self.data["registry"]["signatures"] if s["kind"] == "multiphase"]
        self.assertGreaterEqual(len(sigs), 3)
        for s in sigs:
            self.assertEqual(s["band"], "cycle")
            self.assertTrue(s["id"].startswith("cycle-"))
            self.assertGreaterEqual(s["count"], 3)
            self.assertTrue(s["phases"])
            self.assertTrue(any(p["kind"] == HEAT for p in s["phases"]))
            self.assertEqual(len(s["occurrences"]), s["count"])
            self.assertIsNotNone(s["cycle_kwh"])
        # a dishwasher-like template: two heating phases
        self.assertTrue(any(sum(p["kind"] == HEAT for p in s["phases"]) == 2 for s in sigs))

    def test_identity_is_stable(self):
        reg = self.data["registry"]
        end = self.data["made"][-1][1] + 24 * HOUR
        again = update(reg, self.data["summaries"], end, local_hour=UTC_HOUR)
        ids = lambda r: sorted(s["id"] for s in r["signatures"] if s["kind"] == "multiphase")  # noqa: E731
        self.assertEqual(ids(reg), ids(again))
        # a labelled one survives the loss of its days
        sid = ids(reg)[0]
        labelled = {
            **reg,
            "signatures": [dict(s, label="Dishwasher") if s["id"] == sid else s for s in reg["signatures"]],
        }
        later = update(labelled, {}, end + 86400, local_hour=UTC_HOUR)
        kept = next(s for s in later["signatures"] if s["id"] == sid)
        self.assertEqual(kept["label"], "Dishwasher")
        self.assertTrue(kept["phases"])

    def test_templates_used_only_once_validated(self):
        reg = self.data["registry"]
        self.assertEqual(cycle_templates(reg), {})
        sid = next(s["id"] for s in reg["signatures"] if s["kind"] == "multiphase")
        sigs = [dict(s, label="Dryer") if s["id"] == sid else s for s in reg["signatures"]]
        self.assertEqual(list(cycle_templates({**reg, "signatures": sigs})), [sid])
        ignored = [dict(s, review={"state": "ignored", "at": 0}) if s["id"] == sid else s for s in sigs]
        self.assertEqual(cycle_templates({**reg, "signatures": ignored}), {})

    def test_recognised_cycle_replaces_its_activations(self):
        for report in self.data["reports"]:
            cycles = [a for a in report["activations"] if a.get("multiphase")]
            for c in cycles:
                # no activation of more than 1 kW left inside a recognised cycle
                inside = [
                    a
                    for a in report["activations"]
                    if not a.get("multiphase")
                    and not a["cycling"]
                    and c["start"] <= a["start"] < a["end"] <= c["end"]
                    and a["power_w"] >= 1000
                    and a["end"] - a["start"] >= 300
                ]
                self.assertEqual(inside, [], c)
                self.assertTrue(c["phases"])
                self.assertIn(c["signature"], self.data["templates"])
            # the curve never explains more than the house
            for row in report["curve"]:
                explained = (
                    row["base"] + sum(row["known"].values()) + sum(row["signatures"].values()) + row["other"]
                )
                self.assertLessEqual(explained, row["house"] - row["submetered"] + 2)

    def test_no_double_count(self):
        # the day's energy of a recognised cycle in the curve equals its own
        # energy, and the steady heating signatures lost what it absorbed
        for i, report in enumerate(self.data["reports"]):
            plain_sigs = self.data["summaries"][day_key(i)]["energy_kwh"]["signatures"]
            cycles = [a for a in report["activations"] if a.get("multiphase")]
            if not cycles or any(a["start"] < report["start"] or a["end"] > report["end"] for a in cycles):
                continue
            totals = report["energy_kwh"]["signatures"]
            for sid in {a["signature"] for a in cycles}:
                own = sum(
                    a["power_w"] * (a["end"] - a["start"]) / 3.6e6 for a in cycles if a["signature"] == sid
                )
                self.assertAlmostEqual(totals[sid], own, delta=0.03 + 0.03 * own)
            # everything the signatures explain stays within what is left
            # above the base: the cycle takes "other" (its low phases) and
            # the steady signatures' share (its heating phases), nothing more
            before = sum(plain_sigs.values()) + self.data["summaries"][day_key(i)]["energy_kwh"]["other"]
            after = sum(totals.values()) + report["energy_kwh"]["other"]
            self.assertAlmostEqual(before, after, delta=0.02)
            return
        self.fail("no day with a recognised cycle")

    def test_registry_ids_and_hourly(self):
        reg = self.data["registry"]
        report = next(r for r in self.data["reports"] if any(a.get("multiphase") for a in r["activations"]))
        cyc = next(a for a in report["activations"] if a.get("multiphase"))
        self.assertEqual(Matcher(reg).match(cyc), cyc["signature"])
        self.assertIsNone(Matcher({"signatures": []}).match(cyc))
        annotated = annotate_report(dict(report), reg)
        self.assertEqual(
            next(a for a in annotated["activations"] if a.get("multiphase"))["registry_id"], cyc["signature"]
        )
        daily = next(s for s in report["signatures"] if s["id"] == cyc["signature"])
        self.assertEqual(daily["kind"], "multiphase")
        self.assertTrue(daily["phases"])
        hourly = hourly_breakdown(report, reg)
        self.assertGreater(hourly["energy_kwh"]["signatures"].get(cyc["signature"], 0), 0)
        self.assertGreaterEqual(hourly["uses"]["signatures"][cyc["signature"]], 1)

    def test_evaluation(self):
        result = evaluate()
        self.assertGreaterEqual(result["precision"], 0.75, result)
        self.assertGreaterEqual(result["recall_isolated"], 0.65, result)
        for app, err in result["energy_error"].items():
            self.assertLess(err, 0.15, (app, result))
        for app, err in result["heating_error"].items():
            self.assertLess(err, 0.05, (app, result))


class TestAppliances(unittest.TestCase):
    def test_multiphase_source_accepted(self):
        got = validate(
            {
                "appliances": [
                    {
                        "key": "dw",
                        "name": "Dishwasher",
                        "statistic_id": "energy_mapper:dw",
                        "sources": [
                            {
                                "type": "signature",
                                "id": "cycle-2000w",
                                "kind": "multiphase",
                                "band": "cycle",
                                "power_w": 2000,
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(got[0]["sources"][0]["kind"], "multiphase")


class TestGenerator(unittest.TestCase):
    def test_washing_machine_phases(self):
        import random

        c = washing_machine(0.0, random.Random(1))
        self.assertEqual([p[0] for p in c.phases], ["fill", "heat", "wash", "rinse", "spin"])
        self.assertGreater(c.phase_energy_kwh("heat"), 0.3)
        self.assertLess(c.power_at(c.end + MIN), 1)


if __name__ == "__main__":
    if "--eval" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--eval"]
        days = int(args[0]) if args else 60
        seeds = [int(a) for a in args[1:]] or [7, 11, 13]
        for s in seeds:
            print(s, evaluate(days, s))
    else:
        unittest.main()
