"""The default extension implementations pass their contracts, plus their own behaviour."""

from __future__ import annotations

import dataclasses
import unittest

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.contracts import (
    ChunkCorrector,
    ConfigError,
    FallbackGenerator,
    GiveUp,
    Goal,
    InferenceTrigger,
    LatencyModel,
    ObservationFilter,
    RecoveryPolicy,
    Retry,
    S0Mode,
    Verifier,
    ns,
)
from omarchy_runtime.extensions import (
    CORRECTORS,
    FILTERS,
    INFERENCE_TRIGGERS,
    LATENCY,
    RECOVERY,
    VERIFIERS,
    Blend,
    FixedLatency,
    GeometricVerifier,
    Hold,
    IdentityFilter,
    LiftHeld,
    LognormalLatency,
    Mismatch,
    NoCorrection,
    Periodic,
    RetryOnce,
)
from tests.contracts import (
    CorrectorContract,
    FallbackGeneratorContract,
    FilterContract,
    LatencyContract,
    RecoveryContract,
    TriggerContract,
    VerifierContract,
    gantry_chunk,
    s0_history,
    s0_state,
)
from tests.helpers import GANTRY, scenario


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


class TestNoCorrection(CorrectorContract, unittest.TestCase):
    def make(self) -> ChunkCorrector:
        return NoCorrection()


class TestBlend(CorrectorContract, unittest.TestCase):
    def make(self) -> ChunkCorrector:
        return Blend(ns(0.1))

    def test_starts_where_s0_is_and_joins_the_chunk_after_its_window(self) -> None:
        here = list(GANTRY.home)
        rows = [[here[0] + 0.05, here[1], here[2]]] * 10  # 5 cm away: a jump without blending
        chunk = gantry_chunk(7, ns(1.0), rows, observed_at=ns(0.99))
        state = s0_state(ns(1.2), tuple(here), None)
        out = Blend(ns(0.1)).correct(chunk, s0_history(ns(0.99), ns(1.2), here, here), state, ns(1.2))
        self.assertEqual(out.t_start, ns(1.2))
        self.assertTrue(np.allclose(out.actions[0], here))  # no jump at the switch
        self.assertTrue(np.allclose(out.actions[2:], rows[0]))  # after 0.1 s: the chunk
        self.assertEqual(out.valid_until, chunk.valid_until)


class TestPeriodic(TriggerContract, unittest.TestCase):
    def make(self) -> InferenceTrigger:
        return Periodic()


class TestMismatch(TriggerContract, unittest.TestCase):
    def make(self) -> InferenceTrigger:
        return Mismatch(0.02)

    def test_infers_on_a_deviation_or_an_ending_chunk_never_while_one_is_coming(self) -> None:
        c = self.context()
        self.assertFalse(Mismatch(0.02).due(c))  # on its plan, the chunk lasts
        off = dataclasses.replace(c, history=s0_history(ns(0.5), ns(1.0), [0.6, 1.1, 1.0], [0.63, 1.1, 1.0]))
        self.assertTrue(Mismatch(0.02).due(off))  # 3 cm from what it tracks
        ending = dataclasses.replace(c, now=ns(1.3))  # the chunk ends at 1.35, a new one needs 0.05 + 0.05
        self.assertTrue(Mismatch(0.02).due(ending))
        self.assertFalse(Mismatch(0.02).due(dataclasses.replace(off, in_flight=1)))


class TestRegistries(unittest.TestCase):
    """Configuration selects implementations by id; ids match the registry keys."""

    def test_ids_match_keys(self) -> None:
        for registry in (FILTERS, VERIFIERS, RECOVERY):
            for key, make in registry.items():
                self.assertEqual(make().id, key)
        self.assertEqual(LATENCY["fixed"](0.0, 0.0).id, "fixed")
        params = {"blend_s": 0.1, "mismatch": 0.02}
        for key, make_corrector in CORRECTORS.items():
            self.assertEqual(make_corrector(params).id, key)
        for key, make_trigger in INFERENCE_TRIGGERS.items():
            self.assertEqual(make_trigger(params).id, key)
        self.assertEqual(LATENCY["lognormal"](1.0, 0.5).id, "lognormal")


class TestHold(FallbackGeneratorContract, unittest.TestCase):
    def make(self) -> FallbackGenerator:
        return Hold()


class TestLiftHeld(FallbackGeneratorContract, unittest.TestCase):
    def make(self) -> FallbackGenerator:
        return LiftHeld()

    def test_lifts_a_held_object_on_a_body_with_z_and_holds_otherwise(self) -> None:
        arm = config.body(config.effective("nominal_a"))
        z = GANTRY.dims.index("z")
        for body, gripper, lift in ((GANTRY, 0.0, 0.02), (GANTRY, 1.0, 0.0), (arm, 0.0, None), (arm, 1.0, None)):
            here = (*body.home[:-1], gripper)
            spec = LiftHeld().fallback(body, s0_state(0, here, None, S0Mode.HOLD), 0, ns(60))
            with self.subTest(body=body.id, gripper=gripper):
                expected = np.zeros(len(body.dims))
                if lift is not None:
                    expected[z] = lift
                self.assertTrue(np.array_equal(spec.actions[0], expected))  # relative: from where S0 is when it engages


if __name__ == "__main__":
    unittest.main()
