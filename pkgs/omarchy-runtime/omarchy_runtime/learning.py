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

import dataclasses
import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime.contracts import (
    CARTESIAN,
    BodySpec,
    Claim,
    ConfigError,
    ContactIntent,
    ContactMode,
    Goal,
    IntentChunk,
    Json,
    MotionIntent,
    Ns,
    Observation,
    Report,
    S1Policy,
    Timing,
)
from omarchy_runtime.sim.wipe import ScriptedWiper, chord

CONTACT_N = 1.0  # N: below this, the pad is not touching
STEADY_N = 3.0  # N: contact this close to the target force is steady (CartesianImpedance.STEADY_N): the contact model learns there


class ContactModel:
    """The cerebellum-like forward model: how high the pad rests in contact
    over a surface, as a height above the surface's believed top, anywhere
    on the square of half side `half` in the surface's frame. Built as the
    adaptive-filter account of the cerebellum (Marr 1969, Albus 1971, Dean
    et al. 2010): the position is expanded on `cells` x `cells` broad
    Gaussian basis functions (width `WIDTH` m, as granule cells), read out by
    weights (as Purkinje cells). The weights learn from the error between
    the measured and the predicted height (as the climbing-fibre signal) by
    a Kalman filter, as each context's state in the COIN model (Heald et al.
    2021) and the Kalman account of the cerebellum (Paulin 1989): the gain
    follows the uncertainty, so nothing overshoots, and the predictive
    variance says how much the model knows at each point. The prior: an
    offset common to the whole surface (spread `PRIOR_M`) and a local shape
    around it (`SHAPE_M`), so one touch tells where the whole surface is.
    One height measurement has spread `NOISE_M`, and `OBS_S` seconds of
    contact count as one independent measurement. The surface may drift by
    `DRIFT_M` per square root of a second. S0 uses a prediction once its
    spread is under `CONF_M`. `frame` (centre x, y and top z, from the
    belief) is set at the start of each task: the model moves with the
    surface."""

    WIDTH = 0.04
    PRIOR_M = 0.02
    SHAPE_M = 0.005
    NOISE_M = 0.003
    OBS_S = 0.05
    DRIFT_M = 0.0005
    CONF_M = 0.004

    def __init__(self, half: float, cells: int = 7) -> None:
        self.half = float(half)
        self.n = int(cells)
        g = np.linspace(-self.half, self.half, self.n)
        self.centres = np.array([(x, y) for y in g for x in g])
        k = len(self.centres)
        self.w = np.zeros(k)  # m above the believed top, per basis function
        self.P = self.PRIOR_M**2 * np.ones((k, k)) + self.SHAPE_M**2 * np.eye(k)  # the weights' covariance
        self.frame: tuple[float, float, float] | None = None
        self.learned_s = 0.0  # seconds of steady contact learned in this task
        self.offset_drift = 0.0  # m per square root of a second: the common offset may drift too (a plastic model)

    def _basis(self, x: float, y: float) -> np.ndarray | None:
        if self.frame is None:
            return None
        u, v = x - self.frame[0], y - self.frame[1]
        if abs(u) > self.half or abs(v) > self.half:
            return None
        phi = np.exp(-((self.centres[:, 0] - u) ** 2 + (self.centres[:, 1] - v) ** 2) / (2 * self.WIDTH**2))
        out: np.ndarray = phi / phi.sum()
        return out

    def belief(self, x: float, y: float) -> tuple[float, float] | None:
        """The predicted height at (x, y), in the base frame, and its variance. None off the surface."""
        phi = self._basis(x, y)
        if phi is None or self.frame is None:
            return None
        return self.frame[2] + float(phi @ self.w), float(phi @ self.P @ phi)

    def predict(self, x: float, y: float) -> float | None:
        """The pad's height in contact at (x, y), in the base frame, or None where the model is not sure enough."""
        b = self.belief(x, y)
        return b[0] if b is not None and b[1] <= self.CONF_M**2 else None

    def learn(self, x: float, y: float, z: float, dt: float) -> None:
        """`dt` seconds of steady contact with the pad at height z over (x, y): one Kalman update."""
        phi = self._basis(x, y)
        if phi is None or self.frame is None or dt <= 0:
            return
        p_phi = self.P @ phi
        gain = p_phi / (float(phi @ p_phi) + self.NOISE_M**2 * self.OBS_S / dt)
        self.w += gain * (z - self.frame[2] - float(phi @ self.w))
        self.P -= np.outer(gain, p_phi)
        self.P[np.diag_indices_from(self.P)] += self.DRIFT_M**2 * dt
        if self.offset_drift:
            self.P += self.offset_drift**2 * dt
        self.learned_s += dt

    def to_json(self) -> Json:
        return {"half": self.half, "cells": self.n, "w": self.w.round(7).tolist(), "P": self.P.round(12).tolist()}

    @staticmethod
    def from_json(j: Json) -> ContactModel:
        m = ContactModel(float(j["half"]), int(j["cells"]))
        m.w, m.P = np.array(j["w"], dtype=np.float64), np.array(j["P"], dtype=np.float64)
        return m


