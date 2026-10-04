"""The runtime's world state: a belief built from observations, never ground truth.

Static geometry (surfaces, regions, object sizes) comes from the scene
configuration. Movable entities get the latest estimate from observations,
with its provenance: observation id, timestamp, source and confidence.
Predicates evaluate to TRUE, FALSE or UNKNOWN, with the observations they rest
on. In memory only; snapshots go into traces.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from omarchy_runtime.contracts import Goal, Json, Ns, Observation, Proprioception, Truth

PREDICATES = {"in": 2, "on": 2, "held": 1}


@dataclass(frozen=True)
class Region:
    """An axis-aligned box in the x-z plane, such as the drying rack."""

    x0: float
    x1: float
    z0: float
    z1: float

    def contains(self, x: float, z: float, margin: float = 0.0) -> bool:
        """Inside, at least `margin` from every side (a negative margin widens the box)."""
        return self.x0 + margin <= x <= self.x1 - margin and self.z0 + margin <= z <= self.z1 - margin

    def to_json(self) -> Json:
        return {"x0": self.x0, "x1": self.x1, "z0": self.z0, "z1": self.z1}


@dataclass(frozen=True)
class Surface:
    """A horizontal support from x0 to x1 whose top is at height z."""

    x0: float
    x1: float
    z: float

    def to_json(self) -> Json:
        return {"x0": self.x0, "x1": self.x1, "z": self.z}


@dataclass(frozen=True)
class ObjectSpec:
    """A movable object's size, in metres."""

    width: float
    height: float

    def to_json(self) -> Json:
        return {"width": self.width, "height": self.height}


@dataclass(frozen=True)
class Scene:
    regions: Mapping[str, Region]
    surfaces: Mapping[str, Surface]
    objects: Mapping[str, ObjectSpec]


@dataclass(frozen=True)
class Estimate:
    x: float
    z: float
    confidence: float
    observation: str
    t: Ns
    source: str

    def to_json(self) -> Json:
        return {
            "x": self.x,
            "z": self.z,
            "confidence": self.confidence,
            "observation": self.observation,
            "t": self.t,
            "source": self.source,
        }


@dataclass(frozen=True)
class Evaluation:
    truth: Truth
    evidence: tuple[str, ...]
    reason: str


