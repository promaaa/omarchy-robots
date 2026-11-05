"""An S0 that keeps learning: the robot side of skill automatization.

Each part is a computable model taken from neuroscience. The mapping is a
hypothesis of the paper, not a claim that the robot works like a brain.

- ContactModel, the cerebellum-like part: a forward model of where the pad
  rests in contact over the surface, in the surface's frame. S0's force loop finds
  the surface (feedback); the model learns from that loop's correction and
  becomes S0's feedforward set-point, so the correction the loop must make
  shrinks with practice (feedback-error learning, Kawato et al. 1987). Its
  prediction error is the surprise the supervisor watches.
- Program, the habit: strokes in the surface's frame. Between episodes
  (offline, "sleep"), Memory consolidates it from the episodes it stored:
  the cells the teacher (S1) cleaned, and the cells a practice run of the
  habit left dirty (knowledge of results). Complementary learning systems
  (McClelland et al. 1995): episodes stored at once, a skill built slowly.
- Arbiter, an S1 policy that holds the teacher and the habit. At the start
  of a task it gives control to the habit when the evidence says the habit
  does at least as well as the teacher, for the goal and in the context it
  learned (arbitration by reliability, Daw et al. 2005). While the habit
  drives, its supervisor gives control back to the teacher on surprise
  (supervisory attentional system, Norman and Shallice). S0's guard stays
  under both.

Plain numpy: no simulator, no model server. The teacher is any S1 policy for
clean(surface) that takes Cartesian intents (sim/wipe.py, sim/vla.py).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime.contracts import (
    BodySpec,
    ConfigError,
    ContactMode,
    Goal,
    IntentChunk,
    Json,
    Ns,
    Observation,
    Report,
    S1Policy,
)
from omarchy_runtime.sim.wipe import ScriptedWiper

CONTACT_N = 1.0  # N: below this, the pad is not touching


class ContactModel:
    """The cerebellum-like forward model: how high the pad rests in contact
    over a surface, as a height above the surface's believed top, anywhere
    on the square of half side `half` in the surface's frame. Built as the
    adaptive-filter account of the cerebellum (Marr 1969, Albus 1971, Dean
    et al. 2010): the position is expanded on `cells` x `cells` broad
    Gaussian basis functions (width `WIDTH` m, as granule cells), read out by
    weights (as Purkinje cells), and the weights learn by the normalized
    least-mean-squares rule from the error between the measured and the
    predicted height (as the climbing-fibre signal), with a time constant of
    `TAU` seconds of steady contact. The broad basis spreads what is learned
    at one point to its neighbours. A point predicts once it has `MIN_S`
    seconds of contact evidence near it. `frame` (centre x, y and top z, from
    the belief) is set at the start of each task: the model moves with the
    surface."""

    MIN_S = 0.3
    TAU = 0.5
    WIDTH = 0.04

    def __init__(self, half: float, cells: int = 7) -> None:
        self.half = float(half)
        self.n = int(cells)
        g = np.linspace(-self.half, self.half, self.n)
        self.centres = np.array([(x, y) for y in g for x in g])
        self.w = np.zeros(len(self.centres))  # m above the believed top, per basis function
        self.evidence = np.zeros(len(self.centres))  # seconds of steady contact, spread over the basis
        self.frame: tuple[float, float, float] | None = None
        self.learned_s = 0.0  # seconds of steady contact learned in this task

    def _basis(self, x: float, y: float) -> np.ndarray | None:
        if self.frame is None:
            return None
        u, v = x - self.frame[0], y - self.frame[1]
        if abs(u) > self.half or abs(v) > self.half:
            return None
        phi = np.exp(-((self.centres[:, 0] - u) ** 2 + (self.centres[:, 1] - v) ** 2) / (2 * self.WIDTH**2))
        out: np.ndarray = phi / phi.sum()
        return out

    def predict(self, x: float, y: float) -> float | None:
        """The pad's height in contact at (x, y), in the base frame, or None."""
        phi = self._basis(x, y)
        if phi is None or self.frame is None or float(phi @ self.evidence) / float(phi @ phi) < self.MIN_S:
            return None
        return self.frame[2] + float(phi @ self.w)

    def learn(self, x: float, y: float, z: float, dt: float) -> None:
        """`dt` seconds of steady contact with the pad at height z over (x, y)."""
        phi = self._basis(x, y)
        if phi is None or self.frame is None or dt <= 0:
            return
        error = z - self.frame[2] - float(phi @ self.w)
        self.w += min(dt / self.TAU, 1.0) * error * phi / float(phi @ phi)
        self.evidence += dt * phi
        self.learned_s += dt

    def to_json(self) -> Json:
        return {"half": self.half, "cells": self.n, "w": self.w.round(6).tolist(), "evidence": self.evidence.round(4).tolist()}

    @staticmethod
    def from_json(j: Json) -> ContactModel:
        m = ContactModel(float(j["half"]), int(j["cells"]))
        m.w, m.evidence = np.array(j["w"], dtype=np.float64), np.array(j["evidence"], dtype=np.float64)
        return m


