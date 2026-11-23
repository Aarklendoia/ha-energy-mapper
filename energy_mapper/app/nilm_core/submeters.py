"""Sub-meters that do not report with the grid meter (#85).

The residual load is the house load minus each sub-meter, reading by reading
(:func:`~.signal.sample_at`). That is right for the energy, but a sub-meter
does not report at the instant the grid meter sees its appliance switch: a
smart plug announces a change tens of seconds before or after, or only every
40 s. Between the two reports the subtraction is wrong by the whole power of
the appliance, and the step detector sees it:

* the appliance switches on, the house steps up, the plug still says 0: the
  residual steps **up**, then **down** when the plug reports. Two steps, and
  a phantom activation of the appliance's power lasting the lag;
* a start-up peak lasts 3 s on the grid meter and 40 s on a plug that
  reports every 40 s: the subtraction digs a 40 s hole in the residual, two
  steps where the house had none;
* an appliance that modulates every few seconds (a hob) is read at one
  instant by its plug and at another by the grid meter: the residual jumps
  by its power levels as long as it runs.

Such steps are not steps of the residual load. They are found again from what
makes them, and marked ``explained_by`` the sub-meter, as a switched load
marks its own (:mod:`.known`):

* an **echo** is a step where the sub-meter's subtracted value
  (``Sample.subs``) steps the other way by the same size, and where, at the
  very readings of that change, the residual load moves by the opposite
  amount: the house did not move with the sub-meter, the step is the
  sub-meter's report itself;
* the echo's **partner** is the house's own step, which the sub-meter
  reported early or late: a step of the sub-meter's sign within ``LAG_S`` of
  the echo (plus the detector's merge delay: steps are dated by the grid
  readings) which, measured on the load with that sub-meter **not**
  subtracted, has the size of the sub-meter's change;
* a step is **modulated** when, within the detector's windows around it, the
  sub-meter moved at least three times by its size and the house did not
  follow it: the medians the step was measured with mix the sub-meter's
  levels.

The evidence is always the same: at the very readings where the sub-meter
changes, the residual load moves against it (the house stayed, or went the
other way) instead of staying still (the house followed). An echo needs it
at its own change, a modulated step at two changes at least, and neither is
accepted where the house follows the sub-meter more often than not around
the step. A sub-meter that reports with the grid meter is followed, so its
installation is analysed exactly as before. Only the detected steps are
marked: the detection itself, the curve and the totals keep the plain
subtraction.

The explanation runs on the steps in time order and looks no further than
:meth:`SubmeterEchoes.reach` around a step (:meth:`SubmeterEchoes.feed`): the
nightly analysis and the live view (:class:`~.live.LiveDetector`) run the
same code on the same steps and samples, and mark the same steps.

Estimated sub-meters (an estimation integration's power) are left out: they
change with the state of their entity, not with a measurement, and explain
nothing.
"""

from __future__ import annotations

import bisect
import statistics
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

LAG_S = 45.0  # how far a sub-meter's report may be from the house's own step


@dataclass
class Echo:
    """A step made by a sub-meter's report (see the module)."""

    ts: float
    sub: int  # index of the sub-meter in the configuration
    change: float  # the sub-meter's change the step echoes (W, opposite sign)
    partner: float | None = None  # time of the house's own step, when found
    modulated: bool = False  # made by a sub-meter that modulates: ``change`` is its largest move


@dataclass
class SubmeterLag:
    """What :meth:`SubmeterEchoes.lags` measured for one sub-meter."""

    changes: int = 0  # isolated changes looked at
    lags: list[float] = field(default_factory=list)  # one per change the house shows

    def to_dict(self) -> dict:
        return {
            "changes": self.changes,
            "located": len(self.lags),
            "lag_s": round(statistics.median(self.lags), 1) if self.lags else None,
            "lag_min_s": round(min(self.lags), 1) if self.lags else None,
            "lag_max_s": round(max(self.lags), 1) if self.lags else None,
        }


