"""A fake Franka Emika Panda behind a libfranka-like interface (arms.FrankaLike),
for tests: no network, no robot, the robot's time only.

It reproduces what libfranka 0.9.2's documentation says, as far as a test
needs: the Panda's kinematics (its published DH parameters: they match the
MuJoCo Menagerie model) and limits; a control loop at 1 kHz whose first
period is 0; NaN or infinity in a command refused (libfranka throws
std::invalid_argument); the first torque command zero and the torque rate
within 1000 Nm/s, else the reflex `controller_torque_discontinuity`; a
dropped command (a callback slower than its budget, or a drop injected)
replaced by the last one, and 20 drops in a row the reflex
`communication_constraints_violation`; a joint past its limit the reflex
`joint_position_limits_violation`; an external force above the collision
threshold the reflex `cartesian_reflex`. A reflex stops the loop with
ArmError and leaves the robot in kReflex until automatic_error_recovery();
read_once fails while a loop runs. Its end effector sits where `F_T_EE`
puts it from the flange (the flange itself by default; Franka's hand is
HAND), and it reports the external wrench as libfranka does: the force the
robot applies to the environment, at the end effector. Its dynamics are
simple (constant joint inertia and damping, gravity compensated as the
robot does it): enough for the timing, the frames and the errors, not for
force control.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

from omarchy_runtime.arms import ArmError, FrankaState
from omarchy_runtime.contracts import Array, BodySpec, Ns
from omarchy_runtime.wallclock import perf_ns

# Craig's DH parameters of the Panda (a, d, alpha) per joint, then the flange.
DH = (
    (0.0, 0.333, 0.0),
    (0.0, 0.0, -np.pi / 2),
    (0.0, 0.316, np.pi / 2),
    (0.0825, 0.0, np.pi / 2),
    (-0.0825, 0.384, -np.pi / 2),
    (0.0, 0.0, np.pi / 2),
    (0.088, 0.0, np.pi / 2),
)
FLANGE = 0.107
# Franka's hand from the flange: 0.1034 m along z, turned -45 degrees about z (Desk's default with the hand).
HAND = np.array([[np.sqrt(0.5), np.sqrt(0.5), 0, 0], [-np.sqrt(0.5), np.sqrt(0.5), 0, 0], [0, 0, 1, 0.1034], [0, 0, 0, 1]])
PERIOD = 1_000_000  # ns
TAU_RATE = 1000.0  # Nm/s
DROPS_IN_A_ROW = 20  # then communication_constraints_violation
TOLERANCE = 1e-6


def frames(q: Sequence[float] | Array) -> list[Array]:
    """The 4 x 4 transform of each joint's frame, then the flange's, in the base frame."""
    t, out = np.eye(4), []
    for (a, d, alpha), theta in zip((*DH, (0.0, FLANGE, 0.0)), (*q, 0.0), strict=True):
        ca, sa, ct, st = np.cos(alpha), np.sin(alpha), np.cos(theta), np.sin(theta)
        t = t @ np.array([[ct, -st, 0, a], [st * ca, ct * ca, -sa, -d * sa], [st * sa, ct * sa, ca, d * ca], [0, 0, 0, 1]])
        out.append(t)
    return out


def jacobian(q: Sequence[float] | Array) -> Array:
    """The flange's geometric Jacobian in the base frame (6 x 7)."""
    fs = frames(q)
    p = fs[-1][:3, 3]
    jac = np.zeros((6, 7))
    for i in range(7):
        z, o = fs[i][:3, 2], fs[i][:3, 3]
        jac[:3, i], jac[3:, i] = np.cross(z, p - o), z
    return jac


class FakeModel:
    def zero_jacobian(self, frame: str, state: FrankaState) -> Array:
        assert frame == "flange", "this fake gives the flange's Jacobian only"
        return jacobian(state.q)

    def coriolis(self, state: FrankaState) -> Array:
        return np.zeros(7)  # constant inertia: none


