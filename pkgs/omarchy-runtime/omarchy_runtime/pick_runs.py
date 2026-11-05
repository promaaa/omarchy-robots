"""The pick and place experiment on the lab's arms in MuJoCo (sim/mjarm.py):
the same goal, in(cup_1, drying_rack), the same scripted picker and the same
intents on each body; the check reads the scene's depth camera. Each seed
moves the cup on the table by up to `SHIFT_M`; a scenario with a slip fault
makes the cup slide out of the fingers during transit. Every number comes
from the run's summaries (claim, verdict, outcome) or from ground truth (the
harness's).

Development tools only: MuJoCo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.contracts import Json
from omarchy_runtime.scenario import run

SHIFT_M = 0.03
LABEL = (
    "tested in simulation: MuJoCo 3.8.1 on the CPU, simulated time, physics and S0 at 1 kHz; MuJoCo Menagerie arms; "
    "the arm's joints follow S0's command ideally, the gripper, the cup and the rack are simulated; the check reads a "
    "rendered depth camera, objects found by the simulator's segmentation; no real robot"
)


def cup_at(scenario: str, seed: int) -> list[float]:
    at = config.effective(scenario)["world"]["cups"]["cup_1"]["at"]
    shift = np.random.default_rng([seed, 13]).uniform(-SHIFT_M, SHIFT_M, 2)
    return [round(float(at[0] + shift[0]), 4), round(float(at[1] + shift[1]), 4)]


def cell(scenario: str, seed: int, run_dir: Path, overrides: Json | None = None) -> Json:
    over: Json = {"scenario": {"seed": seed}, "world": {"cups": {"cup_1": {"at": cup_at(scenario, seed)}}}}
    over = config.merge(over, overrides or {})
    r = run(scenario, run_dir, overrides=over, record_s0=True)
    w: Any = r.world  # the MuJoCo scene (sim/mjarm.py ArmWorld)
    out: Json = {
        "scenario": scenario,
        "body": r.cfg["body"]["id"],
        "seed": seed,
        "cup_at": cup_at(scenario, seed),
        "run_dir": str(run_dir),
        "attempts": len(r.summaries),
        "outcomes": [s.get("outcome") for s in r.summaries],
        "categories": [s.get("category") for s in r.summaries],
        "claims": [s.get("claim") and s["claim"].get("claim") for s in r.summaries],
        "verdicts": [s.get("verdict") and s["verdict"].get("kind") for s in r.summaries],
        "reasons": [s.get("verdict") and s["verdict"].get("reason") for s in r.summaries],
        "truths": [None if t is None else bool(t) for t in r.truths],
        "events": [[int(t), k, d] for t, k, d in w.events],
        "violations": len(r.violations()),
        "time_s": [round((s["timing"]["ended"] - s["timing"]["requested"]) / 1e9, 2) for s in r.summaries],
        "cup_final": [round(float(v), 4) for v in w.cup_pose("cup_1")[:3]],
    }
    return out


def experiment(out: Path, scenarios: list[str], seeds: list[int], overrides: Json | None = None) -> Json:
    out.mkdir(parents=True, exist_ok=True)
    cells = []
    for scenario in scenarios:
        for seed in seeds:
            c = cell(scenario, seed, out / f"{scenario}-s{seed}", overrides)
            print(json.dumps({k: c[k] for k in ("scenario", "seed", "outcomes", "verdicts", "truths")}), flush=True)
            cells.append(c)
            (out / "results.json").write_text(json.dumps({"label": LABEL, "cells": cells}, indent=1, default=str) + "\n")
    return {"label": LABEL, "cells": cells}


if __name__ == "__main__":
    seeds = [int(s) for s in sys.argv[3].split(",")] if len(sys.argv) > 3 else [1]
    experiment(Path(sys.argv[1]), sys.argv[2].split(","), seeds, json.loads(sys.argv[4]) if len(sys.argv) > 4 else None)
