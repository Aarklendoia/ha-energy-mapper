"""Benchmark the engine on real history, with the measured devices as ground truth.

An installation with sub-meters already knows when each measured device ran.
This tool replays days of raw history through ``nilm_core`` and, for each
measured device in turn, hides it (the device is left in the whole-house
curve, as if it had no meter) and scores how well the analysis finds its
uses again: the nightly self-test (#15), for every device and every day at
once. It also measures the meters (reporting interval, noise of the curve)
and the share of the house energy in each category.

Run it before and after a change of the engine and compare the two results:

    python3 tools/benchmark.py ~/export.csv --grid sensor.grid_power \\
        --pv sensor.pv_power --submeter sensor.freezer_power=Freezer \\
        --submeter sensor.pump_power --switch switch.water_heater=on \\
        --json before.json
    ... change the engine ...
    python3 tools/benchmark.py ~/export.csv ... --json after.json --compare before.json

The history is the CSV export of ``tools/replay_csv.py`` (one row per state:
``entity_id,state,last_updated_ts``), or a folder of ``.json`` / ``.json.gz``
files, each ``{entity_id: [[timestamp, state], ...]}``. Keep such exports
OUTSIDE the repository: they describe a real household.

What a device's line means:

- ``tested``: the signature learned from its own runs (all the days of the
  export) is above the smallest step threshold; each day was analysed again
  without its sub-meter and scored (precision, recall, energy ratio).
- ``below_threshold``: the curve cannot show it with these thresholds. Its
  uses and energy are still counted, with nothing found, so that a lower
  threshold (``--threshold``, ``--night-threshold``) or a later change of
  the engine shows its gain.
- ``no_use``: the device never ran in the export.

Standard library only, not shipped in the add-on image.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import gzip
import json
import pathlib
import statistics
import sys
import zoneinfo
from collections import defaultdict
from collections.abc import Mapping, Sequence
from itertools import pairwise

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))

import selftest  # noqa: E402
from nilm_core import AnalysisConfig, SubmeterConfig, SwitchedLoadConfig, analyze  # noqa: E402
from nilm_core.groundtruth import merge_evaluations, runs_of  # noqa: E402
from nilm_core.registry import update as update_registry  # noqa: E402
from nilm_core.signal import SampleBuilder, parse_numeric  # noqa: E402
from nilm_core.steps import PV_QUIET_W, Signal, adapted_windows, meter_interval  # noqa: E402

VERSION = 1
BELOW_THRESHOLD = "below_threshold"
NOISE_EVERY_S = 60.0  # the noise is sampled at these instants
MARGIN_S = 1800.0  # history kept after a day (a run ending after midnight, baselines)

Histories = dict[str, list[tuple[float, str]]]


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------
def load(path: str) -> Histories:
    """The raw histories of a CSV export or of a folder of JSON files."""
    p = pathlib.Path(path)
    hist: dict[str, list[tuple[float, str]]] = defaultdict(list)
    if p.is_dir():
        for f in sorted(p.iterdir()):
            if f.name.endswith(".json.gz"):
                with gzip.open(f, "rt") as fh:
                    data = json.load(fh)
            elif f.name.endswith(".json"):
                data = json.loads(f.read_text())
            else:
                continue
            for entity, rows in data.items():
                hist[entity].extend((float(t), str(s)) for t, s in rows)
    else:
        with open(p, newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 3:
                    continue
                try:
                    ts = float(row[2])
                except ValueError:  # a header
                    continue
                hist[row[0]].append((ts, row[1]))
    # files of consecutive days overlap (each one starts before midnight)
    return {e: sorted(set(v)) for e, v in hist.items()}


def window(hist: Mapping[str, Sequence[tuple[float, str]]], a: float, b: float) -> Histories:
    """The states within ``[a, b]``, with the last one before ``a`` (the
    state at ``a``: Home Assistant records a state when it changes)."""
    out: Histories = {}
    for entity, rows in hist.items():
        i = bisect.bisect_left(rows, (a,))
        j = bisect.bisect_right(rows, (b, "￿"))
        out[entity] = list(rows[max(i - 1, 0) : j])
    return out


def days_of(ts: Sequence[float], tz: dt.tzinfo | None) -> list[tuple[str, float, float]]:
    """The whole local days the readings cover: ``(day, start, end)``."""
    if not ts:
        return []
    first = dt.datetime.fromtimestamp(ts[0], tz).date() + dt.timedelta(days=1)
    last = dt.datetime.fromtimestamp(ts[-1], tz).date()
    out = []
    day = first
    while day < last:
        nxt = day + dt.timedelta(days=1)
        start = dt.datetime.combine(day, dt.time(), tz).timestamp()
        end = dt.datetime.combine(nxt, dt.time(), tz).timestamp()
        out.append((day.isoformat(), start, end))
        day = nxt
    return out


# ---------------------------------------------------------------------------
# the meters
# ---------------------------------------------------------------------------
def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1)))]


def _intervals(ts: Sequence[float]) -> dict | None:
    gaps = [b - a for a, b in pairwise(ts)]
    if not gaps:
        return None
    return {"median_s": round(statistics.median(gaps), 1), "p90_s": round(_quantile(gaps, 0.9), 1)}


def _robust_sigma(values: Sequence[float]) -> float | None:
    """1.4826 x the median absolute value: the standard deviation of the
    noise, whatever the appliances that switch (the outliers)."""
    if len(values) < 30:
        return None
    return round(1.4826 * statistics.median(abs(v) for v in values), 1)


def meter_quality(hist: Histories, config: AnalysisConfig, start: float, end: float) -> dict:
    """How the meters report within ``[start, end)``, and the noise of the
    residual load: the detector's own statistic (the difference between the
    medians of the readings before and after an instant) at regular
    instants, where no appliance switches most of the time. Its robust
    spread is what a step must exceed to be seen, apart from the solar
    hours (production above ``PV_QUIET_W``) and during them."""
    builder = SampleBuilder.from_config(config)
    for entity in builder.entities:
        for ts, state in hist.get(entity, ()):
            builder.push(entity, ts, state)
    samples = [s for s in builder.flush() if start <= s.ts < end]
    points = [(s.ts, s.residual) for s in samples if s.residual is not None]
    out: dict = {"grid": _intervals([s.ts for s in samples]), "solar": None, "noise_w": {}}
    if config.pv_entity:
        producing = [
            ts
            for ts, v in parse_numeric(hist.get(config.pv_entity, ()))
            if start <= ts < end
            and v is not None
            and v * config.scales.get(config.pv_entity, 1.0) > PV_QUIET_W
        ]
        # the nights between two productions are not reporting intervals
        gaps = [b - a for a, b in pairwise(producing) if b - a < 3600]
        if gaps:
            out["solar"] = {
                "median_s": round(statistics.median(gaps), 1),
                "p90_s": round(_quantile(gaps, 0.9), 1),
            }
    if len(points) < 3:
        return out
    sig = Signal(points)
    win, _ = adapted_windows(meter_interval(sig.ts), 20.0, 15.0)
    pv = Signal([(s.ts, s.pv) for s in samples if s.pv is not None])
    quiet: list[float] = []
    sunny: list[float] = []
    t = start + win
    while t < end - win:
        before = sig.median(t - win, t - 1e-3)
        after = sig.median(t, t + win)
        if before is not None and after is not None:
            production = pv.median(t - 60, t + 60) if config.pv_entity else 0.0
            if production is not None:
                (sunny if production > PV_QUIET_W else quiet).append(after - before)
        t += NOISE_EVERY_S
    out["window_s"] = round(win, 1)
    out["noise_w"] = {"no_solar": _robust_sigma(quiet), "solar": _robust_sigma(sunny)}
    return out


def _merge_quality(parts: Sequence[Mapping]) -> dict:
    def med(path: tuple[str, str]) -> float | None:
        values = [p[path[0]][path[1]] for p in parts if (p.get(path[0]) or {}).get(path[1]) is not None]
        return round(statistics.median(values), 1) if values else None

    return {
        "grid": {"median_s": med(("grid", "median_s")), "p90_s": med(("grid", "p90_s"))},
        "solar": {"median_s": med(("solar", "median_s")), "p90_s": med(("solar", "p90_s"))},
        "noise_w": {"no_solar": med(("noise_w", "no_solar")), "solar": med(("noise_w", "solar"))},
    }


# ---------------------------------------------------------------------------
# the benchmark
# ---------------------------------------------------------------------------
def _energy(reports: Mapping[str, Mapping]) -> dict:
    """The house energy of the days by category, and each one's share."""
    total = defaultdict(float)
    for r in reports.values():
        e = r["energy_kwh"]
        total["house"] += e["house"]
        total["submetered"] += e["submetered"]
        total["switched"] += sum(e["known"].values())
        total["signatures"] += sum(e["signatures"].values())
        total["base"] += e["base"]
        total["unexplained"] += e["other"]
    house = total["house"]
    out = {k: round(v, 2) for k, v in total.items()}
    out["shares"] = {k: round(v / house, 3) for k, v in total.items() if k != "house"} if house > 0 else {}
    return out


