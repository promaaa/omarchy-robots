"""omarchy-runtime: run the simulated dishwashing scenarios and inspect their traces.

Everything here is simulated: no robot, no real-time guarantee.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from omarchy_runtime import __version__, config, evaluate, link, placement, scenario
from omarchy_runtime.contracts import ConfigError, OmarchyError, ns, seconds
from omarchy_runtime.trace import runtime_identity, validate_run
from omarchy_runtime.wallclock import stamp

DEMO = ["nominal_a", "slip_retry"]


def runs_dir(args: argparse.Namespace) -> Path:
    return Path(args.runs or os.environ.get("OMARCHY_RUNTIME_RUNS") or "runs")


def fresh(parent: Path, name: str) -> Path:
    path, n = parent / name, 1
    while path.exists():
        n += 1
        path = parent / f"{name}-{n}"
    return path


def narrate(results: dict[str, object]) -> str:
    lines = []
    for s in results["scenarios"]:  # type: ignore[attr-defined]
        lines.append(f"\n{s['scenario']} ({s['body']}): {s['description']}")
        for r in s["executions"]:
            retry = f", retry of {r['retry_of']}" if r["retry_of"] else ""
            lines.append(
                f"  {r['execution']}{retry}: claim {r['claim'] or '-'}, verdict {r['verdict'] or '-'}, "
                f"outcome {r['outcome']}{'/' + r['category'] if r['category'] else ''}, "
                f"ground truth {str(r['truth']).lower()}, {r['duration_s']:.2f} s simulated"
            )
    return "\n".join(lines)


def cmd_eval(args: argparse.Namespace, names: list[str], prefix: str) -> int:
    for name in names:
        config.scenario_path(name)  # fail before running anything
    tags = dict(t.partition("=")[::2] for t in args.tag)
    if any(not k for k in tags):
        raise ConfigError(f"--tag takes KEY=VALUE, not {args.tag}")
    remote = {"s2": link.address(args.s2), "s2_timeout": ns(args.s2_timeout)} if args.s2 else {}
    out = fresh(runs_dir(args), f"{prefix}-{stamp()}")
    results = evaluate.evaluate(
        names,
        out,
        record_s0=args.record_s0,
        clock=args.clock,
        s0_hz=args.s0_hz,
        ttft=args.ttft,
        ttft_spread=args.ttft_spread,
        tags=tags,
        **remote,
    )
    if prefix == "demo":
        print(narrate(results))
        print()
    print(evaluate.report(results))
    print(f"\nTraces and results.json: {out}")
    return 0 if results["passed"] else 1


def cmd_run(args: argparse.Namespace) -> int:
    return cmd_eval(args, [args.scenario], "run")


def cmd_show(args: argparse.Namespace) -> int:
    run = Path(args.run_dir)
    header = json.loads((run / "run.json").read_text())
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line.strip()]
    rt = header["runtime"]
    print(f"{header['scenario']}  seed {header['seed']}  body {header['body']}  clock {header['clock']}")
    print(f"runtime {rt['version']}  revision {rt['revision'] or 'unknown'}  config {header['config_hash'][:12]}")
    os_build = header["os"]
    if os_build:
        board = os_build.get("robot", {}).get("board", "?")
        print(f"os      Omarchy Robots {os_build.get('version', '?')} on {board}, revision {os_build.get('revision', '?')[:12]}")
    else:
        print(f"os      {header['os_note']}")
    ext = header["extensions"]
    named = {k: f"{v['id']}@{v['version']}" for k, v in ext.items() if k != "verifiers"}
    verifiers = ", ".join(f"{p} {v['id']}@{v['version']}" for p, v in ext["verifiers"].items())
    print(f"filter  {named['filter']}  recovery {named['recovery']}  S2 latency {named['s2_latency']}  verifiers {verifiers}")
    truths = {}
    if (run / "results.json").exists():
        truths = {r["execution"]: r["truth"] for r in json.loads((run / "results.json").read_text())["executions"]}
    for line in (run / "executions.jsonl").read_text().splitlines():
        s = json.loads(line)
        claim = s["claim"]["claim"] if s["claim"] else "-"
        verdict = s["verdict"]["kind"] if s["verdict"] else "-"
        truth = f"  truth {str(truths[s['execution_id']]).lower()}" if s["execution_id"] in truths else ""
        print(
            f"\n{s['execution_id']}  {s['goal']['predicate']}({', '.join(s['goal']['args'])})  task {s['task_id']}"
            + (f"  retry of {s['retry_of']}" if s["retry_of"] else "")
        )
        print(f"  {s['outcome']}{'/' + s['category'] if s['category'] else ''}  claim {claim}  verdict {verdict}{truth}")
        print(f"  {s['reason']}")
        t = s["timing"]
        times = (
            f"{seconds(t['duration_ns']):.2f} s simulated, fallback {seconds(t['fallback_ns']):.2f} s, hold {seconds(t['hold_ns']):.2f} s"
        )
        print(f"  {times}, implementation {s['implementation'] or '-'}")
    counts = Counter(e["type"] for e in events)
    print("\nevents  " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items())))
    return 0


def cmd_s2(args: argparse.Namespace) -> int:
    report = scenario.serve_s2(link.address(args.listen), ttft=args.ttft, spread=args.ttft_spread, timeout=ns(args.timeout), announce=True)
    print(json.dumps(report, indent=2))
    return 0 if report["done"] else 1


def cmd_placement(args: argparse.Namespace) -> int:
    results = placement.gather([Path(p) for p in args.results])
    print(placement.report(results))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        (out / "table.txt").write_text(placement.report(results) + "\n")
        (out / "table.md").write_text(placement.markdown(results) + "\n")
        print(f"\nresults.json, table.txt and table.md: {out}")
    return 0 if results["passed"] else 1


def cmd_placement_docs(args: argparse.Namespace) -> int:
    results = json.loads(Path(args.results).read_text())
    placement.write_docs(results, [Path(f) for f in args.files])
    print(f"The S2 placement table in {', '.join(args.files)} is now the one of {args.results}.")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    status = 0
    for run in args.run_dir:
        check = validate_run(Path(run))
        state = "complete" if check.complete else "INCOMPLETE"
        print(f"{run}: {state}, {check.events} events, {len(check.executions)} executions, {len(check.errors)} errors")
        for e in check.errors:
            print(f"  {e}")
        status |= 0 if check.complete else 1
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omarchy-runtime", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    rt = runtime_identity()
    parser.add_argument("--version", action="version", version=f"omarchy-runtime {__version__} (revision {rt['revision'] or 'unknown'})")
    sub = parser.add_subparsers(dest="command", required=True)
    runs = argparse.ArgumentParser(add_help=False)
    runs.add_argument("--runs", help="where run directories go (default: $OMARCHY_RUNTIME_RUNS, else ./runs)")
    runs.add_argument("--record-s0", action="store_true", help="also write each execution's per-tick S0 record (s0_<id>.npz)")
    runs.add_argument(
        "--clock",
        choices=["simulated", "wall"],
        default="simulated",
        help="simulated time (default, deterministic) or the wall clock (soft real-time, Python)",
    )
    runs.add_argument("--s0-hz", type=int, help="S0's rate, instead of the configured one")
    runs.add_argument("--s2", metavar="HOST:PORT", help="connect to an S2 process there (omarchy-runtime s2) instead of running S2 here")
    runs.add_argument("--s2-timeout", type=float, default=120.0, help="seconds to wait for the S2 process (default 120)")
    runs.add_argument("--ttft", type=float, help="S2's median time to first token in this process, s (default: the scenario's latency)")
    runs.add_argument("--ttft-spread", type=float, default=0.0, help="lognormal spread of that time (default 0: fixed)")
    runs.add_argument("--tag", action="append", default=[], metavar="KEY=VALUE", help="recorded in the trace header (repeatable)")
    p = sub.add_parser("demo", parents=[runs], help=f"run {' and '.join(DEMO)}, and tell what happened")
    p.set_defaults(func=lambda a: cmd_eval(a, DEMO, "demo"))
    p = sub.add_parser("eval", parents=[runs], help="run scenarios (default: all) and print the results table")
    p.add_argument("scenarios", nargs="*", help=f"from: {', '.join(config.scenario_names())}")
    p.set_defaults(func=lambda a: cmd_eval(a, a.scenarios or config.scenario_names(), "eval"))
    p = sub.add_parser("run", parents=[runs], help="run one scenario (a name, or a path to a .toml file)")
    p.add_argument("scenario")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("s2", help="be S2 for one robot run: listen, run the scripted S2 on the wall clock, report")
    p.add_argument("--listen", default="127.0.0.1:7447", metavar="HOST:PORT", help="where to wait for the robot (default 127.0.0.1:7447)")
    p.add_argument("--ttft", type=float, help="S2's median time to first token, s (default: the scenario's latency)")
    p.add_argument("--ttft-spread", type=float, default=0.0, help="lognormal spread of the time to first token (default 0: fixed)")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for the robot (default 600)")
    p.set_defaults(func=cmd_s2)
    p = sub.add_parser("placement", help="gather S2 placement cells (their results.json) into one table")
    p.add_argument("results", nargs="+", help="results.json of each cell's evaluation")
    p.add_argument("--out", help="write results.json, table.txt and table.md there")
    p.set_defaults(func=cmd_placement)
    p = sub.add_parser("placement-docs", help="write a placement results.json's table into documentation files, between its markers")
    p.add_argument("results", help="the results.json written by `placement --out`")
    p.add_argument("files", nargs="+", help="for example README.md manual/runtime.md")
    p.set_defaults(func=cmd_placement_docs)
    p = sub.add_parser("show", help="summarise a run directory")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("validate-trace", help="check run directories against the trace schema; exits 1 if one is incomplete")
    p.add_argument("run_dir", nargs="+")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("scenarios", help="list the scenarios")
    p.set_defaults(func=lambda a: print("\n".join(config.scenario_names())) or 0)
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except OmarchyError as e:
        print(f"omarchy-runtime: {e.category} error: {e}", file=sys.stderr)
        return 2
    return result


if __name__ == "__main__":
    sys.exit(main())
