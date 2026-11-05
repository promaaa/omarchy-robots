"""S1 for in(object, container) on any arm that takes Cartesian pose
intents with a gripper dimension: a scripted picker, the same for every body.

It plans from the belief it is given at start (where the camera saw the
object, the container's geometry, where the TCP is), then sends rows of TCP
poses and gripper openings: above the object, down, close, up, above the
container, down, open, up. The poses are the same on every arm: the TCP
points down, its fingers close along y (`kinematics.cartesian.orientation`,
the same quaternion for every body that declares the TCP the same way). Until
the fingers close it moves its grasp to where the camera sees the object
now. It never checks the grasp, and claims DONE once its path has ended with the
TCP up again: a self-report that can be wrong, as a learned policy's can.
Plain numpy: no simulator.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from omarchy_runtime.contracts import (
    CARTESIAN,
    BodySpec,
    Claim,
    ConfigError,
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
REACHED = 0.01  # m from the last waypoint: up again
WATCH_M = 0.004  # m: the object seen this far from where the plan grasps it moves the grasp
OPEN, CLOSED = 1.0, 0.0


class ScriptedPicker:
    def __init__(self, implementation: str, params: Json) -> None:
        self.source = implementation
        try:
            self.speed = float(params["speed_m_s"])
            self.slow = float(params["slow_m_s"])
            self.clearance = float(params["clearance_m"])
            self.grasp_below_top = float(params["grasp_below_top_m"])
            self.drop = float(params["drop_m"])
            self.grip_s = float(params["grip_s"])
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"scenario s1: the picker needs {e}") from e
        self._report: Report | None = None
        self._plan: list[tuple[float, np.ndarray, np.ndarray]] = []  # start time (s from t0), from, to: rows of (x, y, z, gripper)
        self._t0: Ns | None = None
        self._seq = 0

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        if goal.predicate != "in" or len(goal.args) != 2:
            raise ValueError(f"the picker achieves in(object, container), not {goal}")
        obj, container = goal.args
        entities = belief["entities"]
        e, g = entities[obj]["estimate"], entities[obj]["geometry"]
        r = entities[container]["geometry"]
        if e is None or belief.get("robot") is None:
            raise ValueError(f"no estimate of {obj}, or no proprioception, in the belief")
        cart = body.kinematics.get("cartesian")
        if cart is None:
            raise ConfigError(f"body {body.id} takes no Cartesian intents (kinematics.cartesian)")
        self._space, self._frame = str(cart["action_space"]), str(cart["frame"])
        self._orientation = np.array(cart["orientation"], dtype=np.float64)
        height = float(g["height"])
        top = float(e["z"]) + height / 2  # the camera sees the cup's middle, about
        grasp = top - self.grasp_below_top
        cx, cy = (r["x0"] + r["x1"]) / 2, (r["y0"] + r["y1"]) / 2
        floor = float(r["z0"])
        high = max(top, floor + height) + self.clearance
        here = np.array([*belief["robot"]["ee"], OPEN], dtype=np.float64)
        ox, oy = float(e["x"]), float(e["y"])
        place = floor + height - self.grasp_below_top + self.drop
        points = [  # (x, y, z, gripper), speed or a dwell
            (np.array([ox, oy, high, OPEN]), self.speed),
            (np.array([ox, oy, grasp, OPEN]), self.slow),
            (np.array([ox, oy, grasp, CLOSED]), -self.grip_s),
            (np.array([ox, oy, high, CLOSED]), self.slow),
            (np.array([cx, cy, high, CLOSED]), self.speed),
            (np.array([cx, cy, place, CLOSED]), self.slow),
            (np.array([cx, cy, place, OPEN]), -self.grip_s),
            (np.array([cx, cy, high, OPEN]), self.slow),
        ]
        t, p = 0.0, here
        for q, speed in points:
            duration = -speed if speed < 0 else max(float(np.linalg.norm(q[:3] - p[:3])) / speed, 0.2)
            self._plan.append((t, p, q))
            t, p = t + duration, q
        self._end, self._last = t, p
        self._plan.append((t, p, p))
        self._object, self._grasp_xy = obj, np.array([ox, oy])
        self._closing = self._plan[2][0]  # when the fingers start closing: the grasp point is fixed from then on

    def _watch(self, s: float, observation: Observation) -> None:
        """Before the fingers close, a fresh sight of the object more than WATCH_M
        from where the plan grasps it moves the grasp there (and the point above it)."""
        seen = [d for d in observation.detections if d.entity == self._object]
        if not seen or s >= self._closing:
            return
        xy = np.array([seen[0].x, seen[0].y])
        if float(np.linalg.norm(xy - self._grasp_xy)) <= WATCH_M:
            return
        for i, (t, start, stop) in enumerate(self._plan[:4]):
            a, b = start.copy(), stop.copy()
            if i > 0:
                a[:2] = xy
            b[:2] = xy
            self._plan[i] = (t, a, b)
        nxt = self._plan[4]
        self._plan[4] = (nxt[0], np.array([*xy, *nxt[1][2:]]), nxt[2])
        self._grasp_xy = xy

    def _at(self, s: float) -> np.ndarray:
        for (t, a, b), (t_next, *_rest) in zip(self._plan, self._plan[1:], strict=False):
            if s < t_next:
                u = (s - t) / (t_next - t)
                u = u * u * (3 - 2 * u)  # smooth start and stop on each segment
                out: np.ndarray = a + (b - a) * u
                return out
        return self._last

    def step(self, now: Ns, observation: Observation | None) -> IntentChunk | None:
        if self._report is not None or observation is None or observation.proprio is None:
            return None
        if self._t0 is None:
            self._t0 = now
        s = (now - self._t0) / 1e9
        self._watch(s, observation)
        if s >= self._end and float(np.linalg.norm(np.array(observation.proprio.ee) - self._last[:3])) <= REACHED:
            self._report = Report(Claim.DONE, f"pick and place path of {len(self._plan) - 1} segments ended, the TCP up again")
            return None
        rows = []
        for k in range(HORIZON):
            p = self._at(s + k * DT / 1e9)
            rows.append([*p[:3], *self._orientation, p[3]])
        self._seq += 1
        timing = Timing(now, DT, now + (HORIZON - 1) * DT, self.source, observation.t)
        motion = MotionIntent.make(timing, self._space, (*CARTESIAN, "gripper"), rows, self._frame)
        return IntentChunk.make(self._seq, self.source, motion)

    def report(self) -> Report | None:
        return self._report
