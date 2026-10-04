"""Unit tests: belief predicates, S0 semantics, lifecycle, resolver,
scheduler, kinematics and configuration."""

from __future__ import annotations

import math
import unittest

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.contracts import (
    ActionChunk,
    Category,
    ChunkRejected,
    ConfigError,
    FallbackSpec,
    Goal,
    GoalRequest,
    LifecycleError,
    Rejection,
    S0Mode,
    State,
    Truth,
    ns,
)
from omarchy_runtime.runtime import Binding, Capability, Execution, resolve
from omarchy_runtime.sched import Mailbox, Scheduler
from omarchy_runtime.sim.kinematics import Kinematics
from omarchy_runtime.sim.s0 import SimS0
from omarchy_runtime.sim.world import World
from tests.helpers import ARM, ARM_CFG, GANTRY, IN_RACK, RACK_SPOT, SCENE, TABLE_SPOT, belief, observation

MS = ns(0.001)


class TestBelief(unittest.TestCase):
    def test_in_true_false_unknown(self) -> None:
        self.assertIs(belief(observation(1, 1.0, {"cup_1": RACK_SPOT})).evaluate(IN_RACK).truth, Truth.TRUE)
        self.assertIs(belief(observation(1, 1.0, {"cup_1": TABLE_SPOT})).evaluate(IN_RACK).truth, Truth.FALSE)
        self.assertIs(belief().evaluate(IN_RACK).truth, Truth.UNKNOWN)

    def test_stale_low_confidence_and_edge_are_unknown(self) -> None:
        b = belief(observation(1, 1.0, {"cup_1": RACK_SPOT}))
        self.assertIs(b.evaluate(IN_RACK, since=ns(1.5)).truth, Truth.UNKNOWN)
        self.assertIn("no observation of cup_1 since", b.evaluate(IN_RACK, since=ns(1.5)).reason)
        weak = belief(observation(1, 1.0, {"cup_1": RACK_SPOT}))
        weak._estimates["cup_1"] = weak._estimates["cup_1"].__class__(0.3, 0.8, 0.2, "obs-1", ns(1.0), "test")
        self.assertIs(weak.evaluate(IN_RACK).truth, Truth.UNKNOWN)
        edge = belief(observation(1, 1.0, {"cup_1": (SCENE.regions["drying_rack"].x0 + 0.004, 0.80)}))
        self.assertIs(edge.evaluate(IN_RACK).truth, Truth.UNKNOWN)

    def test_held(self) -> None:
        held = belief(observation(1, 1.0, {"cup_1": RACK_SPOT}, gripper=0.0, ee=RACK_SPOT))
        self.assertIs(held.evaluate(Goal("held", ("cup_1",))).truth, Truth.TRUE)
        self.assertIs(held.evaluate(IN_RACK).truth, Truth.FALSE)  # in the rack's box, but still in the gripper
        released = belief(observation(1, 1.0, {"cup_1": RACK_SPOT}, gripper=1.0, ee=RACK_SPOT))
        self.assertIs(released.evaluate(Goal("held", ("cup_1",))).truth, Truth.FALSE)

    def test_on(self) -> None:
        b = belief(observation(1, 1.0, {"cup_1": TABLE_SPOT, "cup_2": (0.92, 1.0)}))
        self.assertIs(b.evaluate(Goal("on", ("cup_1", "table"))).truth, Truth.TRUE)
        self.assertIs(b.evaluate(Goal("on", ("cup_2", "table"))).truth, Truth.FALSE)
        self.assertIs(b.evaluate(Goal("on", ("cup_1", "counter"))).truth, Truth.FALSE)

    def test_unknown_predicates_and_entities(self) -> None:
        b = belief(observation(1, 1.0, {"cup_1": RACK_SPOT}))
        self.assertIs(b.evaluate(Goal("clean", ("cup_1",))).truth, Truth.UNKNOWN)
        self.assertIs(b.evaluate(Goal("in", ("cup_1", "sink"))).truth, Truth.UNKNOWN)
        self.assertIs(b.evaluate(Goal("in", ("mug_9", "drying_rack"))).truth, Truth.UNKNOWN)

    def test_snapshot_carries_provenance(self) -> None:
        snap = belief(observation(7, 1.0, {"cup_1": RACK_SPOT})).snapshot()
        self.assertEqual(snap["entities"]["cup_1"]["estimate"]["observation"], "obs-7")
        self.assertEqual(snap["entities"]["drying_rack"]["kind"], "region")
        self.assertIsNone(snap["entities"]["cup_2"]["estimate"])
        self.assertEqual(snap["version"], 1)


