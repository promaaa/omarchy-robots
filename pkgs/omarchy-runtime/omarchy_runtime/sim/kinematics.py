"""Kinematics of the simulated bodies, from BodySpec.kind and BodySpec.kinematics.

The planar arm keeps its gripper pointing down and its elbow up (the scene
lies to the right of its base), so one inverse solution is continuous over
its whole workspace.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from omarchy_runtime.contracts import BodySpec, ConfigError


class Kinematics:
    def __init__(self, body: BodySpec) -> None:
        self.kind = body.kind
        self.joints = len(body.dims) - 1  # every dimension but the gripper
        if body.kind == "planar_arm":
            try:
                (self.bx, self.bz), (self.l1, self.l2, self.l3) = body.kinematics["base"], body.kinematics["links"]
            except (KeyError, TypeError, ValueError) as e:
                raise ConfigError(f"body {body.id}: planar_arm kinematics need base = [x, z] and links = [l1, l2, l3]") from e
        elif body.kind != "gantry":
            raise ConfigError(f"body {body.id}: unknown kind {body.kind!r}")

    def ee(self, q: Sequence[float]) -> tuple[float, float]:
        """Where the gripper's reference point is, for joint values `q`."""
        if self.kind == "gantry":
            return float(q[0]), float(q[1])
        a1 = q[0]
        a2 = a1 + q[1]
        a3 = a2 + q[2]
        return (
            self.bx + self.l1 * math.cos(a1) + self.l2 * math.cos(a2) + self.l3 * math.cos(a3),
            self.bz + self.l1 * math.sin(a1) + self.l2 * math.sin(a2) + self.l3 * math.sin(a3),
        )

    def ik(self, x: float, z: float, near: Sequence[float]) -> list[float] | None:
        """Joint values that put the gripper at (x, z), closest to `near`; None out of reach."""
        if self.kind == "gantry":
            return [x, z]
        wx, wz = x - self.bx, z + self.l3 - self.bz  # the wrist sits l3 above the gripper
        c2 = (wx * wx + wz * wz - self.l1**2 - self.l2**2) / (2 * self.l1 * self.l2)
        if not -1.0 <= c2 <= 1.0:
            return None
        q2 = -math.acos(c2)
        q1 = math.atan2(wz, wx) - math.atan2(self.l2 * math.sin(q2), self.l1 + self.l2 * math.cos(q2))
        q1 += 2 * math.pi * round((near[0] - q1) / (2 * math.pi))
        return [q1, q2, -math.pi / 2 - q1 - q2]
