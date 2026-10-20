"""S0 backends for the lab's arms: a Franka Emika Panda through a libfranka-like
interface, and an AgileX PiPER through its official SDK over CAN.

Each is a RobotIO: S0 (the guard and a controller, s0.S0Loop) reads the arm
through it and commands it through it. Each arm's ControllerCapability is in
config/bodies.toml (`franka`, `piper`): the Panda honours contact through
torque control behind the guard; the PiPER honours motion only, its
documentation proving no force control. The guard is additional to each
arm's own safety (the Panda's reflexes, the PiPER's collision protection),
never a replacement for it.

Neither backend opens a connection: each takes an interface object made
elsewhere, and nothing in this repository makes one to a real arm; the
tests use fakes (sim/fake_franka.py, sim/fake_piper.py). Reading state needs
no authorization. Anything sent to the actuators, enabling the motors
included, is refused (NotAuthorized) unless the body's configuration names
an operator authorization: who authorized it, when, where. Untested on any
real arm.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from omarchy_runtime.contracts import Array, BodySpec, Ns, OmarchyError, RobotState, matrix_quat
from omarchy_runtime.wallclock import wall_ns


class NotAuthorized(OmarchyError):
    """A command for a real arm's actuators without an operator authorization in the configuration."""

    category = "authorization"


class ArmError(OmarchyError):
    """The arm or its interface reported an error: a reflex, a lost connection, a refused command."""

    category = "arm"


def authorization(body: BodySpec) -> str | None:
    """The operator authorization the body's configuration names (kinematics.adapter.operator_authorization), if any."""
    value = body.kinematics.get("adapter", {}).get("operator_authorization")
    return (value.strip() or None) if isinstance(value, str) else None


def _refuse(body: BodySpec, what: str) -> NotAuthorized:
    return NotAuthorized(
        f"{body.id}: {what} refused: no operator authorization in its configuration "
        f'([{body.id}.kinematics.adapter] operator_authorization = "<who>, <date>, <where>"); reading state needs none'
    )


# The Franka Emika Panda (FER), libfranka 0.9.2: the last release for it (robot system >= 4.2.1).


@dataclass(frozen=True, eq=False)
class FrankaState:
    """The fields of franka::RobotState this backend reads, by libfranka's names:
    joint positions, velocities and measured torques, the end-effector pose
    (O_T_EE, 4 x 4 column-major), the end effector in the flange's frame
    (F_T_EE, as Desk or setEE set it) and the stiffness frame in the end
    effector's (EE_T_K), the estimated external wrench at K (O_F_ext_hat_K,
    base frame: libfranka counts the force the robot applies to the
    environment as positive), the robot's mode, the success rate of the last
    100 commands, and the errors it reports."""

    q: Array
    dq: Array
    tau_J: Array
    O_T_EE: Array
    F_T_EE: Array
    EE_T_K: Array
    O_F_ext_hat_K: Array
    robot_mode: str
    control_command_success_rate: float
    current_errors: tuple[str, ...] = ()


class FrankaModel(Protocol):
    """franka::Model: a frame's Jacobian in the base frame (zeroJacobian,
    6 x 7; `frame` "flange" is franka::Frame::kFlange) and the Coriolis torques."""

    def zero_jacobian(self, frame: str, state: FrankaState) -> Array: ...

    def coriolis(self, state: FrankaState) -> Array: ...


class FrankaLike(Protocol):
    """franka::Robot (libfranka 0.9), as a Python binding would expose it. A
    real binding is wrapped to this; none is in this repository.

    `control` calls `callback(state, period_ns)` every millisecond (the
    period is 0 at the first call) until it returns None (MotionFinished),
    and sends the torques it returns; it raises ArmError when the robot
    stops with a reflex (the errors it names). `read_once` raises ArmError
    while a control loop runs."""

    def read_once(self) -> FrankaState: ...

    def load_model(self) -> FrankaModel: ...

    def control(self, callback: Callable[[FrankaState, Ns], Array | None], *, limit_rate: bool, cutoff_frequency: float) -> None: ...

    def set_collision_behavior(self, torque_thresholds: Sequence[float], force_thresholds: Sequence[float]) -> None: ...

    def automatic_error_recovery(self) -> None: ...


