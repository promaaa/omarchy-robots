"""Ground truth of the simulated kitchen. No physics engine, a few rules:

- a cup attaches when the gripper closes (crosses 0.5) within 2 cm of it;
- a released or slipping cup settles on the highest support below it
  (counter, table, or the floor);
- an injected slip detaches the held cup;
- injected scene changes: a "human" moves an object that is not held by
  some centimetres (`move`), or an object that was absent appears (`appear`),
  settling on the support below; each is a ground-truth event;
- an injected actuator fault: while a `stuck` window lasts, the gripper does
  not move, whatever S0 commands.

Cups do not collide with each other or with the arm's links. Only simulator
components and the evaluation harness read this; the runtime never does.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from omarchy_runtime.belief import Scene
from omarchy_runtime.contracts import BodySpec, Goal, Ns, seconds
from omarchy_runtime.sim.kinematics import Kinematics

ATTACH_RADIUS = 0.02
TOLERANCE = 1e-9  # numerical slack on limits
PENETRATION = 0.005  # how far below a surface top a held cup may dip before it counts


@dataclass(frozen=True)
class Trigger:
    """A fault window: `delay` after the first `after` event ("start", "moving", "grasp" or "release"), for `duration`."""

    after: str
    delay: Ns
    duration: Ns

    def active(self, world: World, now: Ns) -> bool:
        t0 = world.first.get(self.after)
        return t0 is not None and t0 + self.delay <= now < t0 + self.delay + self.duration


@dataclass(frozen=True)
class Change:
    """A one-shot scene change, `delay` after the first `after` event: `move`
    shifts `entity` by `x` metres, `appear` puts it at x = `x`."""

    after: str
    delay: Ns
    kind: str
    entity: str
    x: float


class World:
    def __init__(
        self,
        scene: Scene,
        positions: dict[str, float],
        body: BodySpec,
        slips: Sequence[Trigger] = (),
        changes: Sequence[Change] = (),
        stuck: Sequence[Trigger] = (),
    ) -> None:
        self.scene = scene
        self.body = body
        self.kin = Kinematics(body)
        self.gripper = body.dims.index("gripper")
        self.q = list(body.home)
        self.ee = self.kin.ee(self.q)
        self.cups: dict[str, list[float]] = {}
        for name, x in positions.items():
            self.cups[name] = [x, math.inf]  # dropped from above onto whatever is below
            self._settle(name)
        self.held: str | None = None
        self._offset = (0.0, 0.0)
        self._slips = list(slips)
        self._changes = list(changes)
        self._stuck = list(stuck)
        self.first: dict[str, Ns] = {"start": 0}
        self.events: list[tuple[Ns, str, str]] = []
        self.violations: list[tuple[Ns, str]] = []
        self._last: Ns | None = None

    def _note(self, now: Ns, kind: str, detail: str) -> None:
        self.first.setdefault(kind, now)
        self.events.append((now, kind, detail))

    def _violate(self, now: Ns, what: str) -> None:
        if len(self.violations) < 1000:
            self.violations.append((now, what))

    def _settle(self, cup: str) -> None:
        x, z = self.cups[cup]
        half = self.scene.objects[cup].height / 2
        tops = [s.z for s in self.scene.surfaces.values() if s.x0 <= x <= s.x1 and s.z <= z - half + PENETRATION]
        self.cups[cup][1] = max(tops, default=0.0) + half

    def _change(self, now: Ns) -> None:
        for c in [c for c in self._changes if (t0 := self.first.get(c.after)) is not None and now >= t0 + c.delay]:
            self._changes.remove(c)
            if c.kind == "appear":
                self.cups[c.entity] = [c.x, math.inf]
            elif self.held == c.entity or c.entity not in self.cups:
                self._note(now, "move refused", f"{c.entity} is held or absent")
                continue
            else:
                self.cups[c.entity] = [self.cups[c.entity][0] + c.x, math.inf]
            self._settle(c.entity)
            self._note(now, c.kind, f"{c.entity} at ({self.cups[c.entity][0]:.3f}, {self.cups[c.entity][1]:.3f})")

    def apply(self, now: Ns, command: Sequence[float]) -> None:
        """S0's command reaches the actuators (ideal tracking, but for a stuck
        gripper). Checks every limit against ground truth and records any
        violation."""
        if self._changes:
            self._change(now)
        b = self.body
        dt = seconds(now - self._last) if self._last is not None else None
        for i, v in enumerate(command):
            if not b.lower[i] - TOLERANCE <= v <= b.upper[i] + TOLERANCE:
                self._violate(now, f"{b.dims[i]} = {v:.4f} outside [{b.lower[i]}, {b.upper[i]}]")
            if dt and abs(v - self.q[i]) > b.max_velocity[i] * dt * (1 + 1e-6) + TOLERANCE:
                self._violate(now, f"{b.dims[i]} moves at {abs(v - self.q[i]) / dt:.3f}/s, limit {b.max_velocity[i]}")
        was_open = self.q[self.gripper] >= 0.5
        if "moving" not in self.first and any(abs(v - q) > 1e-6 for v, q in zip(command, self.q, strict=True)):
            self.first["moving"] = now  # the body's first motion: S0 executing a first chunk
        gripper = self.q[self.gripper]
        self.q = list(command)
        if any(t.active(self, now) for t in self._stuck):
            self.q[self.gripper] = gripper
        self._last = now
        x, z = self.ee = self.kin.ee(self.q)
        if not b.reaches(x, z, TOLERANCE):
            self._violate(now, f"gripper at ({x:.4f}, {z:.4f}) outside the workspace")
        closed = self.q[self.gripper] < 0.5
        if closed and was_open and self.held is None:
            near = [(math.hypot(cx - x, cz - z), name) for name, (cx, cz) in self.cups.items()]
            distance, name = min(near, default=(math.inf, ""))
            if distance <= ATTACH_RADIUS:
                self.held = name
                self._offset = (self.cups[name][0] - x, self.cups[name][1] - z)
                self._note(now, "grasp", name)
        held = self.held
        if held is None:
            return
        if not closed:
            self._drop(now, "release")
            return
        self.cups[held] = [x + self._offset[0], z + self._offset[1]]
        bottom = self.cups[held][1] - self.scene.objects[held].height / 2
        for name, s in self.scene.surfaces.items():
            if s.x0 <= self.cups[held][0] <= s.x1 and bottom < s.z - PENETRATION:
                self._violate(now, f"held {held} pushed {s.z - bottom:.3f} m into {name}")
        if any(t.active(self, now) for t in self._slips):
            self._slips = [t for t in self._slips if not t.active(self, now)]
            self._drop(now, "slip")

    def _drop(self, now: Ns, kind: str) -> None:
        assert self.held is not None
        cup, self.held = self.held, None
        self._settle(cup)
        self._note(now, kind, f"{cup} settles at ({self.cups[cup][0]:.3f}, {self.cups[cup][1]:.3f})")

    def truth(self, goal: Goal) -> bool | None:
        """Ground truth of a goal predicate; None for predicates the simulator does not model."""
        a = goal.args
        if goal.predicate == "held" and len(a) == 1:
            return self.held == a[0]
        if len(a) != 2 or a[0] not in self.cups:
            return None
        x, z = self.cups[a[0]]
        if goal.predicate == "in" and a[1] in self.scene.regions:
            return self.held != a[0] and self.scene.regions[a[1]].contains(x, z)
        if goal.predicate == "on" and a[1] in self.scene.surfaces:
            s = self.scene.surfaces[a[1]]
            rest = s.z + self.scene.objects[a[0]].height / 2
            return self.held != a[0] and s.x0 <= x <= s.x1 and abs(z - rest) < 1e-6
        return None
