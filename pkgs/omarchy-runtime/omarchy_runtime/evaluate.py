"""The evaluation harness: runs scenarios, checks every trace, compares each
execution with ground truth and with what its scenario expects, and writes
results.json. Durations are simulated time; the overhead figures are
wall-clock, labelled with where they were measured."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime import __version__
from omarchy_runtime.contracts import SECOND, Json, seconds
from omarchy_runtime.extensions import CORRECTORS, INFERENCE_TRIGGERS
from omarchy_runtime.scenario import RunResult, run
from omarchy_runtime.trace import S0_MODES, TraceCheck, validate_run, where_measured

TRACKING, FALLBACK, HOLD = (S0_MODES.index(m) for m in ("TRACKING", "FALLBACK", "HOLD"))


def agreement(verdict: str | None, truth: bool | None) -> str:
    if verdict is None or truth is None:
        return "n/a"
    if verdict == "INCONCLUSIVE":
        return f"inconclusive (truth {str(truth).lower()})"
    return "yes" if (verdict == "SATISFIED") == truth else "NO"


def _invariants(r: RunResult) -> list[str]:
    """Safety and data-plane invariants, from ground truth and S0's own record."""
    problems = []
    rec = r.record()
    tick = SECOND // r.cfg["world"]["rates_hz"]["s0"]
    bound = r.s0.bound * (1 + 1e-9) + 1e-12
    if len(rec["t"]) > 1 and np.any(np.abs(np.diff(rec["position"], axis=0)) > bound):
        problems.append("S0 commanded a step above its bound (velocity limit times the nominal period)")
    if len(rec["t"]) != r.s0.c["ticks"]:
        problems.append("S0's record is incomplete")
    elif r.clock == "simulated" and not r.s0_timing_faults and np.any(np.diff(rec["t"]) != tick):
        problems.append("S0 skipped a tick")
    elif np.any(np.diff(rec["t"]) <= 0):
        problems.append("S0's ticks do not go forward in time")
    if len(rec["mode"]) and (rec["mode"].min() < 0 or rec["mode"].max() > S0_MODES.index("HOLD")):
        problems.append("S0 ticked without a chunk, a fallback or HOLD")
    for t, what in r.violations()[:5]:
        problems.append(f"ground-truth violation at {seconds(t):.3f} s: {what}")
    return problems


def _expected(r: RunResult) -> list[str]:
    expect = r.cfg["scenario"].get("expect", [])
    got = list(zip(r.summaries, r.truths, strict=True))
    if len(expect) != len(got):
        return [f"{len(got)} execution(s), the scenario expects {len(expect)}"]
    problems = []
    for n, (e, (s, truth)) in enumerate(zip(expect, got, strict=True), 1):
        actual = {
            "outcome": s["outcome"],
            "category": s["category"],
            "claim": s["claim"] and s["claim"]["claim"],
            "verdict": s["verdict"] and s["verdict"]["kind"],
            "truth": truth,
        }
        for key, value in actual.items():
            if e.get(key) != value:
                problems.append(f"execution {n}: {key} is {value}, expected {e.get(key)}")
    return problems


