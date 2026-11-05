"""S1 as a real VLA: pi0.5-DROID (Physical Intelligence's openpi, open
weights, trained on real DROID robots only), reached over openpi's websocket
protocol, with what a DROID robot gives it and nothing else: the images of one
exterior camera and of the wrist camera, rendered from the simulated scene at
1280 x 720 and resized with padding to 224 x 224 as DROID's client does, the
arm's seven joint angles, the gripper's position, and an instruction in words.
It never reads the simulator's state of the plate: only pixels.

Its answer, 15 rows of joint positions at 15 Hz (and a gripper position,
recorded and not executed: the gripper holds the sponge), becomes the motion
channel of an intent chunk, each row's pad pose by forward kinematics (S1
knows the arm's model, not the world). With `contact`, a rule adds the
contact channel: MAKE or MAINTAIN `force_n` within bounds where a row of the
path runs over the plate and less than `band_m` above its top, as the belief
has them (the simulated detector, as the scripted wiper's), BREAK elsewhere; the rule is the scripted part, the path is the VLA's. As
DROID's own client, it executes `open_loop` rows of each chunk, then asks
again. It claims DONE when `duration_s` is spent: it has no other way to tell.

Development tools only: it needs MuJoCo (the cameras) and openpi-client.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from omarchy_runtime import wallclock
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

DT = ns(1 / 15)  # DROID's control period: 15 Hz
SIZE = (720, 1280)  # the ZED cameras' HD720 images
INPUT = 224  # the policy's image size, after resize with padding


def resize_with_pad(image: np.ndarray, size: int = INPUT) -> np.ndarray:
    """openpi_client.image_tools.resize_with_pad, as DROID's client calls it."""
    from openpi_client import image_tools

    out: np.ndarray = image_tools.resize_with_pad(image, size, size)
    return out


class Cameras:
    """The scene's cameras on the world's MjData: what the robot's cameras see now."""

    def __init__(self, model: mujoco.MjModel, data: mujoco.MjData, names: tuple[str, ...] = ("exterior", "wrist")) -> None:
        self.model, self.data, self.names = model, data, names
        self.renderer = mujoco.Renderer(model, *SIZE)

    def views(self) -> dict[str, np.ndarray]:
        out = {}
        for name in self.names:
            self.renderer.update_scene(self.data, camera=name)
            out[name] = self.renderer.render().copy()
        return out