class ModelBank:
    """A repertoire of contact models, one per context met, chosen by
    prediction error with no context name: the cerebellum's paired modules
    weighted by their responsibility (MOSAIC: Wolpert and Kawato 1998, Haruno
    et al. 2001), and a new module when no stored one explains the contact,
    as in the contextual inference of COIN (Heald et al. 2021).

    Responsibility is a posterior over the modules and a candidate module
    that stands for an unknown context. Its prior, at the start of each task:
    how many tasks each module was the most responsible for, plus `STICK`
    for the last one, and `ALPHA` for the unknown context (a Chinese
    restaurant process). Its likelihood, in steady contact: each module's
    predictive Gaussian (ContactModel.belief, widened by `SIGMA` m), the
    candidate's being the prior's (a surface somewhere near the believed
    top), so a young module is not mistaken for a new context while it
    learns. The log-likelihood accumulates over `TAU_E` seconds. A confident
    module explains a contact within about 2 `SIGMA`. When the
    candidate is the most responsible, it is born (`births` counts them in
    the task, the supervisor's novelty) and a fresh candidate waits. Each
    module learns in proportion to its responsibility. The prediction is the
    responsibility-weighted mean of the modules that predict at that point,
    or none when they hold less than half of the responsibility."""

    SIGMA = 0.005  # m: one task's contact heights around a module's prediction (belief 2 mm, compression up to 3 N)
    TAU_E = 0.05
    ALPHA = 0.5
    STICK = 2.0
    FLOOR = -30.0  # log-responsibility below the best: a module never drops out for good within a task

    def __init__(
        self, half: float, modules: list[ContactModel] | None = None, counts: list[float] | None = None, last: int | None = None
    ) -> None:
        self.half = float(half)
        self.modules = modules or []
        self.counts = counts or [0.0] * len(self.modules)
        self.last = last
        self.candidate = ContactModel(half)
        self.log_r = np.zeros(len(self.modules) + 1)
        self.births = 0
        self.learned_s = 0.0
        self._frame: tuple[float, float, float] | None = None
        self._time = np.zeros(len(self.modules) + 1)  # seconds of steady contact each was the most responsible, this task

    @property
    def frame(self) -> tuple[float, float, float] | None:
        return self._frame

    @frame.setter
    def frame(self, value: tuple[float, float, float] | None) -> None:
        """A new task on a surface at `value`: every module moves with it, and responsibility starts from the prior."""
        self._frame = value
        self.candidate = ContactModel(self.half)
        for m in [*self.modules, self.candidate]:
            m.frame = value
            m.learned_s = 0.0
        prior = np.array([c + (self.STICK if i == self.last else 0.0) for i, c in enumerate(self.counts)] + [self.ALPHA])
        prior = np.maximum(prior, 1e-3)
        self.log_r = np.log(prior / prior.sum())
        self.births, self.learned_s = 0, 0.0
        self._time = np.zeros(len(self.modules) + 1)

    def responsibility(self) -> np.ndarray:
        r = np.exp(self.log_r - self.log_r.max())
        out: np.ndarray = r / r.sum()
        return out

    def predict(self, x: float, y: float) -> float | None:
        r, num, den = self.responsibility(), 0.0, 0.0
        for i, m in enumerate(self.modules):
            p = m.predict(x, y)
            if p is not None:
                num, den = num + r[i] * p, den + r[i]
        return num / den if den >= 0.5 else None

    def lowest(self, x: float, y: float) -> float | None:
        """The lowest surface any stored module predicts at (x, y): below it, no known context has a surface."""
        ps = [p for m in self.modules if (p := m.predict(x, y)) is not None]
        return min(ps) if ps else None

    def learn(self, x: float, y: float, z: float, dt: float) -> None:
        if self._frame is None or dt <= 0:
            return
        ll = np.zeros(len(self.modules) + 1)
        for i, m in enumerate([*self.modules, self.candidate]):  # the candidate: the prior's prediction, a new context
            b = m.belief(x, y)
            if b is None:
                return
            s2 = self.SIGMA**2 + b[1]
            ll[i] = -0.5 * (z - b[0]) ** 2 / s2 - 0.5 * math.log(2 * math.pi * s2)
        self.log_r += ll * dt / self.TAU_E
        self.log_r = np.maximum(self.log_r - self.log_r.max(), self.FLOOR)
        r = self.responsibility()
        best = int(np.argmax(r))
        self._time[best] += dt
        if best == len(self.modules):  # no stored module explains the contact: a new context
            self.modules.append(self.candidate)
            self.counts.append(0.0)
            self.births += 1
            self.candidate = ContactModel(self.half)
            self.candidate.frame = self._frame
            self.log_r = np.append(self.log_r, self.log_r[best] - 2.0)
            self._time = np.append(self._time, 0.0)
            r = self.responsibility()
        for i, m in enumerate(self.modules):
            if r[i] >= 0.01:
                m.learn(x, y, z, dt * float(r[i]))
        if r[-1] >= 0.1:
            self.candidate.learn(x, y, z, dt * float(r[-1]))
        self.learned_s += dt

    def map_module(self) -> int | None:
        """The module most responsible over this task's steady contact (None before any contact)."""
        if not self.modules or self._time[: len(self.modules)].sum() <= 0:
            return None
        return int(np.argmax(self._time[: len(self.modules)]))

    def close_task(self) -> None:
        k = self.map_module()
        if k is not None:
            self.counts[k] += 1.0
            self.last = k

    def to_json(self) -> Json:
        return {"bank": True, "half": self.half, "modules": [m.to_json() for m in self.modules], "counts": self.counts, "last": self.last}

    @staticmethod
    def from_json(j: Json) -> ModelBank:
        return ModelBank(float(j["half"]), [ContactModel.from_json(m) for m in j["modules"]], [float(c) for c in j["counts"]], j["last"])


