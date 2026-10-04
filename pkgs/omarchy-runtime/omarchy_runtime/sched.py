"""Simulated time: a deterministic discrete-time scheduler, and channels.

Components only ever receive `now` through their step methods. A wall-clock
scheduler can replace this one (R2) without changing them.
"""

from __future__ import annotations

from collections.abc import Callable

from omarchy_runtime.contracts import ConfigError, Ns


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
