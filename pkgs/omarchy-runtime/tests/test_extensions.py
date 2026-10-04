"""The default extension implementations pass their contracts, plus their own behaviour."""

from __future__ import annotations

import unittest

from omarchy_runtime.contracts import GiveUp, Goal, LatencyModel, ObservationFilter, RecoveryPolicy, Retry, Verifier
from omarchy_runtime.extensions import FILTERS, LATENCY, RECOVERY, VERIFIERS, FixedLatency, GeometricVerifier, IdentityFilter, RetryOnce
from tests.contracts import FilterContract, LatencyContract, RecoveryContract, VerifierContract
from tests.helpers import scenario


class TestIdentityFilter(FilterContract, unittest.TestCase):
    def make(self) -> ObservationFilter:
        return IdentityFilter()


class TestGeometricVerifier(VerifierContract, unittest.TestCase):
    def make(self) -> Verifier:
        return GeometricVerifier()


class TestRetryOnce(RecoveryContract, unittest.TestCase):
    def make(self) -> RecoveryPolicy:
        return RetryOnce()

    def test_retries_once_after_violation_only(self) -> None:
        for summary in self.failures():
            choice = RetryOnce().decide(Goal.from_json(summary["goal"]), summary, summary["belief_final"])
            if summary["category"] == "VERIFICATION_FAILED" and summary["retry_of"] is None:
                self.assertIsInstance(choice, Retry)
                assert isinstance(choice, Retry)
                self.assertEqual(choice.goal, Goal.from_json(summary["goal"]))
            else:
                self.assertIsInstance(choice, GiveUp, summary["category"])

    def test_gives_up_after_a_retry_fails(self) -> None:
        summary = dict(scenario("slip_retry").summaries[0], retry_of="earlier")
        self.assertIsInstance(RetryOnce().decide(Goal.from_json(summary["goal"]), summary, {}), GiveUp)


class TestFixedLatency(LatencyContract, unittest.TestCase):
    def make(self) -> LatencyModel:
        return FixedLatency(1.5)

    def test_value(self) -> None:
        import numpy as np

        self.assertEqual(FixedLatency(1.5).sample(np.random.default_rng(0)), 1_500_000_000)


class TestRegistries(unittest.TestCase):
    """Configuration selects implementations by id; ids match the registry keys."""

    def test_ids_match_keys(self) -> None:
        for registry in (FILTERS, VERIFIERS, RECOVERY):
            for key, make in registry.items():
                self.assertEqual(make().id, key)
        for key, make_latency in LATENCY.items():
            self.assertEqual(make_latency(0.0).id, key)


if __name__ == "__main__":
    unittest.main()
