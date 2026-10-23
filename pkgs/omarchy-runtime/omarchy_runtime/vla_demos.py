"""Demonstrations for fine-tuning the VLA, from the scripted wiper (the teacher).

The teacher plans its raster from the belief (the simulated detector) and
wipes with the contact channel; the student will see only what a DROID robot
records. Each episode, at DROID's 15 Hz: the exterior and wrist images
(rendered at 1280 x 720, resized with padding to 224 x 224, as at test), the
seven joint angles, and the action: the joint targets for the next step,
solved from the teacher's commanded pad pose (not from where the arm went:
in contact the commanded pose is below the surface), plus a press flag in
the gripper's dimension, 1 where the teacher's contact mode is MAKE or
MAINTAIN. Plates move by up to 3 cm per seed; the board under them keeps
one height. Development tools only (MuJoCo).

  python -m omarchy_runtime.vla_demos OUT_DIR FIRST LAST [BOARD_M]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from omarchy_runtime import scenario as composition
from omarchy_runtime.contracts import ContactMode
from omarchy_runtime.sim.vla import DT, Cameras, resize_with_pad
from omarchy_runtime.sim.wipe import ScriptedWiper
from omarchy_runtime.vla_runs import SCENARIO, plate_at

PROMPT = "wipe the plate with the sponge"
TAIL_S = 1.0  # recorded after the plan ends: the pad held up


class Teacher(ScriptedWiper):
    made: list[Teacher] = []

    def __init__(self, implementation: str, params: Any) -> None:
        super().__init__(implementation, params)
        Teacher.made.append(self)


def ik(model: mujoco.MjModel, data: mujoco.MjData, site: int, n: int, q0: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Joint angles near q0 that put `site` at target (x, y, z, qw, qx, qy, qz): damped least squares."""
    q = q0.copy()
    jp, jr = np.zeros((3, model.nv)), np.zeros((3, model.nv))
    flat = np.zeros(9)
    mujoco.mju_quat2Mat(flat, target[3:7])
    want = flat.reshape(3, 3)
    for _ in range(30):
        data.qpos[:n] = q
        mujoco.mj_kinematics(model, data)
        mujoco.mj_comPos(model, data)
        mujoco.mj_jacSite(model, data, jp, jr, site)
        rot = data.site_xmat[site].reshape(3, 3)
        turn = 0.5 * sum(np.cross(rot[:, i], want[:, i]) for i in range(3))
        err = np.concatenate((target[:3] - data.site_xpos[site], turn))
        if np.linalg.norm(err[:3]) < 1e-4 and np.linalg.norm(err[3:]) < 1e-3:
            break
        jac = np.vstack((jp[:, :n], jr[:, :n]))
        q = q + jac.T @ np.linalg.solve(jac @ jac.T + 1e-4 * np.eye(6), err)
        q = np.clip(q, model.jnt_range[:n, 0], model.jnt_range[:n, 1])
    return q


def episode(seed: int, out: Path, board_m: float) -> dict[str, Any]:
    Teacher.made.clear()
    over = {
        "scenario": {"seed": seed, "s1": {"policy": "script", "contact": True}},
        "world": {"plate": {"center": plate_at(seed), "board_m": board_m}},
    }
    run_dir = out / f"run-{seed:04d}"
    r = composition.run(SCENARIO, run_dir, overrides=over, record_s0=True)
    (teacher,) = Teacher.made
    w: Any = r.world
    log = w.log()
    model, data = w.model, mujoco.MjData(w.model)
    cams = Cameras(model, data)
    n = 7
    t0 = teacher._t0
    assert t0 is not None
    end = min(int(log["t"][-1]), t0 + int((teacher._end + TAIL_S) * 1e9))
    dirt = [model.geom(f"dirt{k}").id for k in range(len(log["cells"]))]
    stain = model.geom_rgba[dirt[0]].copy()
    fk = mujoco.MjData(model)
    frames: dict[str, list[Any]] = {"exterior": [], "wrist": [], "joint_position": [], "actions": []}
    t = t0
    while t + DT <= end:
        i = min(int(np.searchsorted(log["t"], t)), len(log["t"]) - 1)
        data.qpos[:] = log["qpos"][i]
        mujoco.mj_forward(model, data)
        clean = (log["clean_at"] >= 0) & (log["clean_at"] <= t)
        for g, c in zip(dirt, clean, strict=True):
            model.geom_rgba[g] = (0, 0, 0, 0) if c else stain
        views = cams.views()
        q = np.array(log["qpos"][i][:n])
        pos, mode = teacher._at((t + DT - t0) / 1e9)
        target = np.concatenate((pos, teacher._orientation))
        q_next = ik(model, fk, w.pad, n, q, target)
        press = 1.0 if mode in (ContactMode.MAKE, ContactMode.MAINTAIN) else 0.0
        frames["exterior"].append(resize_with_pad(views["exterior"]))
        frames["wrist"].append(resize_with_pad(views["wrist"]))
        frames["joint_position"].append(q)
        frames["actions"].append(np.concatenate((q_next, [press])))
        t += DT
    cams.renderer.close()
    arrays: dict[str, Any] = {k: np.array(v) for k, v in frames.items()}
    np.savez_compressed(out / f"demo-{seed:04d}.npz", prompt=PROMPT, **arrays)
    summary = r.summaries[0]
    return {
        "seed": seed,
        "plate": plate_at(seed),
        "board_m": board_m,
        "frames": len(frames["actions"]),
        "outcome": summary["outcome"],
        "coverage": w.coverage(),
        "press_share": float(arrays["actions"][:, 7].mean()),
    }


def main() -> None:
    out = Path(sys.argv[1])
    first, last = int(sys.argv[2]), int(sys.argv[3])
    board = float(sys.argv[4]) if len(sys.argv) > 4 else 0.02
    out.mkdir(parents=True, exist_ok=True)
    composition.ScriptedWiper = Teacher  # type: ignore[attr-defined]  # the composition root builds the teacher: keep its plan
    for seed in range(first, last + 1):
        info = episode(seed, out, board)
        print(json.dumps(info), flush=True)
        with (out / "demos.jsonl").open("a") as f:
            f.write(json.dumps(info) + "\n")


if __name__ == "__main__":
    main()