def run(
    hist: Histories,
    config: AnalysisConfig,
    tz: dt.tzinfo | None = None,
    devices: Sequence[str] | None = None,
    log=None,
) -> dict:
    """Analyse every whole day of ``hist``, then hide each measured device
    of ``config`` (``devices``: only those) on each day and score it."""
    log = log or (lambda *_: None)

    def hour(ts: float) -> int:
        return dt.datetime.fromtimestamp(ts, tz).hour

    days = days_of([t for t, _ in hist.get(config.grid_entity, ())], tz)
    names = dict(selftest.candidates(config.submeters))
    chosen = [e for e in names if devices is None or e in devices]

    # 1. the real analysis of each day (every sub-meter subtracted)
    windows: dict[str, Histories] = {}
    reports: dict[str, dict] = {}
    quality: dict[str, dict] = {}
    for day, start, end in days:
        w = windows[day] = window(hist, start - config.context_s - MARGIN_S, end + MARGIN_S)
        reports[day] = analyze(w, config, start, end, local_hour=hour)
        quality[day] = meter_quality(w, config, start, end)
        log(f"{day}: analysed in {reports[day]['quality']['compute_s']} s")
    summaries = {d: {k: v for k, v in r.items() if k != "curve"} for d, r in reports.items()}
    now = days[-1][2] if days else 0.0
    registry = update_registry(None, summaries, now, local_hour=hour)

    # 2. the truth: each device's runs, day by day
    truth: dict[str, dict[str, list]] = {}
    points: dict[str, list] = {}
    for entity in chosen:
        scale = config.scales.get(entity, 1.0)
        points[entity] = [
            (t, None if v is None else v * scale) for t, v in parse_numeric(hist.get(entity, ()))
        ]
        truth[entity] = {day: runs_of(points[entity], start, end)[0] for day, start, end in days}

    # 3. hide and find, each device on each day; its signature is learned
    # from its runs of all the days (the best a perfect teacher would give)
    out_devices = []
    for entity in chosen:
        hidden = selftest.hidden_config(config, entity)
        tests = []
        for day, start, end in days:
            others = [r for d, runs in truth[entity].items() if d != day for r in runs]
            tests.append(
                selftest.hide_and_find(
                    day,
                    entity,
                    names[entity],
                    points[entity],
                    start,
                    end,
                    lambda w=windows[day], c=hidden, a=start, b=end: analyze(w, c, a, b, local_hour=hour),
                    registry,
                    others,
                    now,
                    step_threshold_w=config.smallest_step_w(),
                )
            )
        out_devices.append(_device(entity, names[entity], tests))
        d = out_devices[-1]
        log(f"{names[entity]}: {d['status']}, {d['found']} of {d['uses']} use(s) found")

    return {
        "version": VERSION,
        "days": [d for d, _, _ in days],
        "thresholds": {
            "step_w": config.step_threshold_w,
            "night_step_w": config.low_step_threshold(),
        },
        "meters": {**_merge_quality(list(quality.values())), "per_day": quality},
        "energy": _energy(reports),
        "signatures": len(registry.get("signatures") or []),
        "steps_per_day": round(statistics.mean(r["quality"]["steps"] for r in reports.values()), 1)
        if reports
        else None,
        "devices": out_devices,
    }


