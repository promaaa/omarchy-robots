"""The S0 guard: Omarchy's half of S0, between any controller and the actuators.

It owns what must hold whatever the controller is: the choice of what S0
executes (S1's chunk while it is valid; once it ran out with no successor,
the starvation watchdog engages a fallback, S2's before the base, else
HOLD), and the check of every command before it reaches the actuators:
position, velocity and workspace limits, joint torque and end-effector
force limits, and the bounded step. A command that is not finite or not
computed for this tick is replaced by the guard's own safe command; a
breach no clamp can fix (in torque control: a limit passed, too much force)
too. The guard is plain data and arithmetic on arrays, and never asks the
controller what kind it is: only which command kind it returns. Not
real-time: Python.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from omarchy_runtime.contracts import (
    Array,
    BodySpec,
    ChunkRejected,
    Command,
    FallbackSpec,
    IntentChunk,
    Ns,
    Rejection,
    RobotState,
    S0Mode,
    seconds,
)


@dataclass(frozen=True)
class Limits:
    """The body's limits as the guard enforces them, plain data. Per joint:
    `lower`, `upper` (position), `velocity`; for torque control `torque`
    (Nm) and `torque_rate` (Nm/s, the bounded step of a torque), and the
    joint stiffness and damping of the guard's own hold. `force`: the most
    end-effector force (N) the guard lets the controller cause."""

    lower: Array
    upper: Array
    velocity: Array
    torque: Array | None = None
    torque_rate: Array | None = None
    force: float | None = None
    hold_stiffness: Array | None = None
    hold_damping: Array | None = None

    @staticmethod
    def of(body: BodySpec) -> Limits:
        """From the body's configuration: position and velocity limits, and its
        `limits` kinematics table for torque control."""
        t = body.kinematics.get("limits", {})

        def arr(key: str) -> Array | None:
            return np.array(t[key], dtype=np.float64) if key in t else None

        force = t.get("force_n")
        return Limits(
            np.array(body.lower),
            np.array(body.upper),
            np.array(body.max_velocity),
            arr("torque_nm"),
            arr("torque_rate_nm_s"),
            None if force is None else float(force),
            arr("hold_stiffness"),
            arr("hold_damping"),
        )


class Guard:
    """`kind` is the command kind of the controller it guards. `forward`
    maps commanded positions to the end-effector point the workspace
    holds (position control); in torque control the guard checks the
    measured end-effector instead. `counters` is S0's: the guard counts
    starvation events, fallback expiries, clamps and replacements in it."""

    def __init__(
        self,
        body: BodySpec,
        kind: str,
        nominal: Ns,
        counters: dict[str, int],
        forward: Callable[[Sequence[float]], tuple[float, ...]] | None = None,
        limits: Limits | None = None,
    ) -> None:
        self.body = body
        self.kind = kind
        self.nominal = nominal
        self.c = counters
        self.forward = forward
        self.limits = limits or Limits.of(body)
        self.joints = [i for i, d in enumerate(body.dims) if d != "gripper"]
        self.bound = self.limits.velocity * seconds(nominal)  # the largest step per tick
        self.chunk: IntentChunk | None = None
        self.fallback: FallbackSpec | None = None  # S2's: predicted with the goal, or refined
        self.base: FallbackSpec | None = None  # the capability's default, from the runtime
        self.engaged: tuple[FallbackSpec, IntentChunk] | None = None  # the fallback running, as the intent S0 executes
        self.armed = False  # a chunk ran out, or the runtime asked: a fallback engages (not while awaiting a first chunk)
        self.held: Array | None = None  # where the guard's own safe command holds (torque control)
        # The last command sent, for the bounded step of a torque: torques start from zero (a Franka requires it).
        self.last: Array | None = np.zeros(len(body.dims)) if kind == "torque" else None

    # What S0 executes.

    def submit(self, chunk: IntentChunk, now: Ns) -> None:
        if chunk.valid_until < now:
            raise ChunkRejected(Rejection.EXPIRED, f"chunk {chunk.seq} valid until {chunk.valid_until} ns, now {now} ns")
        active = self.chunk
        if active is not None and active.valid_until >= now and chunk.source == active.source and chunk.seq <= active.seq:
            raise ChunkRejected(Rejection.STALE, f"chunk {chunk.seq} while chunk {active.seq} of {active.source} is active")
        self.chunk = chunk

    def install(self, spec: FallbackSpec, now: Ns, base: bool) -> None:
        if spec.valid_until < now:
            raise ChunkRejected(Rejection.EXPIRED, f"fallback valid until {spec.valid_until} ns, now {now} ns")
        old = self.base if base else self.fallback
        if base:
            self.base = spec
        else:
            self.fallback = spec
        if self.engaged is not None and self.engaged[0] is old:
            self.engaged = None  # the new one engages from where S0 is, at the next tick

    def hold(self, fallback: bool) -> None:
        self.chunk, self.engaged, self.armed = None, None, fallback
        if not fallback:
            self.fallback = self.base = None

    def covering(self, now: Ns) -> FallbackSpec | None:
        """The fallback S0 runs, or would run if it needed one now."""
        for fb in (self.fallback, self.base):
            if fb is not None and ((self.engaged is not None and self.engaged[0] is fb) or fb.valid_from <= now <= fb.valid_until):
                return fb
        return None

    def select(self, now: Ns, state: RobotState, mode: S0Mode) -> tuple[S0Mode, IntentChunk | None]:
        """S1's chunk while valid; else, armed, an installed fallback (S2's
        first), engaged from the robot's position at this tick; else HOLD
        (None). `mode` is S0's mode at the previous tick: leaving TRACKING
        because the chunk ran out is a starvation event."""
        c = self.chunk
        if c is not None and now <= c.valid_until:
            if self.engaged is not None and self.engaged[0] is self.fallback:
                self.fallback = None  # S2's, once used, is spent: the base stays, to engage again
            self.engaged, self.armed = None, False
            return S0Mode.TRACKING, c
        if c is not None:
            self.chunk, self.armed = None, True
            if mode is S0Mode.TRACKING:
                self.c["starvation_events"] += 1
        for slot in ("fallback", "base") if self.armed else ():  # S2's first
            fb: FallbackSpec | None = getattr(self, slot)
            if fb is None:
                continue
            if self.engaged is None or self.engaged[0] is not fb:
                if not fb.valid_from <= now <= fb.valid_until:
                    continue
                p0 = state.position if fb.frame is None else state.ee
                assert p0 is not None, "a Cartesian fallback on a body that reports no end-effector pose"
                self.engaged = (fb, fb.engage(now, np.array(p0, dtype=np.float64)))
            intent = self.engaged[1]
            if now <= intent.valid_until:
                return S0Mode.FALLBACK, intent
            setattr(self, slot, None)  # expired: the next one, from where S0 is, or HOLD
            self.engaged = None
            self.c["fallback_expiries"] += 1
        return S0Mode.HOLD, None

    # What reaches the actuators.

    def safe(self, state: RobotState) -> Array:
        """The guard's own command, the controller aside: keep the position
        (position control), stop (velocity), or, in torque control, a joint
        spring and damper to where the guard took over (gravity is the
        robot's: a Franka compensates it itself)."""
        if self.kind == "position":
            return np.array(state.position, dtype=np.float64)
        if self.kind == "velocity":
            return np.zeros(len(state.position))
        lim = self.limits
        assert lim.hold_stiffness is not None and lim.hold_damping is not None and state.velocity is not None
        if self.held is None:
            self.held = np.array(state.position, dtype=np.float64)
        return np.asarray(lim.hold_stiffness * (self.held - state.position) - lim.hold_damping * state.velocity, dtype=np.float64)

    def check(self, command: Command | None, state: RobotState, mode: S0Mode, now: Ns, dt: Ns) -> tuple[Array, Array]:
        """The command the actuators get, and the one the guard started from
        (the controller's, or its own safe command when it replaced it;
        `command` None is HOLD, the guard's own). Clamps and replacements
        outside HOLD are counted."""
        replaced = command is None or command.kind != self.kind or command.t != now or command.values.shape != state.position.shape
        replaced = replaced or not np.isfinite(command.values).all()  # type: ignore[union-attr]
        if not replaced and self.kind == "torque":
            replaced = self._breach(state)
        if replaced:
            values = self.safe(state)
        else:
            assert command is not None
            values, self.held = np.array(command.values, dtype=np.float64), None
        wanted = values.copy()
        values = (
            self._position(values, state, dt) if self.kind == "position" else self._torque(values, dt) if self.kind == "torque" else values
        )
        if self.kind == "velocity":
            values = np.clip(values, -self.limits.velocity, self.limits.velocity)
        clamped = mode is not S0Mode.HOLD and bool((np.abs(values - wanted) > 1e-9).any())
        if replaced and mode is not S0Mode.HOLD:
            self.c["guard_replacements"] += 1
        elif clamped:
            self.c["limit_clamps"] += 1
        self.last = values
        return values, wanted

    def _position(self, desired: Array, state: RobotState, dt: Ns) -> Array:
        """Position limits, then the bounded step (never more than the nominal
        period's worth, so a late tick never catches up), then the workspace."""
        pos = state.position
        lim = self.limits
        step = self.bound if dt >= self.nominal else lim.velocity * seconds(dt)  # nothing moves in no time: the first tick holds
        command = np.minimum(np.maximum(np.minimum(np.maximum(desired, lim.lower), lim.upper), pos - step), pos + step)
        if self.forward is not None and not self.body.inside(self.forward(command[self.joints].tolist())):
            command[self.joints] = pos[self.joints]  # do not move into or further out of bounds
        return command

    def _breach(self, state: RobotState) -> bool:
        """In torque control: a joint past its position or velocity limit, the
        end-effector out of the workspace, or more end-effector force than
        allowed. The guard's spring and damper then take over."""
        lim = self.limits
        out = bool(np.any(state.position < lim.lower) or np.any(state.position > lim.upper))
        if state.velocity is not None:
            out = out or bool(np.any(np.abs(state.velocity) > lim.velocity))
        if state.ee is not None:
            out = out or not self.body.inside(state.ee[:3] if len(self.body.workspace) == 6 else state.ee[:2])
        if lim.force is not None and state.wrench is not None:
            out = out or float(np.linalg.norm(state.wrench[:3])) > lim.force
        return out

    def _torque(self, tau: Array, dt: Ns) -> Array:
        """Joint torque limits, then the bounded step of a torque: its rate
        limit times the time since the last tick, at most the nominal period
        (the first tick keeps zero)."""
        lim = self.limits
        if lim.torque is not None:
            tau = np.clip(tau, -lim.torque, lim.torque)
        if lim.torque_rate is not None and self.last is not None:
            step = lim.torque_rate * seconds(min(dt, self.nominal))
            tau = np.clip(tau, self.last - step, self.last + step)
        return tau