class PhaseILC:
    """A baseline for the contact model: iterative learning control indexed by the task's time, the engineering
    counterpart of a cerebellar feedforward (Gams et al. 2014, eq. 37, p.6, after Bristow et al. 2006; note 234). The
    feedforward height at time t of the next task is Q (F(t) + L e(t)), e(t) the pad's rest height at the same time of
    this task minus F(t), with Q = 0.99 and L = 1 as Gams et al. use (p.5); bins of `BIN_S` seconds from the start of
    the task. It knows when, not where: it has no map of the surface, so it helps only if the next movement repeats this
    one in time. With `gain` below 1 (`learning.ilc_gain`), the reference moves by a part of the error each period, as
    the trajectory adaptation of Li et al. 2018 does once a force sensor closes the force loop (eq. 26, p.4, and
    p.4 "Implicit and explicit force sensing"; note 213); Q is then their decay, 1 - beta with beta = 0.01 (p.8)."""

    BIN_S = 0.02
    Q = 0.99
    L = 1.0
    SPAN_S = 60.0

    def __init__(self, half: float, f: list[float | None] | None = None, gain: float = L) -> None:
        self.half, self.L = float(half), float(gain)
        n = int(self.SPAN_S / self.BIN_S)
        self.F = np.array([np.nan if v is None else v for v in f], dtype=np.float64) if f else np.full(n, np.nan)
        self._sum, self._n = np.zeros(n), np.zeros(n)
        self._frame: tuple[float, float, float] | None = None
        self._t0: Ns | None = None
        self._now: Ns | None = None
        self.learned_s = 0.0

    @property
    def frame(self) -> tuple[float, float, float] | None:
        return self._frame

    @frame.setter
    def frame(self, value: tuple[float, float, float] | None) -> None:
        """A new task: its clock starts at S0's next tick."""
        self._frame, self._t0, self._now, self.learned_s = value, None, None, 0.0
        self._sum[:], self._n[:] = 0.0, 0.0

    def tick(self, now: Ns) -> None:
        """S0's time, at every tick (CartesianImpedance.command)."""
        if self._t0 is None:
            self._t0 = now
        self._now = now

    def _bin(self, x: float, y: float) -> int | None:
        if self._frame is None or self._t0 is None or self._now is None:
            return None
        if abs(x - self._frame[0]) > self.half or abs(y - self._frame[1]) > self.half:
            return None
        b = int((self._now - self._t0) / 1e9 / self.BIN_S)
        return b if b < len(self.F) else None

    def predict(self, x: float, y: float) -> float | None:
        b = self._bin(x, y)
        if b is None or self._frame is None or np.isnan(self.F[b]):
            return None
        return self._frame[2] + float(self.F[b])

    def learn(self, x: float, y: float, z: float, dt: float) -> None:
        b = self._bin(x, y)
        if b is None or self._frame is None or dt <= 0:
            return
        self._sum[b] += (z - self._frame[2]) * dt
        self._n[b] += dt
        self.learned_s += dt

    def close_task(self) -> None:
        """The iteration's update, after the task: F <- Q (F + L e) where this task measured, Q F elsewhere."""
        seen = self._n > 0
        e = np.where(seen, self._sum / np.maximum(self._n, 1e-12) - np.nan_to_num(self.F), 0.0)
        self.F = np.where(seen, self.Q * (np.nan_to_num(self.F) + self.L * e), self.Q * self.F)

    def to_json(self) -> Json:
        return {"ilc": True, "half": self.half, "L": self.L, "F": [None if np.isnan(v) else round(float(v), 7) for v in self.F]}

    @staticmethod
    def from_json(j: Json) -> PhaseILC:
        return PhaseILC(float(j["half"]), list(j["F"]), float(j.get("L", PhaseILC.L)))


Cerebellum = ContactModel | ModelBank | PhaseILC  # what S0's controller learns into: a model, a bank, or the time-indexed baseline


