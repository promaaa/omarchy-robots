"""Intent contract v2: the channels, each validated alone; a v1 chunk as a
motion-only intent; what an S0 controller declares, what it refuses
(UNSUPPORTED_INTENT) and what it degrades, recorded; fallbacks in the intent
language."""

from __future__ import annotations

import math
import unittest
from collections.abc import Callable, Mapping

import numpy as np

from omarchy_runtime.contracts import (
    CARTESIAN,
    ActionChunk,
    Array,
    ChunkRejected,
    Command,
    ConfigError,
    ContactCapability,
    ContactIntent,
    ContactMode,
    ControllerCapability,
    FallbackSpec,
    IntentChunk,
    MotionIntent,
    Ns,
    ObjectIntent,
    Rejection,
    RobotState,
    S0Mode,
    Timing,
    matrix_quat,
    ns,
    quat_matrix,
    sample,
)
from omarchy_runtime.controllers import MotionTracker
from omarchy_runtime.s0 import S0Loop
from omarchy_runtime.sim.s0 import PlanarRobot, SimS0
from omarchy_runtime.sim.world import World
from tests.helpers import GANTRY, SCENE

MS = ns(0.001)
POSE = [0.5, 0.0, 0.4, 0.0, 1.0, 0.0, 0.0]  # pointing down
FRAME = "base"


def timing(dt_s: float = 0.05, until_s: float = 1.0) -> Timing:
    return Timing(0, ns(dt_s), ns(until_s), "test", 0)


def motion(rows: list[list[float]] | None = None, frame: str | None = FRAME) -> MotionIntent:
    return MotionIntent.make(timing(), "arm.ee_pose.v1", CARTESIAN, rows or [POSE, POSE], frame)


def contact(target: float = 10.0, modes: tuple[str, ...] = ("MAKE", "MAINTAIN"), lower: float = 5.0, upper: float = 20.0) -> ContactIntent:
    return ContactIntent.make(timing(), "pad", FRAME, (0, 0, 1), modes, [[target]] * len(modes), [lower], [upper], [800] * 3 + [50] * 3)


def plate(until_s: float = 1.0) -> ObjectIntent:
    return ObjectIntent.make(timing(until_s=until_s), "plate", FRAME, poses=[[0.5, 0, 0.2, 1, 0, 0, 0]] * 2)


def capability(**over: object) -> ControllerCapability:
    fields: dict[str, object] = {
        "id": "impedance",
        "version": "1",
        "command": "torque",
        "rate_hz": 1000,
        "dt": (MS, ns(1.0)),
        "motion": ("arm.ee_pose.v1",),
        "frames": (FRAME,),
        "object": ("pose",),
        "contact": ContactCapability(("pad",), (FRAME,), 30.0, ((0, 3000),) * 3 + ((0, 300),) * 3),
    }
    return ControllerCapability(**(fields | over))  # type: ignore[arg-type]


