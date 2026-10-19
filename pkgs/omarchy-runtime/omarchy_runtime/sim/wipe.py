"""S1 for clean(surface): a scripted wiper, for a body that takes Cartesian
pose intents.

The same path in every condition: from where the end-effector is, above the
surface's first corner, down onto it, a raster over it at the believed
height (`press_m` below the believed top), and up again. The surface and its
height come from the belief at start, never from the simulator: a wrong
height in the belief is a wrong height in the path. With `contact` it adds a
contact channel: MAKE on the way down, MAINTAIN while wiping (a target normal
force within bounds, a stiffness per axis), BREAK on the way up, FREE before.
It claims DONE once the path has ended with the end-effector up again; it
never looks at the dirt, the verifier does. Plain numpy: no simulator.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
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
    Timing,
    ns,
)

HORIZON = 10  # rows per chunk
DT = ns(0.05)  # between rows
ABOVE = 0.05  # m above the believed top before going down
LIFT = 0.08  # m above it at the end
REACHED = 0.02  # m from the last waypoint: up again


class ScriptedWiper:
    def __init__(self, implementation: str, params: Json) -> None:
        self.source = implementation
        try:
            self.contact = bool(params["contact"])
            self.force = float(params["force_n"])
            self.bounds = (float(params["force_min_n"]), float(params["force_max_n"]))
            self.stiffness = tuple(float(v) for v in params["stiffness"])
            self.press = float(params["press_m"])
            self.spacing = float(params["line_spacing_m"])
            self.margin = float(params["margin_m"])
            self.speeds = {k: float(params[f"{k}_m_s"]) for k in ("transfer", "descend", "wipe")}
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"scenario s1: the wiper needs {e}") from e
        self._report: Report | None = None
        self._plan: list[tuple[float, np.ndarray, np.ndarray, ContactMode]] = []  # start (s, from t0), from, to, mode
        self._end = 0.0
        self._t0: Ns | None = None
        self._seq = 0

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
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
        reach = g["width"] / 2 - self.margin  # the pad's centre stays this far from the centre
        lines = max(2, math.ceil(2 * reach / self.spacing) + 1)
        ys = np.linspace(e["y"] - reach, e["y"] + reach, lines)
        x0, x1 = e["x"] - reach, e["x"] + reach
        here = np.array(belief["robot"]["ee"], dtype=np.float64)
        low = top - self.press
        points: list[tuple[np.ndarray, ContactMode, float]] = [
            (np.array([x0, ys[0], top + ABOVE]), ContactMode.FREE, self.speeds["transfer"])
        ]
        points.append((np.array([x0, ys[0], low]), ContactMode.MAKE, self.speeds["descend"]))
        for n, y in enumerate(ys):
            a, b = (x0, x1) if n % 2 == 0 else (x1, x0)
            if n:
                points.append((np.array([a, y, low]), ContactMode.MAINTAIN, self.speeds["wipe"]))
            points.append((np.array([b, y, low]), ContactMode.MAINTAIN, self.speeds["wipe"]))
        last = points[-1][0]
        points.append((np.array([last[0], last[1], top + LIFT]), ContactMode.BREAK, self.speeds["descend"]))
        t, p = 0.0, here
        for q, mode, speed in points:
            duration = max(float(np.linalg.norm(q - p)) / speed, 0.2)
            self._plan.append((t, p, q, mode))
            t, p = t + duration, q
        self._end, self._last = t, p
        self._plan.append((t, p, p, ContactMode.FREE))

    def _at(self, s: float) -> tuple[np.ndarray, ContactMode]:
        for (t, a, b, mode), (t_next, *_rest) in zip(self._plan, self._plan[1:], strict=False):
            if s < t_next:
                return a + (b - a) * (s - t) / (t_next - t), mode
        return self._last, ContactMode.FREE

    def step(self, now: Ns, observation: Observation | None) -> IntentChunk | None:
        if self._report is not None or observation is None or observation.proprio is None:
            return None
        if self._t0 is None:
            self._t0 = now
        s = (now - self._t0) / 1e9
        if s >= self._end and float(np.linalg.norm(np.array(observation.proprio.ee) - self._last)) <= REACHED:
            self._report = Report(Claim.DONE, f"raster of {len(self._plan) - 4} segments ended, the pad up again")
            return None
        rows, modes = [], []
        for k in range(HORIZON):
            p, mode = self._at(s + k * DT / 1e9)
            rows.append([*p, *self._orientation])
            modes.append(mode)
        self._seq += 1
        timing = Timing(now, DT, now + (HORIZON - 1) * DT, self.source, observation.t)
        motion = MotionIntent.make(timing, self._space, CARTESIAN, rows, self._frame)
        contact = []
        if self.contact:
            lo, hi = self.bounds
            contact.append(
                ContactIntent.make(timing, self._ee, self._frame, (0, 0, 1), modes, [[self.force]] * HORIZON, [lo], [hi], self.stiffness)
            )
        return IntentChunk.make(self._seq, self.source, motion, contact=contact)

    def report(self) -> Report | None:
        return self._report