class WorldState:
    """Belief about the world. Estimates older than `since`, or less confident
    than `min_confidence`, count as no estimate. Positions within `margin` of
    a boundary are UNKNOWN rather than guessed."""

    def __init__(self, scene: Scene, *, margin: float, min_confidence: float, grasp_radius: float) -> None:
        self.scene = scene
        self.margin = margin
        self.min_confidence = min_confidence
        self.grasp_radius = grasp_radius
        self.version = 0
        self._estimates: dict[str, Estimate] = {}
        self._proprio: tuple[Proprioception, str, Ns] | None = None

    def update(self, observation: Observation) -> None:
        for d in observation.detections:
            self._estimates[d.entity] = Estimate(d.x, d.z, d.confidence, observation.id, observation.t, observation.source)
        if observation.proprio is not None:
            self._proprio = (observation.proprio, observation.id, observation.t)
        self.version += 1

    def estimate(self, entity: str, since: Ns | None = None) -> Estimate | None:
        e = self._estimates.get(entity)
        if e is None or e.confidence < self.min_confidence or (since is not None and e.t < since):
            return None
        return e

    def proprio(self, since: Ns | None = None) -> tuple[Proprioception, str] | None:
        if self._proprio is None or (since is not None and self._proprio[2] < since):
            return None
        return self._proprio[0], self._proprio[1]

    def evaluate(self, goal: Goal, since: Ns | None = None) -> Evaluation:
        p, a = goal.predicate, goal.args
        if PREDICATES.get(p) != len(a):
            return Evaluation(Truth.UNKNOWN, (), f"no predicate {p} with {len(a)} argument(s)")
        if p == "held":
            return self._held(a[0], since)
        if p == "in":
            return self._in(a[0], a[1], since)
        return self._on(a[0], a[1], since)

    def _missing(self, entity: str, since: Ns | None) -> Evaluation:
        e = self._estimates.get(entity)
        if e is None:
            why = f"no estimate of {entity}"
        elif e.confidence < self.min_confidence:
            why = f"{entity} last seen with confidence {e.confidence:.2f} < {self.min_confidence:.2f} ({e.observation})"
        else:
            why = f"no observation of {entity} since t={since} ns (last {e.observation} at t={e.t} ns)"
        return Evaluation(Truth.UNKNOWN, (), why)

    def _held(self, entity: str, since: Ns | None) -> Evaluation:
        e = self.estimate(entity, since)
        if e is None:
            return self._missing(entity, since)
        p = self.proprio(since)
        if p is None:
            return Evaluation(Truth.UNKNOWN, (e.observation,), "no proprioception: cannot tell whether it is held")
        proprio, obs = p
        closed = proprio.value("gripper") < 0.5
        distance = math.hypot(e.x - proprio.ee[0], e.z - proprio.ee[1])
        held = closed and distance <= self.grasp_radius
        reason = (
            f"{entity} at ({e.x:.3f}, {e.z:.3f}), gripper {'closed' if closed else 'open'} "
            f"at ({proprio.ee[0]:.3f}, {proprio.ee[1]:.3f}), {distance:.3f} m apart"
        )
        return Evaluation(Truth.TRUE if held else Truth.FALSE, tuple(dict.fromkeys((e.observation, obs))), reason)

    def _in(self, entity: str, region: str, since: Ns | None) -> Evaluation:
        r = self.scene.regions.get(region)
        if r is None:
            return Evaluation(Truth.UNKNOWN, (), f"no region named {region}")
        held = self._held(entity, since)
        if held.truth is not Truth.FALSE:
            truth = Truth.FALSE if held.truth is Truth.TRUE else Truth.UNKNOWN
            return Evaluation(truth, held.evidence, f"held: {held.reason}" if held.truth is Truth.TRUE else held.reason)
        e = self.estimate(entity, since)
        assert e is not None  # _held found it
        where = f"{entity} at ({e.x:.3f}, {e.z:.3f}) [{e.observation}]"
        if r.contains(e.x, e.z, self.margin):
            return Evaluation(Truth.TRUE, held.evidence, f"{where} inside {region}")
        if not r.contains(e.x, e.z, -self.margin):
            return Evaluation(Truth.FALSE, held.evidence, f"{where} outside {region}")
        return Evaluation(Truth.UNKNOWN, held.evidence, f"{where} within {self.margin} m of the edge of {region}")

    def _on(self, entity: str, surface: str, since: Ns | None) -> Evaluation:
        s, spec = self.scene.surfaces.get(surface), self.scene.objects.get(entity)
        if s is None or spec is None:
            return Evaluation(Truth.UNKNOWN, (), f"no surface named {surface}" if s is None else f"no object named {entity}")
        held = self._held(entity, since)
        if held.truth is not Truth.FALSE:
            truth = Truth.FALSE if held.truth is Truth.TRUE else Truth.UNKNOWN
            return Evaluation(truth, held.evidence, f"held: {held.reason}" if held.truth is Truth.TRUE else held.reason)
        e = self.estimate(entity, since)
        assert e is not None
        where = f"{entity} at ({e.x:.3f}, {e.z:.3f}) [{e.observation}]"
        dz = abs(e.z - (s.z + spec.height / 2))
        m = self.margin
        if s.x0 + m <= e.x <= s.x1 - m and dz <= m:
            return Evaluation(Truth.TRUE, held.evidence, f"{where} resting on {surface}")
        if e.x < s.x0 - m or e.x > s.x1 + m or dz > 2 * m:
            return Evaluation(Truth.FALSE, held.evidence, f"{where} not on {surface}")
        return Evaluation(Truth.UNKNOWN, held.evidence, f"{where} at the edge of {surface}")

    def snapshot(self) -> Json:
        entities: Json = {}
        for name, s in self.scene.surfaces.items():
            entities[name] = {"kind": "surface", "geometry": s.to_json(), "estimate": None}
        for name, r in self.scene.regions.items():
            entities[name] = {"kind": "region", "geometry": r.to_json(), "estimate": None}
        for name, o in self.scene.objects.items():
            e = self._estimates.get(name)
            entities[name] = {"kind": "object", "geometry": o.to_json(), "estimate": e.to_json() if e else None}
        for name, e in sorted(self._estimates.items()):
            if name not in entities:
                entities[name] = {"kind": "unknown", "geometry": None, "estimate": e.to_json()}
        robot = None
        if self._proprio is not None:
            proprio, obs, t = self._proprio
            robot = proprio.to_json() | {"observation": obs, "t": t}
        return {"version": self.version, "entities": entities, "robot": robot}