class Program:
    """The habit: strokes along x at fixed lines across the surface (y, in its
    frame), each with the extent [x0, x1] it wipes, or nothing. A cell counts
    as covered when it lies within `REACH` of a line in y and within `ALONG`
    of its extent in x. The pad (sim/panda.py: a 4.4 x 9 cm sponge, turned 45
    degrees) sweeps a band about `REACH` wide on each side of a stroke. Past a
    stroke's end it reaches further on one side of the line only, so a cell
    counts there no further than its own half width (1 cm)."""

    REACH = 0.045
    ALONG = 0.01

    def __init__(
        self, lines: list[float], extents: list[list[float] | None], version: int = 0, scrubs: list[list[float]] | None = None
    ) -> None:
        self.lines = lines
        self.extents = extents
        self.version = version
        self.scrubs = scrubs or []  # [y, x0, x1]: where to go back and forth once more, after the raster

    @staticmethod
    def empty(half: float, margin: float, spacing: float) -> Program:
        reach = half - margin
        n = max(2, math.ceil(2 * reach / spacing) + 1)
        return Program([float(v) for v in np.linspace(-reach, reach, n)], [None] * n)

    def covers(self, x: float, y: float) -> bool:
        for v, e in zip(self.lines, self.extents, strict=True):
            if e is not None and abs(y - v) <= self.REACH and e[0] - self.ALONG <= x <= e[1] + self.ALONG:
                return True
        return False

    def _limit(self, k: int, limit: float, round: bool) -> float:
        """How far along x the pad's centre goes on line k: `limit`, or on a `round` dish that circle's chord."""
        return chord(limit, self.lines[k]) if round else limit

    def include(self, x: float, y: float, limit: float, round: bool = False) -> bool:
        """Extend a line to reach (x, y): the nearest one whose stroke can cover it (on a `round` dish the outer
        lines are short), else the nearest. True if the program changed."""
        if self.covers(x, y):
            return False
        near = sorted(range(len(self.lines)), key=lambda k: abs(y - self.lines[k]))
        can = (k for k in near if abs(y - self.lines[k]) <= self.REACH and abs(x) <= self._limit(k, limit, round) + self.ALONG)
        k = next(can, near[0])
        reach = self._limit(k, limit, round)
        target = float(np.clip(x, -reach, reach))
        e = self.extents[k]
        self.extents[k] = [target, target] if e is None else [min(e[0], target), max(e[1], target)]
        return True

    def strokes(self) -> list[tuple[float, float, float]]:
        """(y, x_from, x_to) in order, alternating direction: a raster over the lines in use, then each scrub, there
        and back."""
        out = []
        for n, (y, e) in enumerate((v, e) for v, e in zip(self.lines, self.extents, strict=True) if e is not None):
            a, b = (e[0], e[1]) if n % 2 == 0 else (e[1], e[0])
            out.append((y, a, b))
        for y, x0, x1 in self.scrubs:
            out += [(y, x0, x1), (y, x1, x0)]
        return out

    SCRUBS = 6  # the most scrub segments the habit keeps

    def insist(self, x: float, y: float, limit: float, round: bool = False) -> bool:
        """A cell the strokes cover stayed dirty: scrub once more over it, on the nearest line that covers it (merged
        with a scrub already there). True if the program changed."""
        cover = [
            k
            for k, (v, e) in enumerate(zip(self.lines, self.extents, strict=True))
            if e is not None and abs(y - v) <= self.REACH and e[0] - self.ALONG <= x <= e[1] + self.ALONG
        ]
        if not cover:
            return False
        k = min(cover, key=lambda k: abs(y - self.lines[k]))
        v, reach = self.lines[k], self._limit(k, limit, round)
        a, b = float(np.clip(x - self.REACH, -reach, reach)), float(np.clip(x + self.REACH, -reach, reach))
        for s in self.scrubs:
            if s[0] == v and a <= s[2] and b >= s[1]:
                if a >= s[1] and b <= s[2]:
                    return False
                s[1], s[2] = min(s[1], a), max(s[2], b)
                return True
        if len(self.scrubs) >= self.SCRUBS:
            return False
        self.scrubs.append([v, a, b])
        return True

    def predicted(self, cells: np.ndarray) -> float:
        """The share of cells (n x 2, the surface's frame) the strokes cover."""
        return float(np.mean([self.covers(float(x), float(y)) for x, y in cells])) if len(cells) else 0.0

    def to_json(self) -> Json:
        return {"version": self.version, "lines": self.lines, "extents": self.extents, "scrubs": self.scrubs}

    @staticmethod
    def from_json(j: Json) -> Program:
        return Program(
            [float(v) for v in j["lines"]],
            [None if e is None else [float(e[0]), float(e[1])] for e in j["extents"]],
            int(j["version"]),
            [[float(v) for v in s] for s in j.get("scrubs", [])],
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
        self.rule = str(cfg.get("rule", "count"))  # "count", or "vpi" (Keramati et al. 2011)
        self.cost = float(cfg.get("cost", 0.03))  # with "vpi": what one task of S1 costs, in share of the plate
        self.prior_sd = float(cfg.get("prior_sd", 0.15))  # with "vpi": the habit's uncertainty after one teacher episode
        self.obs_sd = float(cfg.get("obs_sd", 0.05))  # with "vpi": the spread of one task's outcome
        # What S0 keeps of the teacher's runs to do the task alone: "program" (the habit: strokes built from the results,
        # emulation), "replay" (the best run's movement, imitation), "student" (a small network distilled from the runs).
        self.form = str(cfg.get("form", "program"))
        self.data: Json = json.loads(path.read_text()) if path.exists() else {"episodes": [], "programs": [], "models": {}, "demoted": 0}
        # One contact model per context (the tool or surface the task names), as the brain's many paired models chosen
        # by context (Wolpert and Kawato's modules, note 162): learning one does not overwrite another. With
        # `shared_model`, every context writes the same model (the ablation: interference between contexts). With
        # `contexts = "infer"`, no name: one ModelBank chooses and creates its modules by prediction error.
        self.infer = str(cfg.get("contexts", "named")) == "infer"
        self.cfg_offset_drift = float(cfg.get("offset_drift_m", 0.0))
        self.kind = str(cfg.get("contact_model", "kalman"))  # "kalman" (the map), or "ilc" (the time-indexed baseline)
        self.ilc_gain = float(cfg.get("ilc_gain", PhaseILC.L))  # below 1: the gradual law of Li et al. 2018
        self.context = "bank" if self.infer else "shared" if cfg.get("shared_model") else str(cfg.get("context", "default"))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1) + "\n")
        tmp.replace(self.path)

    def model(self, half: float) -> Cerebellum:
        """This context's contact model, a new one if the context is new (or the bank of all of them)."""
        m = self.data.setdefault("models", {}).get(self.context)
        if self.infer:
            return ModelBank.from_json(m) if m else ModelBank(half)
        if self.kind == "ilc":
            return PhaseILC.from_json(m) if m else PhaseILC(half, gain=self.ilc_gain)
        model = ContactModel.from_json(m) if m else ContactModel(half)
        # The stability-plasticity control: with `offset_drift_m`, one model keeps relearning the surface's offset fast,
        # so it follows a change of dish within a fraction of a second, but forgets the previous dish as fast.
        model.offset_drift = float(self.cfg_offset_drift)
        return model

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
        if self.rule == "vpi":
            if not taught:
                return "teacher", f"no teacher episode for this goal {goal}"
        elif len(taught) < self.n_min:
            return "teacher", f"{len(taught)} teacher episodes for this goal {goal}, {self.n_min} needed"
        elif self.data["demoted"] > 0:
            return "teacher", f"habit demoted for {self.data['demoted']} more tasks"
        tops = [e["top"] for e in taught]
        if min(abs(top - t) for t in tops) > self.top_m:
            return "teacher", f"surface top {top:.3f} m, learned on {min(tops):.3f} to {max(tops):.3f} m"
        mean = float(np.mean([e["kr_coverage"] for e in taught]))
        covered = program.predicted(cells) if self.form == "program" else mean  # the other forms predict no coverage
        if self.rule == "vpi":
            return self._vpi(goal, taught, covered)
        if covered < mean:
            return "teacher", f"habit covers {covered:.2f}, the teacher cleans {mean:.2f}"
        return "habit", f"habit covers {covered:.2f} (version {program.version}), the teacher cleans {mean:.2f} over {len(taught)} episodes"

    def _vpi(self, goal: Json, taught: list[Json], covered: float) -> tuple[str, str]:
        """Keramati et al. 2011 (eq. 18): ask S1 only if the value of perfect information beats S1's cost.
        Both outcomes (share of the plate seen clean) are Gaussian beliefs. S1's: the mean of its episodes and its
        standard error. The habit's: its predicted coverage, with an uncertainty that shrinks with the teacher
        episodes it learned from, updated by its own outcomes. The VPI is the expected gain of S1 over the habit,
        E[max(S1 - habit, 0)]."""
        k1 = np.array([e["kr_coverage"] for e in taught])
        mu1 = float(k1.mean())
        se1 = max(float(k1.std(ddof=1)) / math.sqrt(len(k1)), 0.01) if len(k1) > 1 else self.prior_sd
        own = [e["kr_coverage"] for e in self.data["episodes"] if e["driver"] == "habit" and (e["goal"] == goal or not self.goal_check)]
        prior = self.prior_sd / math.sqrt(len(k1))
        precision = 1 / prior**2 + len(own) / self.obs_sd**2
        mu0 = (covered / prior**2 + sum(own) / self.obs_sd**2) / precision
        sd = math.sqrt(1 / precision + se1**2)
        d = mu1 - mu0
        vpi = d * 0.5 * (1 + math.erf(d / sd / math.sqrt(2))) + sd * math.exp(-0.5 * (d / sd) ** 2) / math.sqrt(2 * math.pi)
        words = f"VPI {vpi:.3f} vs cost {self.cost:.3f}: S1 cleans {mu1:.2f}, the habit {mu0:.2f} (+- {math.sqrt(1 / precision):.2f})"
        return ("teacher" if vpi > self.cost else "habit"), words

    def consolidate(
        self, episode: Json, program: Program, model: Cerebellum, cells: np.ndarray, limit: float, round: bool = False
    ) -> Program:
        """After a task ("sleep"): store the episode, and build the habit's next
        version from what it saw: the cells it observed clean after the
        teacher's run, the cells it observed dirty after its own run. The pad's
        centre stays within `limit` of the surface's centre: along x, or, on a
        `round` dish, within that radius."""

        self.data["episodes"].append(episode)
        close = getattr(model, "close_task", None)
        if close is not None:  # the bank counts its modules' use, the ILC baseline updates its iteration
            close()
        self.data.setdefault("models", {})[self.context] = model.to_json()
        kr = np.array(episode["kr_clean"], dtype=bool)
        changed = False
        if episode["driver"] == "teacher":
            for (x, y), clean in zip(cells, kr, strict=True):
                if clean:
                    changed |= program.include(float(x), float(y), limit, round)
            if self.data["demoted"] > 0:
                self.data["demoted"] -= 1
        elif episode["driver"] == "habit":
            # Knowledge of results. A dirty cell the strokes miss: extend them. A dirty cell they cover, seen dirty after
            # two habit runs in a row (not the camera's noise): scrub it once more. The habit can then do better than the
            # teacher, whose one pass leaves a stubborn stain.
            counts = self.data.setdefault("dirty_counts", [0] * len(cells))
            for i, ((x, y), clean) in enumerate(zip(cells, kr, strict=True)):
                covered = program.covers(float(x), float(y))
                counts[i] = 0 if clean or not covered else counts[i] + 1
                if not clean and not covered:
                    changed |= program.include(float(x), float(y), limit, round)
                elif counts[i] >= 2:
                    changed |= program.insist(float(x), float(y), limit, round)
            taught = self.teacher_runs()
            mean = float(np.mean([e["kr_coverage"] for e in taught])) if taught else 0.0
            recent = [e for e in self.data["episodes"] if e["driver"] != "teacher"][-2:]
            if len(recent) == 2 and all(e["driver"] == "habit" and e["kr_coverage"] < mean - self.delta for e in recent):
                self.data["demoted"] = self.rest
        if changed or not self.data["programs"]:
            program = Program(program.lines, program.extents, program.version + 1, program.scrubs)
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