def _device(entity: str, name: str, tests: Sequence[Mapping]) -> dict:
    """One device over the days: the merged evaluation of its tested days;
    a device the curve cannot show keeps its uses and energy, none found."""
    learned = next((t["learned"] for t in reversed(tests) if t.get("learned")), None)
    tested = [t for t in tests if t["status"] == selftest.TESTED]
    if tested:
        status = selftest.TESTED
        merged = merge_evaluations(t["evaluation"] for t in tested)
    elif learned is None:
        status = selftest.NO_USE
        merged = merge_evaluations([])
    else:
        status = BELOW_THRESHOLD
        runs = [r for t in tests for r in t.get("runs") or []]
        merged = merge_evaluations(
            [{"uses": len(runs), "missed": len(runs), "truth_kwh": sum(r["energy_kwh"] for r in runs)}]
        )
    return {
        "entity_id": entity,
        "name": name,
        "status": status,
        "kind": (learned or {}).get("kind"),
        "band": (learned or {}).get("band"),
        "power_w": (learned or {}).get("power_w"),
        "days_tested": len(tested),
        **merged,
        "per_day": [
            {
                "day": t["day"],
                "status": t["status"],
                "runs": len(t.get("runs") or []),
                **(t.get("evaluation") or {}),
            }
            for t in tests
        ],
    }


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def _pct(value) -> str:
    return "   –" if value is None else f"{100 * value:3.0f}%"


