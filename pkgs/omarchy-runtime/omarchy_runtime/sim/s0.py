"""The simulated S0 of the planar bodies: the guard and the motion tracker
(s0.S0Loop) on the simulated world, ticking at 1 kHz (simulated) and never
waiting.

At every tick it executes the active chunk while now <= valid_until, else,
once a chunk ran out with no successor or the runtime asked (a failure), a
valid installed fallback (S2's, else the base fallback), else HOLD (keep the
position, gripper included). The guard then enforces the body's limits:
position limits, then velocity limits, then the gripper's workspace. A step
is never larger than the velocity limit times the nominal tick period: after
a late tick S0 continues from where it is, at that bound, without catching
up. The world's actuators track the command ideally. Not real-time:
simulated time, or the wall clock, in Python.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from omarchy_runtime.contracts import SECOND, Array, BodySpec, Command, ControllerCapability, IntentChunk, Ns, RobotState, S0Controller
from omarchy_runtime.controllers import MotionTracker
from omarchy_runtime.s0 import S0Loop
from omarchy_runtime.sim.kinematics import Kinematics
from omarchy_runtime.sim.world import Trigger, World


def tracker_capability(body: BodySpec, nominal: Ns) -> ControllerCapability:
    """The motion tracker's declaration for a planar body, when no configuration gives one."""
    return ControllerCapability("motion_tracker", "1", "position", SECOND // nominal, (1_000_000, 10 * SECOND), (body.action_space,))


class PlanarRobot:
    """The planar world as S0's RobotIO: its joints, where the world's actuators put them."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.q = np.array(world.q, dtype=np.float64)

    def state(self, now: Ns) -> RobotState:
        return RobotState(now, self.q)

    def scene(self, now: Ns) -> Mapping[str, Array]:
        return {}

    def apply(self, now: Ns, command: Array) -> None:
        q = command.tolist()
        self.world.apply(now, q)
        self.q = command if self.world.q == q else np.array(self.world.q, dtype=np.float64)


class Adversary:
    """A controller gone wrong, for the guard's tests: while a window lasts,
    the command it wraps becomes NaN ("nan"), huge ("huge"), a jump of one
    unit ("jump"), or one computed for an earlier tick ("stale")."""

    MODES = ("nan", "huge", "jump", "stale")

    def __init__(self, inner: S0Controller, world: World, faults: Sequence[tuple[str, Trigger]]) -> None:
        self.inner = inner
        self.capability = inner.capability
        self.world = world
        self.faults = faults

    def command(self, intent: IntentChunk, state: RobotState, scene: Mapping[str, Array], now: Ns) -> Command:
        c = self.inner.command(intent, state, scene, now)
        for mode, when in self.faults:
            if when.active(self.world, now):
                if mode == "stale":
                    return Command(now - 1, c.kind, c.values)
                bad = {"nan": np.full_like(c.values, np.nan), "huge": np.full_like(c.values, 1e6), "jump": c.values + 1.0}[mode]
                return Command(now, c.kind, bad)
        return c


class SimS0(S0Loop):
    def __init__(
        self,
        body: BodySpec,
        world: World,
        capacity: int,
        nominal: Ns = 1_000_000,
        late_after: Ns | None = None,
        history: Ns = 2_000_000_000,
        capability: ControllerCapability | None = None,
        adversary: Sequence[tuple[str, Trigger]] = (),
    ) -> None:
        self.world = world
        controller: S0Controller = MotionTracker(capability or tracker_capability(body, nominal))
        if adversary:
            controller = Adversary(controller, world, adversary)
        super().__init__(body, controller, PlanarRobot(world), capacity, nominal, late_after, history, forward=Kinematics(body).ee)
