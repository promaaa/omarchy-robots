"""Arms with a gripper that pick and place a cup, in MuJoCo, on the CPU.

The arms and grippers are official MuJoCo Menagerie models, fetched at a
pinned commit (`OMARCHY_MENAGERIE`), never edited: the lab's Franka Panda
(`franka_emika_panda/panda_nohand.xml`) with the Robotiq 2F-85 mounted on
its flange (`robotiq_2f85/2f85.xml`), as on the 3D Vision Lab's arm, and the
AgileX PiPER (`agilex_piper/piper.xml`) with its own gripper. The body's
`kinematics.model` table names the files and where the tool centre point
(TCP) is: between the fingertips, its z axis out of the palm, its y axis
along the fingers' closing. Every arm then takes the same poses.

What is simulated: a table, a cup free to move, a drying rack, the
gripper's fingers and their contacts, at 1 ms. The arm's joints follow S0's
command ideally (as the planar simulator's do; a position-controlled arm's
own servo loop is assumed stiff); the gripper's fingers are driven by their
own actuator, and a grasp holds only by friction. A slip fault makes the
grip fail for a while (the fingers limp, the cup's friction low): the cup
falls out of the fingers.

The check's camera (`CameraSensor`): a fixed RGB-D camera above the table,
as the lab's ZED camera: each observation renders its depth and which
object each pixel shows (the simulator's segmentation, a stand-in for a
detector), back-projects the object's pixels to 3D and reports their mean
as the object's position. An object hidden behind the arm is not seen. No
ground truth reaches the runtime: only what this camera sees, and the arm's
own joints and gripper.

Development tools only (`omarchy-runtime-tools`): MuJoCo.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import mujoco
import numpy as np

from omarchy_runtime.contracts import (
    Array,
    BodySpec,
    ConfigError,
    Detection,
    Goal,
    Json,
    Ns,
    Observation,
    Proprioception,
    RobotState,
    seconds,
)
from omarchy_runtime.sim.world import Trigger

STEP = 1_000_000  # ns: the physics step
LIFTED_M = 0.015  # the cup this far above where it rested, touching the fingers: grasped
RECORD_EVERY = 10  # physics steps between two recorded frames (100 Hz)


def menagerie() -> Path:
    where = os.environ.get("OMARCHY_MENAGERIE")
    if not where or not Path(where).is_dir():
        raise ConfigError(f"OMARCHY_MENAGERIE does not name the MuJoCo Menagerie ({where!r})")
    return Path(where)


def _f(table: Json, key: str) -> Array:
    return np.array(table[key], dtype=np.float64)


def _camera(parent: mujoco.MjsBody, name: str, pos: Array, target: Array, fovy: float) -> None:
    f = (target - pos) / np.linalg.norm(target - pos)
    x = np.cross(f, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    parent.add_camera(name=name, pos=pos, xyaxes=[*x, *np.cross(x, f)], fovy=fovy)


def _no_keys(spec: mujoco.MjSpec) -> mujoco.MjSpec:
    for k in list(spec.keys):
        spec.delete(k)  # their qpos no longer fits once things are added
    return spec


def build(world: Json, body: BodySpec) -> mujoco.MjModel:
    """The arm (and gripper) of `body`, a table, a cup, a drying rack, lights and cameras."""
    k = body.kinematics["model"]
    spec = _no_keys(mujoco.MjSpec.from_file(str(menagerie() / k["arm"])))
    spec.option.timestep = seconds(STEP)
    spec.option.cone = mujoco.mjtCone.mjCONE_ELLIPTIC
    spec.option.impratio = 10
    if "gripper" in k:
        spec.attach(_no_keys(mujoco.MjSpec.from_file(str(menagerie() / k["gripper"]))), prefix="g_", site=spec.site(k["flange_site"]))
        for b in spec.bodies:
            if b.name.startswith("g_"):
                spec.add_exclude(bodyname1=k["flange_body"], bodyname2=b.name)  # the gripper sits on the flange: no contact between them
    for a, b in k.get("exclude", []):  # parts of the gripper that must not catch on each other
        spec.add_exclude(bodyname1=a, bodyname2=b)
    spec.body(k["tcp_body"]).add_site(name="tcp", pos=k["tcp_pos"], quat=k.get("tcp_quat", [1, 0, 0, 0]), size=[0.004] * 3, group=5)
    for b in spec.bodies:
        b.gravcomp = 1  # the arm's own controller holds it up (its joints follow S0's command)
    w = spec.worldbody
    look = world["look"]
    spec.visual.global_.offwidth, spec.visual.global_.offheight = 1920, 1080
    spec.visual.quality.shadowsize = 8192
    spec.visual.headlight.ambient = [0.35] * 3
    spec.visual.headlight.diffuse = [0.3] * 3
    spec.visual.headlight.specular = [0.05] * 3
    w.add_light(
        name="sun",
        pos=[0.4, -0.6, 2.0],
        dir=[-0.1, 0.3, -1.0],
        type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL,
        diffuse=[0.55] * 3,
        specular=[0.1] * 3,
        castshadow=True,
    )
    w.add_light(name="fill", pos=[1.2, 0.8, 1.4], dir=[-0.6, -0.5, -0.8], diffuse=[0.25] * 3, specular=[0.0] * 3, castshadow=False)
    w.add_geom(name="floor", type=mujoco.mjtGeom.mjGEOM_PLANE, pos=[0, 0, float(look["floor_z"])], size=[3, 3, 0.1], rgba=look["floor"])
    t = world["table"]
    w.add_geom(
        name="table",
        type=mujoco.mjtGeom.mjGEOM_BOX,
        pos=_f(t, "center"),
        size=_f(t, "half"),
        rgba=look["table"],
        friction=[0.8, 0.005, 0.0001],
    )
    for n, (pos, size, rgba) in enumerate(look.get("props", [])):
        w.add_geom(name=f"prop{n}", type=mujoco.mjtGeom.mjGEOM_BOX, pos=pos, size=size, rgba=rgba, contype=0, conaffinity=0)
    photos = os.environ.get("OMARCHY_TEXTURES")
    if photos:
        for geom, (file, repeat) in look.get("textures", {}).items():
            name = f"photo_{geom}"
            spec.add_texture(name=name, type=mujoco.mjtTexture.mjTEXTURE_2D, file=str(Path(photos) / file))
            mat = spec.add_material(name=name, texrepeat=repeat, texuniform=True, specular=0.1, shininess=0.2)
            mat.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = name
            for g in [x for x in spec.geoms if x.name == geom or (geom == "walls" and x.name.startswith("prop") and x.name != "prop0")]:
                g.material, g.rgba = name, [1, 1, 1, 1]
    for name, c in world["cups"].items():
        cup = w.add_body(name=name, pos=[*c["at"], float(c["height"]) / 2 + 0.0005])
        cup.add_freejoint(name=name)
        r, h = float(c["radius"]), float(c["height"])
        cup.add_geom(
            name=name,
            type=mujoco.mjtGeom.mjGEOM_CYLINDER,
            size=[r, h / 2, 0],
            rgba=c["rgba"],
            mass=float(c["mass_kg"]),
            friction=[float(c["friction"]), 0.01, 0.0001],
            condim=4,
            priority=2,
        )
        cup.add_geom(
            name=f"{name}_inside",
            type=mujoco.mjtGeom.mjGEOM_CYLINDER,
            pos=[0, 0, h / 2 - 0.0005],
            size=[r - 0.004, 0.001, 0],
            rgba=[0.12, 0.12, 0.13, 1],
            contype=0,
            conaffinity=0,
            density=0,
        )  # drawn only: the cup's opening
        cup.add_geom(
            name=f"{name}_band",
            type=mujoco.mjtGeom.mjGEOM_CYLINDER,
            pos=[0, 0, h / 2 - 0.012],
            size=[r + 0.0008, 0.005, 0],
            rgba=c.get("band", [0.95, 0.95, 0.95, 1]),
            contype=0,
            conaffinity=0,
            density=0,
        )  # drawn only
    rk = world["rack"]
    cx, cy = rk["center"]
    hx, hy, hz = rk["half"]
    base_t, bar = 0.004, 0.0035
    rack = w.add_body(name="drying_rack", pos=[cx, cy, 0.0])
    rack.add_geom(
        name="rack_base",
        type=mujoco.mjtGeom.mjGEOM_BOX,
        pos=[0, 0, base_t],
        size=[hx, hy, base_t],
        rgba=rk["rgba"],
        friction=[0.8, 0.005, 0.0001],
    )
    metal = rk.get("metal", [0.72, 0.74, 0.77, 1])
    for z in (0.018, 2 * hz):  # two rails around, at two heights
        for sx in (-1, 1):
            rack.add_geom(type=mujoco.mjtGeom.mjGEOM_CAPSULE, fromto=[sx * hx, -hy, z, sx * hx, hy, z], size=[bar, 0, 0], rgba=metal)
        for sy in (-1, 1):
            rack.add_geom(type=mujoco.mjtGeom.mjGEOM_CAPSULE, fromto=[-hx, sy * hy, z, hx, sy * hy, z], size=[bar, 0, 0], rgba=metal)
    for sx in (-1, 1):
        for sy in (-1, 1):
            rack.add_geom(
                type=mujoco.mjtGeom.mjGEOM_CAPSULE, fromto=[sx * hx, sy * hy, 0, sx * hx, sy * hy, 2 * hz], size=[bar, 0, 0], rgba=metal
            )
    for x in np.linspace(-hx, hx, 6)[1:-1]:  # the tines along one side, as a dish rack's
        rack.add_geom(
            type=mujoco.mjtGeom.mjGEOM_CAPSULE,
            fromto=[x, hy, 0.008, x, hy, 0.6 * 2 * hz + 0.02],
            size=[0.0025, 0, 0],
            rgba=metal,
            contype=0,
            conaffinity=0,
        )
    for name, c in world["cameras"].items():
        _camera(w, name, _f(c, "pos"), _f(c, "target"), float(c["fovy"]))
    return spec.compile()


class ArmWorld:
    """Ground truth of the scene, and S0's RobotIO: `state` reports the arm's
    joints and the gripper's commanded opening (0 closed, 1 open), the TCP
    pose and its Jacobian; `apply` puts the arm's joints where S0 commands,
    drives the gripper's actuator, steps the physics to the tick's time, and
    records frames for the harness and the videos."""

    def __init__(self, world: Json, body: BodySpec, rng: np.random.Generator, capacity: int, slips: Sequence[Trigger] = ()) -> None:
        self.cfg = world
        self.body = body
        self.model = build(world, body)
        self.data = mujoco.MjData(self.model)
        m, d = self.model, self.data
        k = body.kinematics["model"]
        self.n = len(body.dims) - 1  # the arm's joints; the last dimension is the gripper
        self.joints = [m.joint(j).id for j in k["joints"]]
        self.qadr = np.array([m.jnt_qposadr[j] for j in self.joints])
        self.vadr = np.array([m.jnt_dofadr[j] for j in self.joints])
        self.grip = m.actuator(k["gripper_actuator"]).id
        if "gripper_kp" in k:  # a firmer squeeze than the model's default servo gives, within its force range
            m.actuator_gainprm[self.grip, 0] = float(k["gripper_kp"])
            m.actuator_biasprm[self.grip, 1] = -float(k["gripper_kp"])
        for a in range(m.nu):  # the arm's own servos off: its joints follow S0's command (apply)
            if a != self.grip and m.actuator_trntype[a] == mujoco.mjtTrn.mjTRN_JOINT and m.actuator_trnid[a, 0] in self.joints:
                m.actuator_gainprm[a, :] = 0.0
                m.actuator_biasprm[a, :] = 0.0
        self.grip_closed, self.grip_open = (float(v) for v in k["gripper_ctrl"])  # actuator values for 0 (closed) and 1 (open)
        self.fingers = {m.body(b).id for b in k["finger_bodies"]}
        self.rack_body = m.body("drying_rack").id
        self.table = m.geom("table").id
        self.tcp = m.site("tcp").id
        self.cups = {name: (m.body(name).id, m.geom(name).id, int(m.jnt_qposadr[m.joint(name).id])) for name in world["cups"]}
        self.friction = {name: float(c["friction"]) for name, c in world["cups"].items()}
        self.rack = world["rack"]
        self.q = list(body.home)
        self.data.qpos[self.qadr] = body.home[: self.n]
        self.gripper = float(body.home[-1])
        self.data.ctrl[self.grip] = self._ctrl(self.gripper)
        mujoco.mj_forward(m, d)
        for _ in range(300):  # the cup settles, the gripper opens as at home
            self._hold_arm(np.array(body.home[: self.n]), np.zeros(self.n))
            mujoco.mj_step(m, d)
        d.time = 0.0
        self.rest = {name: float(d.xpos[b][2]) for name, (b, _, _) in self.cups.items()}
        self.jacp, self.jacr = np.zeros((3, m.nv)), np.zeros((3, m.nv))
        self.rng = rng
        self._slips = list(slips)
        self._slipping: str | None = None
        self.first: dict[str, Ns] = {"start": 0}
        self.events: list[tuple[Ns, str, str]] = []
        self.violations: list[tuple[Ns, str]] = []
        self.held: str | None = None
        self._last: Ns = 0
        self._cmd = np.array(body.home[: self.n], dtype=np.float64)
        self.ticks = 0
        frames = capacity // RECORD_EVERY + 2
        self.log_t = np.zeros(frames, dtype=np.int64)
        self.log_qpos = np.zeros((frames, m.nq))
        self.log_gripper = np.zeros(frames)
        self.frames = 0
        self._steps = 0

    def _ctrl(self, g: float) -> float:
        return self.grip_closed + (self.grip_open - self.grip_closed) * float(np.clip(g, 0.0, 1.0))

    def _hold_arm(self, q: Array, dq: Array) -> None:
        self.data.qpos[self.qadr] = q
        self.data.qvel[self.vadr] = dq

    # S0's RobotIO.

    def tcp_pose(self) -> Array:
        d = self.data
        quat = np.zeros(4)
        mujoco.mju_mat2Quat(quat, d.site_xmat[self.tcp])
        return np.concatenate((d.site_xpos[self.tcp], quat))

    def state(self, now: Ns) -> RobotState:
        m, d = self.model, self.data
        mujoco.mj_jacSite(m, d, self.jacp, self.jacr, self.tcp)
        jac = np.zeros((6, self.n + 1))
        jac[:3, : self.n] = self.jacp[:, self.vadr]
        jac[3:, : self.n] = self.jacr[:, self.vadr]
        q = np.concatenate((d.qpos[self.qadr], [self.gripper]))
        dq = np.concatenate((d.qvel[self.vadr], [0.0]))
        return RobotState(now, q, dq, None, self.tcp_pose(), jac[:, :] @ dq, None, jac, None)

    def scene(self, now: Ns) -> Mapping[str, Array]:
        return {}

    def forward(self, q: Sequence[float]) -> tuple[float, ...]:
        """The TCP's position for joint positions q (the gripper ignored), for the guard's workspace check."""
        if not hasattr(self, "_fk"):
            self._fk = mujoco.MjData(self.model)
        self._fk.qpos[self.qadr] = list(q)[: self.n]
        mujoco.mj_kinematics(self.model, self._fk)
        return tuple(float(v) for v in self._fk.site_xpos[self.tcp])

    def apply(self, now: Ns, command: Array) -> None:
        m, d = self.model, self.data
        target = np.array(command[: self.n], dtype=np.float64)
        self.gripper = float(command[-1])
        d.ctrl[self.grip] = self._ctrl(self.gripper)
        steps = int((now - self._last) // STEP)
        start = self._cmd.copy()
        for k in range(steps):
            q = start + (target - start) * (k + 1) / steps
            v = (target - start) / max(steps, 1) / seconds(STEP)
            self._hold_arm(q, v)
            self._slip(self._last + STEP)
            mujoco.mj_step(m, d)
            self._hold_arm(q, v)  # ideal tracking: no force moves the arm off S0's command
            self._last += STEP
            self._steps += 1
            if self._steps % RECORD_EVERY == 0 and self.frames < len(self.log_t):
                f = self.frames
                self.log_t[f], self.log_qpos[f], self.log_gripper[f] = self._last, d.qpos, self.gripper
                self.frames += 1
            self._watch(self._last)
        self._cmd = target
        self.q = [*target.tolist(), self.gripper]
        self.ticks += 1

    def _touching(self, geom: int) -> set[str]:
        """What touches a geom: "pads" (a finger), "rack", "table"."""
        d, m = self.data, self.model
        out = set()
        for i in range(d.ncon):
            c = d.contact[i]
            if geom not in (c.geom1, c.geom2):
                continue
            other = c.geom2 if c.geom1 == geom else c.geom1
            if m.geom_bodyid[other] in self.fingers:
                out.add("pads")
            elif m.geom_bodyid[other] == self.rack_body:
                out.add("rack")
            elif other == self.table:
                out.add("table")
        return out

    def _slip(self, now: Ns) -> None:
        if self.held is None and self._slipping is None:
            return
        active = any(t.active(self, now) for t in self._slips)
        m = self.model
        if active and self._slipping is None and self.held is not None:
            self._slipping = self.held
            self._grip_params = (m.actuator_gainprm[self.grip].copy(), m.actuator_biasprm[self.grip].copy())
            m.actuator_gainprm[self.grip, :] = 0.0  # the fingers go limp
            m.actuator_biasprm[self.grip, :] = 0.0
            m.geom_friction[self.cups[self.held][1], 0] = 0.02
            self.events.append((now, "slip", f"{self.held}: the grip fails while the fault lasts"))
            self.first.setdefault("slip", now)
        elif not active and self._slipping is not None:
            m.geom_friction[self.cups[self._slipping][1], 0] = self.friction[self._slipping]
            m.actuator_gainprm[self.grip], m.actuator_biasprm[self.grip] = self._grip_params
            self._slipping = None

    def _watch(self, now: Ns) -> None:
        d = self.data
        if "moving" not in self.first and np.abs(d.qvel[self.vadr]).max() > 0.01:
            self.first["moving"] = now
        for name, (b, g, _) in self.cups.items():
            touching = self._touching(g)
            lifted = d.xpos[b][2] > self.rest[name] + LIFTED_M
            if self.held is None and lifted and "pads" in touching:
                self.held = name
                self.first.setdefault("grasp", now)
                self.events.append((now, "grasp", name))
            elif self.held == name and "pads" not in touching:
                self.held = None
                kind = "release" if self.gripper >= 0.5 else "drop"
                self.first.setdefault(kind, now)
                self.events.append((now, kind, f"{name} at ({d.xpos[b][0]:.3f}, {d.xpos[b][1]:.3f}, {d.xpos[b][2]:.3f})"))
            if d.xpos[b][2] < -0.05 and len(self.violations) < 1000:
                self.violations.append((now, f"{name} fell off the table"))

    # Ground truth.

    def cup_pose(self, name: str) -> Array:
        a = self.cups[name][2]
        return np.array(self.data.qpos[a : a + 7])

    def inside_rack(self, name: str) -> bool:
        x, y, z = self.data.xpos[self.cups[name][0]]
        cx, cy = self.rack["center"]
        hx, hy, hz = self.rack["half"]
        return bool(abs(x - cx) <= hx and abs(y - cy) <= hy and 0.0 <= z <= 2 * hz + 0.05)

    def truth(self, goal: Goal) -> bool | None:
        a = goal.args
        if goal.predicate == "in" and len(a) == 2 and a[0] in self.cups and a[1] == "drying_rack":
            return bool(self.held != a[0] and "pads" not in self._touching(self.cups[a[0]][1]) and self.inside_rack(a[0]))
        if goal.predicate == "held" and len(a) == 1:
            return bool(self.held == a[0])
        return None

    def log(self) -> dict[str, Array]:
        n = self.frames
        return {"t": self.log_t[:n], "qpos": self.log_qpos[:n], "gripper": self.log_gripper[:n]}


class CameraSensor:
    """The check's camera: a fixed RGB-D camera (`camera`, `width` x `height`),
    rendered from the scene at each observation. Each cup is found by the
    simulator's segmentation (a stand-in for a detector); its depth pixels,
    back-projected to the robot's base frame, give its position: their mean,
    plus `noise_m` of Gaussian noise. Fewer than `min_pixels` visible: not
    seen. Proprioception: the arm's joints, the gripper's opening, the TCP."""

    def __init__(self, world: ArmWorld, rng: np.random.Generator, cfg: Json) -> None:
        self.world = world
        self.rng = rng
        self.source = str(cfg["source"])
        self.camera = str(cfg["camera"])
        self.noise = float(cfg["noise_m"])
        self.confidence = float(cfg["confidence"])
        self.min_pixels = int(cfg["min_pixels"])
        self.size = (int(cfg["height"]), int(cfg["width"]))
        self.radius = {name: float(c["radius"]) for name, c in world.cfg["cups"].items()}
        self.count = 0
        self._renderer: mujoco.Renderer | None = None
        self.seen: list[Json] = []  # what the camera saw, for the videos

    def _render(self) -> tuple[Array, Array]:
        m, d = self.world.model, self.world.data
        if self._renderer is None:
            self._renderer = mujoco.Renderer(m, *self.size)
        r = self._renderer
        r.enable_depth_rendering()
        r.update_scene(d, camera=self.camera)
        depth = r.render().copy()
        r.disable_depth_rendering()
        r.enable_segmentation_rendering()
        r.update_scene(d, camera=self.camera)
        seg = r.render()[:, :, 0].copy()
        r.disable_segmentation_rendering()
        return depth, seg

    def _points(self, depth: Array, mask: Array) -> Array:
        m, d = self.world.model, self.world.data
        cam = m.camera(self.camera).id
        h, w = depth.shape
        f = 0.5 * h / np.tan(np.deg2rad(m.cam_fovy[cam]) / 2)
        v, u = np.nonzero(mask)
        z = depth[v, u]
        local = np.stack(((u + 0.5 - w / 2) / f * z, -(v + 0.5 - h / 2) / f * z, -z), axis=1)
        out: Array = local @ d.cam_xmat[cam].reshape(3, 3).T + d.cam_xpos[cam]
        return out

    def _axis(self, points: Array, radius: float) -> Array:
        """Where a cup of known radius stands, from the points seen of it: a
        circle of that radius fitted to the side's points in x-y (a few
        Gauss-Newton steps from the mean, moved back from the camera), the
        points' middle height. A cup half hidden by the arm still gives its axis."""
        top = points[:, 2].max()
        side = points[points[:, 2] < top - 0.004]  # its rim and top face lie inside the circle: left out
        side = side if len(side) >= 10 else points
        centre = side.mean(axis=0)
        away = centre[:2] - self.world.data.cam_xpos[self.world.model.camera(self.camera).id][:2]
        c = centre[:2] + 2 / np.pi * radius * away / np.linalg.norm(away)
        for _ in range(10):
            d = side[:, :2] - c
            r = np.linalg.norm(d, axis=1)
            jac = -d / np.maximum(r, 1e-9)[:, None]
            c = c - np.linalg.lstsq(jac, r - radius, rcond=None)[0]
        return np.array([c[0], c[1], (points[:, 2].min() + top) / 2])

    def observe(self, now: Ns) -> Observation:
        w = self.world
        depth, seg = self._render()
        detections = []
        record: Json = {"t": now, "cups": {}}
        for name, (_, g, _) in w.cups.items():
            mask = seg == g
            n = int(mask.sum())
            if n < self.min_pixels:
                record["cups"][name] = {"pixels": n}
                continue
            x, y, z = self._axis(self._points(depth, mask), self.radius[name]) + self.rng.normal(0.0, self.noise, 3)
            detections.append(Detection(name, round(float(x), 6), round(float(z), 6), self.confidence, round(float(y), 6)))
            v, u = np.nonzero(mask)
            record["cups"][name] = {
                "pixels": n,
                "xyz": [float(x), float(y), float(z)],
                "box": [int(u.min()), int(v.min()), int(u.max()), int(v.max())],
            }
        self.seen.append(record)
        self.count += 1
        b = w.body
        proprio = Proprioception(b.dims, tuple(float(v) for v in w.q), tuple(float(v) for v in w.data.site_xpos[w.tcp]))
        return Observation(f"obs-{self.count}", now, self.source, tuple(detections), proprio)

    def ghost(self, now: Ns) -> Observation | None:
        return None
