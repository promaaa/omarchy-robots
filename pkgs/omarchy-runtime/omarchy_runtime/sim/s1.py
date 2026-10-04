"""S1 in the simulation: a scripted pick-and-place policy, and the host that runs it.

The policy is deliberately naive. It plans waypoints from the belief it is
given at start, tracks them with proprioception, never checks the grasp, and
claims DONE when its last waypoint is reached: a policy whose self-report can
be wrong, as a learned one's can.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omarchy_runtime.contracts import (
    ActionChunk,
    BodySpec,
    ChunkRejected,
    Claim,
    Goal,
    Json,
    Ns,
    Observation,
    Report,
    S1Policy,
    ns,
)
from omarchy_runtime.sched import Latest
from omarchy_runtime.sim.kinematics import Kinematics
from omarchy_runtime.sim.s0 import SimS0
from omarchy_runtime.sim.world import Trigger, World

SPEED = 0.25  # m/s of the gripper's reference point along each segment
GRIP_RATE = 2.0  # gripper travel per second
HORIZON = 10  # rows per chunk
DT = ns(0.05)  # between rows
REACHED = (0.003, 0.02)  # distance (m) and gripper tolerance at the last waypoint
REPLAN = 0.03  # m (or gripper travel) off the plan: plan again from where the body is. Above
# SPEED x inference latency, the lag at the start of every plan.
OPEN, CLOSED = 1.0, 0.0

log = logging.getLogger(__name__)

Point = tuple[float, float, float]  # x, z, gripper


class ScriptedPickPlace:
    """Achieves in(object, container) on any body this module has kinematics for.

    It follows one time-indexed plan through its waypoints, so consecutive
    chunks agree; when proprioception shows the body off the plan (after a
    stall or a fallback), it plans the remaining waypoints again from there.
    """

    def __init__(self, implementation: str) -> None:
        self.source = implementation
        self._report: Report | None = None
        self._waypoints: list[Point] = []
        self._plan: list[tuple[Ns, Ns, Point, Point, int]] = []  # start, end, from, to, waypoint index
        self._seq = 0

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        obj, container = goal.args
        entities = belief["entities"]
        estimate = entities[obj]["estimate"]
        if estimate is None:
            raise ValueError(f"no estimate of {obj} in the belief")
        height = entities[obj]["geometry"]["height"]
        width = entities[obj]["geometry"]["width"]
        region = entities[container]["geometry"]
        self._body, self._kin = body, Kinematics(body)
        self._gripper = body.dims.index("gripper")
        x0, x1, z0, z1 = body.workspace
        transit = z1 - 0.10
        grasp = max(estimate["z"], z0 + 0.002)
        # The first spot in the container, left to right, that the belief shows free.
        others = [e["estimate"] for n, e in entities.items() if n != obj and e["kind"] == "object" and e["estimate"]]
        spots = [region["x0"] + width / 2 + 0.01 + k * (width + 0.01) for k in range(8)]
        spots = [x for x in spots if x <= region["x1"] - width / 2 - 0.01] or [(region["x0"] + region["x1"]) / 2]
        free = [x for x in spots if all(abs(x - o["x"]) > width + 0.005 or not region["z0"] <= o["z"] <= region["z1"] for o in others)]
        place_x = (free or spots)[0]
        place_z = max(region["z0"] + height / 2 + 0.01, z0 + 0.002)  # released 1 cm above the bottom
        ox = estimate["x"]
        self._waypoints = [
            (ox, transit, OPEN),
            (ox, grasp, OPEN),
            (ox, grasp, CLOSED),
            (ox, transit, CLOSED),
            (place_x, transit, CLOSED),
            (place_x, place_z, CLOSED),
            (place_x, place_z, OPEN),
            (place_x, transit, OPEN),
        ]

    def _replan(self, now: Ns, here: Point, first: int) -> None:
        self._plan, t, p = [], now, here
        for i in range(first, len(self._waypoints)):
            w = self._waypoints[i]
            duration = max(math.hypot(w[0] - p[0], w[1] - p[1]) / SPEED, abs(w[2] - p[2]) / GRIP_RATE)
            end = t + ns(duration)
            self._plan.append((t, end, p, w, i))
            t, p = end, w

    def _at(self, t: Ns) -> tuple[Point, int]:
        for start, end, a, b, i in self._plan:
            if t < end:
                f = (t - start) / (end - start) if end > start else 1.0
                return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f), i
        return self._plan[-1][3], len(self._waypoints)

    def step(self, now: Ns, observation: Observation | None) -> ActionChunk | None:
        if self._report is not None or observation is None or observation.proprio is None:
            return None
        q = observation.proprio.position
        here = (*observation.proprio.ee, q[self._gripper])
        if not self._plan:
            self._replan(now, here, 0)
        planned, index = self._at(now)
        if max(abs(planned[0] - here[0]), abs(planned[1] - here[1]), abs(planned[2] - here[2])) > REPLAN:
            self._replan(now, here, min(index, len(self._waypoints) - 1))
            planned, index = self._at(now)
        if index == len(self._waypoints):
            last = self._waypoints[-1]
            if math.hypot(last[0] - here[0], last[1] - here[1]) <= REACHED[0] and abs(last[2] - here[2]) <= REACHED[1]:
                self._report = Report(Claim.DONE, f"trajectory through {len(self._waypoints)} waypoints ended")
                return None
        joints = [v for i, v in enumerate(q) if i != self._gripper]
        rows = []
        for k in range(HORIZON):
            (x, z, g), _ = self._at(now + k * DT)
            solution = self._kin.ik(x, z, joints)
            if solution is None:
                self._report = Report(Claim.GAVE_UP, f"({x:.3f}, {z:.3f}) is out of reach")
                return None
            joints = solution
            rows.append(joints[: self._gripper] + [g] + joints[self._gripper :])
        self._seq += 1
        b = self._body
        return ActionChunk.make(self._seq, b.action_space, b.dims, now, DT, rows, now + (HORIZON - 1) * DT, self.source)

    def report(self) -> Report | None:
        return self._report


@dataclass(frozen=True)
class Stall:
    """S1 computes nothing during this window: inference hung, in the simulation."""

    when: Trigger


class SimS1Host:
    """Steps the attached policy at the S1 rate, holds each chunk for the
    simulated inference latency (a chunk computed at t reaches S0 at t +
    latency), and hands it straight to S0. Never through the runtime."""

    def __init__(self, s0: SimS0, observations: Latest[Observation], latency: Ns, stalls: Sequence[Stall], world: World) -> None:
        self.s0 = s0
        self.observations = observations
        self.latency = latency
        self.stalls = stalls
        self.world = world
        self.policy: S1Policy | None = None
        self.failure: str | None = None
        self._in_flight: deque[tuple[Ns, ActionChunk]] = deque()

    def attach(self, policy: S1Policy) -> None:
        self.policy, self.failure = policy, None
        self._in_flight.clear()

    def detach(self) -> None:
        self.policy = None
        self._in_flight.clear()

    def error(self) -> str | None:
        return self.failure

    def step(self, now: Ns) -> None:
        if self.policy is None or self.failure or any(s.when.active(self.world, now) for s in self.stalls):
            return
        try:
            chunk = self.policy.step(now, self.observations.get())
        except Exception as e:  # noqa: BLE001 - a crashing policy fails its execution, never S0
            self.failure = f"{type(e).__name__}: {e}"
            return
        if chunk is not None:
            self._in_flight.append((now + self.latency, chunk))

    def deliver(self, now: Ns) -> None:
        while self._in_flight and self._in_flight[0][0] <= now:
            _, chunk = self._in_flight.popleft()
            try:
                self.s0.submit_chunk(chunk, now)
            except ChunkRejected as e:
                # S0 counts rejected chunks; the runtime sees the count in S0's status.
                log.debug("S0 rejected chunk %d: %s", chunk.seq, e)
