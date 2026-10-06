"""The default extension implementations pass their contracts, plus their own behaviour."""

from __future__ import annotations

import unittest

import numpy as np

from omarchy_runtime.contracts import ConfigError, GiveUp, Goal, LatencyModel, ObservationFilter, RecoveryPolicy, Retry, Verifier, ns
from omarchy_runtime.extensions import (
    FILTERS,
    LATENCY,
    RECOVERY,
    VERIFIERS,
    FixedLatency,
    GeometricVerifier,
    IdentityFilter,
    LognormalLatency,
    RetryOnce,
)
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
        self.assertEqual(FixedLatency(1.5).sample(np.random.default_rng(0)), 1_500_000_000)
        with self.assertRaises(ConfigError):
            FixedLatency(1.0, 0.5)


class TestLognormalLatency(LatencyContract, unittest.TestCase):
    def make(self) -> LatencyModel:
        return LognormalLatency(1.0, 0.5)

    def test_median_and_spread(self) -> None:
        rng = np.random.default_rng(1)
        samples = np.array([LognormalLatency(5.0, 0.5).sample(rng) for _ in range(4000)]) / 1e9
        self.assertAlmostEqual(float(np.median(samples)), 5.0, delta=0.15)
        low, high = np.percentile(samples, [5, 95])
        self.assertAlmostEqual(float(low), 5.0 * 0.44, delta=0.15)
        self.assertAlmostEqual(float(high), 5.0 * 2.28, delta=0.5)
        self.assertEqual(LognormalLatency(0.1, 0.0).sample(rng), ns(0.1))
        with self.assertRaises(ConfigError):
            LognormalLatency(0.0, 0.5)


class TestRegistries(unittest.TestCase):
    """Configuration selects implementations by id; ids match the registry keys."""

    def test_ids_match_keys(self) -> None:
        for registry in (FILTERS, VERIFIERS, RECOVERY):
            for key, make in registry.items():
                self.assertEqual(make().id, key)
        self.assertEqual(LATENCY["fixed"](0.0, 0.0).id, "fixed")
        self.assertEqual(LATENCY["lognormal"](1.0, 0.5).id, "lognormal")


if __name__ == "__main__":
    unittest.main()