class FrankaArm:
    """The Panda as S0's RobotIO, torque-controlled. Outside a control loop,
    `state` reads the robot once (no authorization needed); `run` hands S0 to
    libfranka's 1 kHz loop, one S0 tick per callback, the torque S0's guard
    let through returned to the robot. Our guard's torque-rate limit stands
    in for libfranka's rate limiter and its low-pass filter, both off, as
    Franka advises when testing a new controller.

    The end-effector S0 sees is the flange, as the configuration declares
    it, whatever end effector Desk sets: the pose, the Jacobian and the
    wrench are the flange's. The wrench is the environment's on the robot,
    this runtime's convention: O_F_ext_hat_K negated (libfranka 0.9.2's
    robot_state.h), its moment taken about the flange. The robot's time runs
    on across control loops, and each loop restarts S0 (S0Loop.restart): its
    first torque is zero."""

    def __init__(self, robot: FrankaLike, body: BodySpec) -> None:
        self.robot = robot
        self.body = body
        self.authorized = authorization(body)
        self.model = robot.load_model()
        self._state: FrankaState | None = None  # the loop's latest, while one runs
        self._torque = np.zeros(len(body.dims))
        self._now: Ns = 0  # the robot's time, summed over its control loops' periods

    def read(self) -> FrankaState:
        """The robot's state, once: read-only, no authorization needed."""
        return self.robot.read_once()

    def state(self, now: Ns) -> RobotState:
        s = self._state if self._state is not None else self.robot.read_once()
        ee = np.asarray(s.O_T_EE, dtype=np.float64).reshape(4, 4, order="F")
        pose = ee @ np.linalg.inv(np.asarray(s.F_T_EE, dtype=np.float64).reshape(4, 4, order="F"))  # the flange
        k = (ee @ np.asarray(s.EE_T_K, dtype=np.float64).reshape(4, 4, order="F"))[:3, 3]  # where libfranka's wrench acts
        wrench = -np.asarray(s.O_F_ext_hat_K, dtype=np.float64)
        wrench[3:] += np.cross(k - pose[:3, 3], wrench[:3])  # its moment about the flange
        jac = np.asarray(self.model.zero_jacobian("flange", s), dtype=np.float64)
        dq = np.asarray(s.dq, dtype=np.float64)
        return RobotState(
            now,
            np.asarray(s.q, dtype=np.float64),
            dq,
            np.asarray(s.tau_J, dtype=np.float64),
            np.concatenate((pose[:3, 3], matrix_quat(pose[:3, :3]))),
            jac @ dq,
            wrench,
            jac,
            np.asarray(self.model.coriolis(s), dtype=np.float64),
        )

    def scene(self, now: Ns) -> Mapping[str, Array]:
        return {}

    def apply(self, now: Ns, command: Array) -> None:
        if not self.authorized:
            raise _refuse(self.body, "a torque command")
        if self._state is None:
            raise ArmError(f"{self.body.id}: torques go to the robot inside its control loop only (run)")
        self._torque = command

    def run(self, s0: Any, duration: Ns, before: Callable[[Ns], None] | None = None) -> None:
        """libfranka's control loop for `duration`: each callback is one tick
        of `s0` (an S0Loop, restarted first) at the robot's time, after
        `before` if given. Refused, before any connection to the loop,
        without an operator authorization."""
        if not self.authorized:
            raise _refuse(self.body, "torque control")
        end = self._now + duration
        s0.restart()

        def callback(state: FrankaState, period: Ns) -> Array | None:
            self._now += period
            self._state = state
            if before is not None:
                before(self._now)
            s0.tick(self._now)
            return None if self._now >= end else self._torque.copy()

        try:
            self.robot.control(callback, limit_rate=False, cutoff_frequency=1000.0)
        finally:
            self._state = None


# The AgileX PiPER, piper_sdk 0.6.2 (C_PiperInterface_V2, firmware V1.5-2 and later), CAN at 1 Mbit/s.

DEG = math.pi / 180


