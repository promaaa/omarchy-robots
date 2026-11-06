"""S0 controllers: what turns the active intent into commands, behind the guard.

Each declares a ControllerCapability (from the body's configuration) and
returns commands of its kind; the guard (guard.py) checks every one. Plain
numpy: a controller here runs on any body whose RobotIO reports what it
needs, a simulator or a real arm's library.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

import numpy as np

from omarchy_runtime.contracts import (
    CARTESIAN,
    Array,
    Command,
    ContactMode,
    ControllerCapability,
    IntentChunk,
    Ns,
    RobotState,
    quat_matrix,
    sample,
)

if TYPE_CHECKING:
    from omarchy_runtime.learning import Cerebellum


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


class CartesianImpedance:
    """Cartesian impedance control with normal-force regulation, returning
    joint torques; the robot compensates gravity itself (a Panda under torque
    control does; so does the simulator).

    The end-effector tracks the motion channel's pose (a hold of the pose it
    had when the chunk took over, without one) with a stiffness per axis (the
    contact channel's, else its default) and critical damping. Along a
    contact normal in MAKE or MAINTAIN, its set-point is the motion's plus an
    offset that moves, at most `approach` m/s, until the measured normal force
    reaches the target: in free space the end-effector goes on towards the
    surface, in contact it presses with the target force wherever the surface
    is. BREAK brings the offset back to zero at `approach`, and the motion
    leaves. An object intent it honours by pressing less while the object
    drifts from its intended pose (scene input `pose:<entity>`). The torque is
    J^T F, a null-space spring towards `posture`, and Coriolis compensation.
    The guard checks every one.

    With `limit` (the default), S0 keeps the force under the contact
    channel's upper bound on its own, without waiting for S1: it never
    commands more than that bound into the surface, its offset goes no
    deeper while it presses that hard, and a measured force above the bound
    brings the set-point back at once to where the spring gives the target
    force (a reflex, without the `approach` rate limit); in MAINTAIN it follows
    the force error faster (`FOLLOW`, `DEEPER`: it backs off faster than it
    presses deeper), so a surface that moves stays pressed.
    Without `limit`, the force is regulated at `approach` speed only, as
    before 5 November 2026.

    With a `cerebellum` (learning.py's ContactModel: where the pad rests in
    contact, per cell of the surface), on a vertical contact normal, the
    set-point along the normal is the motion's, plus a feedforward from the
    model's prediction (the height that gives the target force there,
    reached at `FF_RATE` at most; going down, at `APPROACH` in MAKE and
    within `SLOW_M` of the expected surface, so the pad never meets the
    surface faster than without a model), plus the feedback offset above. While the
    contact is steady in MAINTAIN, the model learns where the pad rests,
    and what it learns moves from the feedback offset into the feedforward,
    with no jump in the set-point: feedback-error learning, so the
    correction the feedback must hold shrinks as the model learns. Before
    contact, the feedback offset goes no deeper while the pad is still more
    than `ABOVE_M` above the set-point: the feedforward brings it there
    (at the set-point without contact, the surface is lower than predicted,
    and the feedback searches deeper, as without a model). Touching while
    still above the predicted surface, the feedforward goes no deeper: the
    feedback regulates the force, and the model learns the new height. `teaching` sums
    the feedback offset's size over steady contact (m s, and s), with or
    without a cerebellum, so the two can be compared."""

    APPROACH = 0.03  # m/s: the most the force offset moves
    GAIN = 0.004  # m per N s: how fast it follows the force error
    FOLLOW = (0.2, 0.01)  # with `limit`, in MAINTAIN: m/s and m per N s: a surface that moves is followed
    DEEPER = 0.1  # with `limit`, in MAINTAIN: m/s, the most it presses deeper (it backs off at FOLLOW's rate)
    REACH = 0.04  # m: the most it may move from the motion's set-point
    INERTIA = (2.0, 2.0, 2.0, 0.05, 0.05, 0.05)  # kg, kg m^2: for critical damping
    NULL = (10.0, 2.0)  # Nm/rad, Nm s/rad: the null-space posture spring and damper
    DRIFT = (0.003, 0.01)  # m: object drift where it starts pressing less, and over which it reaches a quarter
    FF_RATE = 0.1  # m/s: the most the feedforward set-point moves
    STEADY_N = 3.0  # N: contact this close to the target force is steady, and teaches the cerebellum
    TOUCH_N = 1.0  # N: below this, the pad is not touching
    ABOVE_M = 0.005  # m: above the predicted surface by more than this, the feedback waits before contact, the feedforward after
    SLOW_M = 0.01  # m: closer than this above the predicted surface, the feedforward descends at APPROACH only

    def __init__(
        self,
        capability: ControllerCapability,
        stiffness: Sequence[float],
        posture: Sequence[float],
        limit: bool = True,
        cerebellum: Cerebellum | None = None,
    ) -> None:
        self.capability = capability
        self.stiffness = np.array(stiffness, dtype=np.float64)
        self.posture = np.array(posture, dtype=np.float64)
        self.inertia = np.array(self.INERTIA)
        self.limit = limit
        self.reflexes = 0  # ticks on which the measured force passed the upper bound and S0 backed off at once
        self.cerebellum = cerebellum
        self.teaching = np.zeros(2)  # over steady contact in MAINTAIN: the integral of |feedback offset| (m s), its time (s)
        self._intent: IntentChunk | None = None
        self._hold: Array | None = None
        self._offset = 0.0
        self._ff = 0.0
        self._last: Ns | None = None

    def _press(self, intent: IntentChunk, scene: Mapping[str, Array], now: Ns) -> float:
        """How much of the target force to apply: all, unless the object intent is drifting away."""
        o = intent.object
        pose = scene.get(f"pose:{o.entity}") if o is not None and o.poses is not None else None
        if o is None or o.poses is None or pose is None:
            return 1.0
        drift = float(np.linalg.norm(sample(o.poses, o.timing, now)[:3] - pose[:3]))
        start, span = self.DRIFT
        return float(np.clip(1.0 - (drift - start) / span, 0.25, 1.0))

    def command(self, intent: IntentChunk, state: RobotState, scene: Mapping[str, Array], now: Ns) -> Command:
        assert state.ee is not None and state.jacobian is not None and state.velocity is not None
        dt = (now - self._last) / 1e9 if self._last is not None else 0.0
        self._last = now
        if intent is not self._intent:
            self._intent, self._hold = intent, np.array(state.ee)
        m = intent.motion
        target = sample(m.rows, m.timing, now) if m is not None else self._hold
        assert target is not None
        x, rot = state.ee[:3], quat_matrix(state.ee[3:7])
        want = quat_matrix(target[3:7] / np.linalg.norm(target[3:7]))
        # Orientation error: half the sum of the cross products of matching axes (rows hold the axes' components).
        a, b = rot, want
        turn = 0.5 * np.array([(a[1] * b[2] - a[2] * b[1]).sum(), (a[2] * b[0] - a[0] * b[2]).sum(), (a[0] * b[1] - a[1] * b[0]).sum()])
        error = np.concatenate((target[:3] - x, turn))
        twist = state.twist if state.twist is not None else state.jacobian @ state.velocity
        c = intent.contact[0] if intent.contact else None
        k = np.array(c.stiffness) if c is not None else self.stiffness
        damping = 2.0 * np.sqrt(k * self.inertia)
        force = k * error - damping * twist
        mode = c.mode(now) if c is not None else ContactMode.FREE
        if c is None or mode is ContactMode.FREE:
            self._offset = self._ff = 0.0
        else:
            n = np.array(c.normal)
            measured = float(state.wrench[:3] @ n) if state.wrench is not None else 0.0
            kn = float(k[:3] @ np.abs(n))
            gap = float((target[:3] - x) @ n)  # the motion's set-point along the normal, from the end-effector
            upper = float(c.upper[0]) if self.limit and c.kind == "normal_force" else np.inf
            cb = self.cerebellum if tuple(c.normal) == (0.0, 0.0, 1.0) else None
            if mode is ContactMode.BREAK:
                step = float(np.clip(-self._offset, -self.APPROACH * dt, self.APPROACH * dt))
                self._ff -= float(np.clip(self._ff, -self.FF_RATE * dt, self.FF_RATE * dt))
            else:
                wanted = float(sample(c.target, c.timing, now)[0]) * self._press(intent, scene, now)
                rate, gain = self.FOLLOW if self.limit and mode is ContactMode.MAINTAIN else (self.APPROACH, self.GAIN)
                deeper = self.DEEPER if self.limit and mode is ContactMode.MAINTAIN else rate
                step = float(np.clip(gain * (measured - wanted), -deeper, rate)) * dt
                steady = mode is ContactMode.MAINTAIN and measured >= self.TOUCH_N and abs(measured - wanted) <= self.STEADY_N
                if steady:
                    self.teaching += (abs(self._offset) * dt, dt)
                if cb is not None:
                    rest = cb.predict(float(x[0]), float(x[1]))
                    ff = self._feedforward(rest, wanted, kn, float(target[2]))
                    early = rest is not None and measured >= self.TOUCH_N and float(x[2]) > rest + self.ABOVE_M
                    if steady:  # feedback-error learning: what the model learns leaves the feedback offset
                        cb.learn(float(x[0]), float(x[1]), float(x[2]), dt)
                        rest = cb.predict(float(x[0]), float(x[1]))
                        learned = self._feedforward(rest, wanted, kn, float(target[2])) - ff
                        self._ff += learned
                        self._offset -= learned
                        ff += learned
                    if early:
                        ff = max(ff, self._ff)  # touching above where the model expects the surface: no deeper
                    near = rest is not None and float(x[2]) < rest + self.SLOW_M  # close to the expected surface: slow down
                    down = self.APPROACH if near or mode is ContactMode.MAKE else self.FF_RATE  # never a faster approach
                    self._ff += float(np.clip(ff - self._ff, -down * dt, self.FF_RATE * dt))
                    if (
                        rest is not None
                        and measured < self.TOUCH_N
                        and float(x[2]) > float(target[2]) + self._ff + self._offset + self.ABOVE_M
                    ):
                        step = max(step, 0.0)  # not touching, still on the way to the feedforward's set-point: it goes
                if measured > upper:  # the reflex: back to the target force's set-point, now
                    self.reflexes += 1
                    step = max(step, -wanted / kn - gap - self._ff - self._offset)
                elif kn * (gap + self._ff + self._offset) < -upper:
                    step = max(step, 0.0)  # pressing as hard as allowed: go no deeper
            self._offset = float(np.clip(self._offset + step, -self.REACH, self.REACH))
            along = kn * (gap + self._ff + self._offset) - 2.0 * np.sqrt(kn * self.inertia[0]) * float(twist[:3] @ n)
            along = max(along, -upper)  # never command more than the upper bound into the surface
            force[:3] += (along - float(force[:3] @ n)) * n
        jac = state.jacobian
        # The null space of J: I - J^T (J J^T)^-1 J, lightly damped near singularities.
        null = np.eye(jac.shape[1]) - jac.T @ np.linalg.solve(jac @ jac.T + 1e-6 * np.eye(jac.shape[0]), jac)
        k_null, d_null = self.NULL
        tau = jac.T @ force + null @ (k_null * (self.posture - state.position) - d_null * state.velocity)
        if state.coriolis is not None:
            tau = tau + state.coriolis
        return Command(now, "torque", tau)

    def _feedforward(self, rest: float | None, wanted: float, kn: float, z: float) -> float:
        """The set-point offset from the motion's height z that gives the target force where the pad rests at `rest`."""
        return 0.0 if rest is None else float(np.clip(rest - wanted / kn - z, -self.REACH, self.REACH))