class TestChannels(unittest.TestCase):
    def test_each_channel_is_validated_alone_and_named(self) -> None:
        cases: list[tuple[Callable[[], object], Rejection, str]] = [
            (lambda: motion([[*POSE[:3], 1.0, 1.0, 0, 0]] * 2), Rejection.SHAPE, "motion"),  # not a unit quaternion
            (lambda: MotionIntent.make(timing(), "s", ("x", "z"), [[0, 0]], FRAME), Rejection.SHAPE, "motion"),  # not CARTESIAN
            (lambda: motion([[math.nan] * 7]), Rejection.NOT_FINITE, "motion"),
            (lambda: ObjectIntent.make(timing(), "plate", FRAME), Rejection.SHAPE, "object"),  # neither poses nor flow
            (lambda: ObjectIntent.make(timing(), "p", FRAME, points=[[0, 0, 0]], flow=[[[0, 0, 0], [1, 1, 1]]]), Rejection.SHAPE, "object"),
            (lambda: contact(target=30.0), Rejection.BOUNDS, "contact"),  # a MAINTAIN target above its bound
            (lambda: contact(lower=25.0), Rejection.BOUNDS, "contact"),  # bounds the wrong way round
            (lambda: contact(modes=("TOUCH",)), Rejection.SHAPE, "contact"),
            (lambda: ContactIntent.make(timing(), "pad", FRAME, (0, 0, 2), ["FREE"], [[0]], [0], [1], [1] * 6), Rejection.SHAPE, "contact"),
            (lambda: MotionIntent.make(Timing(0, 0, 1, "t", 0), "s", ("q",), [[0]]), Rejection.SHAPE, "motion"),  # dt 0
        ]
        for make, code, channel in cases:
            with self.subTest(code=code, channel=channel), self.assertRaises(ChunkRejected) as e:
                make()
            self.assertEqual((e.exception.code, e.exception.channel), (code, channel))
            self.assertEqual(set(e.exception.to_json()), {"code", "channel", "detail"})
        with self.assertRaises(ChunkRejected):
            IntentChunk.make(1, "test")  # no channel at all
        with self.assertRaises(ChunkRejected):
            IntentChunk.make(1, "test", contact=[contact(), contact()])  # one end-effector twice

    def test_a_chunk_is_valid_while_every_channel_is(self) -> None:
        chunk = IntentChunk.make(1, "test", motion(), plate(until_s=0.4), [contact()])
        self.assertEqual((chunk.valid_until, chunk.channels), (ns(0.4), ("motion", "object", "contact")))
        c = chunk.contact[0]
        self.assertEqual([c.mode(ns(t)) for t in (0.0, 0.049, 0.05, 2.0)], [ContactMode.MAKE] * 2 + [ContactMode.MAINTAIN] * 2)
        rows = np.array([[0.0], [1.0]])
        self.assertEqual([float(sample(rows, timing(), ns(t))[0]) for t in (0.0, 0.025, 0.05, 1.0)], [0.0, 0.5, 1.0, 1.0])

    def test_a_v1_chunk_is_a_motion_only_intent(self) -> None:
        v1 = ActionChunk.make(3, GANTRY.action_space, GANTRY.dims, 10, ns(0.05), [GANTRY.home], ns(0.5), "s1", observed_at=5)
        intent = IntentChunk.from_v1(v1)
        assert intent.motion is not None
        self.assertEqual((intent.seq, intent.channels, intent.motion.frame), (3, ("motion",), None))
        t = intent.motion.timing
        self.assertEqual((t.t_start, t.dt, t.valid_until, t.observed_at, t.source), (10, ns(0.05), ns(0.5), 5, "s1"))
        self.assertIs(intent.motion.rows, v1.actions)

    def test_s0_runs_a_v1_chunk_and_its_intent_alike(self) -> None:
        x, z, g = GANTRY.home
        rows = [[x, z, g], [x + 0.01, z - 0.01, g], [x + 0.02, z - 0.01, 0.0]]
        runs = []
        for as_intent in (False, True):
            s0 = SimS0(GANTRY, World(SCENE, {"cup_1": 0.78}, GANTRY), capacity=1000)
            v1 = ActionChunk.make(1, GANTRY.action_space, GANTRY.dims, 0, ns(0.05), rows, ns(0.2), "s1")
            s0.submit_chunk(IntentChunk.from_v1(v1) if as_intent else v1, 0)
            for n in range(300):
                s0.tick(n * MS)
            runs.append(s0.record_position[:300].copy())
        np.testing.assert_array_equal(runs[0], runs[1])