class Program:
    """The habit: strokes along x at fixed lines across the surface (y, in its
    frame), each with the extent [x0, x1] it wipes, or nothing. A cell counts
    as covered when it lies within `REACH` of a line in y and within `REACH`
    of its extent in x: the pad's footprint, turned 45 degrees, reaches about
    that far (sim/panda.py: a 4.4 x 9 cm sponge)."""

    REACH = 0.045

    def __init__(self, lines: list[float], extents: list[list[float] | None], version: int = 0) -> None:
        self.lines = lines
        self.extents = extents
        self.version = version

    @staticmethod
    def empty(half: float, margin: float, spacing: float) -> Program:
        reach = half - margin
        n = max(2, math.ceil(2 * reach / spacing) + 1)
        return Program([float(v) for v in np.linspace(-reach, reach, n)], [None] * n)

    def nearest(self, y: float) -> int:
        return int(np.argmin([abs(y - v) for v in self.lines]))

    def covers(self, x: float, y: float) -> bool:
        for v, e in zip(self.lines, self.extents, strict=True):
            if e is not None and abs(y - v) <= self.REACH and e[0] - self.REACH <= x <= e[1] + self.REACH:
                return True
        return False

    def include(self, x: float, y: float, limit: float) -> bool:
        """Extend the nearest line to reach (x, y), its extent within [-limit, limit]: True if it changed."""
        if self.covers(x, y):
            return False
        k = self.nearest(y)
        target = float(np.clip(x, -limit, limit))
        e = self.extents[k]
        self.extents[k] = [target, target] if e is None else [min(e[0], target), max(e[1], target)]
        return True

    def strokes(self) -> list[tuple[float, float, float]]:
        """(y, x_from, x_to) in order, alternating direction: a raster over the lines in use."""
        out = []
        for n, (y, e) in enumerate((v, e) for v, e in zip(self.lines, self.extents, strict=True) if e is not None):
            a, b = (e[0], e[1]) if n % 2 == 0 else (e[1], e[0])
            out.append((y, a, b))
        return out

    def predicted(self, cells: np.ndarray) -> float:
        """The share of cells (n x 2, the surface's frame) the strokes cover."""
        return float(np.mean([self.covers(float(x), float(y)) for x, y in cells])) if len(cells) else 0.0

    def to_json(self) -> Json:
        return {"version": self.version, "lines": self.lines, "extents": self.extents}

    @staticmethod
    def from_json(j: Json) -> Program:
        return Program(
            [float(v) for v in j["lines"]], [None if e is None else [float(e[0]), float(e[1])] for e in j["extents"]], int(j["version"])
        )


