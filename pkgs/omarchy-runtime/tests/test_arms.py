"""The lab's arms behind the S0 contract, against fakes only: a fake Panda
behind a libfranka-like interface, and the official PiPER SDK on a fake CAN
bus. No connection to a real arm is ever opened: the PiPER's socketcan is
replaced for the test's duration, and nothing here makes a libfranka
connection. Without an operator authorization, state reads and nothing
reaches the actuators; without fresh feedback, nothing does either."""

from __future__ import annotations

import ast
import dataclasses
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.arms import ArmError, FrankaArm, NotAuthorized, PiperArm
from omarchy_runtime.attribution import FidelityLimits, attribute, fidelity
from omarchy_runtime.contracts import (
    CARTESIAN,
    Array,
    BodySpec,
    Category,
    ContactIntent,
    IntentChunk,
    MotionIntent,
    Ns,
    RobotState,
    State,
    Timing,
    ns,
    quat_matrix,
)
from omarchy_runtime.controllers import CartesianImpedance, MotionTracker
from omarchy_runtime.s0 import S0Loop
from omarchy_runtime.sim.fake_franka import HAND, FakeFranka, frames, jacobian
from omarchy_runtime.wallclock import monotonic_ns, sleep_until

PACKAGE = Path(__file__).parent.parent / "omarchy_runtime"
CFG = config.effective("nominal_a")


def body(name: str, authorized: bool = False) -> BodySpec:
    cfg = config.merge(CFG, {"body": config.load(config.CONFIG / "bodies.toml")[name] | {"id": name}})
    b = config.body(cfg)
    if authorized:
        adapter = b.kinematics.get("adapter", {}) | {"operator_authorization": "test operator, 2026-10-19, a fake arm"}
        b = dataclasses.replace(b, kinematics=dict(b.kinematics) | {"adapter": adapter})
    return b


def capability(name: str) -> Any:
    cfg = config.merge(CFG, {"body": config.load(config.CONFIG / "bodies.toml")[name] | {"id": name}})
    return config.controller(cfg)[1]


class TestNoRealConnection(unittest.TestCase):
    def test_nothing_imports_a_vendor_library(self) -> None:
        vendor = {"franka", "pylibfranka", "panda_py", "franky", "frankx", "piper_sdk"}
        for path in PACKAGE.rglob("*.py"):
            names = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.split(".")[0])
            self.assertEqual(names & vendor, set(), path.name)


