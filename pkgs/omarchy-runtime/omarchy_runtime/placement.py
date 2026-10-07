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
import math
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime.contracts import ConfigError, Json
from omarchy_runtime.evaluate import md_table, scenario_result, table
from omarchy_runtime.scenario import run
from omarchy_runtime.trace import validate_run

BEGIN, END = "<!-- s2-placement:begin -->", "<!-- s2-placement:end -->"
CLOSE = ("local", "lan")  # profiles held to the simulated task time
KEPT_RATE = 0.01  # S0 kept its rate when it overran in fewer than 1 % of its steps
TOLERANCE = (
    "Wall-clock runs are not bit-identical. Every cell must give the simulated run's outcomes, claims, verdicts and"
    " ground truth exactly, no safety violation, and no S0 tick without a chunk, a fallback or HOLD; outages must "
    "be classified by the deadlines; every execution's fallback coverage at least 99 %. Task time: local and lan "
    "within 15 % of the simulated run with the same time to first token when S0 kept its rate (overruns in under 1 "
    "% of its steps), within 30 % when it did not (a late loop delays every step, as on an emulated CPU); every "
    "profile at least 85 % of it."
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
        "distribution": latency.get("distribution"),
        "ttft_label": f"{latency['distribution']} (median {latency['seconds']:.2f} s)"
        if latency.get("distribution")
        else f"{latency['seconds']:.1f}",
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
        "recovery": recovery
        and {k: recovery.get(k) for k in ("recovery_s", "fallback_engaged", "fallback", "fallback_s", "fallback_expired")},
        "fallback_coverage": s.get("fallback_coverage"),
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
    references: dict[tuple[str, float, float | None, str | None], Json] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for c in cells:
            key = (c["scenario"], c["ttft_s"], c["ttft_spread"], c["distribution"])
            if key not in references:
                where = Path(tmp) / f"ref-{len(references)}"
                if c["distribution"]:
                    r = run(c["scenario"], where, ttft_file=c["distribution"])
                else:
                    r = run(c["scenario"], where, ttft=c["ttft_s"], ttft_spread=c["ttft_spread"])
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
    return _results(cells)


def merge(paths: Sequence[Path]) -> Json:
    """Placement results (results.json written by `placement --out`) from
    several runs of the experiment, one table: the full matrix split over
    CI jobs. Each cell remembers the artifact it came from, for its trace."""
    cells = []
    for path in paths:
        for c in json.loads(path.read_text())["cells"]:
            cells.append(c | {"artifact": c.get("artifact") or str(path.resolve().parent.parent)})
    return _results(cells)


def _results(cells: list[Json]) -> Json:
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
            "slot_blocked, fallback_expiry: from S1 giving up to the retry running; expired: S2's fallback (3 s in slot_blocked, "
            "1.5 s in fallback_expiry) ran out before the retry started, and S0 went on under the capability's default"
        ),
        "coverage": "the least fraction of an execution's running time during which S0 held a valid fallback, as S0 counted it",
        "rtt": "heartbeat echo measured by S2: the network both ways, plus up to one loop period at each end",
        "interval": INTERVAL,
        "tolerance": TOLERANCE,
        "cells": cells,
        "groups": groups(cells),
        "passed": bool(cells) and all(c["passed"] for c in cells),
    }


INTERVAL = (
    "median of a cell's runs, and in brackets a distribution-free confidence interval for that median from order "
    "statistics: the narrowest pair (k-th smallest, k-th largest) whose binomial coverage is at least 95 % (with 6 runs, "
    "the smallest and the largest, 96.9 %); no interval below 6 runs"
)


def median_interval(values: Sequence[float]) -> Json:
    """The median, and a confidence interval for it of at least 95 %
    coverage from order statistics (None below 6 values)."""
    x, n = sorted(values), len(values)
    out: Json = {"median": float(np.median(x)), "low": None, "high": None, "coverage": None, "n": n}

    def coverage(k: int) -> float:  # of [x(k), x(n+1-k)], 1-indexed: 1 - 2 P(B <= k - 1), B ~ Binomial(n, 1/2)
        return 1.0 - 2.0 * float(sum(math.comb(n, i) for i in range(k))) / float(2**n)

    k = 1
    while k + 1 <= n // 2 and coverage(k + 1) >= 0.95:
        k += 1
    if n >= 2 and coverage(k) >= 0.95:
        out |= {"low": x[k - 1], "high": x[n - k], "coverage": coverage(k)}
    return out


