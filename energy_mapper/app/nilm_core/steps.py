"""Step detection: an appliance switching on or off is a step in the load."""

from __future__ import annotations

import bisect
import statistics
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise


@dataclass
class Step:
    ts: float
    delta: float  # W, positive = something switched on
    before: float
    after: float
    explained_by: str | None = None  # name of a known load, if any
    used: bool = False  # consumed by an activation


def meter_interval(ts: Sequence[float]) -> float | None:
    """The meter's reporting interval: the median gap between readings
    (None under 3 readings)."""
    if len(ts) > 2:
        return statistics.median(b - a for a, b in pairwise(ts))
    return None


def adapted_windows(interval_s: float | None, window_s: float, merge_s: float) -> tuple[float, float]:
    """The detector's windows for a meter reporting every ``interval_s``
    (1 s to 60 s meters): at least 3 readings per side."""
    if interval_s is None:
        return window_s, merge_s
    return max(window_s, 3.1 * interval_s), max(merge_s, 1.1 * interval_s)


# the night threshold (#77): a step smaller than the step threshold is kept
# when the PV production is known and below PV_QUIET_W in the PV_QUIET_S
# before and after it (medians), where meter drift cannot fake it
PV_QUIET_W = 20.0
PV_QUIET_S = 60.0


def night_threshold(
    step_threshold_w: float, night_step_threshold_w: float | None, has_pv: bool
) -> float | None:
    """The threshold of the small steps kept while PV produces nothing: None
    when it does not apply (option unset, no PV meter, or not lower than the
    step threshold)."""
    if not has_pv or not night_step_threshold_w or night_step_threshold_w >= step_threshold_w:
        return None
    return float(night_step_threshold_w)


def pv_quiet(
    ts: Sequence[float],
    values: Sequence[float],
    t: float,
    quiet_w: float = PV_QUIET_W,
    window_s: float = PV_QUIET_S,
) -> bool:
    """The PV production is known and below ``quiet_w`` around ``t``: the
    medians of the readings in ``[t - window_s, t)`` and ``[t, t + window_s)``
    (``ts`` sorted). False when either window is empty."""
    i = bisect.bisect_left(ts, t - window_s)
    j = bisect.bisect_left(ts, t)
    k = bisect.bisect_left(ts, t + window_s)
    if i >= j or j >= k:
        return False
    return max(statistics.median(values[i:j]), statistics.median(values[j:k])) < quiet_w


class PvQuiet:
    """:func:`pv_quiet` over a PV series: ``PvQuiet(points)(t)``."""

    def __init__(self, points: Sequence[tuple[float, float]]):
        self.ts = [p[0] for p in points]
        self.v = [p[1] for p in points]

    def __call__(self, t: float) -> bool:
        return pv_quiet(self.ts, self.v, t)


class Signal:
    """Fast windowed medians over an irregular series."""

    def __init__(self, points: Sequence[tuple[float, float]]):
        self.ts = [p[0] for p in points]
        self.v = [p[1] for p in points]

    def median(self, start: float, end: float) -> float | None:
        i = bisect.bisect_left(self.ts, start)
        j = bisect.bisect_right(self.ts, end)
        if i >= j:
            return None
        return statistics.median(self.v[i:j])


def detect_steps(
    points: Sequence[tuple[float, float]],
    threshold: float = 300.0,
    window_s: float = 20.0,
    merge_s: float = 15.0,
    low_threshold: float | None = None,
    low_ok: Callable[[float], bool] | None = None,
) -> list[Step]:
    """Find level changes larger than ``threshold``.

    For each reading that jumps from the previous one, the median of the
    readings in the ``window_s`` before and after is compared (the windows
    grow with the meter's median reporting interval). Medians make
    the detector robust to single-sample spikes (motor inrush, meter noise).
    Neighbouring candidates of the same sign are merged, keeping the largest.

    ``low_threshold`` and ``low_ok`` (the night threshold, #77): a change
    between ``low_threshold`` and ``threshold`` is a candidate too when
    ``low_ok(ts)`` says so (:class:`PvQuiet`: PV produces nothing). Small
    candidates are judged before merging, so where ``low_ok`` is false the
    steps are exactly those found without them.
    """
    sig = Signal(points)
    window_s, merge_s = adapted_windows(meter_interval(sig.ts), window_s, merge_s)
    low = low_threshold if low_ok is not None and low_threshold and low_threshold < threshold else None
    jump = (low if low is not None else threshold) * 0.6
    cand: list[Step] = []
    for i in range(1, len(sig.ts)):
        if abs(sig.v[i] - sig.v[i - 1]) < jump:
            continue
        t = sig.ts[i]
        before = sig.median(t - window_s, t - 1e-3)
        after = sig.median(t, t + window_s)
        if before is None or after is None:
            continue
        d = after - before
        if abs(d) >= threshold or (low is not None and abs(d) >= low and low_ok(t)):
            cand.append(Step(t, d, before, after))
    out: list[Step] = []
    for c in cand:
        if out and c.ts - out[-1].ts < merge_s and (c.delta > 0) == (out[-1].delta > 0):
            if abs(c.delta) > abs(out[-1].delta):
                out[-1] = c
        else:
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# live: the same steps, one reading at a time
# ---------------------------------------------------------------------------
PROVISIONAL = "provisional"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"