def path_of(t: np.ndarray, ee: np.ndarray, force: np.ndarray, frame: tuple[float, float, float], step_s: float = 0.05) -> list[list[float]]:
    """A run's movement as S0 executed it, every `step_s`: [t, u, v, w, contact] in the surface's frame (u, v, w from its
    believed centre and top), contact when the force S0 measured passed CONTACT_N (here the true force, which the
    estimate S0 reads follows, filtered and noisy). What the replay and the distilled network learn from."""
    every = max(1, int(round(step_s * 1000)))
    out = []
    for i in range(0, len(t), every):
        out.append([round((t[i] - t[0]) / 1e9, 3), *(round(float(v), 5) for v in ee[i] - np.array(frame)), int(force[i] >= CONTACT_N)])
    return out


def _plan_from(
    path: list[list[float]], here: np.ndarray, frame: tuple[float, float, float], low: float, descend: float
) -> list[tuple[float, np.ndarray, np.ndarray, ContactMode]]:
    """Segments (start s, from, to, mode) that follow `path` in the current surface frame: from where the pad is to the
    path's first point, then point to point at the recorded pace. In contact the height is the habit's (`low`): S0's
    force loop and contact model find the surface, as for every form, and a segment that makes contact goes down no
    faster than the habit's descent (`descend`, m/s)."""
    pts = [np.array([frame[0] + u, frame[1] + v, low if c else frame[2] + w]) for _, u, v, w, c in path]
    plan, t, p, prev = [], 0.0, here, False
    first = max(float(np.linalg.norm(pts[0] - here)) / 0.15, 0.2)
    plan.append((t, p, pts[0], ContactMode.FREE))
    t, p = t + first, pts[0]
    for (t0, *_r), (t1, *_q, c), q in zip(path, path[1:], pts[1:], strict=False):
        mode = (ContactMode.MAINTAIN if prev else ContactMode.MAKE) if c else (ContactMode.BREAK if prev else ContactMode.FREE)
        plan.append((t, p, q, mode))
        slow = abs(float(q[2] - p[2])) / descend if mode is ContactMode.MAKE else 0.0
        t, p, prev = t + max(t1 - t0, slow, 1e-3), q, bool(c)
    return plan + [(t, p, p, ContactMode.FREE)]