class TestFranka(unittest.TestCase):
    def test_the_kinematics_put_the_flange_where_franka_says(self) -> None:
        start = frames(body("franka").home)[-1]  # libfranka's examples' start pose: the flange down, in front
        np.testing.assert_allclose(start[:3, 3], [0.307, 0.0, 0.590], atol=1e-3)
        np.testing.assert_allclose(start[:3, :3], quat_matrix(np.array(body("franka").kinematics["cartesian"]["orientation"])), atol=1e-4)

    def test_state_reads_without_authorization_and_torques_are_refused(self) -> None:
        robot = FakeFranka(body("franka"))
        arm = FrankaArm(robot, body("franka"))
        state = arm.state(0)
        np.testing.assert_allclose(state.position, body("franka").home)
        assert state.ee is not None and state.jacobian is not None
        self.assertEqual(state.jacobian.shape, (6, 7))
        s0 = S0Loop(body("franka"), CartesianImpedance(capability("franka"), [1000] * 3 + [50] * 3, body("franka").home), arm, capacity=10)
        with self.assertRaises(NotAuthorized):
            arm.run(s0, ns(0.1))
        with self.assertRaises(NotAuthorized):
            s0.tick(0)  # S0 alone cannot reach the actuators either
        self.assertEqual(robot.commands, [])

    def s0(self, robot: FakeFranka, capacity: int = 3000) -> tuple[FrankaArm, S0Loop]:
        b = body("franka", authorized=True)
        arm = FrankaArm(robot, b)
        s0 = S0Loop(b, CartesianImpedance(capability("franka"), [1000] * 3 + [50] * 3, b.home), arm, capacity=capacity)
        ee = arm.state(0).ee
        assert ee is not None
        timing = Timing(0, ns(1.0), ns(100.0), "test", 0)
        motion = MotionIntent.make(timing, "franka.ee_pose.v1", CARTESIAN, [list(ee), [*ee[:2], ee[2] - 0.02, *ee[3:]]], "franka.base")
        s0.submit_chunk(IntentChunk.make(1, "test", motion), 0)
        return arm, s0

    def run_arm(self, robot: FakeFranka, duration: float, push_after: int | None = None, force: float = 0.0) -> FrankaArm:
        arm, s0 = self.s0(robot)
        ticks = 0

        def before(now: Ns) -> None:
            nonlocal ticks
            ticks += 1
            if push_after is not None and ticks == push_after:
                robot.push([0.0, 0.0, force])

        arm.run(s0, ns(duration), before)
        return arm

    def test_torque_control_moves_the_flange_and_starts_from_zero(self) -> None:
        robot = FakeFranka(body("franka"))
        self.run_arm(robot, 1.0)
        self.assertGreater(len(robot.commands), 990)
        np.testing.assert_array_equal(robot.commands[0], np.zeros(7))  # the first command is zero, as Franka requires
        steps = np.abs(np.diff(np.array(robot.commands), axis=0))
        self.assertLessEqual(float(steps.max()), 1.0 + 1e-9)  # the guard's rate limit: 1000 Nm/s at 1 kHz
        self.assertLess(float(frames(robot.q)[-1][2, 3]), 0.590 - 0.01)  # down towards its target
        self.assertEqual(robot.mode, "kIdle")

    def test_a_collision_stops_the_robot_with_its_reflex_and_recovery_clears_it(self) -> None:
        robot = FakeFranka(body("franka"))
        with self.assertRaises(ArmError) as e:
            self.run_arm(robot, 1.0, push_after=200, force=25.0)  # 25 N, above the 20 N collision threshold
        self.assertIn("cartesian_reflex", str(e.exception))
        self.assertEqual((robot.mode, robot.errors), ("kReflex", ("cartesian_reflex",)))
        with self.assertRaises(ArmError):
            self.run_arm(robot, 0.1)  # in reflex: nothing starts until recovery
        robot.automatic_error_recovery()
        robot.push([0.0, 0.0, 0.0])
        self.run_arm(robot, 0.1)

    def test_two_control_loops_in_a_row_each_start_from_zero_torque(self) -> None:
        robot = FakeFranka(body("franka"))
        arm, s0 = self.s0(robot)
        arm.run(s0, ns(0.05))
        self.assertGreater(float(np.abs(robot.commands[-1]).max()), 0.1)  # the first loop ended with torque applied
        n = len(robot.commands)
        arm.run(s0, ns(0.05))  # the same S0: no reflex, the robot's time runs on
        np.testing.assert_array_equal(robot.commands[n], np.zeros(7))
        self.assertEqual(s0.status().t, ns(0.1))
        self.assertEqual(s0.status().counters.late_ticks, 0)

    def test_the_fake_refuses_a_first_torque_that_is_not_zero(self) -> None:
        robot = FakeFranka(body("franka"))
        with self.assertRaises(ArmError) as e:
            robot.control(lambda state, period: np.full(7, 0.5), limit_rate=False, cutoff_frequency=1000.0)  # within one step's rate
        self.assertIn("controller_torque_discontinuity", str(e.exception))

    def test_s0_sees_the_flange_and_the_environments_wrench_whatever_desk_sets(self) -> None:
        robot = FakeFranka(body("franka"), F_T_EE=HAND)
        robot.push([1.0, 0.0, 5.0])  # the surface pushes the flange up (and a little along x)
        s = FrankaArm(robot, body("franka")).state(0)
        flange = frames(body("franka").home)[-1]
        assert s.ee is not None and s.wrench is not None and s.jacobian is not None
        np.testing.assert_allclose(s.ee[:3], flange[:3, 3], atol=1e-9)  # not the hand, 10 cm further
        np.testing.assert_allclose(s.jacobian, jacobian(body("franka").home))
        np.testing.assert_allclose(s.wrench, [1.0, 0.0, 5.0, 0.0, 0.0, 0.0], atol=1e-9)  # positive along the normal, as S0 expects
        self.assertLess(robot.read_once().O_F_ext_hat_K[2], 0)  # libfranka's own sign is the opposite

    def test_an_unmeasured_contact_channel_is_never_faithful(self) -> None:
        class NoWrench:
            def __init__(self, arm: FrankaArm) -> None:
                self.arm = arm

            def state(self, now: Ns) -> RobotState:
                return dataclasses.replace(self.arm.state(now), wrench=None)

            def scene(self, now: Ns) -> dict[str, Array]:
                return {}

            def apply(self, now: Ns, command: Array) -> None:
                pass

        b = body("franka")
        s0 = S0Loop(b, CartesianImpedance(capability("franka"), [1000] * 3 + [50] * 3, b.home), NoWrench(FrankaArm(FakeFranka(b), b)), 100)
        timing = Timing(0, ns(0.05), ns(0.5), "s1", 0)
        motion = MotionIntent.make(timing, "franka.ee_pose.v1", CARTESIAN, [[0.307, 0, 0.55, 0, 1, 0, 0]], "franka.base")
        press = ContactIntent.make(timing, "flange", "franka.base", (0, 0, 1), ["MAINTAIN"], [[10.0]], [5.0], [15.0], [1000] * 3 + [50] * 3)
        s0.submit_chunk(IntentChunk.make(1, "s1", motion, contact=[press]), 0)
        for k in range(50):
            s0.tick(k * ns(0.001))
        c = s0.status().counters
        self.assertEqual((c.contact_unmeasured_ticks, c.contact_ticks), (50, 0))
        fid = fidelity(c, FidelityLimits(0.05, 0.9, 0.8, 0.01))
        self.assertFalse(fid["faithful"])
        blamed = attribute(State.FAILED, Category.VERIFICATION_FAILED, "dirty", fid, c, [], False, False)
        self.assertEqual(blamed and blamed["category"], "EXECUTION")  # never INTENT: nothing shows S0 pressed

    def test_twenty_dropped_commands_in_a_row_stop_the_robot(self) -> None:
        robot = FakeFranka(body("franka"), drops=range(100, 130))
        with self.assertRaises(ArmError) as e:
            self.run_arm(robot, 1.0)
        self.assertIn("communication_constraints_violation", str(e.exception))
        self.assertEqual(len(robot.commands), 100)  # the cycles before the drops; the robot reused the last torque meanwhile

    def test_a_slow_loop_is_a_dropped_one(self) -> None:
        # A Python S0 tick takes about half a millisecond (manual, In the lab): any budget of a microsecond is missed.
        robot = FakeFranka(body("franka"), budget_ns=1_000)
        with self.assertRaises(ArmError) as e:
            self.run_arm(robot, 1.0)
        self.assertIn("communication_constraints_violation", str(e.exception))

    def test_a_torque_jump_is_the_robot_reflex_the_guard_prevents(self) -> None:
        robot = FakeFranka(body("franka"))
        with self.assertRaises(ArmError) as e:
            robot.control(lambda state, period: np.full(7, 5.0), limit_rate=False, cutoff_frequency=1000.0)
        self.assertIn("controller_torque_discontinuity", str(e.exception))

    def test_the_panda_honours_contact_up_to_15_n(self) -> None:
        cap = capability("franka")
        timing = Timing(0, ns(0.05), ns(0.5), "s1", 0)
        motion = MotionIntent.make(timing, "franka.ee_pose.v1", CARTESIAN, [[0.4, 0, 0.3, 0, 1, 0, 0]], "franka.base")
        press = ContactIntent.make(timing, "flange", "franka.base", (0, 0, 1), ["MAINTAIN"], [[10.0]], [5.0], [15.0], [1000] * 3 + [50] * 3)
        chunk = IntentChunk.make(1, "s1", motion, contact=[press])
        self.assertEqual(cap.admit(chunk), (chunk, ()))