def _num(value, unit: str = "") -> str:
    return "–" if value is None else f"{value:g}{unit}"


def _every(meter: Mapping) -> str:
    return f"every {_num(meter['median_s'], ' s')} (90 %: {_num(meter['p90_s'], ' s')})"


def table(result: Mapping) -> str:
    """The result as text."""
    m, e = result["meters"], result["energy"]
    th = result["thresholds"]
    night = f", {th['night_step_w']:g} W without solar" if th.get("night_step_w") else ""
    lines = [
        f"{len(result['days'])} day(s): {result['days'][0]} .. {result['days'][-1]}"
        if result["days"]
        else "no whole day in the export",
        f"thresholds: {th['step_w']:g} W{night}",
        f"grid meter: {_every(m['grid'])} | solar meter: {_every(m['solar'])}",
        f"noise of the curve: {_num(m['noise_w']['no_solar'], ' W')} without solar,"
        f" {_num(m['noise_w']['solar'], ' W')} with solar",
        f"house {e.get('house', 0):.1f} kWh: "
        + ", ".join(f"{k} {_pct(v).strip()}" for k, v in (e.get("shares") or {}).items()),
        f"signatures: {result['signatures']} | steps per day: {_num(result['steps_per_day'])}",
        "",
        f"{'device':28} {'status':16} {'power':>7} {'uses':>5} {'found':>5} {'recall':>6} "
        f"{'detect.':>7} {'precis.':>7} {'energy':>6}",
    ]
    for d in result["devices"]:
        lines.append(
            f"{d['name'][:28]:28} {d['status']:16} {_num(d['power_w'], ' W'):>7} {d['uses']:5d} "
            f"{d['found']:5d} {_pct(d['recall']):>6} {d['detections']:7d} {_pct(d['precision']):>7} "
            f"{_pct(d['energy_ratio']):>6}"
        )
    return "\n".join(lines)


