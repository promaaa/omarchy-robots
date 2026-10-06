"""The scenario matrix: every scenario runs once, and properties P1 to P7 and
the cross-scenario invariants are checked against ground truth, S0's per-tick
record and the trace."""

from __future__ import annotations

import json
import math
import unittest
from typing import Any

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.contracts import SECOND
from omarchy_runtime.evaluate import scenario_result
from omarchy_runtime.scenario import RunResult, run
from omarchy_runtime.trace import S0_MODES, validate_run
from tests.helpers import scenario, tmpdir

TRACKING, FALLBACK, HOLD = (S0_MODES.index(m) for m in ("TRACKING", "FALLBACK", "HOLD"))


def events(r: RunResult) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (r.dir / "events.jsonl").read_text().splitlines()]


def outcomes(r: RunResult) -> list[tuple[Any, ...]]:
    return [(s["outcome"], s["category"], s["claim"] and s["claim"]["claim"], s["verdict"] and s["verdict"]["kind"]) for s in r.summaries]


def blocks(modes: np.ndarray) -> list[tuple[int, int, int]]:
    """(mode, first tick, last tick) for each run of equal modes."""
    edges = np.flatnonzero(np.diff(modes)) + 1
    starts, ends = np.r_[0, edges], np.r_[edges, len(modes)] - 1
    return [(int(modes[s]), int(s), int(e)) for s, e in zip(starts, ends, strict=True)]