@contextmanager
def piper(authorized: bool) -> Iterator[tuple[Any, Any, PiperArm]]:
    from piper_sdk import C_PiperInterface_V2

    from omarchy_runtime.sim.fake_piper import FakePiper, fake_socketcan

    with fake_socketcan(FakePiper(), "fake0") as arm:
        sdk = C_PiperInterface_V2("fake0", judge_flag=False)
        sdk.ConnectPort()
        try:
            deadline = monotonic_ns() + ns(1.0)
            while sdk.GetArmJointMsgs().joint_state.joint_2 == 0 and monotonic_ns() < deadline:
                sleep_until(monotonic_ns() + ns(0.01))
            yield arm, sdk, PiperArm(sdk, body("piper", authorized))
        finally:
            sdk.DisconnectPort()
            type(sdk)._instances.pop("fake0", None)


class TestPiper(unittest.TestCase):
    def test_state_reads_through_the_sdk_without_authorization_and_nothing_moves(self) -> None:
        with piper(authorized=False) as (fake, sdk, arm):
            s = arm.state(0)
            np.testing.assert_allclose(s.position, np.radians([0, 30, -30, 0, 0, 0]), atol=1e-5)
            self.assertEqual(arm.status().arm_status, 0x00)
            s0 = S0Loop(arm.body, MotionTracker(capability("piper")), arm, capacity=10, nominal=ns(0.005))
            with self.assertRaises(NotAuthorized):
                s0.tick(0)
            sent = {can_id for can_id, _ in fake.received}
            self.assertEqual(sent & {0x150, 0x151, 0x155, 0x156, 0x157, 0x471}, set())  # no motion, no enable, no stop

    def test_s0_moves_the_arm_through_the_sdk_within_its_limits(self) -> None:
        with piper(authorized=True) as (fake, sdk, arm):
            b = arm.body
            s0 = S0Loop(b, MotionTracker(capability("piper")), arm, capacity=1000, nominal=ns(0.005))
            target = np.array(b.home) + [0.2, 0.0, 0.0, 0.0, 0.0, 0.0]
            far = np.array(b.home) + [0.2, 0.0, 0.0, 0.0, 2.0, 0.0]  # joint 5 asked past its 70 degrees
            m = MotionIntent.make(Timing(0, ns(0.5), ns(10.0), "test", 0), b.action_space, b.dims, [b.home, target, far])
            s0.submit_chunk(IntentChunk.make(1, "test", m), 0)
            start = monotonic_ns()
            for k in range(400):  # 2 s at 200 Hz, on the wall clock: the SDK and the fake arm run in real time
                sleep_until(start + k * ns(0.005))
                s0.tick(k * ns(0.005))
            sent = [(i, d) for i, d in fake.received if i in (0x471, 0x151, 0x157)]
            self.assertEqual(sent[0][0], 0x471)  # enabled first
            self.assertEqual(fake.status, 0x00)  # never a target past a limit: the guard clamped it
            self.assertLessEqual(float(fake.q[4]), b.upper[4] + 1e-3)
            self.assertAlmostEqual(float(fake.q[0]), float(target[0]), delta=0.02)
            self.assertGreater(s0.status().counters.limit_clamps, 0)

    def test_lost_feedback_stops_s0_before_anything_is_sent(self) -> None:
        with piper(authorized=True) as (fake, sdk, arm):
            b = arm.body
            s0 = S0Loop(b, MotionTracker(capability("piper")), arm, capacity=100, nominal=ns(0.005))
            m = MotionIntent.make(Timing(0, ns(0.5), ns(10.0), "test", 0), b.action_space, b.dims, [b.home, np.array(b.home) + 0.2])
            s0.submit_chunk(IntentChunk.make(1, "test", m), 0)
            fake.silent = True  # the cable pulled before the first command
            sleep_until(monotonic_ns() + ns(0.1))
            self.assertTrue(sdk.isOk())  # the SDK has not noticed yet
            with self.assertRaises(ArmError) as e:
                s0.tick(0)
            self.assertIn("no fresh feedback", str(e.exception))
            with self.assertRaises(ArmError):
                arm.apply(0, np.array(b.home))
            self.assertEqual({i for i, _ in fake.received} & {0x151, 0x155, 0x156, 0x157, 0x471}, set())  # never enabled, nothing sent

    def test_the_arm_reports_a_refused_target_and_a_collision(self) -> None:
        with piper(authorized=True) as (fake, sdk, arm):
            sdk.MotionCtrl_2(0x01, 0x01, 30, 0x00)
            sdk.JointCtrl(0, 30000, -30000, 0, 90000, 0)  # joint 5 at 90 degrees: past its limit
            sleep_until(monotonic_ns() + ns(0.05))
            self.assertEqual(arm.status().arm_status, 0x04)
            fake.collide()
            sleep_until(monotonic_ns() + ns(0.05))
            self.assertEqual(arm.status().arm_status, 0x07)


if __name__ == "__main__":
    unittest.main()
