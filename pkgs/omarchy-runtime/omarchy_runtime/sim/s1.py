"""S1 in the simulation: a scripted pick-and-place policy, and the host that runs it.

The policy is deliberately naive. It plans waypoints from the belief it is
given at start, tracks them with proprioception, never checks the grasp, and
claims DONE when its last waypoint is reached: a policy whose self-report can
be wrong, as a learned one's can.
"""

from __future__ import annotations

import dataclasses
import heapq
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omarchy_runtime.belief import free_spots, takes_spot
from omarchy_runtime.contracts import (
    ActionChunk,
    BodySpec,
    ChunkCorrector,
    ChunkRejected,
    Claim,
    ContractError,
    Goal,
    InferenceContext,
    InferenceTrigger,
    IntentChunk,
    Json,
    Ns,
    Observation,
    Report,
    S1Policy,
    ns,
)
from omarchy_runtime.extensions import NoCorrection, Periodic
from omarchy_runtime.s0 import S0Loop
from omarchy_runtime.sched import Latest
from omarchy_runtime.sim.kinematics import Kinematics
from omarchy_runtime.sim.world import Timeline, Trigger

SPEED = 0.25  # m/s of the gripper's reference point along each segment
GRIP_RATE = 2.0  # gripper travel per second
HORIZON = 10  # rows per chunk
DT = ns(0.05)  # between rows
REACHED = (0.003, 0.02)  # distance (m) and gripper tolerance at the last waypoint
HISTORY = ns(0.5)  # how much of S0's history a trigger sees
REPLAN = 0.03  # m (or gripper travel) off the plan: plan again from where the body is. Above
# SPEED x inference latency, the lag at the start of every plan.
MOVED = 0.015  # m: the target is seen this far from where the plan grasps it: plan the grasp again
HELD = 0.03  # m: an object this close to a closed gripper is held already
OPEN, CLOSED = 1.0, 0.0

log = logging.getLogger(__name__)

Point = tuple[float, float, float]  # x, z, gripper