class Memory:
    """What the robot keeps between tasks, in one JSON file: the episodes
    (hippocampus-like: stored at once, as they happened), the habit's
    versions, the contact model, and the counts the arbiter decides from.

    The rule (`decide`): the habit takes the task when it has at least
    `n_min` teacher episodes for this goal behind it, its strokes cover at least the share
    of cells the teacher cleaned on average, the goal is the one it learned
    (target force and bounds), the believed surface top is within `top_m` of
    the tops it learned on, and it is not demoted. Two habit runs in a row
    below the teacher's mean by more than `delta` demote it for `rest`
    tasks, which the teacher does (and teaches again). With `habit` false the
    teacher always drives, and only the contact model learns."""

    def __init__(self, path: Path, cfg: Json) -> None:
        self.path = path
        self.n_min = int(cfg.get("n_min", 3))
        self.delta = float(cfg.get("delta", 0.1))
        self.top_m = float(cfg.get("top_m", 0.01))
        self.rest = int(cfg.get("rest", 2))
        self.habit_on = bool(cfg.get("habit", True))
        self.goal_check = bool(cfg.get("goal_check", True))  # off: the habit runs whatever the goal (a habit slip)
        self.data: Json = json.loads(path.read_text()) if path.exists() else {"episodes": [], "programs": [], "model": None, "demoted": 0}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1) + "\n")
        tmp.replace(self.path)

    def model(self, half: float) -> ContactModel:
        m = self.data.get("model")
        return ContactModel.from_json(m) if m else ContactModel(half)

    def program(self, half: float, margin: float, spacing: float) -> Program:
        p = self.data["programs"]
        return Program.from_json(p[-1]) if p else Program.empty(half, margin, spacing)

    def teacher_runs(self) -> list[Json]:
        return [e for e in self.data["episodes"] if e["driver"] == "teacher"]

    def learned_goal(self) -> Json:
        """The goal the habit learned (target force and bounds): it replays them, whatever the task asks."""
        taught = self.teacher_runs()
        return dict(taught[-1]["goal"]) if taught else {}

    def decide(self, goal: Json, top: float, program: Program, cells: np.ndarray) -> tuple[str, str]:
        """("habit" or "teacher", the reason in words)."""
        if not self.habit_on:
            return "teacher", "habit disabled"
        taught = [e for e in self.teacher_runs() if e["goal"] == goal or not self.goal_check]
        if len(taught) < self.n_min:
            return "teacher", f"{len(taught)} teacher episodes for this goal {goal}, {self.n_min} needed"
        if self.data["demoted"] > 0:
            return "teacher", f"habit demoted for {self.data['demoted']} more tasks"
        tops = [e["top"] for e in taught]
        if min(abs(top - t) for t in tops) > self.top_m:
            return "teacher", f"surface top {top:.3f} m, learned on {min(tops):.3f} to {max(tops):.3f} m"
        mean = float(np.mean([e["kr_coverage"] for e in taught]))
        covered = program.predicted(cells)
        if covered < mean:
            return "teacher", f"habit covers {covered:.2f}, the teacher cleans {mean:.2f}"
        return "habit", f"habit covers {covered:.2f} (version {program.version}), the teacher cleans {mean:.2f} over {len(taught)} episodes"

    def consolidate(self, episode: Json, program: Program, model: ContactModel, cells: np.ndarray, limit: float) -> Program:
        """After a task ("sleep"): store the episode, and build the habit's next
        version from what it saw: the cells it observed clean after the
        teacher's run, the cells it observed dirty after its own run."""
        self.data["episodes"].append(episode)
        self.data["model"] = model.to_json()
        kr = np.array(episode["kr_clean"], dtype=bool)
        changed = False
        if episode["driver"] == "teacher":
            for (x, y), clean in zip(cells, kr, strict=True):
                if clean:
                    changed |= program.include(float(x), float(y), limit)
            if self.data["demoted"] > 0:
                self.data["demoted"] -= 1
        elif episode["driver"] == "habit":
            for (x, y), clean in zip(cells, kr, strict=True):
                if not clean:
                    changed |= program.include(float(x), float(y), limit)
            taught = self.teacher_runs()
            mean = float(np.mean([e["kr_coverage"] for e in taught])) if taught else 0.0
            recent = [e for e in self.data["episodes"] if e["driver"] != "teacher"][-2:]
            if len(recent) == 2 and all(e["driver"] == "habit" and e["kr_coverage"] < mean - self.delta for e in recent):
                self.data["demoted"] = self.rest
        if changed or not self.data["programs"]:
            program = Program(program.lines, program.extents, program.version + 1)
            self.data["programs"].append(program.to_json() | {"after_episode": len(self.data["episodes"])})
        self.save()
        return program


