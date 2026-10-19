"""The S0 guard against adversarial controllers, property style: over many
seeded random runs, a controller that emits NaN, infinities, huge values,
jumps, stale or malformed commands, or raises, never gets anything but a
finite, limited, bounded command to the actuators, and every intervention
is counted (and so traced: s0.guard events, the execution's fidelity)."""

from __future__ import annotations

import unittest
from collections.abc import Mapping

import numpy as np

from omarchy_runtime.contracts import (
    Array,
    BodySpec,
    Command,
    ControllerCapability,
    IntentChunk,
    MotionIntent,
    Ns,
    RobotState,
    S0Mode,
    Timing,
    ns,
)
from omarchy_runtime.s0 import S0Loop
from omarchy_runtime.sim.kinematics import Kinematics
from omarchy_runtime.sim.s0 import PlanarRobot
from omarchy_runtime.sim.world import World
from tests.helpers import GANTRY, SCENE, scenario

MS = ns(0.001)
BAD = ("nan", "inf", "huge", "jump", "stale", "shape", "kind", "raise")
REPLACED = {"nan", "inf", "stale", "shape", "kind", "raise"}  # the guard's own command instead; huge and jump are clamped


class Adversarial:
    """Each tick, at random: a sane command, or one of BAD."""

    def __init__(self, capability: ControllerCapability, rng: np.random.Generator, sane: Array, spread: float) -> None:
        self.capability = capability
        self.rng = rng
        self.sane = sane
        self.spread = spread
        self.log: list[str] = []

    def command(self, intent: IntentChunk, state: RobotState, scene: Mapping[str, Array], now: Ns) -> Command:
        kind = self.capability.command
        what = str(self.rng.choice(["ok", *BAD], p=[0.5] + [0.5 / len(BAD)] * len(BAD)))
        self.log.append(what)
        v = self.sane + self.rng.normal(0, self.spread, len(self.sane))
        if what == "raise":
            raise RuntimeError("a controller bug")
        bad: dict[str, Command] = {
            "nan": Command(now, kind, np.where(np.arange(len(v)) == self.rng.integers(len(v)), np.nan, v)),
            "inf": Command(now, kind, np.full_like(v, -np.inf)),
            "huge": Command(now, kind, v * 1e6),
            "jump": Command(now, kind, v + self.rng.choice([-1, 1], len(v)) * 10.0),
            "stale": Command(now - MS, kind, v),
            "shape": Command(now, kind, np.append(v, 0.0)),
            "kind": Command(now, "velocity", v),
        }
        return bad.get(what, Command(now, kind, v))


def tracking(body: BodySpec, s0: S0Loop) -> None:
    """A chunk valid for the whole run: S0 is in TRACKING, the controller always asked."""
    m = MotionIntent.make(Timing(0, ns(1.0), ns(100.0), "test", 0), body.action_space, body.dims, [list(body.home)])
    s0.submit_chunk(IntentChunk.make(1, "test", m), 0)


class TestPositionGuard(unittest.TestCase):
    def test_nothing_unsafe_reaches_the_actuators(self) -> None:
        kin = Kinematics(GANTRY)
        for seed in range(12):
            rng = np.random.default_rng([seed, 9])
            world = World(SCENE, {"cup_1": 0.78}, GANTRY)
            cap = ControllerCapability("adversary", "1", "position", 1000, (MS, ns(10)), (GANTRY.action_space,))
            ctl = Adversarial(cap, rng, np.array(GANTRY.home), 0.3)
            s0 = S0Loop(GANTRY, ctl, PlanarRobot(world), capacity=600, forward=kin.ee)
            tracking(GANTRY, s0)
            for n in range(600):
                s0.tick(n * MS)
            with self.subTest(seed=seed):
                pos = s0.record_position[:600]
                self.assertTrue(np.isfinite(pos).all())
                self.assertTrue(np.all((pos >= np.array(GANTRY.lower) - 1e-12) & (pos <= np.array(GANTRY.upper) + 1e-12)))
                self.assertTrue(np.all(np.abs(np.diff(pos, axis=0)) <= s0.bound * (1 + 1e-9) + 1e-12))
                self.assertTrue(all(GANTRY.inside(kin.ee(p.tolist())) for p in pos))
                self.assertEqual(world.violations, [])
                c = s0.status().counters
                self.assertEqual(c.guard_replacements, sum(w in REPLACED for w in ctl.log))
                self.assertGreater(c.limit_clamps, 0)
                self.assertTrue(set(np.unique(s0.record_mode[:600])) <= {0})  # TRACKING throughout: the guard never stops S0