class Pi05Wiper:
    """clean(surface) by a VLA: see the module's docstring."""

    def __init__(self, implementation: str, params: Json, world: Any, record: Path | None = None) -> None:
        self.source = implementation
        try:
            host, port = str(params["server"]).rsplit(":", 1)
            self.prompt = str(params["prompt"])
            self.open_loop = int(params["open_loop"])
            self.duration = float(params["duration_s"])
            self.contact = bool(params["contact"])
            self.force = float(params["force_n"])
            self.bounds = (float(params["force_min_n"]), float(params["force_max_n"]))
            self.stiffness = tuple(float(v) for v in params["stiffness"])
            self.band = float(params["band_m"])
            self.flag = str(params.get("contact_from", "rule")) == "flag"  # a fine-tuned VLA says where to press
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"scenario s1: the VLA wiper needs {e}") from e
        from openpi_client import websocket_client_policy

        for attempt in range(6):  # a busy server answers the handshake late (20 runs on one policy server)
            try:
                self.client = websocket_client_policy.WebsocketClientPolicy(host, int(port))
                break
            except TimeoutError:
                if attempt == 5:
                    raise
        self.world = world
        self.cameras = Cameras(world.model, world.data)
        self.fk = mujoco.MjData(world.model)  # S1's own copy, for forward kinematics only
        self.gripper = float(world.cfg["gripper"]["ctrl"]) / 255.0  # DROID: 0 open, 1 closed
        self.record = record
        self.log: list[Json] = []
        self.frames: dict[str, list[np.ndarray]] = {"exterior": [], "wrist": []}
        self._report: Report | None = None
        self._t0: Ns | None = None
        self._next: Ns = 0
        self._seq = 0
        self._pressing = False

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        (surface,) = goal.args
        entity = belief["entities"][surface]
        if entity["estimate"] is None:
            raise ValueError(f"no estimate of {surface} in the belief")
        cart = body.kinematics.get("cartesian")
        if cart is None:
            raise ConfigError(f"body {body.id} takes no Cartesian intents (kinematics.cartesian)")
        self._space, self._frame, self._ee = str(cart["action_space"]), str(cart["frame"]), str(cart["ee"])
        e, g = entity["estimate"], entity["geometry"]
        self._top = e["z"] + g["height"] / 2
        self._centre = np.array([e["x"], e["y"]])
        self._reach = g["width"] / 2  # the plate's half-width: the rule presses only over it
        self._n = len(body.dims)

    def _pose(self, q: np.ndarray) -> list[float]:
        """The pad's pose for joint angles q, in the base frame (CARTESIAN)."""
        self.fk.qpos[: self._n] = q
        mujoco.mj_kinematics(self.world.model, self.fk)
        quat = np.zeros(4)
        mujoco.mju_mat2Quat(quat, self.fk.site_xmat[self.world.pad])
        return [*self.fk.site_xpos[self.world.pad], *quat]

    def step(self, now: Ns, observation: Observation | None) -> IntentChunk | None:
        if self._report is not None:
            return None
        if self._t0 is None:
            self._t0 = now
        if (now - self._t0) / 1e9 >= self.duration:
            self._report = Report(Claim.DONE, f"{self.duration:.0f} s of wiping spent ({self._seq} chunks from the VLA)")
            return None
        if now < self._next:
            return None  # S0 is still executing the rows of the last chunk
        d = self.world.data
        joints = np.array(d.qpos[: self._n])
        views = self.cameras.views()
        exterior, wrist = resize_with_pad(views["exterior"]), resize_with_pad(views["wrist"])
        request = {
            "observation/exterior_image_1_left": exterior,
            "observation/wrist_image_left": wrist,
            "observation/joint_position": joints,
            "observation/gripper_position": np.array([self.gripper]),
            "prompt": self.prompt,
        }
        started = wallclock.perf_ns()
        answer = self.client.infer(request)
        wall = (wallclock.perf_ns() - started) / 1e9  # the round trip to the policy server, for the record only
        actions = np.asarray(answer["actions"], dtype=np.float64)
        rows = [self._pose(a[: self._n]) for a in actions]
        horizon = len(rows)
        t0 = now + DT  # DROID: row k is the target one period after row k - 1, the first one period from now
        timing = Timing(t0, DT, t0 + (horizon - 1) * DT, self.source, now)
        motion = MotionIntent.make(timing, self._space, CARTESIAN, rows, self._frame)
        contact = []
        modes = []
        if self.contact:
            for k, r in enumerate(rows):
                if self.flag:  # the gripper's dimension, fine-tuned into a press flag (vla_demos.py)
                    near = bool(actions[k, 7] > 0.5)
                else:
                    near = r[2] - self._top <= self.band and bool(np.all(np.abs(np.array(r[:2]) - self._centre) <= self._reach))
                modes.append((ContactMode.MAINTAIN if self._pressing else ContactMode.MAKE) if near else ContactMode.BREAK)
                self._pressing = near
            lo, hi = self.bounds
            contact.append(
                ContactIntent.make(timing, self._ee, self._frame, (0, 0, 1), modes, [[self.force]] * horizon, [lo], [hi], self.stiffness)
            )
            self._pressing = modes[min(self.open_loop, horizon) - 1] is not ContactMode.BREAK
        self._seq += 1
        self._next = now + self.open_loop * DT
        self.frames["exterior"].append(exterior)
        self.frames["wrist"].append(wrist)
        self.log.append(
            {
                "seq": self._seq,
                "t": now,
                "wall_s": wall,
                "server_timing": answer.get("server_timing"),
                "policy_timing": answer.get("policy_timing"),
                "joints": joints.tolist(),
                "actions": actions.tolist(),
                "pad_path": [r[:3] for r in rows],
                "modes": [str(m) for m in modes],
                "gripper_out": actions[:, -1].tolist(),
            }
        )
        return IntentChunk.make(self._seq, self.source, motion, contact=contact)

    def report(self) -> Report | None:
        return self._report

    def save(self, run_dir: Path) -> None:
        """What the VLA saw and answered, for the harness and the videos."""
        import json

        (run_dir / "vla.json").write_text(json.dumps({"prompt": self.prompt, "contact": self.contact, "chunks": self.log}, indent=1) + "\n")
        frames: dict[str, Any] = {k: np.array(v, dtype=np.uint8) for k, v in self.frames.items() if v}
        np.savez_compressed(run_dir / "vla_frames.npz", **frames)
