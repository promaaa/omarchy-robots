"""Contract tests for the collaborator extension points.

A new implementation passes the same tests by mixing the matching class into
a TestCase and saying how to make one:

    class TestMyVerifier(VerifierContract, unittest.TestCase):
        def make(self) -> Verifier:
            return MyVerifier()

The default implementations run them in test_extensions.py.
"""

from __future__ import annotations

import json
import time
import unittest
from typing import Any

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.contracts import (
    ActionChunk,
    BodySpec,
    ChunkCorrector,
    ChunkMeta,
    FallbackGenerator,
    FallbackSpec,
    GiveUp,
    Goal,
    InferenceContext,
    InferenceTrigger,
    LatencyModel,
    ObservationFilter,
    RecoveryPolicy,
    Retry,
    S0Counters,
    S0History,
    S0Mode,
    S0Status,
    VerdictKind,
    Verifier,
    ns,
)
from tests.helpers import GANTRY, IN_RACK, RACK_SPOT, TABLE_SPOT, belief, observation, scenario


def _named(test: unittest.TestCase, extension: Any) -> None:
    test.assertTrue(isinstance(extension.id, str) and extension.id, "id must be a non-empty string")
    test.assertTrue(isinstance(extension.version, str) and extension.version, "version must be a non-empty string")


class FilterContract:
    """Applied to every observation before the belief and the trace see it."""

    def make(self) -> ObservationFilter:
        raise NotImplementedError

    def observations(self) -> list[Any]:
        return [
            observation(1, 0.05, {"cup_1": TABLE_SPOT, "cup_2": (0.92, 0.80)}),
            observation(2, 0.10, {"cup_1": TABLE_SPOT}),
            observation(3, 0.15, {"cup_2": (0.92, 0.80), "cup_1": RACK_SPOT}, gripper=0.0),
        ]

    def test_named(self: Any) -> None:
        _named(self, self.make())

    def test_keeps_provenance_and_ids_and_reports_changes(self: Any) -> None:
        f, mapping = self.make(), dict[str, str]()
        for obs in self.observations():
            out, changes = f.apply(obs)
            self.assertIsInstance(changes, tuple)
            self.assertTrue(all(isinstance(c, str) and c for c in changes))
            self.assertEqual((out.id, out.t, out.source), (obs.id, obs.t, obs.source), "provenance must survive the filter")
            if out != obs:
                self.assertTrue(changes, "the filter changed the observation without saying what")
            else:
                self.assertEqual(changes, ())
            if len(out.detections) == len(obs.detections):
                for before, after in zip(obs.detections, out.detections, strict=True):
                    self.assertEqual(mapping.setdefault(before.entity, after.entity), after.entity, "entity ids must stay stable")
            else:
                self.assertTrue(changes, "detections were dropped without saying so")


class VerifierContract:
    """Judges in(cup_1, drying_rack) from fresh observations only."""

    def make(self) -> Verifier:
        raise NotImplementedError

    def test_named(self: Any) -> None:
        _named(self, self.make())

    def test_satisfied_from_fresh_evidence(self: Any) -> None:
        b = belief(observation(1, 1.0, {"cup_1": TABLE_SPOT}), observation(2, 2.0, {"cup_1": RACK_SPOT}))
        v = self.make().verify(IN_RACK, b, ns(1.5))
        self.assertIs(v.kind, VerdictKind.SATISFIED, v.reason)
        self.assertIn("obs-2", v.evidence)
        self.assertNotIn("obs-1", v.evidence)

    def test_violated_from_fresh_evidence(self: Any) -> None:
        v = self.make().verify(IN_RACK, belief(observation(1, 2.0, {"cup_1": TABLE_SPOT})), ns(1.5))
        self.assertIs(v.kind, VerdictKind.VIOLATED, v.reason)
        self.assertTrue(v.evidence)

    def test_never_decides_from_stale_or_missing_evidence(self: Any) -> None:
        stale = belief(observation(1, 1.0, {"cup_1": RACK_SPOT}), observation(2, 2.0, {}))
        self.assertIs(self.make().verify(IN_RACK, stale, ns(1.5)).kind, VerdictKind.INCONCLUSIVE)
        self.assertIs(self.make().verify(IN_RACK, belief(), 0).kind, VerdictKind.INCONCLUSIVE)

    def test_a_held_cup_is_not_in_the_rack(self: Any) -> None:
        held = belief(observation(1, 2.0, {"cup_1": RACK_SPOT}, gripper=0.0, ee=RACK_SPOT))
        self.assertIsNot(self.make().verify(IN_RACK, held, ns(1.5)).kind, VerdictKind.SATISFIED)


