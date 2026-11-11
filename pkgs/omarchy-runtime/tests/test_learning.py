"""The learning S0's parts without a simulator: the contact model, the habit's program, the arbiter's rule."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from omarchy_runtime.learning import ContactModel, Memory, ModelBank, PhaseILC, Program, Supervisor, train_student

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


def wipe(bank: ModelBank, rest: float, seconds: float) -> None:
    """Steady contact along a raster over the plate (frame at 0.6, 0.0), the pad resting at height `rest`."""
    ticks = int(seconds * 1000)
    for i in range(ticks):
        u = -0.08 + 0.16 * ((i // 50) % 40) / 39  # 40 positions along x, 50 ms each
        v = -0.08 + 0.16 * (i // 2000 % 5) / 4  # 5 lines in y, 2 s each
        bank.learn(0.6 + u, v, rest, 0.001)


class ModelBankTest(unittest.TestCase):
    def test_a_new_context_gets_a_module_and_a_known_one_is_recognized(self) -> None:
        bank = ModelBank(0.12)
        bank.frame = (0.6, 0.0, 0.035)  # task 1, dish A: the pad rests 3 mm under the believed top
        wipe(bank, 0.032, 10.0)
        self.assertEqual((bank.births, len(bank.modules), bank.map_module()), (1, 1, 0))
        bank.close_task()
        bank.frame = (0.6, 0.0, 0.035)  # task 2, dish B: its surface 1.5 cm lower, the belief the same
        wipe(bank, 0.017, 10.0)
        self.assertEqual((bank.births, len(bank.modules), bank.map_module()), (1, 2, 1))  # one birth, not one per spot
        bank.close_task()
        bank = ModelBank.from_json(bank.to_json())  # stored between tasks
        bank.frame = (0.6, 0.0, 0.035)  # task 3, dish A again: recognized, no birth
        wipe(bank, 0.032, 1.0)
        self.assertEqual((bank.births, len(bank.modules), bank.map_module()), (0, 2, 0))
        here = bank.predict(0.6, 0.0) or 0.0  # one pass of the broad basis overshoots a little: closer to A than to B
        self.assertLess(abs(here - 0.032), abs(here - 0.017) / 3)
        bank.close_task()
        bank.frame = (0.6, 0.0, 0.035)  # task 4, dish B again: the prior favours A, the contact says B
        wipe(bank, 0.017, 1.0)
        self.assertEqual((bank.births, bank.map_module()), (0, 1))
        here = bank.predict(0.6, 0.0) or 0.0
        self.assertLess(abs(here - 0.017), abs(here - 0.032))

    def test_supervisor_sees_novelty_not_a_known_context(self) -> None:
        bank = ModelBank(0.12)
        bank.frame = (0.6, 0.0, 0.035)
        wipe(bank, 0.032, 10.0)
        bank.close_task()
        bank.frame = (0.6, 0.0, 0.035)
        s = Supervisor({}, upper=20.0, wanted=10.0)
        self.assertIsNone(s.check(0, (0.6, 0.0, 0.032), 10.0, bank))
        wipe(bank, 0.017, 0.2)  # a dish no module explains
        self.assertIsNone(s.check(int(0.1e9), (0.6, 0.0, 0.017), 10.0, bank))  # the surprise must last 0.2 s
        self.assertIn("new context", s.check(int(0.35e9), (0.6, 0.0, 0.017), 10.0, bank) or "")


class PhaseILCTest(unittest.TestCase):
    def test_remembers_when_not_where(self) -> None:
        ilc = PhaseILC(0.12)
        ilc.frame = (0.6, 0.0, 0.035)
        for i in range(2000):  # a task: in contact from 0.5 s to 1.5 s, the pad resting 3 mm under the believed top
            ilc.tick(i * 1_000_000)
            if 500 <= i < 1500:
                ilc.learn(0.6, 0.0, 0.032, 0.001)
        ilc.close_task()
        ilc = PhaseILC.from_json(ilc.to_json())
        ilc.frame = (0.62, 0.01, 0.035)  # the next task, the plate moved: the same time, anywhere on it
        for i in range(1001):
            ilc.tick(i * 1_000_000)
        self.assertAlmostEqual(ilc.predict(0.5, -0.1) or 0.0, 0.035 + 0.99 * -0.003, delta=1e-6)
        ilc.frame = (0.6, 0.0, 0.035)
        ilc.tick(0)
        ilc.tick(200_000_000)
        self.assertIsNone(ilc.predict(0.6, 0.0))  # at 0.2 s it has nothing: the last task touched only from 0.5 s

    def test_a_gain_below_one_moves_by_a_part_of_the_error(self) -> None:
        ilc = PhaseILC(0.12, gain=0.5)
        for want in (0.5, 0.75):  # two periods: half the error, then half of what is left (times Q)
            ilc.frame = (0.6, 0.0, 0.035)
            ilc.tick(0)
            ilc.learn(0.6, 0.0, 0.031, 0.001)
            ilc.close_task()
            ilc = PhaseILC.from_json(ilc.to_json())
            self.assertAlmostEqual(float(ilc.F[0]), -0.004 * want, delta=1e-4)


class StudentTest(unittest.TestCase):
    def test_the_distilled_network_follows_a_taught_stroke_from_a_disturbed_start(self) -> None:
        stroke = [[0.05 * k, -0.08 + 0.004 * k, 0.02, -0.003, int(k >= 5)] for k in range(41)]  # down, then 16 cm along x
        net, span = train_student([stroke, stroke], seed=1)
        self.assertAlmostEqual(span, 2.0)
        u, c, phase = np.array([-0.07, 0.025, -0.003]), 1.0, 0.25  # 1 cm off the stroke, a quarter of the way
        for _ in range(30):
            out = net(np.array([*u, phase, c]))
            u, c, phase = u + out[:3], float(out[3]), phase + 0.025
        self.assertAlmostEqual(float(u[0]), 0.08, delta=0.01)  # it went along and back onto the stroke
        self.assertAlmostEqual(float(u[1]), 0.02, delta=0.005)
        self.assertGreater(c, 0.5)  # still pressing


class ProgramTest(unittest.TestCase):
    def test_insists_on_a_covered_cell_and_merges(self) -> None:
        p = Program.empty(0.12, 0.05, 0.035)
        for x in (-0.06, 0.06):
            p.include(x, 0.0, 0.09)
        self.assertTrue(p.insist(0.0, 0.0, 0.09))  # a covered cell stayed dirty: scrub around it, on its line
        self.assertFalse(p.insist(0.0, 0.0, 0.09))  # that scrub is there, and it passes right over the cell
        self.assertTrue(p.insist(0.05, 0.0, 0.09))  # overlapping: the scrub grows
        self.assertEqual(len(p.scrubs), 1)
        y, a, b = p.scrubs[0]
        self.assertAlmostEqual(a, -0.045)
        self.assertAlmostEqual(b, 0.09)
        self.assertEqual(p.strokes()[-2:], [(y, a, b), (y, b, a)])  # there and back, after the raster
        self.assertFalse(p.insist(0.0, 0.01, 0.09))  # a cell beside the line, inside that scrub: nothing to add yet
        self.assertTrue(p.insist(0.0, 0.01, 0.09, closer=True))  # still dirty after a run with that scrub: right over the cell
        self.assertEqual(p.scrubs[1], [0.01, -0.045, 0.045])
        self.assertFalse(p.insist(0.0, 0.01, 0.09, closer=True))  # both scrubs are there
        self.assertEqual(Program.from_json(p.to_json()).scrubs, p.scrubs)

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
        s = Supervisor({"hold_s": 0.2}, 20.0, 10.0)
        self.assertIsNone(s.check(0, (0.6, 0.0, 0.0125), 10.0, m))  # where it expects the surface
        self.assertIsNone(s.check(0, (0.6, 0.0, 0.03), 10.0, m))  # contact 1.8 cm high: not yet
        self.assertIsNone(s.check(100_000_000, (0.6, 0.0, 0.03), 10.0, m))
        self.assertIn("from where the habit expects it", s.check(200_000_000, (0.6, 0.0, 0.03), 10.0, m) or "")
        self.assertIn("over the bound", Supervisor({"hold_s": 0.0}, 20.0, 10.0).check(0, (0.6, 0.0, 0.0125), 25.0, m) or "")
        soft = Supervisor({"hold_s": 0.0}, 20.0, 10.0)  # a foam sponge at first touch: 2 N, the pad still above where it will rest
        self.assertIsNone(soft.check(0, (0.6, 0.0, 0.03), 2.0, m))


class RoundDishTest(unittest.TestCase):
    def test_on_a_round_dish_the_strokes_stay_within_its_circle(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cells = np.array([[x, y] for y in (-0.09, -0.03, 0.03, 0.09) for x in (-0.09, -0.03, 0.03, 0.09)])
            run = {"driver": "teacher", "goal": GOAL, "top": 0.035, "kr_clean": [1] * len(cells), "kr_coverage": 1.0}
            program = Memory(Path(d) / "m.json", {}).consolidate(
                run, Program.empty(0.135, 0.05, 0.035), ContactModel(0.135), cells, 0.085, round=True
            )
            ends = [(x, y) for y, e in zip(program.lines, program.extents, strict=True) if e is not None for x in e]
            self.assertTrue(ends)
            self.assertLessEqual(max(float(np.hypot(x, y)) for x, y in ends), 0.09)  # the scripted wiper's circle, and 5 mm
            square = Memory(Path(d) / "s.json", {}).consolidate(run, Program.empty(0.135, 0.05, 0.035), ContactModel(0.135), cells, 0.085)
            self.assertGreater(max(float(np.hypot(x, y)) for y, e in zip(square.lines, square.extents, strict=True) if e for x in e), 0.085)


class VpiRuleTest(unittest.TestCase):
    def test_a_dearer_s1_hands_over_sooner(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cells = np.array([[x, y] for y in (-0.07, 0.07) for x in (-0.05, 0.05)])
            first = {}
            for cost in (0.01, 0.03, 0.1):
                mem = Memory(Path(d) / f"m{cost}.json", {"rule": "vpi", "cost": cost})
                program = Program.empty(0.12, 0.05, 0.035)
                for n in range(1, 13):
                    run = {"driver": "teacher", "goal": GOAL, "top": 0.035, "kr_clean": [1, 1, 1, 1], "kr_coverage": 0.9 + 0.02 * (n % 3)}
                    program = mem.consolidate(run, program, ContactModel(0.12), cells, 0.09)
                    if mem.decide(GOAL, 0.035, program, cells)[0] == "habit":
                        first[cost] = n
                        break
            self.assertLess(first[0.1], first[0.03])
            self.assertLess(first[0.03], first.get(0.01, 99))