@dataclass
class LiveStep:
    """A step seen by :class:`StepStream`. The same object is updated in
    place (and returned again) when it changes state."""

    id: int
    ts: float
    delta: float
    before: float
    after: float
    state: str = PROVISIONAL
    # provisional only: between 60 % and 100 % of the threshold (the night
    # threshold when it applies), so too small to be confirmed as it stands
    # (the guided mode says "too small")
    small: bool = False
    pv_moved: bool = False  # set by the live detector: PV jumped at the same time

    def to_step(self) -> Step:
        return Step(self.ts, self.delta, self.before, self.after)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "delta_w": round(self.delta),
            "before_w": round(self.before),
            "after_w": round(self.after),
            "state": self.state,
            "small": self.small,
            "pv_moved": self.pv_moved,
        }


class StepStream:
    """:func:`detect_steps`, one reading at a time.

    Fed with the same points and the same meter interval, the confirmed
    steps are exactly those of :func:`detect_steps`: a jump is judged once
    the window after it is complete (its median is then the same), and a
    step is confirmed once no later candidate can be merged into it. That
    is ``window_s + merge_s`` after the step (~35 s on a 10 s meter).

    Before that, a **provisional** step is given after
    ``provisional_readings`` readings (or ``provisional_s`` seconds), from
    the median of what is known after the jump: shown greyed, then
    confirmed (with the final values) or cancelled (a spike). Neighbouring
    jumps of the same sign share one provisional step, as they would be
    merged. A step confirmed with no provisional one is new.

    ``interval_s``: the meter's reporting interval that sizes the windows
    (:func:`adapted_windows`); None estimates it from the last readings
    (the night uses the median of the whole day). Points must come in time
    order: an older point is dropped (and counted in ``dropped``).

    ``low_threshold``, ``low_ok`` and ``low_wait_s``: the night threshold of
    :func:`detect_steps`. ``low_ok(ts)`` needs what comes ``low_wait_s``
    after the step (the PV after it), so with them every jump is judged
    once ``max(window_s, low_wait_s)`` has passed, keeping the order (and
    the merges) of the night: confirmation comes later.
    """

    def __init__(
        self,
        threshold: float = 300.0,
        window_s: float = 20.0,
        merge_s: float = 15.0,
        interval_s: float | None = None,
        provisional_readings: int = 3,
        provisional_s: float = 10.0,
        estimate_readings: int = 101,
        low_threshold: float | None = None,
        low_ok: Callable[[float], bool] | None = None,
        low_wait_s: float = PV_QUIET_S,
    ):
        self.threshold = threshold
        self.low_threshold = (
            low_threshold if low_ok is not None and low_threshold and low_threshold < threshold else None
        )
        self.low_ok = low_ok
        self.low_wait_s = low_wait_s
        self._base = (window_s, merge_s)
        self.interval_s = interval_s
        self._fixed = interval_s is not None
        self.window_s, self.merge_s = adapted_windows(interval_s, window_s, merge_s)
        self.provisional_readings = provisional_readings
        self.provisional_s = provisional_s
        self._gaps: deque[float] = deque(maxlen=estimate_readings - 1)
        self._ts: list[float] = []
        self._v: list[float] = []
        self._jumps: deque[float] = deque()  # judged once their window is complete
        self._early: deque[float] = deque()  # waiting for their provisional step
        self._last: Step | None = None  # the last candidate, may still be merged
        self._open: list[LiveStep] = []  # provisional steps
        self._next_id = 1
        self.readings = 0
        self.dropped = 0
        self.confirmed = 0
        self.cancelled = 0

    @property
    def latest(self) -> float | None:
        return self._ts[-1] if self._ts else None

    def push(self, ts: float, value: float) -> list[LiveStep]:
        """Add one reading; returns the steps that appeared or changed."""
        if self._ts and ts < self._ts[-1]:
            self.dropped += 1
            return []
        self.readings += 1
        if self._ts:
            self._gaps.append(ts - self._ts[-1])
            if not self._fixed and len(self._gaps) >= 2:
                self.interval_s = statistics.median(self._gaps)
                self.window_s, self.merge_s = adapted_windows(self.interval_s, *self._base)
            if abs(value - self._v[-1]) >= self.smallest * 0.6:
                self._jumps.append(ts)
                self._early.append(ts)
        self._ts.append(ts)
        self._v.append(value)
        out: list[LiveStep] = []
        self._provisional(out)
        self._judge(out, final=False)
        self._expire(out, final=False)
        self._prune()
        return out

    def flush(self) -> list[LiveStep]:
        """End of the stream: judge the last jumps with what is known (as
        :func:`detect_steps` does at the end of its points)."""
        out: list[LiveStep] = []
        self._early.clear()
        self._judge(out, final=True)
        self._expire(out, final=True)
        return out

    @property
    def smallest(self) -> float:
        """The smallest step that can be kept: the night threshold when set."""
        return self.low_threshold if self.low_threshold is not None else self.threshold

    # -- internals ---------------------------------------------------------
    def _delay(self) -> float:
        """How long after a jump it is judged."""
        if self.low_threshold is None:
            return self.window_s
        return max(self.window_s, self.low_wait_s)

    def _low(self, t: float) -> bool:
        """The night threshold applies to a step at ``t``."""
        return self.low_threshold is not None and self.low_ok(t)

    def _accept(self, t: float, d: float) -> bool:
        """The rule of :func:`detect_steps`."""
        if abs(d) >= self.threshold:
            return True
        return self.low_threshold is not None and abs(d) >= self.low_threshold and self.low_ok(t)

    def _median(self, start: float, end: float) -> float | None:
        i = bisect.bisect_left(self._ts, start)
        j = bisect.bisect_right(self._ts, end)
        if i >= j:
            return None
        return statistics.median(self._v[i:j])

    def _provisional(self, out: list[LiveStep]) -> None:
        latest = self._ts[-1]
        while self._early:
            t = self._early[0]
            n = len(self._ts) - bisect.bisect_left(self._ts, t)
            if n < self.provisional_readings and latest - t < self.provisional_s:
                break  # the later jumps have even fewer readings
            self._early.popleft()
            before = self._median(t - self.window_s, t - 1e-3)
            after = self._median(t, min(latest, t + self.window_s))
            if before is None or after is None:
                continue
            d = after - before
            limit = self.low_threshold if self._low(t) else self.threshold
            if abs(d) < limit * 0.6:
                continue
            small = abs(d) < limit
            near = [p for p in self._open if (p.delta > 0) == (d > 0) and abs(p.ts - t) < self.merge_s]
            if near:
                p = near[-1]
                if abs(d) > abs(p.delta):
                    p.ts, p.delta, p.before, p.after, p.small = t, d, before, after, small
                    self._emit(out, p)
                continue
            p = LiveStep(self._new_id(), t, d, before, after, PROVISIONAL, small)
            self._open.append(p)
            out.append(p)

    def _judge(self, out: list[LiveStep], final: bool) -> None:
        latest = self._ts[-1] if self._ts else None
        delay = self._delay()
        while self._jumps and (final or self._jumps[0] + delay < latest):
            t = self._jumps.popleft()
            before = self._median(t - self.window_s, t - 1e-3)
            after = self._median(t, t + self.window_s)
            if before is None or after is None:
                continue
            d = after - before
            if not self._accept(t, d):
                continue
            c = Step(t, d, before, after)
            last = self._last
            if last and c.ts - last.ts < self.merge_s and (c.delta > 0) == (last.delta > 0):
                if abs(c.delta) > abs(last.delta):
                    self._last = c
            else:
                if last:
                    self._confirm(out, last)
                self._last = c
        last = self._last
        if last and (
            final
            or (
                latest >= last.ts + self.merge_s
                and (not self._jumps or self._jumps[0] >= last.ts + self.merge_s)
            )
        ):
            self._confirm(out, last)
            self._last = None

    def _confirm(self, out: list[LiveStep], step: Step) -> None:
        self.confirmed += 1
        up = step.delta > 0
        near = [p for p in self._open if (p.delta > 0) == up and abs(p.ts - step.ts) < 2 * self.merge_s]
        if near:
            p = min(near, key=lambda p: abs(p.ts - step.ts))
            self._open.remove(p)
        else:
            p = LiveStep(self._new_id(), step.ts, step.delta, step.before, step.after)
        p.ts, p.delta, p.before, p.after = step.ts, step.delta, step.before, step.after
        p.state, p.small = CONFIRMED, False
        self._emit(out, p)

    def _expire(self, out: list[LiveStep], final: bool) -> None:
        """Cancel the provisional steps no confirmed step can claim any more."""
        if not self._open:
            return
        latest = self._ts[-1]
        horizon = latest
        if self._jumps:
            horizon = min(horizon, self._jumps[0])
        if self._last:
            horizon = min(horizon, self._last.ts)
        keep = []
        for p in self._open:
            if final or p.ts + 2 * self.merge_s <= horizon:
                p.state = CANCELLED
                self.cancelled += 1
                self._emit(out, p)
            else:
                keep.append(p)
        self._open = keep

    def _emit(self, out: list[LiveStep], p: LiveStep) -> None:
        if not any(x is p for x in out):
            out.append(p)

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def _prune(self) -> None:
        if len(self._ts) < 1024:
            return
        oldest = self._ts[-1]
        for q in (self._jumps, self._early):
            if q:
                oldest = min(oldest, q[0])
        k = bisect.bisect_left(self._ts, oldest - self.window_s - 1.0)
        if k > 512:
            del self._ts[:k]
            del self._v[:k]
