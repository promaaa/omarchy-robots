"""The simulated Franka Emika Panda scrubbing a plate, in MuJoCo, on the CPU.

The arm is the official MuJoCo Menagerie model (`franka_emika_panda/
panda_nohand.xml`, Apache-2.0), fetched by Nix at a pinned commit; its path
comes in `OMARCHY_MENAGERIE`. This module builds the scene around it and
never edits the file: the position actuators become torque motors within
the Panda's torque limits, the arm is gravity-compensated (a real Panda
compensates gravity itself under torque control: torque commands exclude
it), a sponge is fixed to the flange, and a table and a square plate, free
to slide, are added. Physics steps every millisecond.

Ground truth, read only by the sensors here and the evaluation harness:
the plate's pose; the dirt, a grid of cells on the plate, a cell clean once
the sponge has pressed on it with at least `f_min_n` for `t_min_s` in all;
the true contact force. The runtime never sees this module. Development
tools only (`omarchy-runtime-tools`): the robot's package has no MuJoCo.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import mujoco
import numpy as np
from numpy.typing import NDArray

from omarchy_runtime.contracts import (
    Array,
    BodySpec,
    ConfigError,
    Coverage,
    Detection,
    Goal,
    Json,
    Ns,
    Observation,
    Proprioception,
    RobotState,
    seconds,
)
from omarchy_runtime.sim.sensor import Occlusion
from omarchy_runtime.sim.world import Trigger

PANDA = "franka_emika_panda/panda_nohand.xml"
STEP = 1_000_000  # ns: the physics step
CONTACT_N = 1.0  # N of true normal force: the sponge touches the plate
PRESSED_N = 60.0  # N: more than this is a violation of the scene's safety


def menagerie() -> Path:
    """The pinned MuJoCo Menagerie, as the tools package provides it."""
    where = os.environ.get("OMARCHY_MENAGERIE")
    if not where or not (Path(where) / PANDA).exists():
        raise ConfigError(f"OMARCHY_MENAGERIE does not name the MuJoCo Menagerie ({where!r}): run through .#omarchy-runtime-tools")
    return Path(where)


def _get(table: Json, key: str) -> Array:
    return np.array(table[key], dtype=np.float64)


def build(world: Json) -> mujoco.MjModel:
    """The scene: the Menagerie Panda, torque-driven and gravity-compensated, a sponge, a table, a plate, its dirt."""
    spec = mujoco.MjSpec.from_file(str(menagerie() / PANDA))
    spec.option.timestep = seconds(STEP)
    spec.option.cone = mujoco.mjtCone.mjCONE_ELLIPTIC
    spec.option.noslip_iterations = 5  # no creep of the plate under static friction
    for a in spec.actuators:
        limit = a.forcerange.copy()
        a.set_to_motor()
        a.ctrlrange, a.ctrllimited = limit, True
    for b in spec.bodies:
        if b.name.startswith("link") or b.name == "attachment":
            b.gravcomp = 1
    t, p, s, d = world["table"], world["plate"], world["sponge"], world["dirt"]
    half = _get(s, "half")
    sponge = spec.body("attachment").add_body(name="sponge", pos=[0, 0, half[2]], gravcomp=1)
    sponge.add_geom(
        name="sponge",
        type=mujoco.mjtGeom.mjGEOM_BOX,
        size=half,
        rgba=[1.0, 0.85, 0.2, 1.0],
        mass=float(s["mass_kg"]),
        friction=[float(s["friction"]), 0.005, 0.0001],
        priority=2,
        solref=[float(s["solref_s"]), 1.0],  # soft: a sponge
    )
    sponge.add_site(name="pad", pos=[0, 0, half[2]])
    w = spec.worldbody
    w.add_light(name="sun", pos=[0.5, -0.5, 2.0], dir=[0, 0.3, -1], type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL)
    w.add_geom(name="floor", type=mujoco.mjtGeom.mjGEOM_PLANE, size=[2, 2, 0.1], rgba=[0.35, 0.37, 0.4, 1])
    w.add_geom(name="table", type=mujoco.mjtGeom.mjGEOM_BOX, pos=_get(t, "center"), size=_get(t, "half"), rgba=[0.55, 0.42, 0.3, 1])
    side, thick = float(p["half"]), float(p["thickness"])
    top = float(t["center"][2] + t["half"][2])
    plate = w.add_body(name="plate", pos=[*p["center"], top + thick / 2])
    plate.add_freejoint(name="plate")
    plate.add_geom(
        name="plate",
        type=mujoco.mjtGeom.mjGEOM_BOX,
        size=[side, side, thick / 2],
        rgba=[0.93, 0.94, 0.95, 1],
        mass=float(p["mass_kg"]),
        friction=[float(p["friction"]), 0.005, 0.0001],
        priority=1,
    )
    for n, (cx, cy) in enumerate(cells(world)):  # the dirt: drawn only, never touched
        size = (2 * (side - float(d["margin_m"]))) / int(d["cells"]) / 2
        plate.add_geom(
            name=f"dirt{n}",
            type=mujoco.mjtGeom.mjGEOM_BOX,
            pos=[cx, cy, thick / 2 + 0.0005],
            size=[size * 0.92, size * 0.92, 0.0005],
            rgba=[0.45, 0.3, 0.12, 1],
            contype=0,
            conaffinity=0,
        )
    return spec.compile()


def cells(world: Json) -> Array:
    """The dirt cells' centres in the plate's frame (x, y), row by row."""
    d, side = world["dirt"], float(world["plate"]["half"])
    n, inner = int(d["cells"]), side - float(d["margin_m"])
    centres = -inner + (np.arange(n) + 0.5) * (2 * inner / n)
    return np.array([(x, y) for y in centres for x in centres])


class PandaWorld:
    """Ground truth of the scrubbing scene. S0's RobotIO (state, scene, apply)
    reads the arm through it; `apply` steps physics up to the tick's time,
    holding the torque, and keeps the dirt and the per-tick log for the
    harness (and the video)."""

    def __init__(
        self, world: Json, body: BodySpec, rng: np.random.Generator, capacity: int, weak: Sequence[tuple[float, Trigger]] = ()
    ) -> None:
        self.cfg = world
        self.body = body
        self.model = build(world)
        self.data = mujoco.MjData(self.model)
        m = self.model
        self.pad = m.site("pad").id
        self.sponge = m.geom("sponge").id
        self.plate_geom = m.geom("plate").id
        self.plate_body = m.body("plate").id
        self.plate_qpos = int(m.jnt_qposadr[m.joint("plate").id])
        self.n = len(body.dims)
        self.data.qpos[: self.n] = body.home
        mujoco.mj_forward(m, self.data)
        self.cells = cells(world)
        self.thick = float(world["plate"]["thickness"])
        self.half = _get(world["sponge"], "half")
        d = world["dirt"]
        self.f_min, self.t_min = float(d["f_min_n"]), float(d["t_min_s"])
        self.dwell = np.zeros(len(self.cells))
        self.clean_at = np.full(len(self.cells), -1, dtype=np.int64)
        self.rng = rng
        f = world["force_sensor"]
        self.noise, self.alpha = float(f["noise_n"]), float(f["filter"])
        self.wrench = np.zeros(6)  # the estimate S0 reads: filtered, noisy, one step old
        self.force = np.zeros(3)  # the true force on the sponge, from the last step
        self.normal = 0.0  # its true normal component between sponge and plate
        self.weak = list(weak)
        self.first: dict[str, Ns] = {"start": 0}
        self.events: list[tuple[Ns, str, str]] = []
        self.violations: list[tuple[Ns, str]] = []
        self.jacp, self.jacr = np.zeros((3, m.nv)), np.zeros((3, m.nv))
        self._stepped1 = False
        self._last: Ns = 0
        self.plate0 = self.plate_pose()
        self.q = list(body.home)  # for the planar-world duck type: the arm's joints
        self.ticks = 0
        self.log_t = np.zeros(capacity, dtype=np.int64)
        self.log_qpos = np.zeros((capacity, m.nq))
        self.log_force = np.zeros(capacity)  # true normal force, N
        self.log_ee = np.zeros((capacity, 3))

    # Ground truth.

    def plate_pose(self) -> Array:
        return np.array(self.data.qpos[self.plate_qpos : self.plate_qpos + 7])

    def coverage(self) -> float:
        return float(np.mean(self.clean_at >= 0))

    def truth(self, goal: Goal) -> bool | None:
        if goal.predicate == "clean" and goal.args == ("plate",):
            return self.coverage() >= float(self.cfg["dirt"]["clean_share"])
        return None

    def cells_world(self) -> Array:
        """The cells' centres in the world, on the plate's top."""
        pose = self.plate_pose()
        rot = np.zeros(9)
        mujoco.mju_quat2Mat(rot, pose[3:7])
        local = np.c_[self.cells, np.full(len(self.cells), self.thick / 2)]
        out: Array = local @ rot.reshape(3, 3).T + pose[:3]
        return out

    def under_sponge(self, near: float = 0.0) -> NDArray[np.bool_]:
        """Which cells lie under the sponge's footprint, the sponge within `near` of the plate's top."""
        g = self.sponge
        local = (self.cells_world() - self.data.geom_xpos[g]) @ self.data.geom_xmat[g].reshape(3, 3)
        inside = (np.abs(local[:, 0]) <= self.half[0]) & (np.abs(local[:, 1]) <= self.half[1])
        out: NDArray[np.bool_] = inside & (np.abs(local[:, 2]) <= self.half[2] + near)
        return out

    # S0's RobotIO.

    def _step1(self) -> None:
        if not self._stepped1:
            mujoco.mj_step1(self.model, self.data)
            self._stepped1 = True

    def state(self, now: Ns) -> RobotState:
        self._step1()
        d, n = self.data, self.n
        mujoco.mj_jacSite(self.model, d, self.jacp, self.jacr, self.pad)
        jac = np.vstack([self.jacp[:, :n], self.jacr[:, :n]])
        quat = np.zeros(4)
        mujoco.mju_mat2Quat(quat, d.site_xmat[self.pad])
        q, dq = np.array(d.qpos[:n]), np.array(d.qvel[:n])
        return RobotState(
            now,
            q,
            dq,
            np.array(d.actuator_force[:n]),
            np.concatenate((d.site_xpos[self.pad], quat)),
            jac @ dq,
            self.wrench.copy(),
            jac,
            np.array(d.qfrc_bias[:n] - d.qfrc_gravcomp[:n]),  # Coriolis and centrifugal: gravity is compensated
        )

    def scene(self, now: Ns) -> Mapping[str, Array]:
        return {"pose:plate": self.plate_pose()}

    def apply(self, now: Ns, command: Array) -> None:
        scale = min((s for s, t in self.weak if t.active(self, now)), default=1.0)
        self.data.ctrl[: self.n] = command * scale
        steps = (now - self._last) // STEP
        for k in range(steps):
            if k == 0:
                self._step1()
                mujoco.mj_step2(self.model, self.data)
            else:
                mujoco.mj_step(self.model, self.data)
            self._stepped1 = False
            self._last += STEP
            self._contact(self._last)
        self.q = self.data.qpos[: self.n].tolist()
        i = self.ticks
        if i < len(self.log_t):
            self.log_t[i], self.log_qpos[i], self.log_force[i], self.log_ee[i] = (
                now,
                self.data.qpos,
                self.normal,
                self.data.site_xpos[self.pad],
            )
        self.ticks += 1

    def _contact(self, now: Ns) -> None:
        """The true forces of the step just taken, the force estimate S0 reads next, the dirt, and violations."""
        d, m, c6 = self.data, self.model, np.zeros(6)
        force, normal = np.zeros(3), 0.0
        for i in range(d.ncon):
            c = d.contact[i]
            if self.sponge not in (c.geom1, c.geom2):
                continue
            mujoco.mj_contactForce(m, d, i, c6)
            f = c.frame.reshape(3, 3).T @ c6[:3]  # on geom2, in the world
            force += f if c.geom2 == self.sponge else -f
            if self.plate_geom in (c.geom1, c.geom2):
                normal += c6[0]
        self.force, self.normal = force, normal
        self.wrench[:3] += self.alpha * (force + self.rng.normal(0.0, self.noise, 3) - self.wrench[:3])  # no torque estimate: zeros
        if normal >= CONTACT_N and "contact" not in self.first:
            self.first["contact"] = now
            self.events.append((now, "contact", f"sponge on the plate, {normal:.1f} N"))
        if "moving" not in self.first and np.abs(d.qvel[: self.n]).max() > 0.01:
            self.first["moving"] = now
        if normal >= self.f_min:
            under = self.under_sponge()
            self.dwell[under] += seconds(STEP)
            done = (self.dwell >= self.t_min) & (self.clean_at < 0)
            self.clean_at[done] = now
        if normal > PRESSED_N and len(self.violations) < 1000:
            self.violations.append((now, f"the sponge pressed {normal:.1f} N on the plate, more than {PRESSED_N:.0f} N"))
        q = d.qpos[: self.n]
        b = self.body
        if np.any(q < np.array(b.lower) - 0.02) or np.any(q > np.array(b.upper) + 0.02):
            if len(self.violations) < 1000:
                self.violations.append((now, f"a joint past its limit: {np.round(q, 3).tolist()}"))

    def log(self) -> dict[str, Array]:
        n = min(self.ticks, len(self.log_t))
        return {
            "t": self.log_t[:n],
            "qpos": self.log_qpos[:n],
            "force": self.log_force[:n],
            "ee": self.log_ee[:n],
            "clean_at": self.clean_at,
            "cells": self.cells,
        }


class PandaSensor:
    """The camera above the table: the plate's position with Gaussian noise
    and a bias in height (`plate_bias_z_m`: perception error, in the belief),
    the arm's proprioception, and the dirt as a noisy, occludable coverage:
    the cells it sees (not under the sponge when the sponge is close to the
    plate, nothing while an occlusion lasts), each seen as it is but flipped
    with probability `dirt_flip`. No images."""

    def __init__(self, world: PandaWorld, rng: np.random.Generator, cfg: Json, occlusions: list[Occlusion]) -> None:
        self.world = world
        self.rng = rng
        self.source = str(cfg["source"])
        self.noise = float(cfg["noise_m"])
        self.bias = float(cfg["plate_bias_z_m"])
        self.flip = float(cfg["dirt_flip"])
        self.confidence = float(cfg["confidence"])
        self.occlusions = occlusions
        self.count = 0

    def observe(self, now: Ns) -> Observation:
        w = self.world
        pose = w.plate_pose()
        self.count += 1
        hidden = any(o.when.active(w, now) and o.region.contains(float(pose[0]), float(pose[2])) for o in self.occlusions)
        detections: tuple[Detection, ...] = ()
        coverage: tuple[Coverage, ...] = ()
        if not hidden:
            x, y, z = pose[:3] + self.rng.normal(0.0, self.noise, 3) + [0.0, 0.0, self.bias]
            detections = (Detection("plate", round(float(x), 6), round(float(z), 6), self.confidence, round(float(y), 6)),)
            visible = np.logical_not(w.under_sponge(near=0.05))
            looks_clean = (w.clean_at >= 0) ^ (self.rng.random(len(w.cells)) < self.flip)
            coverage = (Coverage("plate", len(w.cells), int(visible.sum()), int((looks_clean & visible).sum())),)
        b = w.body
        ee = w.data.site_xpos[w.pad]
        proprio = Proprioception(b.dims, tuple(float(v) for v in w.data.qpos[: len(b.dims)]), tuple(float(v) for v in ee))
        return Observation(f"obs-{self.count}", now, self.source, detections, proprio, coverage)

    def ghost(self, now: Ns) -> Observation | None:
        return None