class TestEveryScenario(unittest.TestCase):
    def test_as_expected_with_complete_traces_and_no_violation(self) -> None:
        for name in config.scenario_names():
            with self.subTest(name):
                r = scenario(name)
                result = scenario_result(r, validate_run(r.dir))
                self.assertEqual(result["problems"], [])
                self.assertTrue(result["trace_complete"])
                self.assertEqual(r.world.violations, [])

    def test_s0_always_has_a_chunk_a_fallback_or_hold(self) -> None:
        for name in config.scenario_names():
            with self.subTest(name):
                r = scenario(name)
                rec = r.record()
                self.assertEqual(len(rec["t"]), r.s0.c["ticks"])
                if r.s0_timing_faults:  # skipped ticks are the point: bounded steps instead
                    self.assertTrue(np.all(np.diff(rec["t"]) > 0))
                    self.assertTrue(np.all(np.abs(np.diff(rec["position"], axis=0)) <= r.s0.bound * (1 + 1e-9) + 1e-12))
                else:
                    self.assertTrue(np.all(np.diff(rec["t"]) == SECOND // 1000), "a tick is missing")
                self.assertTrue(set(np.unique(rec["mode"]).tolist()) <= {TRACKING, FALLBACK, HOLD})
                # TRACKING only on a valid chunk, and never past its validity.
                tracking = rec["mode"] == TRACKING
                self.assertTrue(np.all(rec["t"][tracking] <= rec["valid_until"][tracking]))

    def test_s0_leaves_tracking_no_later_than_the_first_tick_after_validity(self) -> None:
        # Earlier is allowed: when an execution stops running, the runtime tells S0 to hold.
        for name in config.scenario_names():
            with self.subTest(name):
                rec = scenario(name).record()
                for mode, first, _ in blocks(rec["mode"]):
                    if mode != TRACKING and first > 0 and rec["mode"][first - 1] == TRACKING:
                        self.assertLessEqual(rec["t"][first], rec["valid_until"][first - 1] + SECOND // 1000)

    def test_no_s0_rate_event_in_the_event_stream(self) -> None:
        for name in config.scenario_names():
            with self.subTest(name):
                stream = events(scenario(name))
                samples = [e["t"] for e in stream if e["type"] == "s0.status"]
                self.assertTrue(np.all(np.diff(samples) >= SECOND // 20), "S0 status sampled faster than the supervisor")
                for e in stream:
                    if e["type"] == "chunk":
                        self.assertEqual(
                            set(e["payload"]),
                            {"seq", "source", "t_start", "dt", "horizon", "valid_until", "received", "observed_at", "inference_ns"},
                        )


class TestProperties(unittest.TestCase):
    def test_p1_verified_success(self) -> None:
        r = scenario("nominal_a")
        self.assertEqual(outcomes(r), [("SUCCEEDED", None, "DONE", "SATISFIED")])
        self.assertEqual(r.truths, [True])

    def test_p2_silent_failure_caught_and_recovered(self) -> None:
        r = scenario("slip_retry")
        failed, retry = r.summaries
        self.assertEqual(outcomes(r), [("FAILED", "VERIFICATION_FAILED", "DONE", "VIOLATED"), ("SUCCEEDED", None, "DONE", "SATISFIED")])
        self.assertEqual(r.truths, [False, True])
        slip = next(t for t, kind, _ in r.world.events if kind == "slip")
        self.assertLess(slip, failed["timing"]["claimed"])  # S1 claimed DONE after the cup was gone
        # The belief at the end of the failed execution has the cup where it fell.
        estimate = failed["belief_final"]["entities"]["cup_1"]["estimate"]
        fell = next(d for t, kind, d in r.world.events if kind == "slip")
        x, z = (float(v) for v in fell.split("(")[1].rstrip(")").split(", "))
        self.assertLess(math.hypot(estimate["x"] - x, estimate["z"] - z), 0.02)
        self.assertEqual((retry["task_id"], retry["retry_of"]), (failed["task_id"], failed["execution_id"]))
        decisions = [e["payload"] for e in events(r) if e["type"] == "decision"]
        self.assertEqual([d["action"] for d in decisions], ["retry"])
        self.assertEqual(decisions[0]["policy"], "retry_once@1")

    def test_p3_honest_inconclusiveness(self) -> None:
        r = scenario("occluded_long")
        self.assertEqual(outcomes(r), [("UNVERIFIED", "VERIFICATION_INCONCLUSIVE", "DONE", "INCONCLUSIVE")])
        self.assertEqual(r.truths, [True])  # the cup is in the rack; nothing could show it
        result = scenario_result(r, validate_run(r.dir))
        self.assertEqual(result["executions"][0]["agreement"], "inconclusive (truth true)")
        decisions = [e["payload"]["action"] for e in events(r) if e["type"] == "decision"]
        self.assertEqual(decisions, ["give_up"])
        short = scenario("occluded_short")
        self.assertEqual(outcomes(short), [("SUCCEEDED", None, "DONE", "SATISFIED")])
        attempts = [e["payload"]["verdict"]["kind"] for e in events(short) if e["type"] == "verify.attempt"]
        self.assertEqual(attempts[0], "INCONCLUSIVE")
        self.assertEqual(attempts[-1], "SATISFIED")
        verdict = short.summaries[0]["verdict"]
        self.assertLess(short.summaries[0]["timing"]["ended"], verdict["not_before"] + 2 * SECOND)

    def stall_window(self, r: RunResult) -> tuple[int, int]:
        grasp = r.world.first["grasp"]
        return grasp + int(1.6 * SECOND), grasp + int(1.6 * SECOND)

    def test_p4_short_stall_holds_the_cup_then_resumes(self) -> None:
        r = scenario("s1_stall_short")
        self.assertEqual(outcomes(r), [("SUCCEEDED", None, "DONE", "SATISFIED")])
        start, _ = self.stall_window(r)
        rec = r.record(start, start + SECOND)
        held = [(m, a, b) for m, a, b in blocks(rec["mode"]) if m == HOLD]
        self.assertEqual(len(held), 1)
        self.assertLessEqual(rec["t"][held[0][1]] - start, SECOND // 2)  # the last chunk ran out within its horizon
        # S0 held at exactly the first tick after the last chunk's validity.
        self.assertEqual(rec["t"][held[0][1]], rec["valid_until"][held[0][1] - 1] + SECOND // 1000)
        self.assertTrue(np.all(rec["position"][held[0][1] : held[0][2] + 1, -1] == 0.0), "gripper opened while holding")
        kinds = [kind for _, kind, _ in r.world.events]
        self.assertEqual(kinds, ["grasp", "release"])  # nothing slipped while S0 held
        self.assertEqual(r.summaries[0]["s0"]["starvation_events"], 1)

    def test_p4_long_stall_fails_and_leaves_s0_holding_the_cup(self) -> None:
        r = scenario("s1_stall_long")
        self.assertEqual(outcomes(r), [("FAILED", "S1_STALL", None, None)])
        self.assertEqual(r.world.held, "cup_1")
        self.assertEqual(r.s0.mode.value, "HOLD")
        s = r.summaries[0]
        start, _ = self.stall_window(r)
        self.assertLess(s["timing"]["ended"] - start, int(3.6 * SECOND))  # timeout 3 s, plus the last chunk's horizon

    def test_fallback_runs_then_hold_then_resumption(self) -> None:
        r = scenario("fallback_installed")
        self.assertEqual(outcomes(r), [("SUCCEEDED", None, "DONE", "SATISFIED")])
        start, _ = self.stall_window(r)
        rec = r.record(start, start + 3 * SECOND)
        sequence = [m for m, _, _ in blocks(rec["mode"])]
        self.assertEqual(sequence[:4], [TRACKING, FALLBACK, HOLD, TRACKING])
        fb = [(a, b) for m, a, b in blocks(rec["mode"]) if m == FALLBACK][0]
        self.assertAlmostEqual((rec["t"][fb[1]] - rec["t"][fb[0]]) / SECOND, 1.5, places=2)
        safe = np.array(r.world.body.safe_pose)
        self.assertTrue(np.allclose(rec["position"][fb[1]], safe))
        self.assertEqual(len(r.summaries[0]["fallbacks"]), 1)
        self.assertEqual([kind for _, kind, _ in r.world.events], ["grasp", "release"])

    def test_p5_one_scenario_two_bodies(self) -> None:
        a, b = config.effective("nominal_a"), config.effective("nominal_b")
        differ = {k for k in a["scenario"] if a["scenario"][k] != b["scenario"].get(k)}
        self.assertEqual(differ, {"name", "description", "body"})
        self.assertNotEqual(a["body"]["action_space"], b["body"]["action_space"])
        self.assertNotEqual(len(a["body"]["dims"]), len(b["body"]["dims"]))
        for name, implementation in (("nominal_a", "pick_place.arm3"), ("nominal_b", "pick_place.gantry")):
            r = scenario(name)
            self.assertEqual(outcomes(r), [("SUCCEEDED", None, "DONE", "SATISFIED")])
            self.assertEqual(r.summaries[0]["implementation"], implementation)

    def test_p6_s2_latency_changes_time_not_outcomes_or_safety(self) -> None:
        runs = [scenario(n) for n in ("s2_latency_0p1", "s2_latency_1", "s2_latency_5")]
        results = [scenario_result(r, validate_run(r.dir)) for r in runs]
        self.assertEqual({tuple(outcomes(r)) for r in runs}, {tuple(outcomes(runs[0]))})
        self.assertEqual([res["violations"] for res in results], [0, 0, 0])
        times = [res["task_time_s"] for res in results]
        idle = [res["idle_fraction"] for res in results]
        self.assertEqual(times, sorted(times))
        self.assertEqual(idle, sorted(idle))
        self.assertGreater(times[2] - times[0], 2 * 4.9)  # two decisions, each 4.9 s slower

    def test_a_retry_right_after_a_failure_starts_clean(self) -> None:
        # No settling and no S2 latency: x02 starts while x01's last chunk would still be valid.
        path = tmpdir() / "fast_retry.toml"
        text = (config.SCENARIOS / "slip_retry.toml").read_text().replace("latency_s = 0.1", "latency_s = 0.0")
        path.write_text(text + "\n[verification]\nsettle_s = 0.0\n")
        r = run(str(path), tmpdir() / "fast_retry")
        self.assertEqual([s["outcome"] for s in r.summaries], ["FAILED", "SUCCEEDED"])
        retry = r.summaries[1]
        self.assertEqual(retry["s0"]["rejected_chunks"], 0)
        self.assertGreater(retry["timing"]["time_to_first_chunk_ns"], 0)
        self.assertLessEqual(retry["timing"]["time_to_first_chunk_ns"], SECOND // 10)  # S1 latency plus one step
        holds = [e for e in events(r) if e["type"] == "s0.hold"]
        self.assertEqual([e["execution"] for e in holds], [s["execution_id"] for s in r.summaries])

    def test_p7_explicit_rejection_with_reasons(self) -> None:
        r = scenario("unsupported_clean")
        self.assertEqual(outcomes(r), [("REJECTED", "NO_CAPABILITY", None, None)])
        s = r.summaries[0]
        self.assertEqual(
            s["candidates"],
            [{"capability": "place_in", "implementation": None, "version": None, "accepted": False, "reason": "achieves in, not clean"}],
        )
        resolution = next(e for e in events(r) if e["type"] == "resolution")
        self.assertEqual(resolution["payload"]["candidates"], s["candidates"])
        self.assertEqual(r.s0.c["limit_clamps"], 0)
        self.assertTrue(np.all(r.record()["mode"] == HOLD))  # nothing moved


if __name__ == "__main__":
    unittest.main()
