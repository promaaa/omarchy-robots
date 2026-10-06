"""Scenes that change while the robot works: a local reaction (cup_moved),
a recovery that needs S2 under its fallback (slot_blocked), the fallback
expiring before S2 answers (fallback_expiry), and the inference matrix's
safety in every cell."""

from __future__ import annotations

import json
import unittest
from typing import Any

from omarchy_runtime import config
from omarchy_runtime.evaluate import inference_markdown, inference_matrix, scenario_result
from omarchy_runtime.extensions import CORRECTORS, INFERENCE_TRIGGERS
from omarchy_runtime.scenario import RunResult, run
from omarchy_runtime.trace import validate_run
from tests.helpers import IN_RACK, scenario, tmpdir


def result(name: str) -> dict[str, Any]:
    r = scenario(name)
    return scenario_result(r, validate_run(r.dir))


def events(r: RunResult) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (r.dir / "events.jsonl").read_text().splitlines()]


class TestCupMoved(unittest.TestCase):
    def test_s1_reacts_locally_without_s2(self) -> None:
        res = result("cup_moved")
        self.assertEqual(res["problems"], [])
        d = res["dynamics"]
        self.assertEqual([c["kind"] for c in d["scene_changes"]], ["move"])
        # The next observation (50 ms), then S1's inference latency (50 ms): no more.
        self.assertLessEqual(d["reaction_s"], 0.1 + 1e-9)
        self.assertGreater(d["reaction_s"], 0)
        self.assertEqual(len(scenario("cup_moved").summaries), 1)  # no second goal: S2 was not needed
        self.assertEqual([e for e in events(scenario("cup_moved")) if e["type"] == "decision"], [])


class TestSlotBlocked(unittest.TestCase):
    def test_s0_waits_under_the_fallback_and_s2_chooses_another_spot(self) -> None:
        r, res = scenario("slot_blocked"), result("slot_blocked")
        self.assertEqual(res["problems"], [])
        failed, retry = r.summaries
        self.assertTrue(failed["claim"]["reason"].startswith("blocked: dish_1"))
        self.assertEqual(retry["retry_of"], failed["execution_id"])
        self.assertAlmostEqual(retry["conditioning"]["place_x"], 0.36)  # the next spot, chosen by S2
        holds = [e["payload"] for e in events(r) if e["type"] == "s0.hold"]
        self.assertTrue(holds[0]["fallback"])  # the failure engaged S2's fallback
        (rec,) = res["dynamics"]["recovery"]
        self.assertTrue(rec["fallback_engaged"])
        self.assertFalse(rec["fallback_expired"])
        self.assertLessEqual(rec["recovery_s"], 0.25)  # S2 decides in 0.1 s, plus steps
        self.assertEqual(r.world.violations, [])
        self.assertTrue(r.world.truth(IN_RACK))


class TestNeighbourOffItsSpot(unittest.TestCase):
    def test_a_cup_released_off_its_spot_leaves_the_next_spot_free(self) -> None:
        # Found by vm-s2-placement under TCG: a wall-clock release left cup_1 millimetres
        # off its spot, both spots then looked taken, and S1 gave up on cup_2 ("blocked").
        path = tmpdir() / "neighbour.toml"
        moved = '[[faults]]\nkind = "move"\nentity = "cup_1"\nafter = "release"\ndelay_s = 0.5\nx = 0.012\n'
        path.write_text((config.SCENARIOS / "s2_placement.toml").read_text() + "\n" + moved)
        r = run(str(path), tmpdir() / "neighbour")
        self.assertEqual([s["outcome"] for s in r.summaries], ["SUCCEEDED", "SUCCEEDED"])
        self.assertEqual([e[1] for e in r.world.events if e[1] == "move"], ["move"])
        self.assertAlmostEqual(r.world.cups["cup_2"][0], 0.36, places=2)  # the next spot, not cup_1's
        self.assertEqual(r.world.violations, [])


class TestFallbackExpiry(unittest.TestCase):
    def test_the_fallback_expires_before_s2_answers_and_s0_holds(self) -> None:
        r, res = scenario("fallback_expiry"), result("fallback_expiry")
        self.assertEqual(res["problems"], [])
        (rec,) = res["dynamics"]["recovery"]
        self.assertTrue(rec["fallback_engaged"])
        self.assertTrue(rec["fallback_expired"])
        self.assertAlmostEqual(rec["fallback_s"], 1.5, places=2)  # its 1.5 s, then HOLD
        self.assertGreaterEqual(rec["recovery_s"], 2.0)  # S2 takes 2 s
        modes = [(e["payload"]["from"], e["payload"]["to"]) for e in events(r) if e["type"] == "s0.mode"]
        self.assertIn(("FALLBACK", "HOLD"), modes)  # the trace shows the expiry
        self.assertEqual(r.world.held, None)
        self.assertEqual(r.world.violations, [])
        self.assertGreaterEqual(r.s0.c["fallback_expiries"], 1)


class TestInferenceMatrix(unittest.TestCase):
    def test_safety_in_every_cell(self) -> None:
        results = inference_matrix(tmpdir())
        self.assertEqual(len(results["cells"]), 2 * 3 * len(CORRECTORS) * len(INFERENCE_TRIGGERS))
        for c in results["cells"]:
            with self.subTest(f"{c['scenario']} {c['inference_s']} {c['corrector']} {c['trigger']}"):
                self.assertEqual(c["violations"], 0)
                self.assertEqual([p for p in c["problems"] if p.startswith("S0")], [])
        moved = [c for c in results["cells"] if c["scenario"] == "cup_moved" and c["trigger"] == "periodic" and c["corrector"] == "none"]
        for c, delay in zip(moved, (0.05, 0.15, 0.3), strict=True):
            # The next observation (every 50 ms), then S1's inference delay.
            self.assertGreater(c["reaction_s"], delay)
            self.assertLessEqual(c["reaction_s"], delay + 0.05 + 1e-9)
        self.assertIn("| cup_moved | 0.05 | none | periodic | SUCCEEDED | 0 |", inference_markdown(results))


if __name__ == "__main__":
    unittest.main()
