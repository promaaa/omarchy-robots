"""The S0-learning experiment: a sequence of wiping tasks on the simulated
Panda of world_vla.toml, in which S0 keeps learning (learning.py). Each task
moves the plate (up to 3 cm), runs the learner (the teacher or the habit
drives, the contact model learns in S0), then consolidates the memory
("sleep"): the episode is stored and the habit's next version built from
what the camera saw of the dirt (each cell flipped with the sensor's
`dirt_flip`, as the runtime's coverage). A schedule of world overrides
changes the scene from a given task on (a raised plate, a wrong belief, a
fragile plate). Every number comes from ground truth or the execution's
summary, as in vla_runs.py.

Development tools only: MuJoCo, and a policy server when the teacher is the VLA.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime import config, learning
from omarchy_runtime.contracts import Json
from omarchy_runtime.scenario import run
from omarchy_runtime.vla_runs import SCENARIO, measure, plate_at

LABEL = (
    "tested in simulation: MuJoCo on the CPU, simulated time, physics and S0 at 1 kHz; the teacher S1 is the scripted wiper "
    "or pi0.5-DROID fine-tuned (openpi, on a lab GPU); the habit, the contact model, the arbiter and the supervisor run "
    "in the robot's process, with no GPU; no real robot"
)


def episode(out: Path, seed: int, k: int, s1: Json, world: Json | None = None, extra: Json | None = None) -> Json:
    """Task k of the sequence: run it, measure it, consolidate the memory."""
    lcfg = s1["learning"]
    memory_path = Path(lcfg["memory"])
    where = plate_at(seed * 1000 + k)
    over: Json = {
        "scenario": {"seed": seed * 1000 + k, "s1": s1 | {"policy": "learner"}},
        "world": {"plate": {"center": where}, "objects": {"plate": {"x": where[0]}}},
    }
    over["world"] = config.merge(over["world"], world or {})
    over = config.merge(over, extra or {})
    run_dir = out / f"e{k:02d}"
    r = run(SCENARIO, run_dir, overrides=over, record_s0=True)
    m = measure(r, run_dir)
    w: Any = r.world
    cfg = r.cfg
    learned = json.loads((run_dir / "learning.json").read_text())
    flip = float(cfg["world"]["sensor"]["dirt_flip"])
    rng = np.random.default_rng([seed, k, 9])
    kr = (w.clean_at >= 0) ^ (rng.random(len(w.clean_at)) < flip)  # what the camera saw of the dirt at the end
    p = cfg["scenario"]["s1"]
    half = float(cfg["world"]["plate"]["half"])
    memory = learning.Memory(memory_path, lcfg)
    program = memory.program(half, float(p["margin_m"]), float(p["line_spacing_m"]))
    controller: Any = r.s0.controller
    model = controller.cerebellum if controller.cerebellum is not None else memory.model(half)
    teaching = controller.teaching
    record = {
        "k": k,
        "driver": learned["driver"],
        "goal": {"force_n": float(p["force_n"]), "force_min_n": float(p["force_min_n"]), "force_max_n": float(p["force_max_n"])},
        "top": learned["top"],
        "kr_clean": kr.astype(int).tolist(),
        "kr_coverage": float(kr.mean()),
        "coverage": m["coverage"],
        "s1_calls": learned["s1_calls"],
    }
    after = memory.consolidate(record, program, model, learning.cells_of(cfg["world"]), limit=half - 0.03)
    return (
        {"k": k, "seed": seed, "plate": where}
        | m
        | {
            "driver": learned["driver"],
            "reason": learned["reason"],
            "handbacks": [e for e in learned["events"] if e["event"] == "handback"],
            "s1_calls": learned["s1_calls"],
            "kr_coverage": record["kr_coverage"],
            "program_version": learned["program"]["version"],
            "program_after": after.version,
            "predicted_coverage": after.predicted(learning.cells_of(cfg["world"])),
            "teaching_mm": 1000 * float(teaching[0] / teaching[1]) if teaching[1] > 0 else None,  # mean |feedback offset| in steady contact
            "steady_s": float(teaching[1]),
            "model_learned_s": learned["model_learned_s"],
        }
    )


def sequence(
    out: Path, seed: int, episodes: int, s1: Json, world: Json | None = None, extra: Json | None = None, schedule: Json | None = None
) -> Json:
    """Tasks 1..`episodes` on one memory (out/memory.json, made fresh); `schedule` maps a task number (as a
    string) to changes that hold from that task on: {"world": overrides, "s1": S1's parameters, such as a goal's
    force and bounds}. Writes results.json as it goes."""
    out.mkdir(parents=True, exist_ok=True)
    memory = out / "memory.json"
    memory.unlink(missing_ok=True)
    s1 = config.merge(s1, {"learning": {"memory": str(memory)}})
    cells = []
    now: Json = dict(world or {})
    for k in range(1, episodes + 1):
        change = (schedule or {}).get(str(k), {})
        now = config.merge(now, change.get("world", {}))
        s1 = config.merge(s1, change.get("s1", {}))
        c = episode(out, seed, k, s1, now, extra)
        print(
            json.dumps({x: c[x] for x in ("k", "driver", "coverage", "s1_calls", "teaching_mm", "force_peak_n", "program_after")}),
            flush=True,
        )
        cells.append(c)
        (out / "results.json").write_text(
            json.dumps({"label": LABEL, "scenario": SCENARIO, "s1": s1, "cells": cells}, indent=1, default=str) + "\n"
        )
    return {"label": LABEL, "cells": cells}


if __name__ == "__main__":
    a = sys.argv
    sequence(
        Path(a[1]),
        int(a[2]),
        int(a[3]),
        json.loads(a[4]),
        json.loads(a[5]) if len(a) > 5 and a[5] else None,
        json.loads(a[6]) if len(a) > 6 and a[6] else None,
        json.loads(a[7]) if len(a) > 7 and a[7] else None,
    )