def groups(cells: Sequence[Json]) -> list[Json]:
    """The runs of each (scenario, profile, TTFT), in the order they came."""
    by: dict[tuple[str, str, str], list[Json]] = {}
    for c in cells:
        by.setdefault((c["scenario"], c["profile"], c.get("ttft_label") or f"{c['ttft_s']:.1f}"), []).append(c)
    out = []
    for (scenario, profile, label), runs in by.items():
        first = runs[0]

        def stat(values: list[float | None]) -> Json | None:
            got = [v for v in values if v is not None]
            return median_interval(got) if got else None

        recovered = [c for c in runs if c["recovery"]]
        covered = [c["fallback_coverage"] for c in runs if c.get("fallback_coverage") is not None]
        out.append(
            {
                "scenario": scenario,
                "profile": profile,
                "ttft_s": first["ttft_s"],
                "ttft_label": label,
                "runs": len(runs),
                "passed": sum(c["passed"] for c in runs),
                "violations": sum(c["violations"] for c in runs),
                "starvation_events": sum(c["starvation_events"] for c in runs),
                "outcomes": sorted({" ".join(c["outcomes"]) for c in runs}),
                "task_time_s": stat([c["task_time_s"] for c in runs]),
                "reaction_s": stat([c["reaction_s"] for c in runs]),
                "recovery_s": stat([c["recovery"]["recovery_s"] for c in recovered]),
                "simulated": {
                    "task_time_s": first["simulated_task_time_s"],
                    "reaction_s": first["simulated_reaction_s"],
                    "recovery_s": first["simulated_recovery_s"],
                },
                "engaged": sum(bool(c["recovery"]["fallback_engaged"]) for c in recovered),
                "expired": sum(bool(c["recovery"]["fallback_expired"]) for c in recovered),
                "recoveries": len(recovered),
                "fallback_coverage": min(covered) if covered else None,
                "rtt_p50_ms": stat([c["rtt_ms"]["p50"] if c["rtt_ms"] else None for c in runs]),
                "long_outage_s": stat([max((o["duration_s"] for o in c["outages"] if o["class"] == "long"), default=None) for c in runs]),
                "s0_overruns_max": max((c["loop"]["s0"]["overruns"] for c in runs if c["loop"].get("s0")), default=None),
            }
        )
    return out