class RecoveryContract:
    """Decides after a real failed execution: retry (a JSON-serializable goal
    and conditioning) or give up, with a reason, the same way every time."""

    def make(self) -> RecoveryPolicy:
        raise NotImplementedError

    def failures(self) -> list[dict[str, Any]]:
        names = ["slip_retry", "occluded_long", "s1_stall_long", "unsupported_clean"]
        return [s for name in names for s in scenario(name).summaries if s["outcome"] != "SUCCEEDED"]

    def test_named(self: Any) -> None:
        _named(self, self.make())

    def test_decides_deterministically_with_a_reason(self: Any) -> None:
        for summary in self.failures():
            goal = Goal.from_json(summary["goal"])
            first = self.make().decide(goal, summary, summary["belief_final"])
            again = self.make().decide(goal, summary, summary["belief_final"])
            self.assertIsInstance(first, Retry | GiveUp)
            self.assertEqual(first, again)
            self.assertTrue(first.reason)
            if isinstance(first, Retry):
                json.dumps(first.goal.to_json())
                json.dumps(dict(first.conditioning))

    def test_never_retries_a_rejected_goal(self: Any) -> None:
        for summary in self.failures():
            if summary["outcome"] == "REJECTED":
                choice = self.make().decide(Goal.from_json(summary["goal"]), summary, summary["belief_final"])
                self.assertIsInstance(choice, GiveUp)


class LatencyContract:
    """A non-negative delay in ns, reproducible from the random generator's seed."""

    def make(self) -> LatencyModel:
        raise NotImplementedError

    def test_named(self: Any) -> None:
        _named(self, self.make())

    def test_non_negative_and_reproducible(self: Any) -> None:
        a = [self.make().sample(np.random.default_rng(7)) for _ in range(5)]
        b = [self.make().sample(np.random.default_rng(7)) for _ in range(5)]
        self.assertEqual(a, b)
        self.assertTrue(all(isinstance(d, int) and d >= 0 for d in a))


MS = 1_000_000


def s0_state(t: int, position: tuple[float, ...], chunk: ChunkMeta | None, mode: S0Mode = S0Mode.TRACKING) -> S0Status:
    return S0Status(t, mode, chunk.seq if chunk else None, position, chunk, False, S0Counters())


def s0_history(t0: int, t1: int, command: list[float], desired: list[float]) -> S0History:
    t = np.arange(t0, t1 + 1, MS, dtype=np.int64)
    return S0History(t, np.tile(command, (len(t), 1)), np.tile(desired, (len(t), 1)))


def gantry_chunk(seq: int, t_start: int, rows: list[list[float]], observed_at: int) -> ActionChunk:
    return ActionChunk.make(
        seq, GANTRY.action_space, GANTRY.dims, t_start, 50 * MS, rows, t_start + 450 * MS, "s1", observed_at=observed_at
    )


def _fast(test: unittest.TestCase, call: Any) -> None:
    start = time.perf_counter()
    for _ in range(100):
        call()
    test.assertLess((time.perf_counter() - start) / 100, 0.0005, "slower than its 0.5 ms budget")


class CorrectorContract:
    """Applied when a chunk is handed to S0: returns a chunk of the same
    identity (seq, source, action space, observation time), finite and
    valid now, within its time budget, without changing its inputs."""

    def make(self) -> ChunkCorrector:
        raise NotImplementedError

    def case(self) -> tuple[ActionChunk, S0History, S0Status, int]:
        here = list(GANTRY.home)
        rows = [[here[0] + 0.01 * k, here[1], here[2]] for k in range(10)]
        chunk = gantry_chunk(7, 1000 * MS, rows, observed_at=990 * MS)
        now = 1200 * MS  # the chunk is 200 ms old when it reaches S0
        meta = ChunkMeta(6, "s1", 900 * MS, 50 * MS, 10, 1350 * MS, 950 * MS)
        return chunk, s0_history(990 * MS, now, here, here), s0_state(now, tuple(here), meta), now

    def test_named(self: Any) -> None:
        _named(self, self.make())

    def test_keeps_identity_and_is_valid_now(self: Any) -> None:
        chunk, history, state, now = self.case()
        before = chunk.actions.copy()
        out = self.make().correct(chunk, history, state, now)
        self.assertIsInstance(out, ActionChunk)
        same = ("seq", "source", "action_space", "dims", "observed_at")
        self.assertEqual([getattr(out, k) for k in same], [getattr(chunk, k) for k in same])
        self.assertTrue(np.all(np.isfinite(out.actions)))
        self.assertGreaterEqual(out.valid_until, now)
        self.assertTrue(np.array_equal(chunk.actions, before), "the corrector changed its input")

    def test_within_its_time_budget(self: Any) -> None:
        corrector, (chunk, history, state, now) = self.make(), self.case()
        _fast(self, lambda: corrector.correct(chunk, history, state, now))


