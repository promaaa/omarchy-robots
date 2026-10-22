"""The VLA wiping experiment: pi0.5-DROID as S1 on the simulated Panda of
world_vla.toml (scenario vla_wipe), against the scripted wiper in the same
scene. Conditions: the VLA's path alone (motion only: today's stack, where
S0 follows a path), the VLA's path with the contact channel (the new S0 is
told to press), and the scripted wiper with the contact channel (it reads
the belief: the simulator's detector, not pixels). Each seed moves the plate
by up to 3 cm on the table and changes the sensors' noise; the VLA's own
sampling noise comes from its server. Every number comes from ground truth
(the harness's) or from the execution's summary.

Development tools only: MuJoCo, openpi-client, and a running policy server.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.contracts import Json
from omarchy_runtime.scenario import run

SCENARIO = "vla_wipe"
CONDITIONS: dict[str, Json] = {
    "vla, motion only": {"policy": "pi05_droid", "contact": False},
    "vla + contact": {"policy": "pi05_droid", "contact": True},
    "script + contact": {"policy": "script", "contact": True},
}
SHIFT_M = 0.03  # the plate moves by up to this much in x and y, per seed
WINDOW_M = 0.02  # the pad over the plate, at most this far above its top
LABEL = (
    "tested in simulation: MuJoCo on the CPU, simulated time, physics and S0 at 1 kHz; S1 pi0.5-DROID (openpi, "
    "joint positions) on a lab GPU, given two cameras rendered by MuJoCo (1280 x 720, resized to 224 x 224), the "
    "joint angles and an instruction; no real robot"
)


def plate_at(seed: int) -> list[float]:
    centre = config.effective(SCENARIO)["world"]["plate"]["center"]
    shift = np.random.default_rng([seed, 11]).uniform(-SHIFT_M, SHIFT_M, 2)
    return [round(float(centre[0] + shift[0]), 4), round(float(centre[1] + shift[1]), 4)]


def cell(condition: str, seed: int, run_dir: Path, s1: Json | None = None) -> Json:
    over: Json = {
        "scenario": {"seed": seed, "s1": CONDITIONS[condition] | (s1 or {})},
        "world": {"plate": {"center": plate_at(seed)}, "objects": {"plate": {"x": plate_at(seed)[0]}}},
    }
    r = run(SCENARIO, run_dir, overrides=over, record_s0=True)
    w: Any = r.world
    log, cfg = w.log(), r.cfg
    lo, hi = cfg["scenario"]["s1"]["force_min_n"], cfg["scenario"]["s1"]["force_max_n"]
    top = cfg["world"]["table"]["center"][2] + cfg["world"]["table"]["half"][2] + cfg["world"]["plate"]["thickness"]
    inner = cfg["world"]["plate"]["half"]
    centre = np.array(cfg["world"]["plate"]["center"])
    ee, force = log["ee"], log["force"]
    over_plate = np.all(np.abs(ee[:, :2] - centre) <= inner, axis=1)
    window = over_plate & (ee[:, 2] <= top + WINDOW_M)
    touching = force >= 1.0
    s = r.summaries[0] if r.summaries else {}
    truth = r.truths[0] if r.truths else None
    fid = s.get("fidelity") or {}
    vla = json.loads((run_dir / "vla.json").read_text()) if (run_dir / "vla.json").exists() else None
    out = {
        "condition": condition,
        "seed": seed,
        "plate": plate_at(seed),
        "run_dir": str(run_dir),
        "outcome": s.get("outcome"),
        "category": s.get("category"),
        "verdict": s.get("verdict") and s["verdict"]["kind"],
        "truth": truth,
        "attribution": s.get("attribution") and s["attribution"]["category"],
        "coverage": w.coverage(),
        "time_s": float(len(force)) / 1000,
        "over_plate_s": float(over_plate.sum()) / 1000,
        "touching_s": float(touching.sum()) / 1000,
        "first_touch_s": float(np.argmax(touching)) / 1000 if touching.any() else None,
        "window_s": float(window.sum()) / 1000,
        "force_in_bounds": float(np.mean((force[window] >= lo) & (force[window] <= hi))) if window.any() else 0.0,
        "force_mean_n": float(force[touching].mean()) if touching.any() else 0.0,
        "force_peak_n": float(force.max()),
        "pad_min_above_top_m": float(ee[over_plate, 2].min() - top) if over_plate.any() else None,
        "guard_interventions": int(fid.get("guard_clamps", 0) + fid.get("guard_replacements", 0)),
        "rejected_chunks": int(s.get("s0", {}).get("rejected_chunks", 0)) if isinstance(s.get("s0"), dict) else None,
        "degraded_chunks": int(fid.get("degraded_chunks", 0)),
        "faithful": fid.get("faithful"),
        "violations": len(r.violations()),
    }
    if vla is not None:
        chunks = vla["chunks"]
        walls = [c["wall_s"] for c in chunks]
        out |= {
            "prompt": vla["prompt"],
            "chunks": len(chunks),
            "inference_wall_s": {"median": float(np.median(walls)), "max": float(max(walls))} if walls else None,
            "gripper_open_asked": sum(1 for c in chunks if min(c["gripper_out"]) < 0.5),
            "pressing_rows": dict(Counter(m for c in chunks for m in c["modes"][:8])),
        }
    return out


def experiment(out: Path, seeds: int, conditions: list[str], s1: Json | None = None) -> Json:
    out.mkdir(parents=True, exist_ok=True)
    cells = []
    for condition in conditions:
        for seed in range(1, seeds + 1):
            name = condition.replace(" + ", "+").replace(", ", "-").replace(" ", "-")
            c = cell(condition, seed, out / f"{name}-s{seed}", s1)
            print(json.dumps({k: c[k] for k in ("condition", "seed", "outcome", "coverage", "touching_s", "force_peak_n", "guard_interventions")}))
            cells.append(c)
            (out / "results.json").write_text(json.dumps({"label": LABEL, "scenario": SCENARIO, "cells": cells}, indent=1, default=str) + "\n")
    return {"label": LABEL, "cells": cells}


if __name__ == "__main__":
    import sys

    experiment(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3].split(";"), json.loads(sys.argv[4]) if len(sys.argv) > 4 else None)