def dynamics(r: RunResult) -> Json:
    """How the robot met a changing scene, from ground truth, the observation
    log and S0's per-tick record (seconds, on the run's clock):
    - reaction: from a scene change (a move) to S0 executing the first chunk
      computed from an observation taken after it; freeze: from the first
      such observation reaching S1 to that tick (S0 executing a plan the
      belief already contradicts);
    - recovery, for each failed execution its retry follows: from the
      failure to the retry running, the time S0 spent in the fallback
      meanwhile, whether the fallback engaged, and whether it expired (S0
      went from FALLBACK to HOLD) before the retry's first chunk;
    - discontinuity at chunk switches: the largest jump of S0's desired
      position, and of its desired velocity, where one chunk takes over
      from another (action-space units, and per second)."""
    rec = r.record()
    t, mode, seq, seen, desired = rec["t"], rec["mode"], rec["seq"], rec["observed_at"], rec["desired"]
    out: Json = {
        "scene_changes": [],
        "reaction_s": None,
        "freeze_s": None,
        "recovery": [],
        "discontinuity": None,
        "inferences": r.inferences,
    }
    for when, kind, detail in r.world.events:
        if kind in ("move", "appear"):
            out["scene_changes"].append({"t_s": seconds(when), "kind": kind, "detail": detail})
    moves = [when for when, kind, _ in r.world.events if kind == "move"]
    if moves:
        tc = moves[0]
        # Strictly after: an observation in the move's own tick was taken before it (the sensor steps first).
        reacted = np.flatnonzero((t > tc) & (seen > tc) & (mode == TRACKING))
        if len(reacted):
            tr = int(t[reacted[0]])
            out["reaction_s"] = seconds(tr - tc)
            delivered = [d for taken, d in r.observations if taken > tc]
            out["freeze_s"] = seconds(tr - delivered[0]) if delivered else None
    by_id = {s["execution_id"]: s for s in r.summaries}
    for s in r.summaries:
        failed = by_id.get(s["retry_of"] or "")
        if failed is None or failed["outcome"] != "FAILED" or s["timing"]["started"] is None:
            continue
        t0, t1 = failed["timing"]["ended"], s["timing"]["started"]
        after = np.flatnonzero((t > t1) & (mode == TRACKING))
        t2 = int(t[after[0]]) if len(after) else t1
        window = (t >= t0) & (t <= t2)
        modes, spans = mode[window], np.diff(np.r_[t[window], t2])
        was_fallback = np.flatnonzero(modes == FALLBACK)
        expired = bool(len(was_fallback)) and bool(np.any(modes[was_fallback[0] :] == HOLD))
        out["recovery"].append(
            {
                "failed": failed["execution_id"],
                "retry": s["execution_id"],
                "reason": failed["reason"],
                "recovery_s": seconds(t1 - t0),
                "fallback_engaged": bool(len(was_fallback)),
                "fallback_s": seconds(int(spans[modes == FALLBACK].sum())),
                "fallback_expired": expired,
            }
        )
    switch = np.flatnonzero((seq[1:] != seq[:-1]) & (mode[1:] == TRACKING) & (mode[:-1] == TRACKING)) + 1
    switch = switch[(switch >= 2) & (switch < len(t) - 1)]
    if len(switch):
        jumps = np.abs(desired[switch] - desired[switch - 1]).max(axis=1)
        dt = np.diff(t).astype(float) / 1e9
        v_after = (desired[switch + 1] - desired[switch]) / dt[switch, None]
        v_before = (desired[switch - 1] - desired[switch - 2]) / dt[switch - 2, None]
        out["discontinuity"] = {
            "switches": len(switch),
            "position": float(jumps.max()),
            "velocity": float(np.abs(v_after - v_before).max()),
        }
    return out