class HabitWiper(ScriptedWiper):
    """The habit as S0 runs it: the scripted wiper's machinery (sim/wipe.py),
    following the program's strokes in the surface's frame instead of a
    fixed raster. It needs no camera and no model server."""

    def __init__(self, implementation: str, params: Json, program: Program) -> None:
        super().__init__(implementation, params)
        self.program = program

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        from omarchy_runtime.sim import wipe

        (surface,) = goal.args
        entity = belief["entities"][surface]
        if entity["estimate"] is None or belief.get("robot") is None:
            raise ValueError(f"no estimate of {surface}, or no proprioception, in the belief")
        cart = body.kinematics.get("cartesian")
        if cart is None:
            raise ConfigError(f"body {body.id} takes no Cartesian intents (kinematics.cartesian)")
        self._space, self._frame, self._ee = str(cart["action_space"]), str(cart["frame"]), str(cart["ee"])
        self._orientation = np.array(cart["orientation"], dtype=np.float64)
        e, g = entity["estimate"], entity["geometry"]
        top = e["z"] + g["height"] / 2
        low = top - self.press
        strokes = self.program.strokes()
        if not strokes:
            raise ValueError("the habit has no stroke")
        here = np.array(belief["robot"]["ee"], dtype=np.float64)
        y0, a0, _ = strokes[0]
        points: list[tuple[np.ndarray, ContactMode, float]] = [
            (np.array([e["x"] + a0, e["y"] + y0, top + wipe.ABOVE]), ContactMode.FREE, self.speeds["transfer"]),
            (np.array([e["x"] + a0, e["y"] + y0, low]), ContactMode.MAKE, self.speeds["descend"]),
        ]
        for n, (y, a, b) in enumerate(strokes):
            if n:
                points.append((np.array([e["x"] + a, e["y"] + y, low]), ContactMode.MAINTAIN, self.speeds["wipe"]))
            points.append((np.array([e["x"] + b, e["y"] + y, low]), ContactMode.MAINTAIN, self.speeds["wipe"]))
        last = points[-1][0]
        points.append((np.array([last[0], last[1], top + wipe.LIFT]), ContactMode.BREAK, self.speeds["descend"]))
        t, p = 0.0, here
        for q, mode, speed in points:
            duration = max(float(np.linalg.norm(q - p)) / speed, 0.2)
            self._plan.append((t, p, q, mode))
            t, p = t + duration, q
        self._end, self._last = t, p
        self._plan.append((t, p, p, ContactMode.FREE))
        self.horizon = math.ceil(t / (wipe.DT / 1e9)) + 2  # each chunk holds the whole program: S0 has it, whatever S1 does


class Supervisor:
    """Watches the habit at S1's rate from what the robot measures (the force
    estimate S0 reads, the pad's position) against the contact model's
    prediction. Surprise, any of: the pad in contact more than `surprise_m`
    from where the model says it rests, for `hold_s`; the pad more than
    `surprise_m` below that height without contact, for `hold_s` (the
    surface is not there); the force over the contact's upper bound."""

    def __init__(self, cfg: Json, upper: float) -> None:
        self.surprise_m = float(cfg.get("surprise_m", 0.01))
        self.hold_s = float(cfg.get("hold_s", 0.2))
        self.upper = upper
        self._since: Ns | None = None
        self.last: str | None = None

    def check(self, now: Ns, ee: tuple[float, ...], force: float, model: ContactModel) -> str | None:
        rest = model.predict(ee[0], ee[1])
        what = None
        if force > self.upper:
            what = f"force {force:.1f} N over the bound {self.upper:.0f} N"
        elif rest is not None and force >= CONTACT_N and abs(ee[2] - rest) > self.surprise_m:
            what = f"contact {100 * (ee[2] - rest):+.1f} cm from where the habit expects it"
        elif rest is not None and force < CONTACT_N and ee[2] < rest - self.surprise_m:
            what = f"no contact {100 * (rest - ee[2]):.1f} cm below where the habit expects the surface"
        if what is None:
            self._since = None
            return None
        if self._since is None:
            self._since = now
        self.last = what
        return what if (now - self._since) / 1e9 >= self.hold_s else None