class TestCapability(unittest.TestCase):
    def test_what_is_not_declared_is_refused(self) -> None:
        chunk = IntentChunk.make(1, "s1", motion(), plate(), [contact()])
        self.assertEqual(capability().admit(chunk), (chunk, ()))
        refused = [
            (capability(contact=None), "contact"),
            (capability(object=()), "object"),
            (capability(frames=("tool",)), "motion"),
            (capability(motion=("other",)), "motion"),
            (capability(dt=(MS, ns(0.01))), "motion"),  # rows 50 ms apart: outside its rates
            (capability(contact=ContactCapability(("pad",), (FRAME,), 15.0, ((0, 3000),) * 3 + ((0, 300),) * 3)), "contact"),
            (capability(contact=ContactCapability(("pad",), (FRAME,), 30.0, ((0, 500),) * 3 + ((0, 300),) * 3)), "contact"),
            (capability(contact=ContactCapability(("tool",), (FRAME,), 30.0, ((0, 3000),) * 3 + ((0, 300),) * 3)), "contact"),
        ]
        for cap, channel in refused:
            with self.subTest(channel=channel), self.assertRaises(ChunkRejected) as e:
                cap.admit(chunk)
            self.assertEqual((e.exception.code, e.exception.channel), (Rejection.UNSUPPORTED_INTENT, channel))

    def test_a_degraded_mode_needs_the_configuration_and_is_recorded(self) -> None:
        chunk = IntentChunk.make(1, "s1", motion(), plate(), [contact(target=20.0, upper=25.0)])
        weak = ContactCapability(("pad",), (FRAME,), 15.0, ((0, 500),) * 3 + ((0, 300),) * 3)
        cap = capability(object=(), contact=weak, degraded=("drop_object", "clamp_force", "clamp_stiffness"))
        admitted, degraded = cap.admit(chunk)
        self.assertEqual([d.split(":")[0] for d in degraded], ["drop_object", "clamp_force", "clamp_stiffness"])
        self.assertIsNone(admitted.object)
        c = admitted.contact[0]
        self.assertEqual((float(c.target.max()), c.upper, c.stiffness[:3]), (15.0, (15.0,), (500.0,) * 3))
        dropped, why = capability(contact=None, degraded=("drop_contact",)).admit(chunk)
        self.assertEqual((dropped.contact, why), ((), ("drop_contact: pad",)))
        with self.assertRaises(ChunkRejected):  # nothing left to execute
            capability(contact=None, degraded=("drop_contact",)).admit(IntentChunk.make(1, "s1", contact=[contact()]))
        with self.assertRaises(ConfigError):
            capability(degraded=("ignore_everything",))