class SubmeterEchoes:
    """Marks the steps a sub-meter's misaligned reports leave in the residual
    load (see the module).

    ``names``: one per configured sub-meter, in the order of ``Sample.subs``;
    None leaves a sub-meter out (an estimate). The samples of the residual
    load are added in time order (:meth:`add`), then the steps are fed in
    time order (:meth:`feed`), each once the samples up to ``merge_s`` after
    it (and its window, if it may be a partner) are in.

    Sizes agree within ``max(tol_w, tol_frac x size)``, the tolerance of
    :func:`~.activations.pair_steps`: an echo and its partner are one change
    seen twice. ``min_share``: the detector's medians can halve a step when
    the lag is about half its window, so an echo is a step of 40 % to 100 %
    (plus the tolerance) of the sub-meter's change. ``min_moves``: the moves
    of a sub-meter around a step that make it modulated (two are one use).
    """

    def __init__(
        self,
        names: Sequence[str | None],
        lag_s: float = LAG_S,
        tol_frac: float = 0.25,
        tol_w: float = 60.0,
        min_share: float = 0.4,
        min_moves: int = 3,
    ):
        self.names = list(names)
        self.indices = [i for i, n in enumerate(self.names) if n is not None]
        self.lag_s, self.tol_frac, self.tol_w, self.min_share = lag_s, tol_frac, tol_w, min_share
        self.min_moves = min_moves
        self._ts: list[float] = []
        self._r: list[float] = []
        self._x: list[list[float]] = [[] for _ in self.indices]
        self._free: deque = deque()  # recent steps no echo claimed yet
        self._open: deque[Echo] = deque()  # recent echoes
        self.echoes: list[Echo] = []
        self.explained: list[tuple[float, int]] = []  # (time, sub-meter index) of each marked step

    @classmethod
    def for_config(cls, config, **kwargs) -> SubmeterEchoes | None:
        """For an ``AnalysisConfig``: its sub-meters that measure; None when
        it has none (nothing to explain)."""
        names = [None if s.estimated else (s.name or s.entity_id) for s in config.submeters]
        return cls(names, **kwargs) if any(n is not None for n in names) else None

    # -- samples -----------------------------------------------------------
    def add(self, ts: float, residual: float, subs: Sequence[float]) -> None:
        """One sample of the residual load and what each sub-meter took out
        of it (``Sample.subs``)."""
        self._ts.append(ts)
        self._r.append(residual)
        for column, i in zip(self._x, self.indices, strict=True):
            column.append(subs[i] if i < len(subs) else 0.0)

    def prune(self, before: float) -> None:
        """Forget the samples and the echoes before ``before`` (live)."""
        k = bisect.bisect_left(self._ts, before)
        if k > 256:
            del self._ts[:k]
            del self._r[:k]
            for column in self._x:
                del column[:k]
        if len(self.echoes) > 256:
            self.echoes = [e for e in self.echoes if e.ts >= before]
            self.explained = [x for x in self.explained if x[0] >= before]

    # -- steps -------------------------------------------------------------
    def feed(self, step, window_s: float, merge_s: float) -> None:
        """One step (``ts``, ``delta``, ``explained_by``), in time order,
        with the detector's windows. Sets its ``explained_by`` when it is an
        echo, modulated or the partner of an earlier echo, and that of an
        earlier step when ``step`` is the echo it was waiting for. A step
        still free once every step up to :meth:`reach` after it has been fed
        stays free."""
        reach = self.reach(merge_s)
        while self._free and step.ts - self._free[0].ts > reach:
            self._free.popleft()
        while self._open and step.ts - self._open[0].ts > reach:
            self._open.popleft()
        found = self._echo(step, window_s, merge_s)
        if found is not None:
            echo = Echo(step.ts, *found)
            self._mark(step, echo.sub)
            # the house's own step: the nearest one before, else the first after
            for p in reversed(self._free):
                if self._is_partner(p, echo, window_s, reach):
                    self._pair(echo, p)
                    self._free.remove(p)
                    break
            self._open.append(echo)
            self.echoes.append(echo)
            return
        found = self._modulated(step, window_s)
        if found is not None:
            self._mark(step, found[0])
            self.echoes.append(Echo(step.ts, *found, modulated=True))
            return
        for echo in reversed(self._open):
            if echo.partner is None and self._is_partner(step, echo, window_s, reach):
                self._pair(echo, step)
                return
        self._free.append(step)

    def reach(self, merge_s: float) -> float:
        """How far from an echo its partner may be: the lag, and the merge
        delay of the detector (a step is dated by a grid reading, and
        neighbouring candidates are merged)."""
        return self.lag_s + merge_s

    def _mark(self, step, sub: int) -> None:
        step.explained_by = self.names[self.indices[sub]]
        self.explained.append((step.ts, sub))

    def _pair(self, echo: Echo, step) -> None:
        echo.partner = step.ts
        self._mark(step, echo.sub)

    def _tol(self, size: float) -> float:
        return max(self.tol_w, self.tol_frac * abs(size))

    def _echo(self, step, window_s: float, merge_s: float) -> tuple[int, float] | None:
        """``(sub-meter, its change)`` when the step is an echo.

        1. Measured as the detector measured the step (window medians), the
           sub-meter's subtracted value steps the other way by the step's
           size (``min_share`` to 100 %, plus the tolerance): a one-reading
           peak of the sub-meter is no change.
        2. At the very readings where the sub-meter makes that change (two
           readings no more than ``merge_s`` apart, within ``merge_s`` of
           the step: the detector merges neighbouring candidates), the
           residual load moves by the opposite amount: the house did not
           move with the sub-meter.
        3. Around the step the house does not follow that sub-meter more
           often than not (:meth:`_moves`): where it does, a step of the
           house met a move of the sub-meter at the same reading.

        The sub-meter closest in size wins."""
        ts = self._ts
        lo = bisect.bisect_left(ts, step.ts - merge_s - window_s)
        first = bisect.bisect_left(ts, step.ts - merge_s)
        hi = bisect.bisect_left(ts, step.ts + merge_s)
        size = abs(step.delta)
        best: tuple[float, int, float] | None = None
        for sub in range(len(self._x)):
            change = self._measure(sub, step.ts, window_s, load=False)
            if not change or (change > 0) == (step.delta > 0):
                continue
            tol = self._tol(change)
            if not self.min_share * abs(change) <= size <= abs(change) + tol:
                continue
            if not self._against(sub, lo, first, hi, change, tol, merge_s):
                continue
            against, followed, _, _ = self._moves(sub, step.ts, abs(change), window_s)
            if followed > against:
                continue  # around the step the house follows this sub-meter: a coincidence
            err = abs(abs(change) - size)
            if best is None or err < best[0]:
                best = (err, sub, change)
        return None if best is None else (best[1], best[2])

    def _against(
        self, sub: int, lo: int, first: int, hi: int, change: float, tol: float, merge_s: float
    ) -> bool:
        """Between two samples of ``[lo, hi)``, the second from ``first``, the
        sub-meter makes ``change`` and the residual load the opposite."""
        ts, r, x = self._ts, self._r, self._x[sub]
        for a in range(lo, hi - 1):
            for b in range(max(a + 1, first), hi):
                if ts[b] - ts[a + 1] > merge_s:
                    break
                if abs(x[b] - x[a] - change) <= tol and abs(r[b] - r[a] + change) <= tol:
                    return True
        return False

    def _moves(self, sub: int, t: float, size: float, window_s: float) -> tuple[int, int, int, float]:
        """What the house does when the sub-meter moves around ``t``:
        ``(against, followed, moves, largest move)`` over the moves of at
        least ``min_share`` of ``size`` between consecutive readings (no
        further apart than a window) within the detector's windows. Against:
        the residual load moves the other way by as much or more (the house
        stayed, or went the other way); followed: it moves by less than
        ``min_share`` of the move (the house moved with the sub-meter)."""
        ts, r, x = self._ts, self._r, self._x[sub]
        against = followed = moves = 0
        top = 0.0
        for k in range(max(bisect.bisect_left(ts, t - window_s), 1), bisect.bisect_right(ts, t + window_s)):
            move = x[k] - x[k - 1]
            if abs(move) < self.min_share * size or ts[k] - ts[k - 1] > window_s:
                continue
            moves += 1
            top = max(top, abs(move))
            went = r[k] - r[k - 1]
            if (went > 0) != (move > 0) and abs(went) >= abs(move) - self._tol(move):
                against += 1
            elif abs(went) < self.min_share * abs(move):
                followed += 1
        return against, followed, moves, top

    def _modulated(self, step, window_s: float) -> tuple[int, float] | None:
        """``(sub-meter, its largest move)`` when the step is modulated:
        within the detector's windows around it the sub-meter moves at least
        ``min_moves`` times by ``min_share`` of the step or more
        (:meth:`_moves`), the house does not follow it (against twice at
        least, and no less often than followed), and the step is no larger
        than twice the largest of those moves (plus the tolerance): read
        late, a sub-meter that pulses by ``A`` is wrong by ``-A`` to ``+A``."""
        size = abs(step.delta)
        for sub in range(len(self._x)):
            against, followed, moves, top = self._moves(sub, step.ts, size, window_s)
            if moves >= self.min_moves and against >= max(2, followed) and size <= 2 * top + self._tol(top):
                return sub, top
        return None

    def _is_partner(self, step, echo: Echo, window_s: float, reach: float) -> bool:
        if step.explained_by or (step.delta > 0) != (echo.change > 0):
            return False
        if abs(step.ts - echo.ts) > reach:
            return False
        seen = self._measure(echo.sub, step.ts, window_s)
        return seen is not None and abs(seen - echo.change) <= self._tol(echo.change)

    def _measure(self, sub: int, t: float, window_s: float, load: bool = True) -> float | None:
        """A step at ``t`` as :func:`~.steps.detect_steps` measures one (the
        medians of the readings in ``window_s`` after and before): that of
        the load with one sub-meter not subtracted, or with ``load`` false
        that of the sub-meter's subtracted value alone."""
        ts, r, x = self._ts, self._r, self._x[sub]
        i = bisect.bisect_left(ts, t - window_s)
        j = bisect.bisect_right(ts, t - 1e-3)
        k = bisect.bisect_left(ts, t)
        m = bisect.bisect_right(ts, t + window_s)
        if i >= j or k >= m:
            return None
        if not load:
            return statistics.median(x[k:m]) - statistics.median(x[i:j])
        before = statistics.median(r[n] + x[n] for n in range(i, j))
        after = statistics.median(r[n] + x[n] for n in range(k, m))
        return after - before

    # -- the measured lag (nightly report) -----------------------------------
    def lags(
        self,
        index: int,
        readings: Sequence[tuple[float, float | None]],
        window_s: float,
        min_w: float,
        start: float,
        end: float,
    ) -> SubmeterLag:
        """How far the reports of sub-meter ``index`` (of the configuration)
        are from the grid readings where the house moves.

        Each change of at least ``min_w`` between two of its ``readings``
        within ``[start, end)`` is looked at when it is isolated (no other
        change of half that within ``lag_s`` plus the window: a load that
        modulates has no lag to measure). The house shows it at the grid
        reading, within ``lag_s``, where the load without that sub-meter
        jumps the same way by its size (measured as the detector does). The
        lag is 0 when the report falls between that reading and the one
        before it (the subtraction is then right), the seconds after that
        reading when the sub-meter reports late, minus the seconds before
        the previous one when it reports early."""
        out = SubmeterLag()
        if index not in self.indices:
            return out
        sub = self.indices.index(index)
        values = [(t, max(v, 0.0)) for t, v in readings if v is not None]
        moves = [(t, b - a) for (_, a), (t, b) in pairwise(values) if abs(b - a) >= min_w / 2]
        times = [t for t, _ in moves]
        reach = self.lag_s + window_s
        for t, change in moves:
            if abs(change) < min_w or not start <= t < end:
                continue
            if bisect.bisect_right(times, t + reach) - bisect.bisect_left(times, t - reach) > 1:
                continue
            out.changes += 1
            lag = self._locate(sub, t, change, window_s)
            if lag is not None:
                out.lags.append(lag)
        return out

    def _locate(self, sub: int, t: float, change: float, window_s: float) -> float | None:
        ts, r, x = self._ts, self._r, self._x[sub]
        best: float | None = None
        first = max(bisect.bisect_left(ts, t - self.lag_s), 1)
        for k in range(first, bisect.bisect_right(ts, t + self.lag_s)):
            jump = (r[k] + x[k]) - (r[k - 1] + x[k - 1])
            if (jump > 0) != (change > 0) or abs(jump) < self.min_share * abs(change):
                continue
            seen = self._measure(sub, ts[k], window_s)
            if seen is None or abs(seen - change) > self._tol(change):
                continue
            lag = t - ts[k] if t > ts[k] else min(t - ts[k - 1], 0.0)
            if best is None or abs(lag) < abs(best):
                best = lag
        return best