def scenario_result(r: RunResult, check: TraceCheck) -> Json:
    traced = {s["execution_id"] for s in check.executions}
    rows = []
    for s, truth in zip(r.summaries, r.truths, strict=True):
        timing = s["timing"]
        verdict = s["verdict"] and s["verdict"]["kind"]
        rows.append(
            {
                "scenario": r.name,
                "body": s["body"],
                "execution": s["execution_id"],
                "task": s["task_id"],
                "retry_of": s["retry_of"],
                "goal": s["goal"],
                "outcome": s["outcome"],
                "category": s["category"],
                "claim": s["claim"] and s["claim"]["claim"],
                "verdict": verdict,
                "truth": truth,
                "agreement": agreement(verdict, truth),
                "duration_s": seconds(timing["duration_ns"]),
                "fallback_s": seconds(timing["fallback_ns"]),
                "hold_s": seconds(timing["hold_ns"]),
                "violations": len(r.violations(timing["requested"], timing["ended"])),
                "trace_complete": check.complete and s["execution_id"] in traced,
                "wall": s["wall"],
            }
        )
    end = max((s["timing"]["ended"] for s in r.summaries), default=r.end)
    task = r.record(0, end)
    modes = task["mode"]
    # Time in each mode over the whole task, between executions too: tick t holds its mode until the next tick.
    spans = np.diff(np.r_[task["t"], end]) if len(task["t"]) else np.zeros(0, dtype=np.int64)
    problems = _invariants(r) + _expected(r) + [f"trace: {e}" for e in check.errors]
    if not r.completed:
        problems.append("the time limit ended the run")
    return {
        "scenario": r.name,
        "description": r.cfg["scenario"].get("description", ""),
        "body": r.cfg["body"]["id"],
        "run_dir": str(r.dir),
        "s2_latency_s": r.cfg["scenario"]["s2"]["latency_s"],
        "clock": r.clock,
        "tags": dict(r.tags),
        "s2": r.s2,
        "s2_link": r.s2_link,
        "task_time_s": seconds(end),
        "idle_fraction": float(np.mean(modes != TRACKING)) if len(modes) else 0.0,
        "task_fallback_s": seconds(int(spans[modes == FALLBACK].sum())),
        "task_hold_s": seconds(int(spans[modes == HOLD].sum())),
        "loop": r.loop,
        "dynamics": dynamics(r),
        "violations": len(r.violations()),
        "starvation_events": sum(s["s0"]["starvation_events"] for s in r.summaries),
        "fallback_s": sum(row["fallback_s"] for row in rows),
        "hold_s": sum(row["hold_s"] for row in rows),
        "trace_complete": check.complete,
        "executions": rows,
        "problems": problems,
        "passed": not problems,
    }


def evaluate(names: Sequence[str], out: Path, *, record_s0: bool = False, **options: Any) -> Json:
    """Run each scenario (options go to scenario.run) and write results.json."""
    scenarios = []
    for name in names:
        r = run(name, out / name, record_s0=record_s0, **options)
        result = scenario_result(r, validate_run(r.dir))
        (r.dir / "results.json").write_text(json.dumps(result, indent=2) + "\n")
        scenarios.append(result)
    rows = [row for s in scenarios for row in s["executions"]]
    pairs = Counter((row["verdict"], row["truth"]) for row in rows if row["verdict"] is not None)
    missed_by_claim = [row for row in rows if row["claim"] == "DONE" and row["truth"] is False]
    walls: dict[str, list[int]] = {k: [row["wall"][k] for row in rows] for k in ("resolution_ns", "verification_ns", "trace_write_ns")}
    results = {
        "runtime": __version__,
        "time": TIME[options.get("clock", "simulated")],
        "scenarios": scenarios,
        "verifier_vs_truth": {
            "pairs": [{"verdict": v, "truth": t, "count": n} for (v, t), n in sorted(pairs.items(), key=lambda p: (p[0][0], str(p[0][1])))],
            "claim_done_but_false": len(missed_by_claim),
            "caught_by_verifier": sum(row["verdict"] == "VIOLATED" for row in missed_by_claim),
            "inconclusive": sum(row["verdict"] == "INCONCLUSIVE" for row in rows),
            "with_verdict": sum(pairs.values()),
        },
        "s2_latency": sorted(
            (
                {
                    "scenario": s["scenario"],
                    "s2_latency_s": s["s2_latency_s"],
                    "outcomes": [row["outcome"] for row in s["executions"]],
                    "task_time_s": s["task_time_s"],
                    "idle_fraction": s["idle_fraction"],
                    "violations": s["violations"],
                    "starvation_events": s["starvation_events"],
                    "fallback_s": s["fallback_s"],
                }
                for s in scenarios
                if s["scenario"].startswith("s2_latency")
            ),
            key=lambda s: s["s2_latency_s"],
        ),
        "overhead": {
            "measured": where_measured(),
            "per_execution_us": {
                k.removesuffix("_ns"): {"mean": float(np.mean(v)) / 1e3, "max": max(v) / 1e3} for k, v in walls.items() if v
            },
        },
        "passed": all(s["passed"] for s in scenarios),
    }
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