class CartesianIK:
    """Cartesian pose intents on a position-controlled arm with a gripper:
    at each tick, one damped least-squares step from the measured joints
    towards the motion channel's pose (from the end-effector's Jacobian the
    body reports), and the gripper dimension of the row as it is. An intent
    without motion holds the pose the end-effector had when the chunk took
    over, gripper included. Plain numpy, the same for every arm whose
    RobotIO reports `ee` and `jacobian`: the arm's own kinematics stay in the
    body. The guard bounds every step."""

    DAMPING = 0.02  # m: the damped least squares' lambda
    TURN = 0.3  # m per rad: how much an orientation error weighs against a position error
    STEP = 0.02  # m: the largest task error one tick corrects

    def __init__(self, capability: ControllerCapability, velocity: Sequence[float] | None = None) -> None:
        self.capability = capability
        self.velocity = (
            np.array(velocity, dtype=np.float64) if velocity is not None else None
        )  # the joints' limits: a step keeps its direction
        self._intent: IntentChunk | None = None
        self._hold: Array | None = None
        self._last: Ns | None = None

    def command(self, intent: IntentChunk, state: RobotState, scene: Mapping[str, Array], now: Ns) -> Command:
        assert state.ee is not None and state.jacobian is not None
        q = np.array(state.position, dtype=np.float64)
        m = intent.motion
        if m is None and (self._hold is None or intent is not self._intent):
            self._hold = np.concatenate((state.ee, q[-1:]))
        self._intent = intent
        if m is not None and m.frame is None:
            return Command(now, "position", sample(m.rows, m.timing, now))  # joint targets, such as a fallback's
        target = sample(m.rows, m.timing, now) if m is not None else self._hold
        assert target is not None
        rot, want = quat_matrix(state.ee[3:7]), quat_matrix(target[3:7] / np.linalg.norm(target[3:7]))
        a, b = rot, want
        turn = 0.5 * np.array([(a[1] * b[2] - a[2] * b[1]).sum(), (a[2] * b[0] - a[0] * b[2]).sum(), (a[0] * b[1] - a[1] * b[0]).sum()])
        error = np.concatenate((target[:3] - state.ee[:3], self.TURN * turn))
        size = float(np.linalg.norm(error))
        if size > self.STEP:
            error *= self.STEP / size
        jac = state.jacobian.copy()
        jac[3:] *= self.TURN
        step = jac.T @ np.linalg.solve(jac @ jac.T + self.DAMPING**2 * np.eye(6), error)
        dt = (now - self._last) / 1e9 if self._last is not None else 0.0
        self._last = now
        if self.velocity is not None and dt > 0:
            over = float(np.max(np.abs(step[:-1]) / (self.velocity[:-1] * dt)))
            if over > 1.0:
                step /= over  # every joint slowed alike: the TCP keeps its path
        out = q + step
        if len(target) > len(CARTESIAN):
            out[-1] = target[len(CARTESIAN)]  # the gripper: 0 closed, 1 open
        return Command(now, "position", out)


CONTROLLERS: Mapping[str, Callable[[ControllerCapability], object]] = {"motion_tracker": MotionTracker, "cartesian_ik": CartesianIK}