class ReplayWiper(ScriptedWiper):
    """S0 as an episodic copy: it replays the movement of the teacher's best run so far (the most cells it saw clean),
    moved into the current surface frame, at the teacher's pace (imitation of the motion, as a muscle-memory cache)."""

    def __init__(self, implementation: str, params: Json, path: list[list[float]]) -> None:
        super().__init__(implementation, params)
        self.path = path

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        from omarchy_runtime.sim import wipe

        (surface,) = goal.args
        e, g = belief["entities"][surface]["estimate"], belief["entities"][surface]["geometry"]
        cart = body.kinematics["cartesian"]
        self._space, self._frame, self._ee = str(cart["action_space"]), str(cart["frame"]), str(cart["ee"])
        self._orientation = np.array(cart["orientation"], dtype=np.float64)
        top = e["z"] + g["height"] / 2
        here = np.array(belief["robot"]["ee"], dtype=np.float64)
        self._plan = _plan_from(self.path, here, (e["x"], e["y"], top), top - self.press, self.speeds["descend"])
        self._end, self._last = self._plan[-1][0], self._plan[-1][1]
        self.horizon = math.ceil(self._end / (wipe.DT / 1e9)) + 2


class TinyNet:
    """A small multilayer perceptron (two hidden layers of `H` tanh units) in numpy, trained by Adam on the mean squared
    error of the movement and the cross-entropy of contact: the distilled student, as EMS and FIERCE train a small
    policy on the large one's runs (notes 150, 121)."""

    H = 64

    def __init__(self, n_in: int, n_out: int, seed: int = 0) -> None:
        rng = np.random.default_rng(seed)
        sizes = [n_in, self.H, self.H, n_out]
        self.p = [x for a, b in zip(sizes, sizes[1:], strict=False) for x in (rng.normal(0, 1 / math.sqrt(a), (a, b)), np.zeros(b))]
        self.mu_x = self.sd_x = self.mu_y = self.sd_y = np.zeros(0)

    def _forward(self, x: np.ndarray) -> tuple[np.ndarray, list[np.ndarray]]:
        hs, h = [], x
        for i in range(0, len(self.p) - 2, 2):
            h = np.tanh(h @ self.p[i] + self.p[i + 1])
            hs.append(h)
        return h @ self.p[-2] + self.p[-1], hs

    def fit(self, x: np.ndarray, y: np.ndarray, steps: int = 3000, lr: float = 3e-3, seed: int = 0) -> float:
        """y's last column is contact (0 or 1), the others are regressed. Returns the last batch's loss."""
        self.mu_x, self.sd_x = x.mean(0), x.std(0) + 1e-6
        self.mu_y, self.sd_y = y[:, :-1].mean(0), y[:, :-1].std(0) + 1e-6
        xn = (x - self.mu_x) / self.sd_x
        yn = (y[:, :-1] - self.mu_y) / self.sd_y
        rng = np.random.default_rng(seed)
        m = [np.zeros_like(q) for q in self.p]
        v = [np.zeros_like(q) for q in self.p]
        loss = 0.0
        for k in range(1, steps + 1):
            idx = rng.integers(0, len(x), min(256, len(x)))
            xb, yb, cb = xn[idx], yn[idx], y[idx, -1]
            out, hs = self._forward(xb)
            prob = 1 / (1 + np.exp(-out[:, -1]))
            d_out = np.concatenate([2 * (out[:, :-1] - yb) / yb.shape[1], (prob - cb)[:, None]], axis=1) / len(xb)
            loss = float(((out[:, :-1] - yb) ** 2).mean() - (cb * np.log(prob + 1e-9) + (1 - cb) * np.log(1 - prob + 1e-9)).mean())
            grads = [hs[-1].T @ d_out, d_out.sum(0)]
            d = d_out @ self.p[-2].T
            for i in range(len(hs) - 1, -1, -1):
                d = d * (1 - hs[i] ** 2)
                below = hs[i - 1] if i > 0 else xb
                grads = [below.T @ d, d.sum(0), *grads]
                if i > 0:
                    d = d @ self.p[2 * i].T
            for j, g in enumerate(grads):
                m[j] = 0.9 * m[j] + 0.1 * g
                v[j] = 0.999 * v[j] + 0.001 * g * g
                self.p[j] -= lr * (m[j] / (1 - 0.9**k)) / (np.sqrt(v[j] / (1 - 0.999**k)) + 1e-8)
        return loss

    def __call__(self, x: np.ndarray) -> np.ndarray:
        out, _ = self._forward((x - self.mu_x) / self.sd_x)
        return np.concatenate([out[..., :-1] * self.sd_y + self.mu_y, 1 / (1 + np.exp(-out[..., -1:]))], axis=-1)