TIME = {
    "simulated": "durations are simulated time; nothing here is real-time",
    "wall": "durations are wall-clock time on the runtime's monotonic clock, soft real-time in Python; nothing here is real-time",
}


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[_cell(v) for v in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(header)]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in cells]
    return "\n".join(out)


def md_table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    body = [[_cell(v) for v in row] for row in rows]
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in body]


def report(results: Json) -> str:
    rows = [
        [
            r["scenario"],
            r["body"],
            r["outcome"],
            r["category"],
            r["claim"],
            r["verdict"],
            r["truth"],
            r["agreement"],
            r["duration_s"],
            f"{r['fallback_s']:.2f}/{r['hold_s']:.2f}",
            r["violations"],
            r["trace_complete"],
        ]
        for s in results["scenarios"]
        for r in s["executions"]
    ]
    header = [
        "scenario",
        "body",
        "outcome",
        "category",
        "claim",
        "verdict",
        "truth",
        "agreement",
        "duration s",
        "fallback/hold s",
        "violations",
        "trace complete",
    ]
    clock = "simulated time" if results["time"] == TIME["simulated"] else "wall-clock time, soft real-time"
    parts = [f"Executions ({clock}; truth is the simulator's ground truth, never seen by the runtime)", table(header, rows)]
    v = results["verifier_vs_truth"]
    pairs = ", ".join(f"{p['verdict']} with truth {_cell(p['truth'])}: {p['count']}" for p in v["pairs"])
    parts.append(
        f"\nVerifier against ground truth: {pairs}.\n"
        f"Executor claimed DONE while the goal was false: {v['claim_done_but_false']} time(s); "
        f"the verifier caught {v['caught_by_verifier']}. Inconclusive verdicts: {v['inconclusive']} of {v['with_verdict']}."
    )
    if results["s2_latency"]:
        lat = [
            [
                s["s2_latency_s"],
                " ".join(s["outcomes"]),
                s["task_time_s"],
                f"{100 * s['idle_fraction']:.0f}%",
                s["violations"],
                s["starvation_events"],
            ]
            for s in results["s2_latency"]
        ]
        parts.append(f"\nS2 decision latency, two-cup task ({clock})")
        parts.append(table(["S2 latency s", "outcomes", "task time s", "robot idle", "violations", "starvations"], lat))
    for s in results["scenarios"]:
        link = s["s2_link"]
        if link:
            rtt, outages = link["rtt_ns"], link["outages"]
            kinds = ", ".join(f"{o['class']} {o['duration_ns'] / 1e9:.2f} s" for o in outages) or "none"
            parts.append(
                f"\nS2 link, {s['scenario']} (S2 at {s['s2']['address']}): {link['heartbeats']} heartbeats, round trip "
                f"p50 {_ms(rtt['p50'])} ms, p99 {_ms(rtt['p99'])} ms, max {_ms(rtt['max'])} ms (heartbeat echo, wall-clock); "
                f"largest gap {link['max_gap_ns'] / 1e9:.2f} s; outages: {kinds}"
            )
        if s["loop"]:
            parts.append(f"\nLoop intervals, {s['scenario']} ({s['loop']['label']})")
            parts.append(loop_table(s["loop"]))
    o = results["overhead"]
    costs = ", ".join(f"{k} {c['mean']:.0f} us mean, {c['max']:.0f} us max" for k, c in o["per_execution_us"].items())
    parts.append(f"\nRuntime overhead per execution ({o['measured']}): {costs}.")
    failed = [s for s in results["scenarios"] if not s["passed"]]
    for s in failed:
        parts.append(f"FAILED {s['scenario']}: " + "; ".join(s["problems"]))
    parts.append(f"\n{len(results['scenarios']) - len(failed)} of {len(results['scenarios'])} scenarios as expected.")
    return "\n".join(parts)


def _ms(value: int | None) -> str:
    return "-" if value is None else f"{value / 1e6:.2f}"


