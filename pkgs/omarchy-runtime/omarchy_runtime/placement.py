"""The S2 placement experiment (RQ5: where should S2 live?).

A cell is one robot run of a scenario with S2 in another process, over one
network profile, with one S2 time to first token (TTFT). The scenarios: the
two-cup task (s2_placement), and three where the scene changes during the
task (cup_moved, slot_blocked, fallback_expiry), for the reaction and the
recovery times. The profiles are emulated with tc netem between two VMs by
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
from omarchy_runtime.evaluate import md_table, scenario_result, table
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
    dyn = s.get("dynamics") or {}
    recovery = (dyn.get("recovery") or [None])[0]
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
        "reaction_s": dyn.get("reaction_s"),
        "recovery": recovery and {k: recovery[k] for k in ("recovery_s", "fallback_engaged", "fallback_s", "fallback_expired")},
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
            c["simulated_reaction_s"] = ref["dynamics"]["reaction_s"]
            c["simulated_recovery_s"] = ref["dynamics"]["recovery"][0]["recovery_s"] if ref["dynamics"]["recovery"] else None
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
        "experiment": "S2 placement (RQ5): the two-cup task and the dynamic scenarios",
        "label": label,
        "measured": first.get("loop_label"),
        "dates": sorted({c["measured"] for c in cells}),
        "runtime": first.get("runtime"),
        "os": first.get("os"),
        "time": "wall-clock seconds on the robot's monotonic clock; simulated: the same task, S2 in one process, simulated time",
        "reaction": "cup_moved: from the move to S0 executing the first chunk computed from an observation taken after it",
        "recovery": (
            "slot_blocked, fallback_expiry: from S1 giving up to the retry running; expired: S0 went from the fallback to HOLD "
            "before the retry's first chunk (the fallback lasts 3 s in slot_blocked, 1.5 s in fallback_expiry)"
        ),
        "rtt": "heartbeat echo measured by S2: the network both ways, plus up to one loop period at each end",
        "tolerance": TOLERANCE,
        "cells": cells,
        "passed": bool(cells) and all(c["passed"] for c in cells),
    }


def _f(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _vs(wall: float | None, simulated: float | None) -> str:
    return "-" if wall is None else f"{wall:.2f} ({_f(simulated)})"


def _recovery(c: Json | None) -> str:
    if c is None or c["recovery"] is None:
        return "-"
    return _vs(c["recovery"]["recovery_s"], c["simulated_recovery_s"]) + (", expired" if c["recovery"]["fallback_expired"] else "")


def summary(results: Json) -> tuple[list[str], list[list[str]]]:
    """One row for each profile and TTFT, every scenario's cell in it: the
    two-cup task time, cup_moved's reaction, the recovery times (wall-clock,
    and simulated in parentheses), and safety over the row's cells."""
    header = [
        "profile",
        "TTFT s",
        "two-cup task s",
        "cup_moved reaction s",
        "slot_blocked recovery s",
        "fallback_expiry recovery s",
        "fallbacks expired",
        "violations",
        "S0 starvations",
        "cells within tolerance",
    ]
    groups: dict[tuple[str, float], dict[str, Json]] = {}
    for c in results["cells"]:
        groups.setdefault((c["profile"], c["ttft_s"]), {})[c["scenario"]] = c
    out = []
    for (profile, ttft), by in groups.items():
        cells = list(by.values())
        two_cup, moved = by.get("s2_placement"), by.get("cup_moved")
        recovered = [c for c in cells if c["recovery"]]
        out.append(
            [
                profile,
                _f(ttft, 1),
                _vs(two_cup["task_time_s"], two_cup["simulated_task_time_s"]) if two_cup else "-",
                _vs(moved["reaction_s"], moved["simulated_reaction_s"]) if moved else "-",
                _recovery(by.get("slot_blocked")),
                _recovery(by.get("fallback_expiry")),
                f"{sum(c['recovery']['fallback_expired'] for c in recovered)} of {len(recovered)}" if recovered else "-",
                str(sum(c["violations"] for c in cells)),
                str(sum(c["starvation_events"] for c in cells)),
                f"{sum(c['passed'] for c in cells)} of {len(cells)}",
            ]
        )
    return header, out


def rows(results: Json) -> tuple[list[str], list[list[str]]]:
    header = [
        "scenario",
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
        "reaction s",
        "recovery s",
    ]
    out = []
    for c in results["cells"]:
        rtt, s0 = c["rtt_ms"], c["loop"].get("s0", {})
        outcomes = sorted(set(c["outcomes"]))
        out.append(
            [
                c["scenario"],
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
                _vs(c["reaction_s"], c["simulated_reaction_s"]),
                _recovery(c),
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
        f"S2 placement, the two-cup task and the dynamic scenarios ({results['label']}). "
        f"Measured {when} with omarchy-runtime {rt.get('version')} ({revision}), "
        f"{system}, S0 at {hz} Hz; {results['measured']}. Times are wall-clock; in parentheses and in the simulated column, "
        f"the same task with S2 in one process on simulated time. Reaction, {results['reaction']}. Recovery, "
        f"{results['recovery']}. S2 RTT: {results['rtt']}."
    )


def report(results: Json) -> str:
    parts = [caption(results), "", table(*summary(results)), "", table(*rows(results)), "", f"Tolerance: {results['tolerance']}"]
    for c in results["cells"]:
        if not c["passed"]:
            parts.append(f"FAILED {c['scenario']} {c['profile']} TTFT {c['ttft_s']} s: " + "; ".join(c["problems"]))
    parts.append(f"\n{sum(c['passed'] for c in results['cells'])} of {len(results['cells'])} cells within tolerance.")
    return "\n".join(parts)


def markdown(results: Json, source: str | None = None) -> str:
    """The tables in Markdown, every cell in a collapsed section; `source` says
    where the results come from (a CI run, a host)."""
    text = caption(results) + (f" Source: {source}." if source else "")
    every = f"<details><summary>Every cell ({len(results['cells'])})</summary>"
    lines = [text, "", *md_table(*summary(results)), "", every, "", *md_table(*rows(results)), "", "</details>"]
    return "\n".join(lines + ["", f"Tolerance: {results['tolerance']}"])


def write_block(files: Sequence[Path], name: str, text: str) -> None:
    """Replace what is between the markers `<!-- name:begin -->` and
    `<!-- name:end -->` in each file with `text`."""
    begin, end = f"<!-- {name}:begin -->", f"<!-- {name}:end -->"
    for path in files:
        old = path.read_text()
        start, stop = old.find(begin), old.find(end)
        if start < 0 or stop < start:
            raise ConfigError(f"{path}: no {begin} ... {end} section")
        path.write_text(old[:start] + f"{begin}\n{text}\n{end}" + old[stop + len(end) :])


def write_docs(results: Json, files: Sequence[Path], source: str | None = None) -> None:
    """Replace what is between the markers in each file with the tables."""
    write_block(files, "s2-placement", markdown(results, source))