def train_student(paths: list[list[list[float]]], seed: int = 0, noise_m: float = 0.01, copies: int = 4) -> tuple[TinyNet, float]:
    """The student from the teacher's runs: (u, v, w, phase, contact) -> (du, dv, dw, next contact) per 0.05 s step.
    Each sample is also copied `copies` times with the position disturbed by `noise_m`, its target pointing back to
    the demonstrated next point (DART, Laskey et al. 2017), so that small errors do not compound in closed loop.
    Returns the student and the mean duration of the runs (the phase's unit)."""
    xs, ys = [], []
    span = float(np.mean([p[-1][0] for p in paths]))
    rng = np.random.default_rng(seed)
    for p in paths:
        a = np.array(p, dtype=np.float64)
        for n in range(copies + 1):
            here = a[:-1, 1:4] + (rng.normal(0.0, noise_m, (len(a) - 1, 3)) if n else 0.0)
            xs.append(np.c_[here, a[:-1, 0] / span, a[:-1, 4]])
            ys.append(np.c_[a[1:, 1:4] - here, a[1:, 4]])
    net = TinyNet(5, 4, seed)
    net.fit(np.concatenate(xs), np.concatenate(ys), seed=seed)
    return net, span


class StudentWiper(ScriptedWiper):
    """S0 as a distilled network: a small policy trained on the teacher's runs (TinyNet), run closed loop from the
    pad's measured position at S1's rate, rolled forward over each chunk. Ends after the teacher's mean duration."""

    def __init__(self, implementation: str, params: Json, net: TinyNet, span: float) -> None:
        super().__init__(implementation, params)
        self.net, self.span = net, span
        self._c = 0.0
        self._z: float | None = None  # the height commanded last, for the descent's ramp
        self._lift: np.ndarray | None = None

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        (surface,) = goal.args
        e, g = belief["entities"][surface]["estimate"], belief["entities"][surface]["geometry"]
        cart = body.kinematics["cartesian"]
        self._space, self._frame, self._ee = str(cart["action_space"]), str(cart["frame"]), str(cart["ee"])
        self._orientation = np.array(cart["orientation"], dtype=np.float64)
        self._top = float(e["z"] + g["height"] / 2)
        self._origin = np.array([e["x"], e["y"], self._top])

    def step(self, now: Ns, observation: Observation | None) -> IntentChunk | None:
        from omarchy_runtime.sim import wipe

        if self._report is not None or observation is None or observation.proprio is None:
            return None
        if self._t0 is None:
            self._t0 = now
        s = (now - self._t0) / 1e9
        ee = np.array(observation.proprio.ee[:3], dtype=np.float64)
        rows, modes = [], []
        if s >= self.span:  # the learned movement is over: up, then done
            if self._lift is None:
                self._lift = np.array([ee[0], ee[1], self._top + wipe.LIFT])
            if float(np.linalg.norm(ee - self._lift)) <= wipe.REACHED:
                self._report = Report(Claim.DONE, "the distilled movement ended, the pad up again")
                return None
            step = (self._lift - ee) * min(1.0, 0.03 * wipe.DT / 1e9 / max(float(np.linalg.norm(self._lift - ee)), 1e-6))
            for k in range(self.horizon):
                rows.append([*(ee + step * (k + 1)), *self._orientation])
                modes.append(ContactMode.BREAK if self._c >= 0.5 else ContactMode.FREE)
        else:
            u = ee - self._origin
            c, phase = self._c, s / self.span
            z_prev = self._z if self._z is not None else float(ee[2])
            for k in range(self.horizon):
                out = self.net(np.array([*u, phase, c]))
                u = u + out[:3]
                nc = float(out[3])
                was, now_c = c >= 0.5, nc >= 0.5
                mode = (ContactMode.MAINTAIN if was else ContactMode.MAKE) if now_c else (ContactMode.BREAK if was else ContactMode.FREE)
                z = self._origin[2] + u[2]
                slowest = z_prev - self.speeds["descend"] * wipe.DT / 1e9  # near the surface, no faster than the habit's descent
                if now_c:  # pressing: down to the habit's height
                    z = max(self._top - self.press, slowest)
                elif z < z_prev and z < self._top + wipe.ABOVE:
                    z = max(z, slowest)
                rows.append([self._origin[0] + u[0], self._origin[1] + u[1], z, *self._orientation])
                if k == 0:
                    self._z = z
                z_prev = z
                modes.append(mode)
                if k == 0:
                    self._c = nc
                c, phase = nc, phase + (wipe.DT / 1e9) / self.span
        self._seq += 1
        timing = Timing(now, wipe.DT, now + (self.horizon - 1) * wipe.DT, self.source, observation.t)
        motion = MotionIntent.make(timing, self._space, CARTESIAN, rows, self._frame)
        lo, hi = self.bounds
        contact = [
            ContactIntent.make(timing, self._ee, self._frame, (0, 0, 1), modes, [[self.force]] * self.horizon, [lo], [hi], self.stiffness)
        ]
        return IntentChunk.make(self._seq, self.source, motion, contact=contact)