def _f(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _stat(s: Json | None, simulated: float | None = None) -> str:
    """median [interval] (simulated)."""
    if s is None:
        return "-"
    text = f"{s['median']:.2f}"
    if s["low"] is not None:
        text += f" [{s['low']:.2f}, {s['high']:.2f}]"
    return text + (f" ({simulated:.2f})" if simulated is not None else "")


def _coverage(cells: Sequence[Json]) -> str:
    covered = [c["fallback_coverage"] for c in cells if c.get("fallback_coverage") is not None]
    return f"{100 * min(covered):.1f} %" if covered else "-"


def _ordered(results: Json) -> list[tuple[tuple[str, str], dict[str, Json]]]:
    rows: dict[tuple[str, str], dict[str, Json]] = {}
    for g in results.get("groups") or groups(results["cells"]):
        rows.setdefault((g["profile"], g["ttft_label"]), {})[g["scenario"]] = g
    return list(rows.items())


def summary(results: Json) -> tuple[list[str], list[list[str]]]:
    """One row for each profile and TTFT, every scenario's cell in it: the
    two-cup task time, cup_moved's reaction, the recovery times (the median
    of the runs, its interval in brackets, the simulated run in parentheses),
    and S2's fallbacks expired, the fallback coverage and safety over the
    row's runs."""
    header = [
        "profile",
        "TTFT s",
        "two-cup task s",
        "cup_moved reaction s",
        "slot_blocked recovery s",
        "fallback_expiry recovery s",
        "S2 fallbacks expired",
        "fallback coverage",
        "violations",
        "S0 starvations",
        "runs within tolerance",
    ]
    out = []
    for (profile, label), by in _ordered(results):
        gs = list(by.values())
        two_cup, moved, blocked, expiry = (by.get(s) for s in ("s2_placement", "cup_moved", "slot_blocked", "fallback_expiry"))
        recoveries = sum(g["recoveries"] for g in gs)
        out.append(
            [
                profile,
                label,
                _stat(two_cup["task_time_s"], two_cup["simulated"]["task_time_s"]) if two_cup else "-",
                _stat(moved["reaction_s"], moved["simulated"]["reaction_s"]) if moved else "-",
                _stat(blocked["recovery_s"], blocked["simulated"]["recovery_s"]) if blocked else "-",
                _stat(expiry["recovery_s"], expiry["simulated"]["recovery_s"]) if expiry else "-",
                f"{sum(g['expired'] for g in gs)} of {recoveries}" if recoveries else "-",
                _coverage(gs),
                str(sum(g["violations"] for g in gs)),
                str(sum(g["starvation_events"] for g in gs)),
                f"{sum(g['passed'] for g in gs)} of {sum(g['runs'] for g in gs)}",
            ]
        )
    return header, out


def rows(results: Json) -> tuple[list[str], list[list[str]]]:
    """One row for each cell (scenario, profile, TTFT), over its runs."""
    header = [
        "scenario",
        "profile",
        "TTFT s",
        "runs",
        "outcomes",
        "violations",
        "S0 starvations",
        "task s (simulated)",
        "reaction s",
        "recovery s",
        "fallback engaged",
        "S2 fallbacks expired",
        "fallback coverage",
        "S2 RTT p50 ms",
        "long outage s",
        "S0 overruns, most",
        "runs within tolerance",
    ]
    out = []
    for _, by in _ordered(results):
        for g in by.values():
            n = g["recoveries"]
            out.append(
                [
                    g["scenario"],
                    g["profile"],
                    g["ttft_label"],
                    str(g["runs"]),
                    "; ".join(g["outcomes"]),
                    str(g["violations"]),
                    str(g["starvation_events"]),
                    _stat(g["task_time_s"], g["simulated"]["task_time_s"]),
                    _stat(g["reaction_s"]),
                    _stat(g["recovery_s"]),
                    f"{g['engaged']} of {n}" if n else "-",
                    f"{g['expired']} of {n}" if n else "-",
                    _coverage([g]),
                    _stat(g["rtt_p50_ms"]),
                    _stat(g["long_outage_s"]),
                    "-" if g["s0_overruns_max"] is None else str(g["s0_overruns_max"]),
                    f"{g['passed']} of {g['runs']}",
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
        f"the same task with S2 in one process on simulated time. Values: {results.get('interval', 'one run per cell')}. "
        f"Reaction, {results['reaction']}. Recovery, "
        f"{results['recovery']}. Fallback coverage, {results.get('coverage', 'not measured')}. S2 RTT: {results['rtt']}."
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
    # markdown="1": the manual's kramdown reads Markdown inside the HTML element only then.
    every = f'<details markdown="1"><summary>Every cell ({len(results["cells"])})</summary>'
    lines = [text, "", *md_table(*summary(results)), "", every, "", *md_table(*rows(results)), "", "</details>"]
    return "\n".join(lines + ["", f"Tolerance: {results['tolerance']}"])


def _local(results_path: Path, cell: Json) -> Path:
    """A cell's run directory: where it was written, or under the `runs`
    directory of the downloaded CI artifact it came from (`artifact`, set by
    merge; else the one around `results_path`)."""
    path = Path(cell["run_dir"])
    if path.exists() or "/runs/" not in cell["run_dir"]:
        return path
    artifact = Path(cell["artifact"]) if cell.get("artifact") else results_path.parent.parent
    return artifact / "runs" / str(cell["run_dir"]).split("/runs/", 1)[1]


def _held(changes: Sequence[tuple[int, str]], t0: int, t1: int) -> int:
    """Time in HOLD between t0 and t1, from S0's mode changes (time, new mode)."""
    mode = next((to for t, to in reversed(changes) if t <= t0), None)
    total, last = 0, t0
    for t, to in changes:
        if t0 < t < t1:
            total += t - last if mode == "HOLD" else 0
            mode, last = to, t
    return total + (t1 - last if mode == "HOLD" else 0)


def trace_fallbacks(run_dir: Path) -> Json:
    """From a run's events, the same way for a trace of any schema since 2:
    each execution's fallback coverage, the fraction of S0's status samples
    (taken by the supervisor, 20 Hz) while it ran that report a fallback
    installed, leaving out the sample at its start (taken before the runtime
    installs anything); and for each execution after a failed one, the time
    S0 spent in HOLD from the failure to that execution's start (from S0's
    mode changes, as the supervisor saw them)."""
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    spans: dict[str, list[int | None]] = {}
    failed: dict[str, int] = {}
    order: list[str] = []
    for e in events:
        if e["type"] == "lifecycle":
            ex, to = e["execution"], e["payload"]["to"]
            if to == "RUNNING":
                spans[ex] = [e["t"], None]
                order.append(ex)
            elif ex in spans and spans[ex][1] is None:
                spans[ex][1] = e["t"]
            if to == "FAILED":
                failed[ex] = e["t"]
    coverage: dict[str, float | None] = {}
    for ex, (a, b) in spans.items():
        assert a is not None
        samples = [
            bool(e["payload"]["fallback_installed"])
            for e in events
            if e["type"] == "s0.status" and e["execution"] == ex and a < e["t"] and (b is None or e["t"] <= b)
        ]
        coverage[ex] = sum(samples) / len(samples) if samples else None
    changes = [(e["t"], e["payload"]["to"]) for e in events if e["type"] == "s0.mode"]
    waits = []
    for before, after in zip(order, order[1:], strict=False):
        start = spans[after][0]
        if before in failed and start is not None:
            waits.append({"failed": before, "next": after, "hold_s": _held(changes, failed[before], start) / 1e9})
    return {"coverage": coverage, "waits": waits}


def compare(before: Path, after: Path) -> Json:
    """Two placement results (results.json written by `placement --out`, with
    the cells' traces beside them), compared for each profile and TTFT:
    fallback coverage and the HOLD time while S2 decided (from the traces,
    the same way for both), how often the fallback engaged and S2's expired
    in a recovery, and the recovery times (from the results)."""
    rows: dict[tuple[str, float], Json] = {}
    sources = {}
    for side, path in (("before", before), ("after", after)):
        results = json.loads(path.read_text())
        sources[side] = {"runtime": results["runtime"], "label": results["label"], "measured": results["measured"]}
        for c in results["cells"]:
            measures = trace_fallbacks(_local(path, c))
            row = rows.setdefault((c["profile"], c["ttft_s"]), {"profile": c["profile"], "ttft_s": c["ttft_s"]})
            mine = row.setdefault(side, {"coverage": [], "hold_s": [], "engaged": [], "expired": [], "recovery_s": {}})
            mine["coverage"] += [v for v in measures["coverage"].values() if v is not None]
            mine["hold_s"] += [w["hold_s"] for w in measures["waits"]]
            if c["recovery"]:
                mine["engaged"].append(bool(c["recovery"]["fallback_engaged"]))
                mine["expired"].append(bool(c["recovery"]["fallback_expired"]))
                mine["recovery_s"][c["scenario"]] = c["recovery"]["recovery_s"]
    return {"sources": sources, "rows": list(rows.values())}


def _side(row: Json, side: str, what: str) -> str:
    m = row.get(side)
    if m is None:
        return "-"
    if what == "coverage":
        return f"{100 * min(m['coverage']):.1f} %" if m["coverage"] else "-"
    if what == "hold_s":
        return f"{max(m['hold_s']):.2f}" if m["hold_s"] else "-"
    if what in ("engaged", "expired"):
        return f"{sum(m[what])} of {len(m[what])}" if m[what] else "-"
    value = m["recovery_s"].get(what)
    return "-" if value is None else f"{value:.2f}"


def compare_markdown(comparison: Json, before: str, after: str) -> str:
    header = [
        "profile",
        "TTFT s",
        "fallback coverage",
        "fallback engaged in recoveries",
        "S2 fallbacks expired",
        "HOLD while S2 decides s",
        "slot_blocked recovery s",
        "fallback_expiry recovery s",
    ]
    whats = ["coverage", "engaged", "expired", "hold_s", "slot_blocked", "fallback_expiry"]
    body = [
        [r["profile"], _f(r["ttft_s"], 1)] + [f"{_side(r, 'before', w)} → {_side(r, 'after', w)}" for w in whats]
        for r in comparison["rows"]
    ]
    text = (
        f'Before and after, each cell "before → after". Before: {before}. After: {after}. Coverage: the least, over the row\'s '
        "executions, of the fraction of S0's status samples (the supervisor's, 20 Hz) taken while an execution ran that report "
        "a fallback installed; HOLD while S2 decides: the longest time S0 spent in HOLD from a failure to the next execution's "
        "start, from S0's mode changes as the supervisor saw them; both from the traces, the same way for both runs. Engaged, "
        "expired and recovery: from each run's results (expired: before, S0 went from S2's fallback to HOLD; after, S2's ran "
        "out and the capability's default took over)."
    )
    return "\n".join([text, "", *md_table(header, body)])


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
