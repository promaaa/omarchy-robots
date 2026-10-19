"""S0 controllers: what turns the active intent into commands, behind the guard.

Each declares a ControllerCapability (from the body's configuration) and
returns commands of its kind; the guard (guard.py) checks every one. Plain
numpy: a controller here runs on any body whose RobotIO reports what it
needs, a simulator or a real arm's library.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import numpy as np

from omarchy_runtime.contracts import Array, Command, ContactMode, ControllerCapability, IntentChunk, Ns, RobotState, quat_matrix, sample


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
    The guard checks every one."""

    APPROACH = 0.03  # m/s: the most the force offset moves
    GAIN = 0.004  # m per N s: how fast it follows the force error
    REACH = 0.04  # m: the most it may move from the motion's set-point
    INERTIA = (2.0, 2.0, 2.0, 0.05, 0.05, 0.05)  # kg, kg m^2: for critical damping
    NULL = (10.0, 2.0)  # Nm/rad, Nm s/rad: the null-space posture spring and damper
    DRIFT = (0.003, 0.01)  # m: object drift where it starts pressing less, and over which it reaches a quarter

    def __init__(self, capability: ControllerCapability, stiffness: Sequence[float], posture: Sequence[float]) -> None:
        self.capability = capability
        self.stiffness = np.array(stiffness, dtype=np.float64)
        self.posture = np.array(posture, dtype=np.float64)
        self.inertia = np.array(self.INERTIA)
        self._intent: IntentChunk | None = None
        self._hold: Array | None = None
        self._offset = 0.0
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
            self._offset = 0.0
        else:
            n = np.array(c.normal)
            measured = float(state.wrench[:3] @ n) if state.wrench is not None else 0.0
            if mode is ContactMode.BREAK:
                step = float(np.clip(-self._offset, -self.APPROACH * dt, self.APPROACH * dt))
            else:
                wanted = float(sample(c.target, c.timing, now)[0]) * self._press(intent, scene, now)
                step = float(np.clip(self.GAIN * (measured - wanted), -self.APPROACH, self.APPROACH)) * dt
            self._offset = float(np.clip(self._offset + step, -self.REACH, self.REACH))
            kn = float(k[:3] @ np.abs(n))
            along = kn * (float((target[:3] - x) @ n) + self._offset) - 2.0 * np.sqrt(kn * self.inertia[0]) * float(twist[:3] @ n)
            force[:3] += (along - float(force[:3] @ n)) * n
        jac = state.jacobian
        # The null space of J: I - J^T (J J^T)^-1 J, lightly damped near singularities.
        null = np.eye(jac.shape[1]) - jac.T @ np.linalg.solve(jac @ jac.T + 1e-6 * np.eye(jac.shape[0]), jac)
        k_null, d_null = self.NULL
        tau = jac.T @ force + null @ (k_null * (self.posture - state.position) - d_null * state.velocity)
        if state.coriolis is not None:
            tau = tau + state.coriolis
        return Command(now, "torque", tau)


CONTROLLERS: Mapping[str, Callable[[ControllerCapability], object]] = {"motion_tracker": MotionTracker}