class Arbiter:
    """S1 for clean(surface) with a habit beside the teacher: see the module's docstring."""

    def __init__(
        self,
        implementation: str,
        params: Json,
        world: Any,
        teacher: Callable[[Json], S1Policy],
        memory: Memory,
        model: ContactModel,
    ) -> None:
        self.source = implementation
        self.params = params
        self.cfg: Json = params.get("learning", {})
        self.world = world
        self.make_teacher = teacher
        self.memory = memory
        self.model = model
        self.supervise = bool(self.cfg.get("supervisor", True))
        busy = self.cfg.get("busy")  # [from s, for s]: S1 busy elsewhere for that window of the task (a dual task)
        self.busy = (float(busy[0]), float(busy[1])) if busy else None
        self.half = float(world.cfg["plate"]["half"])
        self.margin = float(params["margin_m"])
        self.spacing = float(params["line_spacing_m"])
        self.program = memory.program(self.half, self.margin, self.spacing)
        self.driver: S1Policy | None = None
        self.driving = ""
        self.teacher: S1Policy | None = None
        self.events: list[Json] = []
        self.reason = ""
        self._args: tuple[Goal, Json, BodySpec, Mapping[str, Any]] | None = None
        self._t0: Ns | None = None
        self._report: Report | None = None
        self.top = float("nan")
        self.supervisor = Supervisor(self.cfg, float(params["force_max_n"]))

    def goal_params(self) -> Json:
        p = self.params
        return {"force_n": float(p["force_n"]), "force_min_n": float(p["force_min_n"]), "force_max_n": float(p["force_max_n"])}

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        (surface,) = goal.args
        entity = belief["entities"][surface]
        if entity["estimate"] is None:
            raise ValueError(f"no estimate of {surface} in the belief")
        e, g = entity["estimate"], entity["geometry"]
        self.top = float(e["z"] + g["height"] / 2)
        self.model.frame = (float(e["x"]), float(e["y"]), self.top)
        self._args = (goal, belief, body, conditioning)
        cells = cells_of(self.world.cfg)
        self.driving, self.reason = self.memory.decide(self.goal_params(), self.top, self.program, cells)
        if self.driving == "habit":
            habit = HabitWiper(self.source, self.params | self.memory.learned_goal(), self.program)
            habit.start(goal, belief, body, conditioning)
            self.driver = habit
        else:
            self._teach(None)
        self.events.append({"t": 0, "event": "start", "driver": self.driving, "reason": self.reason})

    def _teach(self, now: Ns | None, here: tuple[float, ...] | None = None) -> None:
        """The teacher takes the task, for the time that is left, from where the pad is now (`here`)."""
        assert self._args is not None
        goal, belief, body, conditioning = self._args
        params = dict(self.params)
        if now is not None and self._t0 is not None and "duration_s" in params:
            params["duration_s"] = max(1.0, float(params["duration_s"]) - (now - self._t0) / 1e9)
        if here is not None and belief.get("robot") is not None:
            belief = belief | {"robot": belief["robot"] | {"ee": list(here)}}
        self.teacher = self.make_teacher(params)
        self.teacher.start(goal, belief, body, conditioning)
        self.driver = self.teacher

    def step(self, now: Ns, observation: Observation | None) -> IntentChunk | None:
        if self._report is not None or self.driver is None:
            return None
        if self._t0 is None:
            self._t0 = now
        if self.driving != "habit" and self.busy is not None and self.busy[0] <= (now - self._t0) / 1e9 < sum(self.busy):
            return None  # S1 is busy elsewhere (a dual task): it computes nothing, S0 runs what it holds
        if self.driving == "habit" and self.supervise and observation is not None and observation.proprio is not None:
            force = float(self.world.wrench[2])  # the force estimate S0 reads: filtered, noisy
            what = self.supervisor.check(now, observation.proprio.ee, force, self.model)
            if what is not None:
                self.events.append({"t": now, "event": "handback", "why": what})
                self.driving = "habit+teacher"
                self._teach(now, observation.proprio.ee)
        chunk = self.driver.step(now, observation)
        r = self.driver.report()
        if r is not None:
            self._report = r
        return chunk  # type: ignore[return-value]

    def report(self) -> Report | None:
        return self._report

    def s1_calls(self) -> int:
        """How many times the teacher computed a chunk (the VLA's inferences)."""
        return int(getattr(self.teacher, "_seq", 0)) if self.teacher is not None else 0

    def save(self, run_dir: Path) -> None:
        save = getattr(self.teacher, "save", None)
        if save is not None:
            save(run_dir)
        out = {"driver": self.driving, "reason": self.reason, "top": self.top, "s1_calls": self.s1_calls(), "events": self.events}
        out |= {"program": self.program.to_json(), "model_learned_s": self.model.learned_s}
        (run_dir / "learning.json").write_text(json.dumps(out, indent=1, default=str) + "\n")


def cells_of(world: Json) -> np.ndarray:
    """The dirt cells' centres in the plate's frame (sim/panda.py's `cells`, without MuJoCo)."""
    d, side = world["dirt"], float(world["plate"]["half"])
    n, inner = int(d["cells"]), side - float(d["margin_m"])
    centres = -inner + (np.arange(n) + 0.5) * (2 * inner / n)
    return np.array([(x, y) for y in centres for x in centres])