def loop_table(loop: Json) -> str:
    rows = [
        [
            name,
            _ms(c["period_ns"]),
            c["steps"],
            *(_ms(c["interval_ns"][k]) for k in ("p50", "p99", "max")),
            c["overruns"],
            *(_ms(c["step_ns"][k]) for k in ("p99", "max")),
        ]
        for name, c in loop["components"].items()
    ]
    header = ["component", "period ms", "steps", "interval p50 ms", "p99 ms", "max ms", "overruns", "step p99 ms", "step max ms"]
    return table(header, rows)


INFERENCE_SCENARIOS = ("nominal_a", "cup_moved")
INFERENCE_DELAYS = (0.05, 0.15, 0.3)  # S1's inference latency, s
INFERENCE_LABEL = "simulated time; a baseline for asynchronous inference, not a claim about which method is best"


def inference_matrix(out: Path) -> Json:
    """S1's inference delay x chunk corrector x inference trigger (every one
    registered in extensions.py), on nominal_a and cup_moved, in simulated
    time: success, task time, S0 starvations, inferences, the discontinuity
    at chunk switches, and reaction and freeze after the scene change."""
    cells = []
    for name in INFERENCE_SCENARIOS:
        for delay in INFERENCE_DELAYS:
            for corrector in CORRECTORS:  # every registered one: a new corrector or trigger joins the matrix
                for trigger in INFERENCE_TRIGGERS:
                    over = {
                        "scenario": {"s1": {"latency_s": delay}},
                        "runtime": {"extensions": {"corrector": corrector, "trigger": trigger}},
                    }
                    r = run(name, out / f"{name}-{delay}-{corrector}-{trigger}", overrides=over)
                    res = scenario_result(r, validate_run(r.dir))
                    d = res["dynamics"]
                    cells.append(
                        {
                            "scenario": name,
                            "inference_s": delay,
                            "corrector": corrector,
                            "trigger": trigger,
                            "outcomes": [s["outcome"] for s in r.summaries],
                            "passed": res["passed"],
                            "problems": res["problems"],
                            "violations": res["violations"],
                            "task_time_s": res["task_time_s"],
                            "starvation_events": res["starvation_events"],
                            "inferences": d["inferences"],
                            "discontinuity": d["discontinuity"],
                            "reaction_s": d["reaction_s"],
                            "freeze_s": d["freeze_s"],
                        }
                    )
    results = {"label": INFERENCE_LABEL, "cells": cells}
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


INFERENCE_LEGEND = (
    "jump: the largest jump of S0's desired position at a chunk switch (rad for the arm's joints); "
    "velocity jump: of its desired velocity. reaction: from the cup's move to S0 executing a chunk computed "
    "after it; freeze: from the first observation after the move reaching S1 to that tick."
)


def inference_rows(results: Json) -> tuple[list[str], list[list[Any]]]:
    rows = []
    for c in results["cells"]:
        d = c["discontinuity"] or {}
        rows.append(
            [
                c["scenario"],
                c["inference_s"],
                c["corrector"],
                c["trigger"],
                " ".join(c["outcomes"]) or "none (time limit)",
                c["violations"],
                c["task_time_s"],
                c["starvation_events"],
                c["inferences"],
                d.get("position"),
                d.get("velocity"),
                c["reaction_s"],
                c["freeze_s"],
            ]
        )
    header = ["scenario", "inference s", "corrector", "trigger", "outcomes", "violations", "task s", "starvations", "inferences"]
    header += ["jump", "velocity jump /s", "reaction s", "freeze s"]
    return header, rows


def inference_report(results: Json) -> str:
    return f"Inference matrix ({results['label']})\n" + table(*inference_rows(results)) + "\n" + INFERENCE_LEGEND


def inference_markdown(results: Json) -> str:
    header, rows = inference_rows(results)
    return "\n".join([f"Inference matrix ({results['label']}).", "", *md_table(header, rows), "", INFERENCE_LEGEND])
