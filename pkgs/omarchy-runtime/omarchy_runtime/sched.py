"""Schedulers and channels.

Components only ever receive `now` through their step methods. The simulated
scheduler is the default and is deterministic; the wall-clock scheduler steps
the same components on the real clock, soft real-time, without changing them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from omarchy_runtime.contracts import ConfigError, Json, Ns
from omarchy_runtime.wallclock import monotonic_ns, sleep_until


class Scheduler:
    """Steps components at fixed periods in the order they were added.

    Every period is a whole number of base ticks; at each tick the components
    whose period divides it are stepped, first added first. The same
    components and the same seed always give the same run.
    """

    clock = "simulated"

    def __init__(self, tick: Ns) -> None:
        self.tick = tick
        self.now: Ns = 0
        self._components: list[tuple[str, int, Callable[[Ns], None]]] = []

    def add(self, name: str, period: Ns, step: Callable[[Ns], None]) -> None:
        if period <= 0 or period % self.tick:
            raise ConfigError(f"{name}: period {period} ns is not a multiple of the {self.tick} ns tick")
        self._components.append((name, period // self.tick, step))

    def run(self, done: Callable[[], bool], until: Ns, check_every: Ns) -> bool:
        """Run from the current time until `done()` (checked every `check_every`)
        or until simulated time `until`. Returns whether done() was reached."""
        n, last, every = self.now // self.tick, until // self.tick, max(1, check_every // self.tick)
        components = self._components
        while n <= last:
            now = n * self.tick
            self.now = now
            for _, k, step in components:
                if n % k == 0:
                    step(now)
            if n % every == 0 and done():
                return True
            n += 1
        return False


def distribution(values: Sequence[int]) -> Json:
    """Count, median, 99th percentile and maximum of integer samples (ns)."""
    if not values:
        return {"n": 0, "p50": None, "p99": None, "max": None}
    a = np.asarray(values, dtype=np.int64)
    return {"n": len(a), "p50": int(np.percentile(a, 50)), "p99": int(np.percentile(a, 99)), "max": int(a.max())}


@dataclass
class _Loop:
    name: str
    period: Ns
    step: Callable[[Ns], None]
    due: Ns = 0
    last: Ns | None = None
    intervals: list[int] = field(default_factory=list)  # between consecutive step starts
    busy: list[int] = field(default_factory=list)  # how long each step took
    overruns: int = 0
    skipped: int = 0


class WallScheduler:
    """Steps components at fixed periods on the wall clock, in the order they
    were added: soft real-time, in Python, on a general-purpose OS. Nothing
    here is real-time.

    Each step gets the real time elapsed since the first run() began, read
    when the step starts. A step that starts a whole period or more after it
    was due is an overrun: the releases it missed are skipped, never caught up
    in a burst. Every interval between two steps of a component and every
    step's duration are recorded (stats()).
    """

    clock = "wall"

    def __init__(self) -> None:
        self.now: Ns = 0
        self._start: int | None = None
        self._loops: list[_Loop] = []

    def add(self, name: str, period: Ns, step: Callable[[Ns], None]) -> None:
        if period <= 0:
            raise ConfigError(f"{name}: period {period} ns")
        self._loops.append(_Loop(name, period, step))

    def run(self, done: Callable[[], bool], until: Ns, check_every: Ns) -> bool:
        """Run until `done()` (checked every `check_every`) or until `until` ns
        have elapsed since the first run began. Returns whether done() was reached."""
        if self._start is None:
            self._start = monotonic_ns()
        start, loops, check = self._start, self._loops, self.now
        while True:
            sleep_until(start + min(c.due for c in loops))
            for c in loops:
                now = monotonic_ns() - start
                if now < c.due:
                    continue
                if c.last is not None:
                    c.intervals.append(now - c.last)
                late = now - c.due
                if late >= c.period:
                    c.overruns += 1
                    c.skipped += late // c.period
                c.last, c.due = now, c.due + (late // c.period + 1) * c.period
                c.step(now)
                c.busy.append(monotonic_ns() - start - now)
            self.now = now
            if now >= check:
                if done():
                    return True
                check = now + check_every
            if now > until:
                return False

    def stats(self) -> Json:
        """Per component: its period, step count, intervals between steps and
        step durations (n, p50, p99, max in ns), overruns and skipped releases."""
        return {
            c.name: {
                "period_ns": c.period,
                "steps": len(c.busy),
                "interval_ns": distribution(c.intervals),
                "step_ns": distribution(c.busy),
                "overruns": c.overruns,
                "skipped": c.skipped,
            }
            for c in self._loops
        }


class Latest[T]:
    """Latest-value channel: readers get the most recent value, older ones are dropped."""

    def __init__(self) -> None:
        self._value: T | None = None

    def put(self, value: T) -> None:
        self._value = value

    def get(self) -> T | None:
        return self._value


class Mailbox[T]:
    """Every message, in order, until drained."""

    def __init__(self) -> None:
        self._items: list[T] = []

    def put(self, item: T) -> None:
        self._items.append(item)

    def __len__(self) -> int:
        return len(self._items)

    def drain(self) -> list[T]:
        items, self._items = self._items, []
        return items
