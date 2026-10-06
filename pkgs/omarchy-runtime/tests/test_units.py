"""Unit tests: belief predicates, S0 semantics, lifecycle, resolver,
scheduler, kinematics and configuration."""

from __future__ import annotations

import json
import math
import unittest

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.contracts import (
    ActionChunk,
    BodySpec,
    Cancel,
    Category,
    ChunkRejected,
    ConfigError,
    ContractError,
    Decision,
    FallbackSpec,
    Goal,
    GoalRequest,
    Heartbeat,
    InstallFallback,
    LifecycleError,
    Message,
    Rejection,
    S0Mode,
    State,
    Truth,
    Update,
    message_from_json,
    ns,
)
from omarchy_runtime.runtime import Binding, Capability, Execution, resolve
from omarchy_runtime.sched import Mailbox, Scheduler, WallScheduler, distribution
from omarchy_runtime.sim.kinematics import Kinematics
from omarchy_runtime.sim.s0 import SimS0
from omarchy_runtime.sim.s1 import ScriptedPickPlace
from omarchy_runtime.sim.world import World
from omarchy_runtime.wallclock import monotonic_ns, sleep_until
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

    def test_hold_drops_the_chunk_and_the_fallback(self) -> None:
        x, z, g = self.home
        self.s0.install_fallback(FallbackSpec.make(GANTRY.action_space, GANTRY.dims, ns(0.5), [[x, z + 0.05, g]], 0, ns(10), "s2"), 0)
        self.s0.submit_chunk(chunk(1, 0.0, [[x, z, g], [x + 0.1, z, g]], 5.0, dt=1.0), 0)
        self.run_until(0.1)
        self.assertIs(self.s0.mode, S0Mode.TRACKING)
        moved = float(self.s0.pos[0])
        self.s0.hold()
        self.run_until(0.3, 0.101)
        self.assertIs(self.s0.mode, S0Mode.HOLD)  # not the fallback, and not the rest of the chunk
        self.assertEqual(float(self.s0.pos[0]), moved)
        self.assertEqual(self.s0.status().counters.starvation_events, 0)
        self.assertFalse(self.s0.status().fallback_installed)
        self.s0.submit_chunk(chunk(1, 0.3, [[x, z, g]], 1.0), ns(0.3))  # a new run may start at 1 at once
        self.s0.tick(ns(0.301))
        self.assertIs(self.s0.mode, S0Mode.TRACKING)

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
        return Execution("x1", GoalRequest(IN_RACK, "t1", "r1"), 0, {})

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


class TestMessages(unittest.TestCase):
    """Every message of the API, every Update and a body spec survive JSON, as they do between processes."""

    def test_round_trip(self) -> None:
        spec = FallbackSpec.make(ARM.action_space, ARM.dims, ns(1.5), [list(ARM.safe_pose)], ns(2.0), ns(62.0), "s2.scripted")
        messages: list[Message] = [
            GoalRequest(IN_RACK, "t01", "t01-r01"),
            GoalRequest(IN_RACK, "t01", "t01-r02", "run-x01", {"speed": 0.5, "note": ["a", 1]}),
            InstallFallback("run-x01", spec),
            Cancel("run-x02"),
            Decision("t01", "run-x01", "retry", "verification failed", "retry_once@1"),
            Heartbeat(3, ns(0.2), (ns(0.0012), ns(0.0009))),
            Heartbeat(4, ns(0.3), final=True),
        ]
        for message in messages:
            with self.subTest(type(message).__name__):
                wire = json.loads(json.dumps(message.to_json()))
                again = message_from_json(wire)
                self.assertIs(type(again), type(message))
                self.assertEqual(again.to_json(), message.to_json())
        self.assertEqual(message_from_json(messages[1].to_json()), messages[1])
        update = Update("t01-r01", "run-x01", "t01", State.SUCCEEDED, ns(8.05), {"outcome": "SUCCEEDED"})
        self.assertEqual(Update.from_json(json.loads(json.dumps(update.to_json()))), update)
        for body in (ARM, GANTRY):
            self.assertEqual(BodySpec.from_json(json.loads(json.dumps(body.to_json()))), body)

    def test_refuses_what_is_not_a_message(self) -> None:
        good = GoalRequest(IN_RACK, "t01", "t01-r01").to_json()
        bad = [
            [],
            {"type": "launch"},
            good | {"request_id": 7},
            good | {"retry_of": True},
            {k: v for k, v in good.items() if k != "task_id"},
            good | {"goal": {"predicate": "in"}},
            Heartbeat(1, 0).to_json() | {"rtt": [-1]},
            Heartbeat(1, 0).to_json() | {"final": 1},
            InstallFallback("x", FallbackSpec.make(ARM.action_space, ARM.dims, 1, [[0.0] * 4], 0, 1, "s")).to_json() | {"spec": {}},
        ]
        for value in bad:
            with self.subTest(value), self.assertRaises(ContractError):
                message_from_json(value)
        with self.assertRaises(ContractError):
            Update.from_json(Update("r", "x", "t", State.RUNNING, 0, None).to_json() | {"state": "DANCING"})
        with self.assertRaises(ContractError):
            BodySpec.from_json(ARM.to_json() | {"workspace": [0.0, 1.0]})


