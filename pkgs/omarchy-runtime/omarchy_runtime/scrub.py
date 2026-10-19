"""The scrubbing experiment on the simulated Panda, its tables and its videos.

Two interface conditions, the same S1 path in both: motion only, and motion
with a contact channel (a target normal force within bounds). Each crossed
with an error in the plate's height in the belief (perception error), each
cell run with several seeds. Every number comes from ground truth (the
harness's) or from the execution's summary, the same way in both conditions:

- coverage: the share of dirt cells clean at the end (ground truth);
- force within bounds: the share of the raster window (the pad over the
  dirty area, at most 2 cm above the plate's top) with the true normal force
  within the contact channel's bounds (5 to 20 N), in both conditions;
- peak force: the largest true normal force between sponge and plate;
- guard interventions: commands the guard clamped or replaced;
- verifier agreement: the verdict against ground truth;
- attribution: the runtime's, for a failed execution.

Development tools only: it runs the MuJoCo scene (omarchy-runtime-tools).
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime.contracts import Json
from omarchy_runtime.evaluate import agreement, md_table, table
from omarchy_runtime.scenario import run

SCENARIO = "scrub_contact"
CONDITIONS = {"motion only": False, "motion + contact": True}
ERRORS = (-0.02, -0.01, 0.0, 0.01, 0.02)  # m added to the plate's height in the belief
WINDOW_M = 0.02  # the raster window: the pad at most this far above the plate's top
LABEL = (
    "tested in simulation: MuJoCo on the CPU, simulated time, physics and S0 at 1 kHz, S1 at 20 Hz; "
    "the Panda is the MuJoCo Menagerie model, the dirt and its sensor are simulated; no real robot"
)


def _cell(args: tuple[str, int, float, bool, str]) -> Json:
    scenario, seed, error, contact, run_dir = args
    over = {"scenario": {"seed": seed, "s1": {"contact": contact}}, "world": {"sensor": {"plate_bias_z_m": error}}}
    r = run(scenario, Path(run_dir), overrides=over, record_s0=True)
    w: Any = r.world
    log, cfg = w.log(), r.cfg
    lo, hi = cfg["scenario"]["s1"]["force_min_n"], cfg["scenario"]["s1"]["force_max_n"]
    top = cfg["world"]["table"]["center"][2] + cfg["world"]["table"]["half"][2] + cfg["world"]["plate"]["thickness"]
    inner = cfg["world"]["plate"]["half"] - cfg["world"]["dirt"]["margin_m"]
    centre = np.array(cfg["world"]["plate"]["center"])
    ee, force = log["ee"], log["force"]
    window = np.all(np.abs(ee[:, :2] - centre) <= inner, axis=1) & (ee[:, 2] <= top + WINDOW_M)
    plate = log["qpos"][:, w.plate_qpos : w.plate_qpos + 2]
    (s,) = r.summaries
    (truth,) = r.truths
    verdict = s["verdict"] and s["verdict"]["kind"]
    fid = s["fidelity"] or {}
    return {
        "condition": "motion + contact" if contact else "motion only",
        "error_m": error,
        "seed": seed,
        "run_dir": run_dir,
        "outcome": s["outcome"],
        "category": s["category"],
        "verdict": verdict,
        "truth": truth,
        "agreement": agreement(verdict, truth),
        "attribution": s["attribution"] and s["attribution"]["category"],
        "coverage": w.coverage(),
        "window_s": float(window.sum()) / 1000,
        "force_in_bounds": float(np.mean((force[window] >= lo) & (force[window] <= hi))) if window.any() else 0.0,
        "force_mean_n": float(force[window].mean()) if window.any() else 0.0,
        "force_peak_n": float(force.max()),
        "guard_interventions": int(fid.get("guard_clamps", 0) + fid.get("guard_replacements", 0)),
        "s0_fidelity": {k: fid.get(k) for k in ("tracking_error", "contact_mode_share", "force_share", "faithful")},
        "plate_moved_mm": float(1000 * np.linalg.norm(plate - plate[0], axis=1).max()),
        "violations": len(r.violations()),
    }


def experiment(out: Path, seeds: int = 5, jobs: int = 1, scenario: str = SCENARIO) -> Json:
    """Every condition x error x seed (seeds 1..`seeds`), `jobs` runs at a time; writes results.json."""
    out.mkdir(parents=True, exist_ok=True)
    work = [
        (scenario, seed, error, contact, str(out / f"{'contact' if contact else 'motion'}{error:+.2f}-s{seed}"))
        for contact in CONDITIONS.values()
        for error in ERRORS
        for seed in range(1, seeds + 1)
    ]
    if jobs > 1:
        with concurrent.futures.ProcessPoolExecutor(jobs) as pool:
            cells = list(pool.map(_cell, work))
    else:
        cells = [_cell(w) for w in work]
    results = {"label": LABEL, "scenario": scenario, "seeds": seeds, "cells": cells, "summary": summarize(cells)}
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


def _spread(group: Sequence[Json], key: str) -> Json:
    v = [c[key] for c in group]
    return {"mean": float(np.mean(v)), "min": float(min(v)), "max": float(max(v))}


def summarize(cells: Sequence[Json]) -> list[Json]:
    rows = []
    for condition in CONDITIONS:
        for error in ERRORS:
            group = [c for c in cells if c["condition"] == condition and c["error_m"] == error]
            if not group:
                continue

            rows.append(
                {
                    "condition": condition,
                    "error_m": error,
                    "runs": len(group),
                    "outcomes": dict(Counter(c["outcome"] for c in group)),
                    "coverage": _spread(group, "coverage"),
                    "force_in_bounds": _spread(group, "force_in_bounds"),
                    "force_mean_n": _spread(group, "force_mean_n"),
                    "force_peak_n": _spread(group, "force_peak_n"),
                    "guard_interventions": _spread(group, "guard_interventions"),
                    "agreement": dict(Counter(c["agreement"] for c in group)),
                    "attribution": dict(Counter(c["attribution"] or "-" for c in group)),
                    "plate_moved_mm": _spread(group, "plate_moved_mm"),
                }
            )
    return rows


def _range(s: Json, scale: float = 1.0, digits: int = 0) -> str:
    f = f"{{:.{digits}f}}"
    lo, mean, hi = (f.format(scale * s[k]) for k in ("min", "mean", "max"))
    return mean if lo == hi else f"{mean} ({lo} to {hi})"


def _counts(c: Json) -> str:
    return ", ".join(f"{k} {n}" for k, n in sorted(c.items()))


HEADER = [
    "condition",
    "belief error",
    "outcomes",
    "coverage %",
    "force in bounds %",
    "mean force N",
    "peak force N",
    "guard",
    "verdict = truth",
    "attribution",
]


def rows(results: Json) -> list[list[Any]]:
    return [
        [
            r["condition"],
            f"{100 * r['error_m']:+.0f} cm",
            _counts(r["outcomes"]),
            _range(r["coverage"], 100),
            _range(r["force_in_bounds"], 100),
            _range(r["force_mean_n"], 1, 1),
            _range(r["force_peak_n"], 1, 1),
            _range(r["guard_interventions"]),
            f"{r['agreement'].get('yes', 0)} of {r['runs']}",
            _counts(r["attribution"]),
        ]
        for r in results["summary"]
    ]


LEGEND = (
    "belief error: added to the plate's height in the belief (+ : believed higher than it is). Each row: "
    "the runs' mean, then their range. coverage: dirt cells clean at the end (ground truth). force in bounds: "
    "the share of the raster window (the pad over the dirty area, at most 2 cm above the plate) with the true "
    "normal force within 5 to 20 N. guard: commands the guard clamped or replaced. verdict = truth: the "
    "verifier's verdict agrees with ground truth. attribution: the runtime's, for each failed run (- for none)."
)


def report(results: Json) -> str:
    return f"Scrubbing experiment ({results['label']}), {results['seeds']} seeds per row\n" + table(HEADER, rows(results)) + "\n" + LEGEND


def markdown(results: Json, source: str) -> str:
    lines = [f"Scrubbing experiment: {results['label']}. {results['seeds']} seeds per row; {source}.", ""]
    return "\n".join([*lines, *md_table(HEADER, rows(results)), "", LEGEND])


# The videos.

FORCE_SCALE_N = 30.0  # the force bar's full width


def render(run_dir: Path, out: Path, fps: int = 30, size: tuple[int, int] = (720, 1280)) -> Json:
    """An mp4 of a recorded run (`--record-s0`): the scene from its tick-by-tick log, the dirt as it was at each
    frame, and two bars at the bottom: the true normal force (grey below 5 N, green within 5 to 20 N, red above;
    full width 30 N; the band's edges marked) and the plate's coverage (blue). Software rendering, no display."""
    import mujoco

    from omarchy_runtime.sim import panda

    header = json.loads((run_dir / "run.json").read_text())
    cfg = header["config"]
    with np.load(run_dir / "world.npz") as f:
        log = {k: f[k] for k in f.files}
    model = panda.build(cfg["world"])
    data = mujoco.MjData(model)
    height, width = size
    model.vis.global_.offwidth, model.vis.global_.offheight = width, height
    camera = mujoco.MjvCamera()
    camera.lookat[:] = [0.55, 0.0, 0.45]
    camera.distance, camera.azimuth, camera.elevation = 1.25, 150.0, -28.0
    dirt = [model.geom(f"dirt{n}").id for n in range(len(log["cells"]))]
    brown = model.geom_rgba[dirt[0]].copy()
    lo, hi = cfg["scenario"]["s1"]["force_min_n"], cfg["scenario"]["s1"]["force_max_n"]
    t = log["t"]
    frames = np.arange(0, t[-1], 1_000_000_000 // fps)
    command = ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps)]
    command += ["-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(out)]
    encoder = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert encoder.stdin is not None
    renderer = mujoco.Renderer(model, height, width)
    try:
        for when in frames:
            i = min(int(np.searchsorted(t, when)), len(t) - 1)
            data.qpos[:] = log["qpos"][i]
            mujoco.mj_forward(model, data)
            clean = (log["clean_at"] >= 0) & (log["clean_at"] <= when)
            for g, c in zip(dirt, clean, strict=True):
                model.geom_rgba[g] = (0, 0, 0, 0) if c else brown
            renderer.update_scene(data, camera)
            image = renderer.render().copy()
            _bars(image, float(log["force"][i]), lo, hi, float(clean.mean()))
            encoder.stdin.write(image.tobytes())
    finally:
        renderer.close()
        encoder.stdin.close()
    if encoder.wait() != 0:
        raise RuntimeError(f"ffmpeg failed on {out}")
    return {
        "video": str(out),
        "run_dir": str(run_dir),
        "scenario": header["scenario"],
        "seed": header["seed"],
        "config_hash": header["config_hash"],
        "runtime": header["runtime"],
        "s1_contact": cfg["scenario"]["s1"]["contact"],
        "plate_bias_z_m": cfg["world"]["sensor"]["plate_bias_z_m"],
        "frames": len(frames),
        "fps": fps,
        "size": [width, height],
        "label": "rendered from a recorded simulated run: MuJoCo (software rendering, no display), simulated time; no real robot",
        "legend": render.__doc__,
    }


def _bars(image: np.ndarray, force: float, lo: float, hi: float, coverage: float) -> None:
    """Two bars along the bottom of a frame, drawn in place."""
    h, w, _ = image.shape
    pad, bar = 16, 14
    y1, y0 = h - pad, h - pad - bar
    image[y0 - 4 : h - 4, pad - 4 : w - pad + 4] = (25, 25, 30)
    span = w - 2 * pad
    filled = int(span * min(force, FORCE_SCALE_N) / FORCE_SCALE_N)
    colour = (150, 150, 150) if force < lo else (60, 190, 90) if force <= hi else (220, 60, 50)
    image[y0:y1, pad : pad + filled] = colour
    for edge in (lo, hi):
        x = pad + int(span * edge / FORCE_SCALE_N)
        image[y0 - 3 : y1 + 3, x : x + 2] = (240, 240, 240)
    c0, c1 = y0 - 4 - bar // 2 - 6, y0 - 10
    image[c0 - 4 : c1 + 4, pad - 4 : w - pad + 4] = (25, 25, 30)
    image[c0:c1, pad : pad + int(span * coverage)] = (70, 130, 220)