class PiperLike(Protocol):
    """The methods of the official SDK's C_PiperInterface_V2 this backend
    uses, with the SDK's names and units (0.001 degree for joints)."""

    def GetArmJointMsgs(self) -> Any: ...

    def GetArmHighSpdInfoMsgs(self) -> Any: ...

    def GetArmStatus(self) -> Any: ...

    def GetArmEnableStatus(self) -> list[bool]: ...

    def isOk(self) -> bool: ...

    def EnableArm(self, motor_num: int = 7, enable_flag: int = 0x02) -> None: ...

    def MotionCtrl_2(self, ctrl_mode: int, move_mode: int, move_spd_rate_ctrl: int, is_mit_mode: int = 0x00) -> None: ...

    def JointCtrl(self, joint_1: int, joint_2: int, joint_3: int, joint_4: int, joint_5: int, joint_6: int) -> None: ...


CAN_COMMAND, MOVE_J, POSITION_VELOCITY = 0x01, 0x01, 0x00


class PiperArm:
    """The PiPER as S0's RobotIO, position-controlled through MOVE J (the
    SDK's documented joint mode), at the rate S0 ticks (200 Hz, as the SDK's
    demos send). `state` reads joints, motor speeds and the SDK's effort
    estimate (from motor current: a joint torque estimate, no force sensor)
    without authorization. `apply` enables the motors once, sets CAN command
    mode MOVE J at `speed_percent`, and sends the joint targets the guard
    let through: only with an operator authorization.

    Both need fresh feedback: joints received within `feedback_timeout_s`
    (0.05 s; the arm sends them every 5 ms), by the CAN frames' receive time,
    and the SDK's reading thread well (isOk). Else ArmError, and nothing is
    sent: the SDK keeps its last values when frames stop, and its isOk turns
    false only after several hundred milliseconds."""

    def __init__(self, sdk: PiperLike, body: BodySpec) -> None:
        self.sdk = sdk
        self.body = body
        self.authorized = authorization(body)
        adapter = body.kinematics.get("adapter", {})
        self.speed = int(adapter.get("speed_percent", 30))
        self.timeout = float(adapter.get("feedback_timeout_s", 0.05))
        self._enabled = False

    def _joints(self) -> Any:
        """The SDK's latest joint feedback, if fresh; else ArmError."""
        msgs = self.sdk.GetArmJointMsgs()
        age = wall_ns() / 1e9 - msgs.time_stamp  # python-can's receive time: the system clock, as socketcan stamps it
        if age > self.timeout or not self.sdk.isOk():
            raise ArmError(f"{self.body.id}: no fresh feedback (joints {age:.3f} s old, SDK ok: {self.sdk.isOk()}): nothing sent")
        return msgs.joint_state

    def status(self) -> Any:
        """The SDK's arm status (0x2A1): control mode, arm status code, motion status. Read-only."""
        return self.sdk.GetArmStatus().arm_status

    def state(self, now: Ns) -> RobotState:
        j = self._joints()
        q = np.array([getattr(j, f"joint_{i}") for i in range(1, 7)], dtype=np.float64) * 0.001 * DEG
        motors = self.sdk.GetArmHighSpdInfoMsgs()
        dq = np.array([getattr(motors, f"motor_{i}").motor_speed for i in range(1, 7)], dtype=np.float64) * 0.001
        tau = np.array([getattr(motors, f"motor_{i}").effort for i in range(1, 7)], dtype=np.float64) * 0.001
        return RobotState(now, q, dq, tau)

    def scene(self, now: Ns) -> Mapping[str, Array]:
        return {}

    def apply(self, now: Ns, command: Array) -> None:
        if not self.authorized:
            raise _refuse(self.body, "a joint command")
        self._joints()
        if not self._enabled:
            self.sdk.EnableArm(7, 0x02)
            self._enabled = all(self.sdk.GetArmEnableStatus())
            if not self._enabled:
                return  # the motors are not enabled yet: S0 keeps asking, nothing moves
        self.sdk.MotionCtrl_2(CAN_COMMAND, MOVE_J, self.speed, POSITION_VELOCITY)
        targets = [round(float(v) / DEG * 1000) for v in command[:6]]
        self.sdk.JointCtrl(*targets)