class TestScheduler(unittest.TestCase):
    def test_order_and_rates(self) -> None:
        sched, seen = Scheduler(MS), []
        sched.add("fast", MS, lambda t: seen.append(("fast", t)))
        sched.add("slow", ns(0.003), lambda t: seen.append(("slow", t)))
        self.assertTrue(sched.run(lambda: sched.now >= ns(0.003), until=ns(1), check_every=MS))
        self.assertEqual(seen, [("fast", 0), ("slow", 0), ("fast", MS), ("fast", 2 * MS), ("fast", 3 * MS), ("slow", 3 * MS)])
        with self.assertRaises(ConfigError):
            sched.add("odd", ns(0.0015), lambda t: None)

    def test_wall_clock_steps_in_order_and_counts_overruns(self) -> None:
        sched, seen = WallScheduler(), []

        def slow(now: int) -> None:
            seen.append(("b", now))
            if len([n for n, _ in seen if n == "b"]) == 3:
                sleep_until(monotonic_ns() + ns(0.006))  # blocks a for three of its periods

        sched.add("a", ns(0.002), lambda now: seen.append(("a", now)))
        sched.add("b", ns(0.010), slow)
        self.assertFalse(sched.run(lambda: False, until=ns(0.1), check_every=ns(0.01)))
        self.assertEqual(seen[:2], [("a", seen[0][1]), ("b", seen[1][1])])  # due together: first added first
        times = [t for _, t in seen]
        self.assertEqual(times, sorted(times))
        self.assertGreater(sched.now, ns(0.1))
        stats = sched.stats()
        self.assertGreaterEqual(stats["a"]["overruns"], 1)
        self.assertGreaterEqual(stats["a"]["skipped"], 2)
        self.assertLessEqual(stats["a"]["steps"], 51 - stats["a"]["skipped"])  # missed releases are skipped, not caught up
        self.assertEqual(stats["b"]["interval_ns"]["n"], stats["b"]["steps"] - 1)
        self.assertGreaterEqual(stats["b"]["step_ns"]["max"], ns(0.006))
        stop = WallScheduler()
        stop.add("a", ns(0.002), lambda now: None)
        self.assertTrue(stop.run(lambda: stop.now > ns(0.01), until=ns(1.0), check_every=ns(0.002)))
        self.assertLess(stop.now, ns(0.5))

    def test_distribution(self) -> None:
        self.assertEqual(distribution([]), {"n": 0, "p50": None, "p99": None, "max": None})
        self.assertEqual(distribution(list(range(1, 101))), {"n": 100, "p50": 50, "p99": 99, "max": 100})

    def test_mailbox_keeps_order(self) -> None:
        box: Mailbox[int] = Mailbox()
        for i in range(3):
            box.put(i)
        self.assertEqual(box.drain(), [0, 1, 2])
        self.assertEqual(box.drain(), [])


