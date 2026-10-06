"""The S2 placement experiment (RQ5: where should S2 live?).

A cell is one robot run of the two-cup task (scenario s2_placement) with S2
in another process, over one network profile, with one S2 time to first
token (TTFT). The profiles are emulated with tc netem between two VMs by
tests/s2-placement.nix. This module gathers the cells' results into one
results.json and one table, compares every cell with the same task run in
one process on simulated time, and writes the table into the documentation
between two markers. Every number keeps its label: emulated network, VMs,
soft real-time.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omarchy_runtime.contracts import ConfigError, Json
from omarchy_runtime.evaluate import scenario_result, table
from omarchy_runtime.scenario import run
from omarchy_runtime.trace import validate_run

BEGIN, END = "<!-- s2-placement:begin -->", "<!-- s2-placement:end -->"
CLOSE = ("local", "lan")  # profiles held to the simulated task time
KEPT_RATE = 0.01  # S0 kept its rate when it overran in fewer than 1 % of its steps
TOLERANCE = (
    "Wall-clock runs are not bit-identical. Every cell must give the simulated run's outcomes, claims, verdicts and "
    "ground truth exactly, no safety violation, and no S0 tick without a chunk, a fallback or HOLD; outages must be "
    "classified by the deadlines. Task time: local and lan within 15 % of the simulated run with the same time to "
    "first token when S0 kept its rate (overruns in under 1 % of its steps), within 30 % when it did not (a late "
    "loop delays every step, as on an emulated CPU); every profile at least 85 % of it."
)


def _ms(d: Json | None) -> Json | None:
    return None if d is None else {k: None if d[k] is None else d[k] / 1e6 for k in ("p50", "p99", "max")}


def _expected(s: Json) -> list[tuple[Any, ...]]:
    return [(r["outcome"], r["category"], r["claim"], r["verdict"], r["truth"]) for r in s["executions"]]


def _cell(s: Json) -> Json:
    header = json.loads((Path(s["run_dir"]) / "run.json").read_text())
    latency = header["extensions"]["s2_latency"]
    link = s["s2_link"]
    loop = s["loop"]["components"] if s["loop"] else {}
    os_build = header["os"] or {}
    deadlines = {k: v / 1e9 for k, v in link["deadlines_ns"].items()} if link else None
    outages = [
        {"class": o["class"], "duration_s": o["duration_ns"] / 1e9, "from_s": o["from"] / 1e9} for o in (link or {}).get("outages", [])
    ]
    problems = list(s["problems"])
    for o in outages:
        if deadlines and o["class"] != "unrecovered" and o["class"] != ("long" if o["duration_s"] >= deadlines["long"] else "short"):
            problems.append(f"outage of {o['duration_s']:.2f} s classified {o['class']}")
    return {
        "profile": s["tags"].get("profile", "-"),
        "netem": s["tags"].get("netem", ""),
        "scenario": s["scenario"],
        "ttft_s": latency["seconds"],
        "ttft_spread": latency["spread"],
        "latency_model": f"{latency['id']}@{latency['version']}",
        "s2": s["s2"]["placement"],
        "s2_address": s["s2"].get("address"),
        "executions": _expected(s),
        "outcomes": [r["outcome"] for r in s["executions"]],
        "violations": s["violations"],
        "starvation_events": s["starvation_events"],
        "fallback_s": s["task_fallback_s"],
        "hold_s": s["task_hold_s"],
        "task_time_s": s["task_time_s"],
        "idle_fraction": s["idle_fraction"],
        "heartbeats": link["heartbeats"] if link else None,
        "rtt_ms": _ms(link["rtt_ns"]) if link else None,
        "max_gap_s": link["max_gap_ns"] / 1e9 if link else None,
        "deadlines_s": deadlines,
        "outages": outages,
        "s0_hz": header["rates_hz"]["s0"],
        "loop": {
            n: {"steps": c["steps"], "overruns": c["overruns"], "interval_ms": _ms(c["interval_ns"]), "step_ms": _ms(c["step_ns"])}
            for n, c in loop.items()
        },
        "loop_label": s["loop"]["label"] if s["loop"] else None,
        "measured": header["wall_created"],
        "runtime": header["runtime"],
        "os": {k: os_build.get(k) for k in ("version", "revision")} | {"board": os_build.get("robot", {}).get("board")},
        "run_dir": s["run_dir"],
        "problems": problems,
    }


def gather(paths: Sequence[Path]) -> Json:
    """The cells in the evaluation results.json files at `paths`, each compared
    with its simulated reference (the same scenario and latency model, S2 in
    one process, simulated time)."""
    cells = [_cell(s) for path in paths for s in json.loads(path.read_text())["scenarios"]]
    references: dict[tuple[str, float, float], Json] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for c in cells:
            key = (c["scenario"], c["ttft_s"], c["ttft_spread"])
            if key not in references:
                r = run(c["scenario"], Path(tmp) / f"ref-{len(references)}", ttft=key[1], ttft_spread=key[2])
                references[key] = scenario_result(r, validate_run(r.dir))
            ref = references[key]
            c["simulated_task_time_s"] = expected = ref["task_time_s"]
            if c["executions"] != _expected(ref):
                c["problems"].append(f"executions {c['executions']}, the simulated run gives {_expected(ref)}")
            s0 = c["loop"].get("s0")
            c["s0_kept_rate"] = bool(s0) and s0["overruns"] < KEPT_RATE * s0["steps"]
            margin = 0.15 if c["s0_kept_rate"] else 0.30
            if c["profile"] in CLOSE and abs(c["task_time_s"] - expected) > margin * expected:
                c["problems"].append(
                    f"task time {c['task_time_s']:.2f} s, not within {margin * 100:.0f} % of the simulated {expected:.2f} s"
                )
            if c["task_time_s"] < 0.85 * expected:
                c["problems"].append(f"task time {c['task_time_s']:.2f} s, below 85 % of the simulated {expected:.2f} s")
            c["passed"] = not c["problems"]
    first = cells[0] if cells else {}
    emulated = any(c["netem"] not in ("", "none") for c in cells)
    in_vms = bool(cells) and all(c["os"]["board"] == "vm" for c in cells)
    label = f"{'emulated network (tc netem)' if emulated else 'no emulated network'}, {'VMs' if in_vms else 'not in VMs'}, soft real-time"
    return {
        "experiment": "S2 placement (RQ5), two-cup task",
        "label": label,
        "measured": first.get("loop_label"),
        "dates": sorted({c["measured"] for c in cells}),
        "runtime": first.get("runtime"),
        "os": first.get("os"),
        "time": "wall-clock seconds on the robot's monotonic clock; simulated: the same task, S2 in one process, simulated time",
        "rtt": "heartbeat echo measured by S2: the network both ways, plus up to one loop period at each end",
        "tolerance": TOLERANCE,
        "cells": cells,
        "passed": bool(cells) and all(c["passed"] for c in cells),
    }


def _f(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def rows(results: Json) -> tuple[list[str], list[list[str]]]:
    header = [
        "profile",
        "TTFT s",
        "outcomes",
        "violations",
        "S0 starvations",
        "fallback s",
        "HOLD s",
        "task s",
        "simulated s",
        "S2 RTT p50/p99/max ms",
        "outages",
        "S0 interval p99/max ms",
        "S0 overruns",
    ]
    out = []
    for c in results["cells"]:
        rtt, s0 = c["rtt_ms"], c["loop"].get("s0", {})
        outcomes = sorted(set(c["outcomes"]))
        out.append(
            [
                c["profile"],
                _f(c["ttft_s"], 1),
                f"{len(c['outcomes'])}x {outcomes[0]}" if len(outcomes) == 1 else " ".join(c["outcomes"]),
                str(c["violations"]),
                str(c["starvation_events"]),
                _f(c["fallback_s"]),
                _f(c["hold_s"]),
                _f(c["task_time_s"]),
                _f(c["simulated_task_time_s"]),
                "/".join(_f(rtt[k], 1) for k in ("p50", "p99", "max")) if rtt else "-",
                ", ".join(f"{o['class']} {o['duration_s']:.1f} s" for o in c["outages"]) or "none",
                "/".join(_f(s0["interval_ms"][k]) for k in ("p99", "max")) if s0 else "-",
                f"{s0['overruns']} of {s0['steps']}" if s0 else "-",
            ]
        )
    return header, out


def caption(results: Json) -> str:
    rt, os_build = results["runtime"] or {}, results["os"] or {}
    hz = "/".join(str(h) for h in sorted({c["s0_hz"] for c in results["cells"]}))
    days = sorted({f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in results["dates"]})
    when = " to ".join(days[:: max(1, len(days) - 1)])
    system = f"Omarchy Robots {os_build['version']} (board {os_build['board']})" if os_build.get("board") else "no Omarchy Robots identity"
    revision = (rt.get("revision") or "unknown")[:12]
    return (
        f"S2 placement, two-cup task ({results['label']}). Measured {when} with omarchy-runtime {rt.get('version')} ({revision}), "
        f"{system}, S0 at {hz} Hz; {results['measured']}. Task and HOLD times are wall-clock; simulated is the same task "
        f"with S2 in one process on simulated time. S2 RTT: {results['rtt']}."
    )


def report(results: Json) -> str:
    header, body = rows(results)
    parts = [caption(results), "", table(header, body), "", f"Tolerance: {results['tolerance']}"]
    for c in results["cells"]:
        if not c["passed"]:
            parts.append(f"FAILED {c['profile']} TTFT {c['ttft_s']} s: " + "; ".join(c["problems"]))
    parts.append(f"\n{sum(c['passed'] for c in results['cells'])} of {len(results['cells'])} cells within tolerance.")
    return "\n".join(parts)


def markdown(results: Json) -> str:
    header, body = rows(results)
    lines = [caption(results), "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines + ["", f"Tolerance: {results['tolerance']}"])


def write_docs(results: Json, files: Sequence[Path]) -> None:
    """Replace what is between the markers in each file with the table."""
    block = f"{BEGIN}\n{markdown(results)}\n{END}"
    for path in files:
        text = path.read_text()
        start, end = text.find(BEGIN), text.find(END)
        if start < 0 or end < start:
            raise ConfigError(f"{path}: no {BEGIN} ... {END} section")
        path.write_text(text[:start] + block + text[end + len(END) :])