class NullTeacher:
    """S1 removed, as a lesion: it computes nothing and claims nothing, so S0 keeps executing what it holds and the
    task ends at the scenario's time limit. Lesioned before practice, nothing is taught (Kawai et al. 2015, note 192)."""

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        return None

    def step(self, now: Ns, observation: Observation | None) -> IntentChunk | None:
        return None

    def report(self) -> Report | None:
        return None


class Supervisor:
    """Watches the habit at S1's rate from what the robot measures (the force
    estimate S0 reads, the pad's position) against the contact model's
    prediction. Surprise, any of: the pad in steady contact (the force
    within `STEADY_N` of the habit's target `wanted`, where the model learned:
    before that a soft sponge is still giving way, and the pad is above where
    it will rest) more than `surprise_m` from where the model says it rests,
    for `hold_s`; the pad more than `surprise_m` below that height without
    contact, for `hold_s` (the surface is not there); the force over the
    contact's upper bound."""

    def __init__(self, cfg: Json, upper: float, wanted: float) -> None:
        self.novelty = bool(cfg.get("novelty", True))  # with a ModelBank: a module born while the habit drives is a surprise
        self.surprise_m = float(cfg.get("surprise_m", 0.01))
        self.hold_s = float(cfg.get("hold_s", 0.2))
        self.upper = upper
        self.wanted = wanted
        self._since: Ns | None = None
        self.last: str | None = None

    def check(self, now: Ns, ee: tuple[float, ...], force: float, model: Cerebellum) -> str | None:
        rest = model.predict(ee[0], ee[1])
        what = None
        if force > self.upper:
            what = f"force {force:.1f} N over the bound {self.upper:.0f} N"
        elif isinstance(model, ModelBank):  # the bank explains a known context by itself: only novelty, or no surface at all
            low = model.lowest(ee[0], ee[1])
            if self.novelty and model.births > 0:
                what = "a new context: no stored contact model explains the contact"
            elif low is not None and force < CONTACT_N and ee[2] < low - self.surprise_m:
                what = f"no contact {100 * (low - ee[2]):.1f} cm below every known surface"
        elif rest is not None and force >= CONTACT_N and abs(force - self.wanted) <= STEADY_N and abs(ee[2] - rest) > self.surprise_m:
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
        teacher: Callable[[Json], S1Policy],
        memory: Memory,
        model: Cerebellum,
        *,
        half: float,
        cells: np.ndarray,
        force: Callable[[], float],
    ) -> None:
        """`half`: the surface's half side (m). `cells`: the dirt cells' centres in its frame, as the camera reports
        them. `force`: the normal force estimate S0 reads (N), for the supervisor. The composition gives all three
        (the simulated Panda's, or a real robot's): the arbiter never reads a simulator."""
        self.source = implementation
        self.params = params
        self.cfg: Json = params.get("learning", {})
        self.make_teacher = teacher
        self.cells = cells
        self.force = force
        self.memory = memory
        self.model = model
        self.supervise = bool(self.cfg.get("supervisor", True))
        busy = self.cfg.get("busy")  # [from s, for s]: S1 busy elsewhere for that window of the task (a dual task)
        self.busy = (float(busy[0]), float(busy[1])) if busy else None
        self.half = float(half)
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
        self._sent = 0
        self.top = float("nan")
        self.supervisor = Supervisor(self.cfg, float(params["force_max_n"]), float(params["force_n"]))

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
        self.driving, self.reason = self.memory.decide(self.goal_params(), self.top, self.program, self.cells)
        if self.driving == "habit":
            params = self.params | self.memory.learned_goal()
            self.supervisor.wanted = float(params["force_n"])  # the force the habit presses with: the one it learned
            habit: ScriptedWiper
            if self.memory.form == "replay":
                best = max((e for e in self.memory.teacher_runs() if e.get("path")), key=lambda e: e["kr_coverage"])
                habit = ReplayWiper(self.source, params, best["path"])
            elif self.memory.form == "student":
                runs = [e["path"] for e in self.memory.teacher_runs() if e.get("path")]
                habit = StudentWiper(self.source, params, *train_student(runs, seed=len(runs)))
            else:
                habit = HabitWiper(self.source, params, self.program)
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
            what = self.supervisor.check(now, observation.proprio.ee, self.force(), self.model)
            if what is not None:
                self.events.append({"t": now, "event": "handback", "why": what})
                self.driving = "habit+teacher"
                self._teach(now, observation.proprio.ee)
        chunk = self.driver.step(now, observation)
        r = self.driver.report()
        if r is not None:
            self._report = r
        if not isinstance(chunk, IntentChunk):
            return None
        self._sent += 1  # one sequence for both drivers: S0 drops a chunk older than the last it took
        return dataclasses.replace(chunk, seq=self._sent)

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
        out |= {"program": self.program.to_json(), "model_learned_s": self.model.learned_s, "frame": self.model.frame}
        (run_dir / "learning.json").write_text(json.dumps(out, indent=1, default=str) + "\n")


def cells_of(world: Json) -> np.ndarray:
    """The dirt cells' centres in the plate's frame (sim/panda.py's `cells`, without MuJoCo)."""
    d, side = world["dirt"], float(world["plate"]["half"])
    n, inner = int(d["cells"]), side - float(d["margin_m"])
    centres = -inner + (np.arange(n) + 0.5) * (2 * inner / n)
    grid = np.array([(x, y) for y in centres for x in centres])
    out: np.ndarray = grid[np.hypot(grid[:, 0], grid[:, 1]) <= inner] if world["plate"].get("round") else grid
    return out
