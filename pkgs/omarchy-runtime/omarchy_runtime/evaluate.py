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
    elif r.clock == "simulated" and np.any(np.diff(rec["t"]) != tick):
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