def chunk(seq: int, t0: float, rows: list[list[float]], valid_s: float, dt: float = 0.01, source: str = "test") -> ActionChunk:
    return ActionChunk.make(seq, GANTRY.action_space, GANTRY.dims, ns(t0), ns(dt), rows, ns(t0 + valid_s), source)


class TestS0(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(SCENE, {"cup_1": 0.78}, GANTRY)
        self.s0 = SimS0(GANTRY, self.world, capacity=10_000)
        self.home = list(GANTRY.home)

    def run_until(self, t_s: float, start_s: float = 0.0) -> None:
        for n in range(round(start_s * 1000), round(t_s * 1000) + 1):
            self.s0.tick(n * MS)

    def test_holds_without_chunk_and_keeps_the_gripper(self) -> None:
        self.run_until(0.1)
        self.assertIs(self.s0.mode, S0Mode.HOLD)
        self.assertEqual(self.s0.pos.tolist(), self.home)

    def test_interpolates_then_starves_at_the_first_tick_after_validity(self) -> None:
        x, z, g = self.home
        self.s0.submit_chunk(chunk(1, 0.0, [[x, z, g], [x + 0.002, z, g]], valid_s=0.05), 0)
        self.run_until(0.005)
        self.assertAlmostEqual(float(self.s0.pos[0]), x + 0.001)
        self.run_until(0.05, 0.006)
        self.assertIs(self.s0.mode, S0Mode.TRACKING)  # valid until 0.05 included
        self.s0.tick(ns(0.051))
        self.assertIs(self.s0.mode, S0Mode.HOLD)
        self.assertEqual(self.s0.status().counters.starvation_events, 1)
        self.assertAlmostEqual(float(self.s0.pos[0]), x + 0.002)

    def test_latest_sequence_wins_and_stale_chunks_are_rejected(self) -> None:
        x, z, g = self.home
        self.s0.submit_chunk(chunk(1, 0.0, [[x, z, g]], 1.0), 0)
        self.s0.submit_chunk(chunk(2, 0.0, [[x + 0.001, z, g]], 1.0), 0)
        with self.assertRaises(ChunkRejected) as e:
            self.s0.submit_chunk(chunk(2, 0.0, [[x, z, g]], 1.0), 0)
        self.assertIs(e.exception.code, Rejection.STALE)
        self.s0.submit_chunk(chunk(1, 0.0, [[x, z, g]], 1.0, source="another"), 0)  # another source replaces it
        self.run_until(1.2)
        self.s0.submit_chunk(chunk(1, 1.2, [[x, z, g]], 1.0), ns(1.2))  # nothing valid is active: a new run starts at 1
        self.assertEqual(self.s0.status().counters.rejected_chunks, 1)

    def test_rejects_other_action_spaces_expired_and_malformed_chunks(self) -> None:
        bad = ActionChunk.make(1, ARM.action_space, ARM.dims, 0, MS, [list(ARM.home)], ns(1), "test")
        with self.assertRaises(ChunkRejected) as e:
            self.s0.submit_chunk(bad, 0)
        self.assertIs(e.exception.code, Rejection.ACTION_SPACE)
        with self.assertRaises(ChunkRejected) as e:
            self.s0.submit_chunk(chunk(1, 0.0, [self.home], 0.5), ns(0.6))
        self.assertIs(e.exception.code, Rejection.EXPIRED)
        with self.assertRaises(ChunkRejected) as e:
            chunk(1, 0.0, [[math.nan, 1.0, 1.0]], 1.0)
        self.assertIs(e.exception.code, Rejection.NOT_FINITE)
        with self.assertRaises(ChunkRejected) as e:
            chunk(1, 0.0, [[1.0, 1.0]], 1.0)
        self.assertIs(e.exception.code, Rejection.SHAPE)

    def test_enforces_position_velocity_and_workspace_limits(self) -> None:
        x, z, g = self.home
        self.s0.submit_chunk(chunk(1, 0.0, [[5.0, z, g]], 2.0), 0)
        self.run_until(0.1)
        self.assertAlmostEqual(float(self.s0.pos[0]), x + 0.5 * 0.1, places=9)  # 0.5 m/s on x
        self.run_until(2.0, 0.101)
        self.assertAlmostEqual(float(self.s0.pos[0]), GANTRY.upper[0])
        self.assertGreater(self.s0.status().counters.limit_clamps, 0)
        self.assertEqual(self.world.violations, [])

    def test_arm_stays_in_its_workspace(self) -> None:
        world = World(SCENE, {}, ARM)
        s0 = SimS0(ARM, world, capacity=10_000)
        low = Kinematics(ARM).ik(0.6, 0.70, ARM.home)  # 10 cm under the workspace
        assert low is not None
        s0.submit_chunk(ActionChunk.make(1, ARM.action_space, ARM.dims, 0, ns(1), [list(ARM.home), [*low, 1.0]], ns(3), "t"), 0)
        for n in range(3001):
            s0.tick(n * MS)
        self.assertGreaterEqual(world.ee[1], ARM.workspace[2] - 1e-9)
        self.assertEqual(world.violations, [])

    def test_fallback_engages_after_the_chunk_then_holds(self) -> None:
        x, z, g = self.home
        self.s0.install_fallback(FallbackSpec.make(GANTRY.action_space, GANTRY.dims, ns(0.5), [[x, z + 0.05, 0.0]], 0, ns(10), "s2"), 0)
        self.s0.submit_chunk(chunk(1, 0.0, [[x, z, g]], 0.2), 0)
        self.run_until(0.2)
        self.assertIs(self.s0.mode, S0Mode.TRACKING)
        self.s0.tick(ns(0.201))
        self.assertIs(self.s0.mode, S0Mode.FALLBACK)
        self.run_until(0.701, 0.202)
        self.assertIs(self.s0.mode, S0Mode.FALLBACK)
        self.assertAlmostEqual(float(self.s0.pos[1]), z + 0.05)  # its last row, at te + dt
        self.assertEqual(float(self.s0.pos[2]), 0.0)
        self.s0.tick(ns(0.702))
        self.assertIs(self.s0.mode, S0Mode.HOLD)
        self.assertFalse(self.s0.status().fallback_installed)  # spent: engaged once
        c = self.s0.status().counters
        self.assertEqual((c.starvation_events, c.fallback_ns), (1, ns(0.501)))  # ticks 0.201 to 0.701 included

    def test_fallback_outside_its_window_is_not_used(self) -> None:
        x, z, g = self.home
        self.s0.install_fallback(FallbackSpec.make(GANTRY.action_space, GANTRY.dims, ns(0.5), [[x, z + 0.05, g]], ns(5), ns(6), "s2"), 0)
        self.s0.submit_chunk(chunk(1, 0.0, [[x, z, g]], 0.1), 0)
        self.run_until(0.2)
        self.assertIs(self.s0.mode, S0Mode.HOLD)

    def test_a_new_chunk_takes_over_from_a_fallback(self) -> None:
        x, z, g = self.home
        self.s0.install_fallback(FallbackSpec.make(GANTRY.action_space, GANTRY.dims, ns(1.0), [[x, z + 0.1, g]], 0, ns(10), "s2"), 0)
        self.run_until(0.1)
        self.assertIs(self.s0.mode, S0Mode.FALLBACK)
        self.s0.submit_chunk(chunk(1, 0.1, [[x, z, g]], 1.0), ns(0.1))
        self.s0.tick(ns(0.101))
        self.assertIs(self.s0.mode, S0Mode.TRACKING)


class TestLifecycle(unittest.TestCase):
    def execution(self) -> Execution:
        return Execution("x1", GoalRequest(IN_RACK, "t1"), 0, {})

    def test_legal_path(self) -> None:
        ex = self.execution()
        ex.transition(State.RUNNING)
        ex.transition(State.VERIFYING)
        ex.transition(State.SUCCEEDED)
        self.assertIs(ex.state, State.SUCCEEDED)

    def test_illegal_transitions_raise(self) -> None:
        ex = self.execution()
        with self.assertRaises(LifecycleError):
            ex.transition(State.SUCCEEDED)  # never without verification
        ex.transition(State.RUNNING)
        with self.assertRaises(LifecycleError):
            ex.transition(State.FAILED)  # an outcome other than SUCCEEDED needs a category
        with self.assertRaises(LifecycleError):
            ex.transition(State.FAILED, Category.NO_CAPABILITY)  # a rejection category
        ex.transition(State.FAILED, Category.S1_STALL)
        with self.assertRaises(LifecycleError):
            ex.transition(State.RUNNING)  # terminal

    def test_inconclusive_is_never_success(self) -> None:
        ex = self.execution()
        ex.transition(State.RUNNING)
        ex.transition(State.VERIFYING)
        with self.assertRaises(LifecycleError):
            ex.transition(State.SUCCEEDED, Category.VERIFICATION_INCONCLUSIVE)
        ex.transition(State.UNVERIFIED, Category.VERIFICATION_INCONCLUSIVE)


class TestResolver(unittest.TestCase):
    caps = config.capabilities(ARM_CFG)
    binds = config.bindings(ARM_CFG)

    def test_chooses_the_binding_for_the_body_with_reasons(self) -> None:
        b = belief(observation(1, 1.0, {"cup_1": TABLE_SPOT}))
        for body, chosen, other in ((ARM, "pick_place.arm3", "pick_place.gantry"), (GANTRY, "pick_place.gantry", "pick_place.arm3")):
            r = resolve(IN_RACK, b, body, self.caps, self.binds, ns(0.5))
            self.assertEqual(r.binding.implementation if r.binding else None, chosen)
            reasons = {c.implementation: c.reason for c in r.candidates}
            self.assertTrue(reasons[chosen].startswith("chosen"))
            self.assertIn(f"not bound to body {body.id}", reasons[other])
            self.assertEqual(r, resolve(IN_RACK, b, body, self.caps, self.binds, ns(0.5)))  # deterministic

    def test_unknown_goal_has_no_capability(self) -> None:
        r = resolve(Goal("clean", ("cup_1",)), belief(), ARM, self.caps, self.binds, 0)
        self.assertIs(r.category, Category.NO_CAPABILITY)
        self.assertEqual([c.reason for c in r.candidates], ["achieves in, not clean"])

    def test_preconditions(self) -> None:
        r = resolve(IN_RACK, belief(), ARM, self.caps, self.binds, 0)
        self.assertIs(r.category, Category.PRECONDITION_UNMET)
        stale = belief(observation(1, 1.0, {"cup_1": TABLE_SPOT}))
        self.assertIs(resolve(IN_RACK, stale, ARM, self.caps, self.binds, ns(2.0)).category, Category.PRECONDITION_UNMET)
        far = belief(observation(1, 1.0, {"cup_1": (1.4, 0.80)}))
        r = resolve(IN_RACK, far, ARM, self.caps, self.binds, ns(0.5))
        self.assertIs(r.category, Category.PRECONDITION_UNMET)
        self.assertIn("outside the workspace of arm3", r.candidates[0].reason)

    def test_body_features_and_bindings(self) -> None:
        no_gripper = ARM.__class__(**{**ARM.__dict__, "features": ()})
        r = resolve(IN_RACK, belief(observation(1, 1.0, {"cup_1": TABLE_SPOT})), no_gripper, self.caps, self.binds, ns(0.5))
        self.assertIs(r.category, Category.NO_CAPABILITY)
        self.assertIn("lacks gripper", r.candidates[0].reason)

    def test_priority_then_name_decides(self) -> None:
        binds = [Binding("place_in", "b_impl", "1", ("arm3",), 5), Binding("place_in", "a_impl", "1", ("arm3",), 5)]
        binds.append(Binding("place_in", "z_impl", "2", ("arm3",), 9))
        b = belief(observation(1, 1.0, {"cup_1": TABLE_SPOT}))
        self.assertEqual(resolve(IN_RACK, b, ARM, self.caps, binds, ns(0.5)).binding, binds[2])
        self.assertEqual(resolve(IN_RACK, b, ARM, self.caps, binds[:2], ns(0.5)).binding, binds[1])

    def test_already_satisfied_short_circuits(self) -> None:
        r = resolve(IN_RACK, belief(observation(1, 1.0, {"cup_1": RACK_SPOT})), ARM, self.caps, self.binds, ns(0.5))
        self.assertIsNone(r.binding)
        self.assertIsNotNone(r.satisfied)
        self.assertTrue(r.reason.startswith("already satisfied"))

    def test_capability_shapes(self) -> None:
        self.assertEqual(self.caps, [Capability("place_in", "in", ("object", "container"), ("gripper",), self.caps[0].preconditions)])


class TestScheduler(unittest.TestCase):
    def test_order_and_rates(self) -> None:
        sched, seen = Scheduler(MS), []
        sched.add("fast", MS, lambda t: seen.append(("fast", t)))
        sched.add("slow", ns(0.003), lambda t: seen.append(("slow", t)))
        self.assertTrue(sched.run(lambda: sched.now >= ns(0.003), until=ns(1), check_every=MS))
        self.assertEqual(seen, [("fast", 0), ("slow", 0), ("fast", MS), ("fast", 2 * MS), ("fast", 3 * MS), ("slow", 3 * MS)])
        with self.assertRaises(ConfigError):
            sched.add("odd", ns(0.0015), lambda t: None)

    def test_mailbox_keeps_order(self) -> None:
        box: Mailbox[int] = Mailbox()
        for i in range(3):
            box.put(i)
        self.assertEqual(box.drain(), [0, 1, 2])
        self.assertEqual(box.drain(), [])


class TestKinematics(unittest.TestCase):
    def test_arm_inverse_matches_forward_over_the_workspace(self) -> None:
        k = Kinematics(ARM)
        x0, x1, z0, z1 = ARM.workspace
        q = list(ARM.home[:3])
        for x in np.linspace(x0, x1, 15):
            for z in np.linspace(z0, z1, 8):
                solution = k.ik(float(x), float(z), q)
                assert solution is not None
                self.assertTrue(all(lo <= v <= hi for v, lo, hi in zip(solution, ARM.lower, ARM.upper, strict=False)))
                ex, ez = k.ee(solution)
                self.assertAlmostEqual(ex, x)
                self.assertAlmostEqual(ez, z)
        self.assertIsNone(k.ik(3.0, 0.8, q))

    def test_home_and_safe_pose_are_inside_the_workspace(self) -> None:
        for body in (ARM, GANTRY):
            k = Kinematics(body)
            for pose in (body.home, body.safe_pose):
                self.assertTrue(body.reaches(*k.ee(pose[:-1])), (body.id, pose))


class TestConfig(unittest.TestCase):
    def test_effective_and_digest(self) -> None:
        self.assertEqual(config.digest(config.effective("nominal_a")), config.digest(ARM_CFG))
        self.assertNotEqual(config.digest(config.effective("nominal_b")), config.digest(ARM_CFG))

    def test_bad_names_raise_config_errors(self) -> None:
        with self.assertRaises(ConfigError):
            config.effective("no_such_scenario")
        with self.assertRaises(ConfigError):
            config.get({"a": "x"}, "a", float, "test")


if __name__ == "__main__":
    unittest.main()