def compare(before: Mapping, after: Mapping) -> str:
    """What changed between two results (same export, another engine or
    other thresholds)."""

    def delta(a, b, pct: bool = True) -> str:
        if a is None and b is None:
            return "–"
        fmt = (lambda v: _pct(v).strip()) if pct else _num
        return f"{fmt(a)} -> {fmt(b)}"

    old = {d["entity_id"]: d for d in before.get("devices") or []}
    lines = [
        f"signatures: {delta(before.get('signatures'), after.get('signatures'), False)}"
        f" | steps per day: {delta(before.get('steps_per_day'), after.get('steps_per_day'), False)}",
        "shares: "
        + ", ".join(
            f"{k} {delta((before.get('energy', {}).get('shares') or {}).get(k), v)}"
            for k, v in (after.get("energy", {}).get("shares") or {}).items()
        ),
        "",
        f"{'device':28} {'recall':>14} {'precision':>14} {'energy':>14}",
    ]
    for d in after.get("devices") or []:
        o = old.get(d["entity_id"], {})
        lines.append(
            f"{d['name'][:28]:28} {delta(o.get('recall'), d['recall']):>14} "
            f"{delta(o.get('precision'), d['precision']):>14} "
            f"{delta(o.get('energy_ratio'), d['energy_ratio']):>14}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def _pair(text: str, default: str) -> tuple[str, str]:
    key, _, value = text.partition("=")
    return key, value or default


def build_config(args: argparse.Namespace) -> AnalysisConfig:
    submeters = [SubmeterConfig(*_pair(s, s.partition("=")[0])) for s in args.submeter]
    submeters += [SubmeterConfig(*_pair(s, s.partition("=")[0]), estimated=True) for s in args.estimated]
    return AnalysisConfig(
        grid_entity=args.grid,
        grid_export_entity=args.grid_export,
        pv_entity=args.pv,
        submeters=submeters,
        switched_loads=[
            SwitchedLoadConfig(entity, entity, (state,))
            for entity, state in (_pair(s, "on") for s in args.switch)
        ],
        scales={entity: float(factor) for entity, factor in (_pair(s, "1") for s in args.scale)},
        step_threshold_w=args.threshold,
        night_step_threshold_w=args.night_threshold,
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("history", help="CSV export, or a folder of .json / .json.gz files")
    ap.add_argument("--grid", required=True, help="grid power sensor (signed, or the import)")
    ap.add_argument("--grid-export", help="export power sensor, when the grid has two")
    ap.add_argument("--pv", help="solar power sensor")
    ap.add_argument(
        "--submeter", action="append", default=[], metavar="ENTITY[=NAME]", help="measured device"
    )
    ap.add_argument(
        "--estimated",
        action="append",
        default=[],
        metavar="ENTITY[=NAME]",
        help="estimated device: subtracted, never hidden",
    )
    ap.add_argument("--switch", action="append", default=[], metavar="ENTITY[=STATE]", help="switched load")
    ap.add_argument(
        "--scale", action="append", default=[], metavar="ENTITY=FACTOR", help="1000 for a kW sensor"
    )
    ap.add_argument("--threshold", type=float, default=300.0, help="step threshold, W")
    ap.add_argument("--night-threshold", type=float, help="step threshold without solar production, W")
    ap.add_argument("--device", action="append", metavar="ENTITY", help="hide only these measured devices")
    ap.add_argument("--tz", help="time zone of the days (default: this machine's)")
    ap.add_argument("--json", help="write the result to this file")
    ap.add_argument("--compare", help="a result written earlier with --json")
    args = ap.parse_args(argv)

    hist = load(args.history)
    config = build_config(args)
    if not hist.get(config.grid_entity):
        print(f"no reading of {config.grid_entity} in {args.history}", file=sys.stderr)
        return 1
    tz = zoneinfo.ZoneInfo(args.tz) if args.tz else None
    result = run(hist, config, tz, args.device, log=lambda m: print(m, file=sys.stderr))
    print(table(result))
    if args.compare:
        print()
        print(compare(json.loads(pathlib.Path(args.compare).read_text()), result))
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
