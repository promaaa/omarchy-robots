"""The lab's arms behind the S0 contract, against fakes only: a fake Panda
behind a libfranka-like interface, and the official PiPER SDK on a fake CAN
bus. No connection to a real arm is ever opened: the PiPER's socketcan is
replaced for the test's duration, and nothing here makes a libfranka
connection. Without an operator authorization, state reads and nothing
reaches the actuators."""

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
from omarchy_runtime.contracts import CARTESIAN, BodySpec, ContactIntent, IntentChunk, MotionIntent, Timing, ns, quat_matrix
from omarchy_runtime.controllers import CartesianImpedance, MotionTracker
from omarchy_runtime.s0 import S0Loop
from omarchy_runtime.sim.fake_franka import FakeFranka, frames
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
            arm.run(s0.tick, ns(0.1))
        with self.assertRaises(NotAuthorized):
            s0.tick(0)  # S0 alone cannot reach the actuators either
        self.assertEqual(robot.commands, [])

    def run_arm(self, robot: FakeFranka, duration: float, push_after: int | None = None, force: float = 0.0) -> FrankaArm:
        b = body("franka", authorized=True)
        arm = FrankaArm(robot, b)
        s0 = S0Loop(b, CartesianImpedance(capability("franka"), [1000] * 3 + [50] * 3, b.home), arm, capacity=round(duration * 1000) + 10)
        ee = arm.state(0).ee
        assert ee is not None
        timing = Timing(0, ns(1.0), ns(100.0), "test", 0)
        motion = MotionIntent.make(timing, "franka.ee_pose.v1", CARTESIAN, [list(ee), [*ee[:2], ee[2] - 0.02, *ee[3:]]], "franka.base")
        s0.submit_chunk(IntentChunk.make(1, "test", motion), 0)
        ticks = 0

        def tick(now: int) -> None:
            nonlocal ticks
            ticks += 1
            if push_after is not None and ticks == push_after:
                robot.push([0.0, 0.0, force])
            s0.tick(now)

        arm.run(tick, ns(duration))
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