class TriggerContract:
    """Asked at S1's rate: answers the same way for the same context, within
    its time budget, and asks for an inference whenever S0 has no chunk
    and none is on its way."""

    def make(self) -> InferenceTrigger:
        raise NotImplementedError

    def context(self, mode: S0Mode = S0Mode.TRACKING, in_flight: int = 0) -> InferenceContext:
        here = tuple(GANTRY.home)
        meta = ChunkMeta(6, "s1", 900 * MS, 50 * MS, 10, 1350 * MS, 950 * MS) if mode is S0Mode.TRACKING else None
        h = s0_history(500 * MS, 1000 * MS, list(here), list(here))
        return InferenceContext(1000 * MS, s0_state(1000 * MS, here, meta, mode), h, in_flight, 50 * MS, 50 * MS)

    def test_named(self: Any) -> None:
        _named(self, self.make())

    def test_infers_when_s0_has_nothing_coming(self: Any) -> None:
        self.assertTrue(self.make().due(self.context(S0Mode.HOLD)))

    def test_deterministic_and_within_its_time_budget(self: Any) -> None:
        trigger, context = self.make(), self.context()
        self.assertEqual(trigger.due(context), trigger.due(context))
        self.assertIsInstance(trigger.due(context), bool)
        _fast(self, lambda: trigger.due(context))


class FallbackGeneratorContract:
    """A capability's default fallback: one for the body, valid over the
    window it is given and lasting all of it, that never moves the gripper
    (a relative zero: a held object stays held, a closing gripper is not
    reopened), the same for the same status, within its time budget."""

    def make(self) -> FallbackGenerator:
        raise NotImplementedError

    def cases(self) -> list[tuple[BodySpec, S0Status]]:
        out = []
        for body in (GANTRY, config.body(config.effective("nominal_a"))):
            g = body.dims.index("gripper")
            for grip in (body.lower[g], body.upper[g], (body.lower[g] + body.upper[g]) / 2):
                here = tuple(grip if i == g else v for i, v in enumerate(body.home))
                out.append((body, s0_state(1000 * MS, here, None, S0Mode.HOLD)))
        return out

    def test_named(self: Any) -> None:
        _named(self, self.make())

    def test_for_the_body_over_its_window_never_moving_the_gripper(self: Any) -> None:
        for body, status in self.cases():
            with self.subTest(body=body.id, gripper=status.position[body.dims.index("gripper")]):
                spec = self.make().fallback(body, status, 1000 * MS, 61_000 * MS)
                self.assertIsInstance(spec, FallbackSpec)
                self.assertEqual((spec.action_space, spec.dims), (body.action_space, body.dims))
                self.assertEqual((spec.valid_from, spec.valid_until), (1000 * MS, 61_000 * MS))
                self.assertGreaterEqual(spec.lasts, spec.valid_until - spec.valid_from)
                g = body.dims.index("gripper")
                self.assertTrue(spec.relative[g] and bool(np.all(spec.actions[:, g] == 0.0)), "the fallback moves the gripper")

    def test_deterministic_and_within_its_time_budget(self: Any) -> None:
        generator = self.make()
        for body, status in self.cases():
            a, b = generator.fallback(body, status, 0, 60_000 * MS), generator.fallback(body, status, 0, 60_000 * MS)
            self.assertTrue(a.same_motion(b))
        body, status = self.cases()[0]
        _fast(self, lambda: generator.fallback(body, status, 0, 60_000 * MS))