class FakeFranka:
    """`budget_ns`: a callback slower than this (wall clock) is a dropped
    command, as the robot sees it; None never drops on time. `drops`: the
    cycles whose command is dropped anyway. `force_threshold`: the collision
    threshold on the end-effector force (N). `F_T_EE`: the end effector in
    the flange's frame, as Desk sets it. Every command the robot accepted is
    in `commands`."""

    INERTIA = np.array([0.5, 0.5, 0.3, 0.3, 0.05, 0.05, 0.02])  # kg m^2, constant
    DAMPING = np.array([2.0, 2.0, 1.5, 1.5, 0.3, 0.3, 0.1])  # Nm s/rad

    def __init__(
        self,
        body: BodySpec,
        *,
        budget_ns: Ns | None = None,
        drops: Sequence[int] = (),
        force_threshold: float = 20.0,
        F_T_EE: Array = np.eye(4),  # noqa: B008, N803 - libfranka's name; never mutated
    ) -> None:
        self.body = body
        self.F_T_EE = F_T_EE
        self.q = np.array(body.home, dtype=np.float64)
        self.dq = np.zeros(7)
        self.tau = np.zeros(7)
        self.external = np.zeros(6)  # the wrench the environment applies on the flange (moment about it), base frame
        self.mode = "kIdle"
        self.errors: tuple[str, ...] = ()
        self.budget = budget_ns
        self.drops = set(drops)
        self.force_threshold = force_threshold
        self.success: list[bool] = []
        self.commands: list[Array] = []
        self.running = False

    def _state(self) -> FrankaState:
        flange = frames(self.q)[-1]
        ee = flange @ self.F_T_EE
        rate = float(np.mean(self.success[-100:])) if self.success else 1.0
        tau_ext = jacobian(self.q).T @ self.external
        force = self.external[:3]
        moment = self.external[3:] + np.cross(flange[:3, 3] - ee[:3, 3], force)  # about K, the end effector
        return FrankaState(
            self.q.copy(),
            self.dq.copy(),
            self.tau - tau_ext,
            ee.flatten(order="F"),
            self.F_T_EE.flatten(order="F"),
            np.eye(4).flatten(order="F"),
            -np.concatenate((force, moment)),  # libfranka: the robot's on the environment
            self.mode,
            rate,
            self.errors,
        )

    def read_once(self) -> FrankaState:
        if self.running:
            raise ArmError("InvalidOperationException: readOnce cannot run while a control loop runs")
        return self._state()

    def load_model(self) -> FakeModel:
        return FakeModel()

    def set_collision_behavior(self, torque_thresholds: Sequence[float], force_thresholds: Sequence[float]) -> None:
        self.force_threshold = float(max(force_thresholds[:3]))

    def automatic_error_recovery(self) -> None:
        self.mode, self.errors = "kIdle", ()

    def push(self, force: Sequence[float]) -> None:
        """The environment pushes on the flange (N, base frame), from now on."""
        self.external[:3] = force

    def _reflex(self, *errors: str) -> ArmError:
        self.mode, self.errors, self.running = "kReflex", errors, False
        self.tau = np.zeros(7)
        return ArmError(f"ControlException: motion aborted by reflex! [{', '.join(errors)}]")

    def _step(self) -> None:
        tau_ext = jacobian(self.q).T @ self.external
        self.dq += (self.tau + tau_ext - self.DAMPING * self.dq) / self.INERTIA * (PERIOD / 1e9)
        self.q += self.dq * (PERIOD / 1e9)

    def control(self, callback: Callable[[FrankaState, Ns], Array | None], *, limit_rate: bool, cutoff_frequency: float) -> None:
        if self.mode != "kIdle":
            raise ArmError(f"CommandException: the robot is in {self.mode} {list(self.errors)}: recover first")
        if limit_rate or cutoff_frequency < 1000.0:
            raise ArmError("this fake runs without libfranka's rate limiter and low-pass filter, as the adapter asks")
        self.running, self.mode, self.success = True, "kMove", []
        cycle, in_a_row, period, first = 0, 0, 0, True
        try:
            while True:
                start = perf_ns()
                command = callback(self._state(), period)
                late = self.budget is not None and perf_ns() - start > self.budget
                if command is None:
                    self.mode, self.tau = "kIdle", np.zeros(7)
                    return
                command = np.asarray(command, dtype=np.float64)
                if command.shape != (7,) or not np.all(np.isfinite(command)):
                    raise ArmError(f"invalid_argument: torques {command.tolist()} are not 7 finite numbers")
                dropped = late or cycle in self.drops
                self.success.append(not dropped)
                if dropped:
                    in_a_row += 1
                    if in_a_row >= DROPS_IN_A_ROW:
                        raise self._reflex("communication_constraints_violation")
                else:
                    in_a_row = 0
                    jump = np.any(np.abs(command - self.tau) > TAU_RATE * PERIOD / 1e9 + TOLERANCE)
                    if jump or (first and np.any(np.abs(command) > TOLERANCE)):
                        raise self._reflex("controller_torque_discontinuity")
                    first = False
                    self.tau = command
                    self.commands.append(command)
                self._step()
                if np.any(self.q < np.array(self.body.lower)) or np.any(self.q > np.array(self.body.upper)):
                    raise self._reflex("joint_position_limits_violation")
                if float(np.linalg.norm(self.external[:3])) > self.force_threshold:
                    raise self._reflex("cartesian_reflex")
                cycle, period = cycle + 1, PERIOD
        finally:
            self.running = False
