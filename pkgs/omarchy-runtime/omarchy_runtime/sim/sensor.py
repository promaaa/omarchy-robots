"""The simulated observation source: structured detections with seeded
Gaussian noise, and proprioception. No images. An occlusion hides every cup
inside its region while it lasts. A ghost is a second detector that, while
it lasts, reports one entity `x` metres from where it is: a source that
contradicts the camera."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from omarchy_runtime.belief import Region
from omarchy_runtime.contracts import Detection, Ns, Observation, Proprioception
from omarchy_runtime.sim.world import Trigger, World


@dataclass(frozen=True)
class Occlusion:
    region: Region
    when: Trigger


@dataclass(frozen=True)
class Ghost:
    entity: str
    x: float
    when: Trigger


class Sensor:
    def __init__(
        self,
        world: World,
        rng: np.random.Generator,
        *,
        source: str,
        noise: float,
        confidence: float,
        occlusions: list[Occlusion],
        ghosts: list[Ghost] | None = None,
    ) -> None:
        self.world = world
        self.rng = rng
        self.source = source
        self.noise = noise
        self.confidence = confidence
        self.occlusions = occlusions
        self.ghosts = ghosts or []
        self.count = 0

    def ghost(self, now: Ns) -> Observation | None:
        """The second detector's observation, while a ghost is active."""
        w = self.world
        seen = [
            Detection(g.entity, w.cups[g.entity][0] + g.x, w.cups[g.entity][1], self.confidence)
            for g in self.ghosts
            if g.when.active(w, now) and g.entity in w.cups
        ]
        if not seen:
            return None
        self.count += 1
        return Observation(f"obs-{self.count}", now, f"{self.source}.b", tuple(seen), None)

    def observe(self, now: Ns) -> Observation:
        w = self.world
        hidden = [o.region for o in self.occlusions if o.when.active(w, now)]
        detections = []
        for name in sorted(w.cups):
            x, z = w.cups[name]
            if any(r.contains(x, z) for r in hidden):
                continue
            dx, dz = self.rng.normal(0.0, self.noise, 2)
            detections.append(Detection(name, round(x + float(dx), 6), round(z + float(dz), 6), self.confidence))
        self.count += 1
        proprio = Proprioception(w.body.dims, tuple(w.q), w.ee)
        return Observation(f"obs-{self.count}", now, self.source, tuple(detections), proprio)
