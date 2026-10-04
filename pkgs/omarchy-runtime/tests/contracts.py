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
import unittest
from typing import Any

import numpy as np

from omarchy_runtime.contracts import (
    GiveUp,
    Goal,
    LatencyModel,
    ObservationFilter,
    RecoveryPolicy,
    Retry,
    VerdictKind,
    Verifier,
    ns,
)
from tests.helpers import IN_RACK, RACK_SPOT, TABLE_SPOT, belief, observation, scenario


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
