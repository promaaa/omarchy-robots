"""S0 controllers: what turns the active intent into commands, behind the guard.

Each declares a ControllerCapability (from the body's configuration) and
returns commands of its kind; the guard (guard.py) checks every one. Plain
numpy: a controller here runs on any body whose RobotIO reports what it
needs, a simulator or a real arm's library.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import numpy as np

from omarchy_runtime.contracts import Array, Command, ControllerCapability, IntentChunk, Ns, RobotState, sample


class MotionTracker:
    """The motion channel interpolated at each tick, as a position command:
    S0's behaviour before contract v2. An intent without motion holds the
    position S0 had when the chunk took over. It honours motion only, as its
    capability says: anything else is refused or degraded before it gets here."""

    def __init__(self, capability: ControllerCapability) -> None:
        self.capability = capability
        self._hold: tuple[IntentChunk, Array] | None = None

    def command(self, intent: IntentChunk, state: RobotState, scene: Mapping[str, Array], now: Ns) -> Command:
        m = intent.motion
        if m is not None:
            return Command(now, "position", sample(m.rows, m.timing, now))
        if self._hold is None or self._hold[0] is not intent:
            self._hold = (intent, np.array(state.position, dtype=np.float64))
        return Command(now, "position", self._hold[1])


CONTROLLERS: Mapping[str, Callable[[ControllerCapability], object]] = {"motion_tracker": MotionTracker}