class ScriptedPickPlace:
    """Achieves in(object, container) on any body this module has kinematics for.

    It follows one time-indexed plan through its waypoints, so consecutive
    chunks agree; when proprioception shows the body off the plan at the time
    it was observed (after a stall or a fallback), it plans the remaining
    waypoints again from there.

    It watches its target, simply and explicitly, in every observation it
    receives: before the grasp, the target seen more than MOVED from where
    the plan grasps it means the scene changed, and S1 plans the grasp again
    there, without S2. Before it places, another object seen in its place
    spot is a precondition it cannot meet: it reports GAVE_UP ("blocked")
    for S2 to choose another spot. S2 names a spot with conditioning
    {"place_x": x}; a body already holding the object goes straight to it.
    """

    def __init__(self, implementation: str) -> None:
        self.source = implementation
        self._report: Report | None = None
        self._waypoints: list[Point] = []
        self._plan: list[tuple[Ns, Ns, Point, Point, int]] = []  # start, end, from, to, waypoint index
        self._seq = 0
        self._picking = True  # the waypoints start with the pick
        self.replans: list[tuple[Ns, str]] = []  # when S1 changed its plan for the scene, and why

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        obj, container = goal.args
        entities = belief["entities"]
        estimate = entities[obj]["estimate"]
        if estimate is None:
            raise ValueError(f"no estimate of {obj} in the belief")
        self._obj, self._width = obj, entities[obj]["geometry"]["width"]
        height = entities[obj]["geometry"]["height"]
        self._region = region = entities[container]["geometry"]
        self._body, self._kin = body, Kinematics(body)
        self._gripper = body.dims.index("gripper")
        x0, x1, z0, z1 = body.workspace
        self._transit, self._floor = z1 - 0.10, z0 + 0.002
        # S2's spot, else the first spot in the container, left to right, that the belief shows free.
        others = [e["estimate"] for n, e in entities.items() if n != obj and e["kind"] == "object" and e["estimate"]]
        place_x = float(conditioning["place_x"]) if "place_x" in conditioning else free_spots(region, self._width, others)[0]
        self._place = (place_x, max(region["z0"] + height / 2 + 0.01, self._floor))  # released 1 cm above the bottom
        robot = belief.get("robot")
        held = False
        if robot is not None:
            closed = robot["position"][robot["dims"].index("gripper")] < 0.5
            held = closed and math.hypot(estimate["x"] - robot["ee"][0], estimate["z"] - robot["ee"][1]) <= HELD
        self._picking = not held
        self._waypoints = self._route(estimate["x"], estimate["z"])

    def _route(self, ox: float, oz: float) -> list[Point]:
        """The waypoints: pick at (ox, oz) unless the object is held, then place."""
        transit, (px, pz) = self._transit, self._place
        grasp = max(oz, self._floor)
        pick = [(ox, transit, OPEN), (ox, grasp, OPEN), (ox, grasp, CLOSED), (ox, transit, CLOSED)] if self._picking else []
        return [*pick, (px, transit, CLOSED), (px, pz, CLOSED), (px, pz, OPEN), (px, transit, OPEN)]

    def _watch(self, observation: Observation, index: int, here: Point, seen: Ns) -> None:
        """The scene against the plan: the target moved before the grasp
        (plan again), or the place spot is taken before the release (give up)."""
        seen_now = {d.entity: d for d in observation.detections}
        target = seen_now.get(self._obj)
        if self._picking and index <= 1 and target is not None and abs(target.x - self._waypoints[0][0]) > MOVED:
            self.replans.append((seen, f"{self._obj} moved to x = {target.x:.3f}"))
            self._waypoints = self._route(target.x, target.z)
            self._replan(seen, here, 0)
            return
        place = len(self._waypoints) - 4  # the first waypoint of the placing part
        px, r = self._place[0], self._region
        if place <= index <= place + 1:
            for d in observation.detections:
                if d.entity != self._obj and takes_spot(px, self._width, r, d.x, d.z):
                    self._report = Report(Claim.GAVE_UP, f"blocked: {d.entity} at ({d.x:.3f}, {d.z:.3f}) in the place spot x = {px:.3f}")
                    return

    def _judged_at(self, observation: Observation, now: Ns) -> Ns:
        """The time at which S1 compares an observation with its plan: when it
        was taken. (Comparing at `now` made an old observation look off the
        plan: v0.0.2 part 2's first fix, tests/test_timing.py.)"""
        return observation.t

    def _reach_spot_first(self, spot: Point, here: Point) -> bool:
        """A gripper change happens in place: a replan into a grasp or release
        reaches its spot first, unless the body is there already."""
        return math.hypot(spot[0] - here[0], spot[1] - here[1]) > REACHED[0]

    def _replan(self, now: Ns, here: Point, first: int) -> None:
        if 0 < first < len(self._waypoints) and self._waypoints[first][:2] == self._waypoints[first - 1][:2]:
            if self._reach_spot_first(self._waypoints[first], here):
                first -= 1
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
        ee = observation.proprio.ee
        here = (ee[0], ee[1], q[self._gripper])
        seen = self._judged_at(observation, now)  # the body was `here` then, not at `now`
        if not self._plan:
            self._replan(seen, here, 0)
        planned, index = self._at(seen)
        if max(abs(planned[0] - here[0]), abs(planned[1] - here[1]), abs(planned[2] - here[2])) > REPLAN:
            self._replan(seen, here, min(index, len(self._waypoints) - 1))
        self._watch(observation, self._at(seen)[1], here, seen)
        if self._report is not None:
            return None
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
        return ActionChunk.make(
            self._seq, b.action_space, b.dims, now, DT, rows, now + (HORIZON - 1) * DT, self.source, observed_at=observation.t
        )

    def report(self) -> Report | None:
        return self._report


@dataclass(frozen=True)
class Stall:
    """S1 computes nothing during this window: inference hung, in the simulation."""

    when: Trigger