class TestS0Admission(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(SCENE, {"cup_1": 0.78}, GANTRY)

    def contact_chunk(self, seq: int) -> IntentChunk:
        rows = [list(GANTRY.home)] * 2
        m = MotionIntent.make(timing(), GANTRY.action_space, GANTRY.dims, rows)
        return IntentChunk.make(seq, "s1", m, contact=[contact()])

    def test_the_motion_tracker_refuses_contact_and_says_so(self) -> None:
        s0 = SimS0(GANTRY, self.world, capacity=100)
        with self.assertRaises(ChunkRejected) as e:
            s0.submit_chunk(self.contact_chunk(1), 0)
        self.assertEqual((e.exception.code, e.exception.channel), (Rejection.UNSUPPORTED_INTENT, "contact"))
        status = s0.status()
        self.assertEqual(status.counters.rejected_chunks, 1)
        assert status.rejection is not None
        self.assertEqual(
            (status.rejection["code"], status.rejection["channel"], status.rejection["seq"]), ("UNSUPPORTED_INTENT", "contact", 1)
        )

    def test_allowed_to_drop_contact_it_runs_the_motion_and_records_it(self) -> None:
        cap = ControllerCapability(
            "motion_tracker", "1", "position", 1000, (MS, ns(10)), (GANTRY.action_space,), degraded=("drop_contact",)
        )
        s0 = SimS0(GANTRY, self.world, capacity=100, capability=cap)
        s0.submit_chunk(self.contact_chunk(1), 0)
        s0.tick(0)
        status = s0.status()
        assert status.last_chunk is not None
        self.assertEqual((status.mode, status.last_chunk.channels, status.counters.degraded_chunks), (S0Mode.TRACKING, ("motion",), 1))
        self.assertEqual(status.last_chunk.degraded, ("drop_contact: pad",))


class TestSceneInputs(unittest.TestCase):
    def test_the_controller_gets_the_streams_it_declares_and_no_others(self) -> None:
        class Robot(PlanarRobot):
            def scene(self, now: Ns) -> Mapping[str, Array]:
                return {"pose:cup_1": np.array([now / 1e9, 0, 0.8, 1, 0, 0, 0]), "pose:cup_2": np.zeros(7)}

        seen: list[set[str]] = []

        class Watcher(MotionTracker):
            def command(self, intent: IntentChunk, state: RobotState, scene: Mapping[str, Array], now: Ns) -> Command:
                seen.append(set(scene))
                return super().command(intent, state, scene, now)

        cap = ControllerCapability("watcher", "1", "position", 1000, (MS, ns(10)), (GANTRY.action_space,), scene=("pose:cup_1",))
        s0 = S0Loop(GANTRY, Watcher(cap), Robot(World(SCENE, {"cup_1": 0.78}, GANTRY)), capacity=10)
        m = MotionIntent.make(timing(), GANTRY.action_space, GANTRY.dims, [list(GANTRY.home)])
        s0.submit_chunk(IntentChunk.make(1, "s1", m), 0)
        s0.tick(0)
        self.assertEqual(seen, [{"pose:cup_1"}])


class TestRotations(unittest.TestCase):
    def test_a_rotation_matrix_and_its_quaternion_agree_half_turns_included(self) -> None:
        rng = np.random.default_rng(1)
        for k in range(500):
            q = rng.normal(size=4)
            q[0] *= 1e-9 if k % 3 == 0 else 1.0  # a third of them near a half turn
            q /= np.linalg.norm(q)
            back = matrix_quat(quat_matrix(q))
            self.assertGreaterEqual(back[0], 0.0)
            np.testing.assert_allclose(quat_matrix(back), quat_matrix(q), atol=1e-9)


class TestFallbackIntent(unittest.TestCase):
    def test_a_joint_fallback_starts_where_s0_is(self) -> None:
        spec = FallbackSpec.make("s", ("a", "b"), ns(0.2), [[0.0, 5.0]], 0, ns(9), "fb", ns(1.0), [True, False])
        intent = spec.engage(ns(2), np.array([1.0, 2.0]))
        assert intent.motion is not None
        np.testing.assert_array_equal(intent.motion.rows, [[1.0, 2.0], [1.0, 5.0]])
        self.assertEqual((intent.motion.timing.t_start, intent.valid_until), (ns(2), ns(3)))

    def test_a_cartesian_fallback_composes_orientations_and_carries_contact(self) -> None:
        up = ContactIntent.make(timing(), "pad", FRAME, (0, 0, 1), ["BREAK", "FREE"], [[0.0], [0.0]], [0.0], [30.0], [200] * 3 + [20] * 3)
        spec = FallbackSpec.make(
            "arm.ee_pose.v1", CARTESIAN, ns(0.5), [[0, 0, 0.02, 1, 0, 0, 0]], 0, ns(9), "lift", relative=[True] * 7, frame=FRAME, contact=up
        )
        intent = spec.engage(ns(1), np.array(POSE))
        assert intent.motion is not None
        np.testing.assert_allclose(intent.motion.rows[-1], [0.5, 0.0, 0.42, *POSE[3:]])
        self.assertEqual(intent.contact[0].timing.t_start, ns(1))
        again = FallbackSpec.from_json(spec.to_json())
        self.assertTrue(again.same_motion(spec))
        self.assertEqual(again.to_json(), spec.to_json())


if __name__ == "__main__":
    unittest.main()
