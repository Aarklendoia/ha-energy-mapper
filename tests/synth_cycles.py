"""Synthetic multi-phase appliances (#26) for the tests and the evaluation.

Generic appliances only (no real data): a dishwasher, a washing machine and
a tumble dryer, each cycle drawn with random phase durations and powers, a
motor in bursts, noise, and other loads overlapping them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from synth import DAY0, HOUR, Cycling, House, Steady

MIN = 60.0


@dataclass
class Burst:
    """A motor turning in bursts: ``power`` for ``on_s``, then ``off_s`` idle."""

    start: float
    end: float
    power: float
    on_s: float = 20.0
    off_s: float = 10.0

    def power_at(self, t: float) -> float:
        if not self.start <= t < self.end:
            return 0.0
        return self.power if (t - self.start) % (self.on_s + self.off_s) < self.on_s else 0.0


@dataclass
class Ramp:
    """A spin: the power rises linearly from ``p0`` to ``p1``."""

    start: float
    end: float
    p0: float
    p1: float

    def power_at(self, t: float) -> float:
        if not self.start <= t < self.end:
            return 0.0
        return self.p0 + (self.p1 - self.p0) * (t - self.start) / (self.end - self.start)


@dataclass
class ApplianceCycle:
    """One cycle of a multi-phase appliance: its loads and its true phases."""

    appliance: str
    start: float
    loads: list = field(default_factory=list)
    phases: list[tuple[str, float, float]] = field(default_factory=list)  # (name, start, end)

    @property
    def end(self) -> float:
        return max(x.end for x in self.loads)

    def power_at(self, t: float) -> float:
        return sum(x.power_at(t) for x in self.loads)

    def energy_kwh(self, dt: float = 10.0) -> float:
        t, e = self.start, 0.0
        while t < self.end:
            e += self.power_at(t) * dt
            t += dt
        return e / 3.6e6

    def phase_energy_kwh(self, name: str, dt: float = 10.0) -> float:
        e = 0.0
        for n, a, b in self.phases:
            if n != name:
                continue
            t = a
            while t < b:
                e += self.power_at(t) * dt
                t += dt
        return e / 3.6e6


def _u(rnd: random.Random, lo: float, hi: float) -> float:
    return rnd.uniform(lo, hi)


def dishwasher(start: float, rnd: random.Random) -> ApplianceCycle:
    """Pre-wash, heating, wash, second heating (rinse), final rinse; a
    circulation pump runs all along."""
    c = ApplianceCycle("dishwasher", start)
    pump = _u(rnd, 70, 110)
    heat = _u(rnd, 1900, 2150)
    t = start
    plan = [
        ("prewash", _u(rnd, 6, 10), 0),
        ("heat", _u(rnd, 15, 25), heat),
        ("wash", _u(rnd, 25, 35), 0),
        ("heat", _u(rnd, 8, 12), heat),
        ("rinse", _u(rnd, 12, 18), 0),
    ]
    for name, minutes, extra in plan:
        end = t + minutes * MIN
        c.loads.append(Steady(t, end, pump))
        if extra:
            c.loads.append(Steady(t, end, extra))
        c.phases.append((name, t, end))
        t = end
    return c


def washing_machine(start: float, rnd: random.Random) -> ApplianceCycle:
    """Heating with the drum turning, wash in bursts, rinses, then a spin."""
    c = ApplianceCycle("washing_machine", start)
    motor = _u(rnd, 250, 380)
    t = start
    fill = t + _u(rnd, 3, 5) * MIN
    c.loads.append(Burst(t, fill, motor * 0.6))
    c.phases.append(("fill", t, fill))
    t = fill
    end = t + _u(rnd, 12, 22) * MIN
    c.loads += [Steady(t, end, _u(rnd, 1950, 2200)), Burst(t, end, motor * 0.5, 15, 15)]
    c.phases.append(("heat", t, end))
    t = end
    end = t + _u(rnd, 30, 45) * MIN
    c.loads.append(Burst(t, end, motor, 20, 10))
    c.phases.append(("wash", t, end))
    t = end
    end = t + _u(rnd, 12, 20) * MIN
    c.loads.append(Burst(t, end, motor * 0.8, 20, 20))
    c.phases.append(("rinse", t, end))
    t = end
    end = t + _u(rnd, 5, 9) * MIN
    c.loads.append(Ramp(t, end, 300, _u(rnd, 550, 700)))
    c.phases.append(("spin", t, end))
    return c


def tumble_dryer(start: float, rnd: random.Random) -> ApplianceCycle:
    """The drum motor all along, the heater cycling on its thermostat, then
    a cool-down with the motor only."""
    c = ApplianceCycle("tumble_dryer", start)
    motor = _u(rnd, 180, 260)
    heat = _u(rnd, 2200, 2500)
    t = start
    end = t + _u(rnd, 60, 90) * MIN
    c.loads += [Steady(t, end + 12 * MIN, motor), Cycling(t, end, heat, _u(rnd, 300, 420), _u(rnd, 90, 150))]
    c.phases += [("drying", t, end), ("cooldown", end, end + 12 * MIN)]
    return c


APPLIANCES = {"dishwasher": dishwasher, "washing_machine": washing_machine, "tumble_dryer": tumble_dryer}


def distractors(day0: float, rnd: random.Random) -> list:
    """Other loads of a day: a fridge, kettles, an oven, a hob session, a
    water heater at night, a TV in the evening."""
    loads: list = [Cycling(day0, day0 + 24 * HOUR, _u(rnd, 90, 130), 15 * MIN, 30 * MIN)]
    for _ in range(rnd.randint(1, 4)):
        t = day0 + _u(rnd, 6, 22) * HOUR
        loads.append(Steady(t, t + _u(rnd, 2, 4) * MIN, _u(rnd, 1900, 2200)))  # kettle
    if rnd.random() < 0.5:
        t = day0 + _u(rnd, 11, 20) * HOUR
        loads.append(Steady(t, t + _u(rnd, 25, 50) * MIN, _u(rnd, 2400, 2800)))  # oven
    if rnd.random() < 0.5:
        t = day0 + _u(rnd, 11, 20) * HOUR
        loads.append(Cycling(t, t + _u(rnd, 10, 25) * MIN, _u(rnd, 1200, 1600), 40, 30))  # hob
    t = day0 + _u(rnd, 0.5, 2) * HOUR
    loads.append(Steady(t, t + _u(rnd, 2.5, 4) * HOUR, _u(rnd, 2800, 3200)))  # water heater
    t = day0 + _u(rnd, 19, 20) * HOUR
    loads.append(Steady(t, t + _u(rnd, 1.5, 3) * HOUR, _u(rnd, 90, 140)))  # TV
    return loads


def make_day(index: int, seed: int = 7, appliances=("dishwasher", "washing_machine", "tumble_dryer")):
    """One synthetic day: ``(house, day start, [ApplianceCycle])``. Each
    appliance runs on about half of the days, at random hours, sometimes
    while other loads run."""
    rnd = random.Random(seed * 1000 + index)
    day0 = DAY0 + index * 24 * HOUR
    cycles = []
    for name in appliances:
        if rnd.random() < 0.55:
            t = day0 + _u(rnd, 7, 19) * HOUR
            cycles.append(APPLIANCES[name](t, rnd))
    loads = distractors(day0, rnd) + cycles
    house = House(base_w=_u(rnd, 200, 320), loads=loads, noise_w=15.0, seed=seed * 1000 + index)
    return house, day0, cycles
