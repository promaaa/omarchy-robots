"""Deterministic timing faults in simulated time: the conditions a loaded
machine imposes on a wall-clock run (old observations, a stopped process,
late S0 ticks, jittery S1 latency), reproduced from a scenario's seed. Each
regression scenario fails with the S1 behaviour it guards against."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from omarchy_runtime import config
from omarchy_runtime.contracts import ConfigError, Ns, Observation, ns
from omarchy_runtime.scenario import RunResult, run
from omarchy_runtime.sim.s1 import ScriptedPickPlace
from tests.helpers import scenario, tmpdir
from tests.test_trace import trace


class CompareAtNow(ScriptedPickPlace):
    """S1 before 88ca35d: compares an observation with its plan at its own time."""

    def _judged_at(self, observation: Observation, now: Ns) -> Ns:
        return now


class S1v020(CompareAtNow):
    """v0.0.2 part 1: a replan into a grasp moves and closes together."""

    def _reach_spot_first(self, spot: tuple[float, float, float], here: tuple[float, float, float]) -> bool:
        return False


class S1cc47465(CompareAtNow):
    """v0.0.2 part 2's first fix (a982155): a replan into a grasp always steps back, reopening the gripper."""

    def _reach_spot_first(self, spot: tuple[float, float, float], here: tuple[float, float, float]) -> bool:
        return True


def with_s1(policy: type[ScriptedPickPlace], name: str) -> RunResult:
    with mock.patch("omarchy_runtime.scenario.ScriptedPickPlace", policy):
        return run(name, tmpdir() / name)


def outcomes(r: RunResult) -> list[tuple[str, str | None]]:
    return [(s["outcome"], s["category"]) for s in r.summaries]


class TestRegressions(unittest.TestCase):
    def test_old_observations(self) -> None:
        self.assertEqual(outcomes(scenario("timing_observation_age")), [("SUCCEEDED", None)])
        self.assertEqual(outcomes(with_s1(S1cc47465, "timing_observation_age")), [("FAILED", "TIMEOUT")])

    def test_a_pause_during_the_descent(self) -> None:
        r = scenario("timing_pause_grasp")
        self.assertEqual((outcomes(r), r.world.violations), ([("SUCCEEDED", None)], []))
        before = with_s1(S1v020, "timing_pause_grasp")
        self.assertTrue(before.world.violations)
        self.assertIn("pushed", before.world.violations[0][1])  # the held cup, into the table


class TestTimingFaults(unittest.TestCase):
    def events(self, r: RunResult) -> list[dict[str, object]]:
        return [json.loads(line) for line in (r.dir / "events.jsonl").read_text().splitlines()]

    def test_an_s0_stall_is_a_late_tick_without_a_jump(self) -> None:
        r = scenario("timing_s0_stall")
        c = r.summaries[0]["s0"]
        self.assertEqual(c["late_ticks"], 1)
        self.assertGreaterEqual(c["max_interval_ns"], ns(0.3))
        self.assertLessEqual(c["max_step"], 1.0 + 1e-9)
        late = [e for e in self.events(r) if e["type"] == "s0.late"]
        self.assertEqual(len(late), 1)
        payload = late[0]["payload"]
        assert isinstance(payload, dict)
        self.assertGreaterEqual(payload["max_interval_ns"], ns(0.3))

    def test_a_pause_stops_everything_and_the_trace_shows_it(self) -> None:
        r = scenario("timing_pause_grasp")
        rec = r.record()
        gaps = rec["t"][1:][(rec["t"][1:] - rec["t"][:-1]) > ns(0.2)]
        self.assertEqual(len(gaps), 1)  # one gap: the pause
        self.assertTrue(any(e["type"] == "s0.late" for e in self.events(r)))

    def test_noisy_timing_is_deterministic(self) -> None:
        again = run("timing_jitter", tmpdir() / "timing_jitter")
        self.assertEqual(trace(scenario("timing_jitter").dir), trace(again.dir))
        c = scenario("timing_jitter").summaries[0]["s0"]
        self.assertGreater(c["late_ticks"], 100)  # 5 % of the ticks skipped
        self.assertLessEqual(c["max_step"], 1.0 + 1e-9)

    def test_refuses_bad_timing(self) -> None:
        base = (config.SCENARIOS / "timing_jitter.toml").read_text()
        for bad in ("s0_skip_probability = 0.05", "observation_age_s = 0.01"):
            for replacement in (bad.split(" = ")[0] + " = -1", "observation_lag = 1"):
                path = tmpdir() / "bad.toml"
                path.write_text(base.replace(bad, replacement))
                with self.subTest(replacement), self.assertRaises(ConfigError):
                    run(str(path), tmpdir() / "bad")


if __name__ == "__main__":
    unittest.main()
