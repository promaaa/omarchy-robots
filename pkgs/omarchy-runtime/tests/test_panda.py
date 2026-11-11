"""The simulated Franka Panda (MuJoCo, the Menagerie model): contact intent
honoured behind the guard, what its controller refuses, the guard against a
controller gone wrong on the real dynamics, the same trace for the same
seed, and a video of a run. The scrubbing scenarios' outcomes and
attributions are held by test_scenarios, like every scenario's."""

from __future__ import annotations

import json
import unittest
from typing import Any

import numpy as np

from omarchy_runtime import config, scrub
from omarchy_runtime.contracts import (
    CARTESIAN,
    ActionChunk,
    ChunkRejected,
    ContactIntent,
    IntentChunk,
    MotionIntent,
    Rejection,
    Timing,
    ns,
)
from omarchy_runtime.s0 import S0Loop
from omarchy_runtime.scenario import run
from omarchy_runtime.sim import panda
from tests.helpers import scenario, tmpdir
from tests.test_guard import Adversarial
from tests.test_trace import trace

CFG = config.effective("scrub_contact")
BODY = config.body(CFG)
_, CAPABILITY = config.controller(CFG)
DOWN = list(BODY.kinematics["cartesian"]["orientation"])


def world() -> panda.PandaWorld:
    return panda.PandaWorld(CFG["world"], BODY, np.random.default_rng(0), capacity=2000)


class TestRefusals(unittest.TestCase):
    def setUp(self) -> None:
        from omarchy_runtime.controllers import CartesianImpedance

        self.s0 = S0Loop(BODY, CartesianImpedance(CAPABILITY, [1000] * 3 + [50] * 3, BODY.home), world(), capacity=10)

    def test_joint_targets_and_too_much_force_are_refused(self) -> None:
        joints = ActionChunk.make(1, BODY.action_space, BODY.dims, 0, ns(0.05), [list(BODY.home)], ns(0.5), "s1")
        with self.assertRaises(ChunkRejected) as e:
            self.s0.submit_chunk(joints, 0)  # its controller declares Cartesian motion only
        self.assertEqual((e.exception.code, e.exception.channel), (Rejection.UNSUPPORTED_INTENT, "motion"))
        timing = Timing(0, ns(0.05), ns(0.5), "s1", 0)
        motion = MotionIntent.make(timing, "panda.ee_pose.v1", CARTESIAN, [[0.55, 0, 0.45, *DOWN]], "panda.base")
        hard = ContactIntent.make(timing, "pad", "panda.base", (0, 0, 1), ["MAINTAIN"], [[35.0]], [5.0], [40.0], [800] * 3 + [50] * 3)
        with self.assertRaises(ChunkRejected) as e:
            self.s0.submit_chunk(IntentChunk.make(2, "s1", motion, contact=[hard]), 0)
        self.assertEqual((e.exception.code, e.exception.channel), (Rejection.UNSUPPORTED_INTENT, "contact"))
        self.assertEqual(self.s0.status().counters.rejected_chunks, 2)


class TestGuardOnTheArm(unittest.TestCase):
    def test_a_controller_gone_wrong_never_drives_the_arm_past_its_limits(self) -> None:
        w = world()
        ctl = Adversarial(CAPABILITY, np.random.default_rng(3), np.zeros(7), 50.0)
        s0 = S0Loop(BODY, ctl, w, capacity=1500)
        m = MotionIntent.make(
            Timing(0, ns(1.0), ns(100.0), "test", 0), "panda.ee_pose.v1", CARTESIAN, [[0.55, 0, 0.5, *DOWN]], "panda.base"
        )
        s0.submit_chunk(IntentChunk.make(1, "test", m), 0)
        for n in range(1500):
            s0.tick(n * ns(0.001))
        applied = np.array(w.data.actuator_force[:7])  # what the actuators got at the last tick
        self.assertTrue(np.all(np.abs(applied) <= np.array([87, 87, 87, 87, 12, 12, 12]) + 1e-9))
        c = s0.status().counters
        self.assertGreater(c.guard_replacements, 0)
        self.assertGreater(c.limit_clamps, 0)
        self.assertEqual(w.violations, [])