class TestScriptedS1(unittest.TestCase):
    def test_after_a_lag_it_reaches_the_grasp_point_before_closing(self) -> None:
        # Found by vm-s2-placement in CI: the wall-clock loop stalled for 283 ms,
        # S0 lagged more than REPLAN behind the plan, and the plan was replanned
        # into its gripper-closing waypoint. Moving down and closing together,
        # the gripper caught the cup above the grasp point and pushed it into the
        # table. A gripper change happens in place: reach the spot, then close.
        policy = ScriptedPickPlace("pick_place.gantry")
        policy.start(IN_RACK, belief(observation(1, 0.0, {"cup_1": TABLE_SPOT})).snapshot(), GANTRY, {})
        self.assertIsNotNone(policy.step(0, observation(2, 0.0, {})))
        closing = next(start for start, _, a, b, i in policy._plan if a[:2] == b[:2] and b[2] < a[2])
        grasp_x, grasp_z = policy._waypoints[1][:2]
        late = observation(3, (closing + ns(0.2)) / 1e9, {}, gripper=1.0, ee=(grasp_x, grasp_z + 0.04))  # 4 cm above, open
        chunk = policy.step(closing + ns(0.2), late)
        assert chunk is not None
        for _, z, g in chunk.actions.tolist():
            if g < 0.5:  # where the cup would attach
                self.assertLessEqual(z, grasp_z + 0.002, f"the gripper closes at z = {z:.3f}, above the grasp point {grasp_z:.3f}")

    def test_a_lagging_gripper_at_the_grasp_point_keeps_closing(self) -> None:
        # Found by vm-s2-placement in CI on a starved runner, after the fix above:
        # with the arm at the grasp point and only the gripper behind the plan,
        # stepping back to the open waypoint reopened the gripper at every
        # replan, and the grasp repeated for seconds. At the spot, keep closing.
        policy = ScriptedPickPlace("pick_place.gantry")
        policy.start(IN_RACK, belief(observation(1, 0.0, {"cup_1": TABLE_SPOT})).snapshot(), GANTRY, {})
        self.assertIsNotNone(policy.step(0, observation(2, 0.0, {})))
        closing = next(start for start, _, a, b, i in policy._plan if a[:2] == b[:2] and b[2] < a[2])
        grasp_x, grasp_z = policy._waypoints[1][:2]
        late = observation(3, (closing + ns(0.3)) / 1e9, {}, gripper=0.9, ee=(grasp_x, grasp_z))  # planned 0.4
        chunk = policy.step(closing + ns(0.3), late)
        assert chunk is not None
        grip = [g for _, _, g in chunk.actions.tolist()]
        self.assertEqual(grip, sorted(grip, reverse=True), f"the gripper reopens: {grip}")
        self.assertLess(grip[-1], 0.5)

    def test_compares_the_body_with_the_plan_when_it_was_observed(self) -> None:
        # The root cause of the replans above: on a starved VM the sensor stepped
        # up to 30 ms before S1. Compared with the plan at S1's time, a gripper
        # closing at 2/s looked 0.04 behind (REPLAN is 0.03), and S1 replanned at
        # every step. An observation is compared with the plan at its own time.
        policy = ScriptedPickPlace("pick_place.gantry")
        policy.start(IN_RACK, belief(observation(1, 0.0, {"cup_1": TABLE_SPOT})).snapshot(), GANTRY, {})
        self.assertIsNotNone(policy.step(0, observation(2, 0.0, {})))
        plan = list(policy._plan)
        closing = next(start for start, _, a, b, i in policy._plan if a[:2] == b[:2] and b[2] < a[2])
        seen = closing + ns(0.2)
        (x, z, g), _ = policy._at(seen)
        self.assertIsNotNone(policy.step(seen + ns(0.02), observation(3, seen / 1e9, {}, gripper=g, ee=(x, z))))
        self.assertEqual(policy._plan, plan, "S1 replanned although the body was on its plan when observed")


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