class TorqueArm:
    """Three unit-inertia joints with damping, gravity compensated (as a Franka
    does it), an end-effector at the joint vector's first two values, and an
    external force S0 can read."""

    def __init__(self, body: BodySpec) -> None:
        self.q = np.array(body.home, dtype=np.float64)
        self.v = np.zeros(3)
        self.force = np.zeros(6)
        self.applied: list[Array] = []

    def state(self, now: Ns) -> RobotState:
        return RobotState(
            now, self.q.copy(), self.v.copy(), ee=np.r_[self.q[:2], 0.0, 1.0, 0.0, 0.0, 0.0][[0, 1]], wrench=self.force.copy()
        )

    def scene(self, now: Ns) -> Mapping[str, Array]:
        return {}

    def apply(self, now: Ns, command: Array) -> None:
        self.applied.append(command.copy())
        self.v += (command - 2.0 * self.v) * 0.001
        self.q += self.v * 0.001


ARM = BodySpec(
    "torque3",
    "test",
    "torque3.joint_position.v1",
    ("j1", "j2", "j3"),
    (-1.0, -1.0, -1.0),
    (1.0, 1.0, 1.0),
    (2.0, 2.0, 2.0),
    (-1.0, 1.0, -1.0, 1.0),
    (),
    (0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0),
    {
        "limits": {
            "torque_nm": [10, 10, 5],
            "torque_rate_nm_s": [1000] * 3,
            "force_n": 20.0,
            "hold_stiffness": [30] * 3,
            "hold_damping": [5] * 3,
        }
    },
)


class TestTorqueGuard(unittest.TestCase):
    def run_arm(self, seed: int, force: float = 0.0) -> tuple[TorqueArm, S0Loop, Adversarial]:
        rng = np.random.default_rng([seed, 10])
        arm = TorqueArm(ARM)
        arm.force[2] = force
        cap = ControllerCapability("adversary", "1", "torque", 1000, (MS, ns(10)), (ARM.action_space,))
        ctl = Adversarial(cap, rng, np.zeros(3), 20.0)
        s0 = S0Loop(ARM, ctl, arm, capacity=800)
        tracking(ARM, s0)
        for n in range(800):
            s0.tick(n * MS)
        return arm, s0, ctl

    def test_torques_stay_finite_limited_and_rate_bounded(self) -> None:
        for seed in range(12):
            arm, s0, ctl = self.run_arm(seed)
            tau = np.array(arm.applied)
            with self.subTest(seed=seed):
                self.assertTrue(np.isfinite(tau).all())
                self.assertTrue(np.all(np.abs(tau) <= np.array([10, 10, 5]) + 1e-9))
                self.assertTrue(np.all(np.abs(np.diff(tau, axis=0)) <= 1000 * 0.001 + 1e-9))
                self.assertGreaterEqual(s0.status().counters.guard_replacements, sum(w in REPLACED for w in ctl.log))

    def test_too_much_force_hands_over_to_the_guard(self) -> None:
        arm, s0, ctl = self.run_arm(0, force=25.0)  # above the 20 N the guard allows: every tick is a breach
        self.assertEqual(s0.status().counters.guard_replacements, 800)
        np.testing.assert_allclose(arm.applied[-1], 30 * (np.array(ARM.home) - arm.q) - 5 * arm.v, atol=1e-6)  # its spring and damper


class TestTraced(unittest.TestCase):
    def test_a_faulty_controller_is_replaced_and_the_trace_says_so(self) -> None:
        r = scenario("guard_controller_fault")
        (s,) = r.summaries
        self.assertEqual(s["outcome"], "SUCCEEDED")  # S1 plans again from where the guard held the arm
        self.assertEqual(s["fidelity"]["guard_replacements"], 300)  # 0.3 s of NaN at 1 kHz
        events = [line for line in (r.dir / "events.jsonl").read_text().splitlines() if '"s0.guard"' in line]
        self.assertTrue(any('"replacements":50' in e for e in events), events[:3])
        self.assertEqual(r.s0.status().mode, S0Mode.HOLD)


if __name__ == "__main__":
    unittest.main()