class TestScrubbing(unittest.TestCase):
    def test_contact_intent_keeps_the_force_within_bounds(self) -> None:
        (s,) = scenario("scrub_contact").summaries
        f = s["fidelity"]
        self.assertEqual((s["outcome"], f["faithful"]), ("SUCCEEDED", True))
        self.assertGreater(f["force_share"], 0.95)
        self.assertLess(f["force_peak"], 20.0)
        w: Any = scenario("scrub_contact").world
        self.assertEqual(w.coverage(), 1.0)
        self.assertLess(float(np.linalg.norm(w.plate_pose()[:2] - w.plate0[:2])), 0.001)  # the plate stays

    def test_a_foam_of_columns_wipes_the_flat_plate_within_bounds(self) -> None:
        r = run("scrub_contact", tmpdir() / "scrub_foam", overrides={"world": {"sponge": {"columns": [3, 6]}}})
        (s,) = r.summaries
        w: Any = r.world
        self.assertEqual((s["outcome"], len(w.pads)), ("SUCCEEDED", 18))  # the columns touch, not the block
        self.assertGreaterEqual(w.coverage(), 0.95)
        self.assertLess(s["fidelity"]["force_peak"], 20.0)
        none = w.pressed(np.full(18, w.f_min / 18 - 0.01))  # every column under its share of the cleaning force
        self.assertFalse(none.any())

    def test_the_foam_wipes_a_bowl_the_block_cannot(self) -> None:
        bowl = {"world": {"plate": {"bowl_m": 0.4}}}
        foam = run("scrub_contact", tmpdir() / "bowl_foam", overrides=config.merge(bowl, {"world": {"sponge": {"columns": [3, 6]}}}))
        block = run("scrub_contact", tmpdir() / "bowl_block", overrides=bowl)
        w: Any = foam.world
        self.assertAlmostEqual(float(w.rise.max()), 2 * 0.09**2 / (2 * 0.4), places=4)  # a corner cell, 9 cm out in x and in y
        self.assertEqual(foam.summaries[0]["outcome"], "SUCCEEDED")
        self.assertGreaterEqual(w.coverage(), 0.9)
        self.assertLess(block.world.coverage(), 0.5)  # type: ignore[attr-defined]

    def test_the_foam_wipes_a_round_plate_and_a_round_bowl(self) -> None:
        # The scripted wiper (sim/wipe.py), whose lines follow the circle; a bowl 3.6 cm deep at its rim.
        dish: Any = {"plate": {"half": 0.135, "round": True}, "dirt": {"margin_m": 0.035}, "sponge": {"columns": [3, 6]}}
        dish["objects"] = {"plate": {"width": 0.27, "round": True}}
        rim = {"world": config.merge(dish, {"plate": {"well_m": 0.09, "rim_m": 0.015}})}
        plate = run("vla_wipe", tmpdir() / "round_plate", overrides=rim)
        bowl = run("vla_wipe", tmpdir() / "round_bowl", overrides={"world": config.merge(dish, {"plate": {"bowl_m": 0.25}})})
        w: Any = plate.world
        self.assertEqual(len(w.cells), 80)  # the cells of the 10 x 10 grid within 10 cm of the centre
        self.assertLessEqual(float(np.hypot(*w.cells.T).max()), 0.1)
        self.assertEqual(float(panda.rise(rim["world"], np.array([0.09, 0.0]))), 0.0)  # flat within the well
        self.assertAlmostEqual(float(panda.rise(rim["world"], np.array([0.135, 0.0]))), 0.015)  # the rim, at the edge
        self.assertAlmostEqual(float(bowl.world.rise.max()), 2 * 0.07**2 / (2 * 0.25), places=4)  # type: ignore[attr-defined]  # 7 cm out in x and y
        for r in (plate, bowl):
            self.assertEqual(r.summaries[0]["outcome"], "SUCCEEDED")
            self.assertGreaterEqual(r.world.coverage(), 0.95)  # type: ignore[attr-defined]
            self.assertLess(r.summaries[0]["fidelity"]["force_peak"], 25.0)

    def test_the_same_seed_gives_the_same_trace(self) -> None:
        again = run("scrub_weak", tmpdir() / "scrub_weak_again", record_s0=True)
        self.assertEqual(trace(scenario("scrub_weak").dir), trace(again.dir))
        with np.load(scenario("scrub_weak").dir / "world.npz") as a, np.load(again.dir / "world.npz") as b:
            for key in a:
                self.assertTrue(np.array_equal(a[key], b[key]), key)

    def test_a_recorded_run_renders_to_a_video(self) -> None:
        out = tmpdir() / "hover.mp4"
        provenance = scrub.render(scenario("scrub_hover").dir, out, fps=2, size=(120, 160))
        self.assertGreater(out.stat().st_size, 1000)
        self.assertEqual((provenance["scenario"], provenance["s1_contact"], provenance["plate_bias_z_m"]), ("scrub_hover", False, 0.02))
        self.assertGreater(provenance["frames"], 30)
        json.dumps(provenance)


if __name__ == "__main__":
    unittest.main()
