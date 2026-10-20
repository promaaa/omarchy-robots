"""Two-level verification: the fidelity check and failure attribution. Each
category comes from a scenario that produces it; the rules' order and the
belief's contradictions are checked on their own."""

from __future__ import annotations

import json
import unittest

from omarchy_runtime.attribution import FidelityLimits, attribute, fidelity
from omarchy_runtime.contracts import Category, Detection, Observation, S0Counters, State, ns
from omarchy_runtime.trace import validate_run
from tests.helpers import IN_RACK, belief, scenario

LIMITS = FidelityLimits(0.05, 0.9, 0.8, 0.01)


def blamed(name: str) -> list[str | None]:
    return [s["attribution"] and s["attribution"]["category"] for s in scenario(name).summaries]


class TestEachCategoryFromAScenario(unittest.TestCase):
    def test_intent_the_slip_with_motion_only_intents(self) -> None:
        self.assertEqual(blamed("slip_retry"), ["INTENT", None])  # motion-only intents cannot say "keep the grasp"
        self.assertEqual(blamed("slot_blocked"), ["INTENT", None])

    def test_execution_the_gripper_never_closed(self) -> None:
        (s,) = scenario("gripper_stuck").summaries
        self.assertEqual(s["attribution"]["category"], "EXECUTION")
        self.assertFalse(s["fidelity"]["faithful"])
        self.assertGreater(s["fidelity"]["tracking_error"], 0.05)

    def test_perception_inconclusive_or_contradictory(self) -> None:
        self.assertEqual(blamed("occluded_long"), ["PERCEPTION"])
        self.assertEqual(blamed("ghost_verify"), ["PERCEPTION", None])
        first = scenario("ghost_verify").summaries[0]
        self.assertIn("belief contradictory: cup_1: sim.camera and sim.camera.b", first["attribution"]["reason"])

    def test_infrastructure_the_stall(self) -> None:
        self.assertEqual(blamed("s1_stall_long"), ["INFRASTRUCTURE"])

    def test_success_rejection_and_no_execution_have_none(self) -> None:
        self.assertEqual(blamed("nominal_a"), [None])
        self.assertEqual(blamed("unsupported_clean"), [None])
        (s,) = scenario("unsupported_clean").summaries
        self.assertIsNone(s["fidelity"])

    def test_faithful_executions_report_their_fidelity(self) -> None:
        (s,) = scenario("nominal_a").summaries
        f = s["fidelity"]
        self.assertTrue(f["faithful"])
        self.assertLess(f["tracking_error"], 0.01)
        self.assertEqual((f["contact_mode_share"], f["force_share"], f["object_error"]), (None, None, None))


class TestRules(unittest.TestCase):
    def test_the_first_rule_that_holds(self) -> None:
        faithful = fidelity(S0Counters(motion_ticks=10, motion_error_sum=0.01), LIMITS)
        unfaithful = fidelity(S0Counters(motion_ticks=10, motion_error_sum=1.0), LIMITS)
        failed = (State.FAILED, Category.VERIFICATION_FAILED, "violated")
        late = S0Counters(late_ticks=2)
        cases = [
            (failed, unfaithful, late, [], False, "INFRASTRUCTURE"),  # infrastructure before anything else
            ((State.UNVERIFIED, Category.VERIFICATION_INCONCLUSIVE, "occluded"), unfaithful, S0Counters(), [], False, "PERCEPTION"),
            (failed, unfaithful, S0Counters(), [{"entity": "cup_1", "sources": ["a", "b"], "distance_m": 0.3}], False, "PERCEPTION"),
            (failed, unfaithful, S0Counters(), [], False, "EXECUTION"),
            (failed, faithful, S0Counters(), [], False, "INTENT"),
            (failed, faithful, S0Counters(), [], True, "INFRASTRUCTURE"),  # S2's link lost
        ]
        for (outcome, category, reason), fid, c, contradictions, s2_lost, expected in cases:
            with self.subTest(expected=expected):
                got = attribute(outcome, category, reason, fid, c, contradictions, s2_lost, False)
                assert got is not None
                self.assertEqual(got["category"], expected)
        self.assertIsNone(attribute(State.SUCCEEDED, None, "", faithful, S0Counters(), [], False, False))

    def test_fidelity_shares_and_thresholds(self) -> None:
        c = S0Counters(
            contact_ticks=100, contact_mode_ticks=85, force_ticks=50, force_in_bounds_ticks=45, object_ticks=4, object_error_sum=0.08
        )
        f = fidelity(c, LIMITS)
        self.assertEqual((f["contact_mode_share"], f["force_share"], f["object_error"]), (0.85, 0.9, 0.02))
        self.assertEqual(len(f["failed"]), 2)  # the contact mode and the object
        self.assertFalse(fidelity(S0Counters(guard_replacements=1), LIMITS)["faithful"])
        self.assertFalse(fidelity(S0Counters(degraded_chunks=1), LIMITS)["faithful"])
        self.assertFalse(fidelity(S0Counters(contact_unmeasured_ticks=1), LIMITS)["faithful"])  # no evidence is no fidelity
        self.assertFalse(fidelity(S0Counters(object_unmeasured_ticks=1), LIMITS)["faithful"])


class TestContradictions(unittest.TestCase):
    def test_two_sources_far_apart_at_once_contradict(self) -> None:
        def seen(n: int, t: float, source: str, x: float, confidence: float = 0.95) -> Observation:
            return Observation(f"obs-{n}", ns(t), source, (Detection("cup_1", x, 0.80, confidence),), None)

        b = belief(seen(1, 1.0, "a", 0.30), seen(2, 1.0, "b", 0.31))  # 1 cm apart: agree
        self.assertEqual(b.contradictions, [])
        b.update(seen(3, 1.05, "b", 0.60))
        self.assertEqual([(c["entity"], c["sources"]) for c in b.contradictions], [("cup_1", ["a", "b"])])
        b.update(seen(4, 2.0, "b", 0.90))  # long after a's last estimate: not a contradiction, a change
        b.update(seen(5, 2.0, "a", 0.30, confidence=0.1))  # not confident
        self.assertEqual(len(b.contradictions), 1)
        self.assertEqual(b.evaluate(IN_RACK).truth.value, "UNKNOWN")  # the latest estimate rules, as before: here a weak one


class TestTraced(unittest.TestCase):
    def test_traces_carry_fidelity_and_attribution_and_the_validator_checks_them(self) -> None:
        r = scenario("gripper_stuck")
        self.assertEqual(validate_run(r.dir).errors, [])
        path = r.dir / "executions.jsonl"
        summary = json.loads(path.read_text())
        broken = r.dir.parent / "gripper_stuck-broken"
        broken.mkdir()
        for name in ("run.json", "events.jsonl"):
            (broken / name).write_text((r.dir / name).read_text())
        (broken / "executions.jsonl").write_text(json.dumps(summary | {"attribution": {"category": "BAD LUCK", "reason": "?"}}) + "\n")
        self.assertTrue(any("attribution" in e for e in validate_run(broken).errors))
        (broken / "executions.jsonl").write_text(json.dumps(summary | {"attribution": None}) + "\n")
        self.assertTrue(any("attribution" in e for e in validate_run(broken).errors))


if __name__ == "__main__":
    unittest.main()
