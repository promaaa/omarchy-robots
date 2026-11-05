"""The learning S0's parts without a simulator: the contact model, the habit's program, the arbiter's rule."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from omarchy_runtime.learning import ContactModel, Memory, Program, Supervisor

GOAL = {"force_n": 10.0, "force_min_n": 5.0, "force_max_n": 20.0}


class ContactModelTest(unittest.TestCase):
    def test_learns_the_height_and_spreads_it_to_neighbours(self) -> None:
        m = ContactModel(0.12)
        self.assertIsNone(m.predict(0.6, 0.0))  # no frame yet
        m.frame = (0.6, 0.0, 0.015)
        self.assertIsNone(m.predict(0.6, 0.0))  # no contact yet
        for _ in range(2000):  # 2 s of steady contact, the pad resting 3 mm under the believed top
            m.learn(0.6, 0.0, 0.012, 0.001)
        here, near = m.predict(0.6, 0.0), m.predict(0.62, 0.01)
        assert here is not None and near is not None
        self.assertAlmostEqual(here, 0.012, delta=0.0005)
        self.assertAlmostEqual(near, 0.012, delta=0.002)  # the broad basis carries it 2 cm away
        self.assertIsNone(m.predict(0.6, 0.2))  # off the surface
        m.frame = (0.63, 0.02, 0.025)  # the surface moved and rose 1 cm: the model moves with it
        moved = m.predict(0.63, 0.02)
        assert moved is not None
        self.assertAlmostEqual(moved, 0.022, delta=0.0005)
        back = ContactModel.from_json(m.to_json())
        back.frame = m.frame
        self.assertAlmostEqual(back.predict(0.63, 0.02) or 0.0, moved, delta=1e-5)  # stored to the micrometre


class ProgramTest(unittest.TestCase):
    def test_includes_cells_and_strokes_alternate(self) -> None:
        p = Program.empty(0.12, 0.05, 0.035)
        self.assertEqual(p.strokes(), [])
        self.assertTrue(p.include(-0.05, -0.07, 0.09))
        self.assertTrue(p.include(0.05, -0.07, 0.09))
        self.assertFalse(p.include(0.0, -0.07, 0.09))  # already covered
        self.assertTrue(p.include(0.2, 0.07, 0.09))  # clipped to the limit
        np.testing.assert_allclose(p.strokes(), [(-0.07, -0.05, 0.05), (0.07, 0.09, 0.09)])
        self.assertAlmostEqual(p.predicted(np.array([[0.0, -0.07], [0.0, 0.0], [0.09, 0.07]])), 2 / 3)


class ArbiterRuleTest(unittest.TestCase):
    def test_habit_needs_episodes_for_this_goal_and_its_surface(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mem = Memory(Path(d) / "m.json", {})
            cells = np.array([[x, y] for y in (-0.07, 0.07) for x in (-0.05, 0.05)])
            program = Program.empty(0.12, 0.05, 0.035)
            for _ in range(3):
                program = mem.consolidate(
                    {"driver": "teacher", "goal": GOAL, "top": 0.035, "kr_clean": [1, 1, 1, 0], "kr_coverage": 0.75},
                    program,
                    ContactModel(0.12),
                    cells,
                    0.09,
                )
            self.assertEqual(mem.decide(GOAL, 0.035, program, cells)[0], "habit")
            self.assertEqual(mem.decide(GOAL | {"force_max_n": 8.0}, 0.035, program, cells)[0], "teacher")
            self.assertEqual(mem.decide(GOAL, 0.06, program, cells)[0], "teacher")
            self.assertEqual(
                Memory(Path(d) / "m.json", {"goal_check": False}).decide(GOAL | {"force_max_n": 8.0}, 0.035, program, cells)[0], "habit"
            )
            for _ in range(2):  # two habit runs well under the teacher's mean demote it
                program = mem.consolidate(
                    {"driver": "habit", "goal": GOAL, "top": 0.035, "kr_clean": [1, 1, 0, 0], "kr_coverage": 0.5},
                    program,
                    ContactModel(0.12),
                    cells,
                    0.09,
                )
            self.assertEqual(mem.decide(GOAL, 0.035, program, cells)[0], "teacher")


class SupervisorTest(unittest.TestCase):
    def test_surprise_must_last(self) -> None:
        m = ContactModel(0.12)
        m.frame = (0.6, 0.0, 0.015)
        for _ in range(1000):
            m.learn(0.6, 0.0, 0.012, 0.001)
        s = Supervisor({"hold_s": 0.2}, 20.0)
        self.assertIsNone(s.check(0, (0.6, 0.0, 0.0125), 10.0, m))  # where it expects the surface
        self.assertIsNone(s.check(0, (0.6, 0.0, 0.03), 10.0, m))  # contact 1.8 cm high: not yet
        self.assertIsNone(s.check(100_000_000, (0.6, 0.0, 0.03), 10.0, m))
        self.assertIn("from where the habit expects it", s.check(200_000_000, (0.6, 0.0, 0.03), 10.0, m) or "")
        self.assertIn("over the bound", Supervisor({"hold_s": 0.0}, 20.0).check(0, (0.6, 0.0, 0.0125), 25.0, m) or "")