class SimS1Host:
    """Asks its InferenceTrigger at the S1 rate whether the attached policy
    infers now, holds each chunk for the simulated inference latency (a
    chunk computed at t reaches S0 at t + latency, the latency drawn for
    each chunk), passes it through its ChunkCorrector, and hands it straight
    to S0, in the order the chunks arrive. Never through the runtime. A
    policy, trigger or corrector that raises, or a corrector that changes a
    chunk's identity, fails the execution, never S0."""

    def __init__(
        self,
        s0: S0Loop,
        observations: Latest[Observation],
        latency: Callable[[], Ns],
        stalls: Sequence[Stall],
        world: Timeline,
        *,
        period: Ns,
        corrector: ChunkCorrector | None = None,
        trigger: InferenceTrigger | None = None,
    ) -> None:
        self.s0 = s0
        self.observations = observations
        self.latency = latency
        self.stalls = stalls
        self.world = world
        self.period = period
        self.corrector: ChunkCorrector = corrector or NoCorrection()
        self.trigger: InferenceTrigger = trigger or Periodic()
        self.policy: S1Policy | None = None
        self.failure: str | None = None
        self.inferences = 0
        self._measured: Ns = 0  # the last inference latency
        self._in_flight: list[tuple[Ns, int, Ns, ActionChunk | IntentChunk]] = []  # a heap: arrival, sequence, inference start, chunk

    def attach(self, policy: S1Policy) -> None:
        self.policy, self.failure = policy, None
        self._in_flight = []

    def detach(self) -> None:
        self.policy = None
        self._in_flight = []

    def error(self) -> str | None:
        return self.failure

    def step(self, now: Ns) -> None:
        if self.policy is None or self.failure or any(s.when.active(self.world, now) for s in self.stalls):
            return
        try:
            context = InferenceContext(
                now, self.s0.status(), self.s0.history(now - HISTORY), len(self._in_flight), self._measured, self.period
            )
            if not self.trigger.due(context):
                return
            self.inferences += 1
            chunk = self.policy.step(now, self.observations.get())
            if isinstance(chunk, IntentChunk) and self.corrector.id != NoCorrection.id:
                raise ContractError(f"corrector {self.corrector.id} corrects v1 action chunks, and this S1 sends intent chunks")
        except Exception as e:  # noqa: BLE001 - a crashing policy or trigger fails its execution, never S0
            self.failure = f"{type(e).__name__}: {e}"
            return
        if chunk is not None:
            heapq.heappush(self._in_flight, (now + self.latency(), chunk.seq, now, chunk))

    def deliver(self, now: Ns) -> None:
        while self._in_flight and self._in_flight[0][0] <= now:
            _, _, started, chunk = heapq.heappop(self._in_flight)
            self._measured = now - started
            if isinstance(chunk, IntentChunk):
                self._submit(chunk.inferred(self._measured), now)  # no corrector: the correctors take v1 chunks (checked at start)
                continue
            chunk = dataclasses.replace(chunk, inference_ns=self._measured)
            try:
                corrected = self.corrector.correct(chunk, self.s0.history(chunk.observed_at), self.s0.status(), now)
                same = (corrected.seq, corrected.source, corrected.action_space, corrected.observed_at)
                if same != (chunk.seq, chunk.source, chunk.action_space, chunk.observed_at):
                    raise ContractError(f"the corrector changed chunk {chunk.seq}'s identity")
            except Exception as e:  # noqa: BLE001 - a failing corrector fails its execution, never S0
                self.failure = f"corrector {self.corrector.id}: {type(e).__name__}: {e}"
                self._in_flight = []
                return
            self._submit(corrected, now)

    def _submit(self, chunk: ActionChunk | IntentChunk, now: Ns) -> None:
        try:
            self.s0.submit_chunk(chunk, now)
        except ChunkRejected as e:
            # S0 counts rejected chunks and keeps the last refusal; the runtime records it from S0's status.
            log.debug("S0 rejected chunk %d: %s", chunk.seq, e)
