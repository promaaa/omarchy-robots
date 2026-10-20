"""omarchy-runtime: run the simulated dishwashing scenarios and inspect their traces.

Everything here is simulated: no robot, no real-time guarantee.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path

from omarchy_runtime import __version__, config, evaluate, latency, link, placement, scenario, ttft
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


def runnable() -> list[str]:
    """Every scenario this package can run: without MuJoCo (the robot's package), not the simulated Panda's, and it says so."""
    names = config.scenario_names()
    if importlib.util.find_spec("mujoco") is not None:
        return names
    left = [n for n in names if config.effective(n)["body"]["kind"] == "mujoco_panda"]
    if left:
        print(f"Not run, for want of MuJoCo (omarchy-runtime-tools has it): {', '.join(left)}\n")
    return [n for n in names if n not in left]


def cmd_eval(args: argparse.Namespace, names: list[str], prefix: str) -> int:
    for name in names:
        config.scenario_path(name)  # fail before running anything
    tags = dict(t.partition("=")[::2] for t in args.tag)
    if any(not k for k in tags):
        raise ConfigError(f"--tag takes KEY=VALUE, not {args.tag}")
    remote = {}
    if args.s2:
        where = link.address(args.s2)
        remote = {"s2": where, "s2_timeout": ns(args.s2_timeout), "s2_bind": link.through_tunnel(link.tunnel(), where[0], listening=False)}
    out = fresh(runs_dir(args), f"{prefix}-{stamp()}")
    results = evaluate.evaluate(
        names,
        out,
        record_s0=args.record_s0,
        clock=args.clock,
        s0_hz=args.s0_hz,
        ttft=args.ttft,
        ttft_spread=args.ttft_spread,
        ttft_file=args.ttft_file,
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
    named = {k: f"{v['id']}@{v['version']}" for k, v in ext.items() if "id" in v}  # the others map a key to one each
    verifiers = ", ".join(f"{p} {v['id']}@{v['version']}" for p, v in ext["verifiers"].items())
    print(f"filter  {named['filter']}  recovery {named['recovery']}  S2 latency {named['s2_latency']}  verifiers {verifiers}")
    defaults = ", ".join(f"{c} {v['id']}@{v['version']}" for c, v in ext.get("default_fallbacks", {}).items())
    print(f"default fallbacks  {defaults or 'none'}")
    c = header["s0"]
    honours = ["motion"] + (["object"] if c["object"] else []) + (["contact"] if c["contact"] else [])
    degraded = ", ".join(c["degraded"]) or "none"
    print(f"s0      {c['id']}@{c['version']} ({c['command']}), honours {', '.join(honours)}; degraded modes {degraded}")
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
        f = s["fidelity"]
        if f is not None:
            shown = {
                "tracking": f["tracking_error"],
                "contact mode": f["contact_mode_share"],
                "force": f["force_share"],
                "object": f["object_error"],
            }
            measured = ", ".join(f"{k} {v:.3f}" for k, v in shown.items() if v is not None)
            print(f"  fidelity {'faithful' if f['faithful'] else 'not faithful'}: {measured or 'nothing measured'}")
        if s["attribution"] is not None:
            print(f"  attribution {s['attribution']['category']}: {s['attribution']['reason']}")
    counts = Counter(e["type"] for e in events)
    print("\nevents  " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items())))
    return 0


def cmd_s2(args: argparse.Namespace) -> int:
    tunnel = link.tunnel()
    where = link.address(args.listen or (f"{tunnel['address']}:{tunnel['port']}" if tunnel else "127.0.0.1:7447"))
    link.through_tunnel(tunnel, where[0], listening=True)
    report = scenario.serve_s2(
        where, ttft=args.ttft, spread=args.ttft_spread, timeout=ns(args.timeout), announce=True, ttft_file=args.ttft_file
    )
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
    placement.write_docs(results, [Path(f) for f in args.files], args.source)
    print(f"The S2 placement table in {', '.join(args.files)} is now the one of {args.results}.")
    return 0


def cmd_placement_merge(args: argparse.Namespace) -> int:
    results = placement.merge([Path(p) for p in args.results])
    print(placement.report(results))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (out / "table.txt").write_text(placement.report(results) + "\n")
    (out / "table.md").write_text(placement.markdown(results) + "\n")
    print(f"\nresults.json, table.txt and table.md: {out}")
    return 0 if results["passed"] else 1


def cmd_placement_compare(args: argparse.Namespace) -> int:
    comparison = placement.compare(Path(args.before), Path(args.after))
    text = placement.compare_markdown(comparison, args.before_source, args.after_source)
    print(text)
    if args.docs:
        placement.write_block([Path(f) for f in args.docs], "s2-placement-compare", text)
        print(f"\nThe comparison in {', '.join(args.docs)} is now this one.")
    return 0


def cmd_ttft_measure(args: argparse.Namespace) -> int:
    model: dict[str, object] = {"name": args.model_name or args.model}
    if args.model_file:
        digest = hashlib.sha256()
        with open(args.model_file, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        model |= {"file": Path(args.model_file).name, "sha256": digest.hexdigest()}
    model |= {k: v for k, v in (("quantization", args.quantization), ("source", args.model_source), ("license", args.license)) if v}
    try:
        key = ttft.api_key(args.api_key_env, args.api_key_file)
        result = ttft.measure(
            args.url,
            args.model,
            args.n,
            key=key,
            backend=args.backend,
            model_info=model,
            warmup=args.warmup,
            no_prompt_cache=args.no_prompt_cache,
            network_path=args.network_path,
        )
    except OmarchyError as e:
        if "no API key" in str(e):
            print(f"No API key for {args.url}: set {args.api_key_env} or pass --api-key-file (a file outside the repository).")
            return 2
        raise
    print(ttft.report(result))
    if args.out:
        Path(args.out).write_text(ttft.as_text(result))
        print(f"\nDistribution: {args.out}")
    return 0


def cmd_ttft_docs(args: argparse.Namespace) -> int:
    names = args.names or sorted(p.stem for p in latency.DATA.glob("*.json") if json.loads(p.read_text()).get("kind") == "s2_ttft")
    text = ttft.docs_table(names)
    print(text)
    if args.docs:
        placement.write_block([Path(f) for f in args.docs], "s2-ttft", text)
        print(f"\nThe TTFT table in {', '.join(args.docs)} is now this one.")
    return 0


def cmd_inference_matrix(args: argparse.Namespace) -> int:
    out = fresh(runs_dir(args), f"inference-{stamp()}")
    results = evaluate.inference_matrix(out, args.delay_file or ())
    print(evaluate.inference_report(results))
    print(f"\nTraces and results.json: {out}")
    if args.docs:
        placement.write_block([Path(f) for f in args.docs], "inference-matrix", evaluate.inference_markdown(results))
        print(f"The inference matrix in {', '.join(args.docs)} is now this run's.")
    return 0 if all(c["violations"] == 0 for c in results["cells"]) else 1


def cmd_scrub_experiment(args: argparse.Namespace) -> int:
    from omarchy_runtime import scrub  # MuJoCo: in omarchy-runtime-tools only

    out = fresh(runs_dir(args), f"scrub-{stamp()}")
    results = scrub.experiment(out, seeds=args.seeds, jobs=args.jobs)
    print(scrub.report(results))
    print(f"\nTraces and results.json: {out}")
    if args.docs:
        source = (
            f"runtime {runtime_identity()['revision'] or 'unknown'}, {stamp()}, `omarchy-runtime scrub-experiment --seeds {args.seeds}`"
        )
        placement.write_block([Path(f) for f in args.docs], "scrub", scrub.markdown(results, source))
        print(f"The scrubbing tables in {', '.join(args.docs)} are now this run's.")
    return 0 if all(c["violations"] == 0 for c in results["cells"]) else 1


def cmd_scrub_docs(args: argparse.Namespace) -> int:
    from omarchy_runtime import scrub  # MuJoCo: in omarchy-runtime-tools only

    path = Path(args.results)
    results = json.loads(path.read_text())
    header = json.loads((Path(results["cells"][0]["run_dir"]) / "run.json").read_text())
    w = header["wall_created"]  # 20261019T065433Z
    command = f"`omarchy-runtime scrub-experiment --seeds {results['seeds']}`"
    source = f"runtime {header['runtime']['revision'][:12]}, runs of {w[:4]}-{w[4:6]}-{w[6:8]}, {command}"
    placement.write_block([Path(f) for f in args.docs], "scrub", scrub.markdown(results, source))
    print(f"The scrubbing tables in {', '.join(args.docs)} are now {path}'s.")
    return 0


def cmd_scrub_render(args: argparse.Namespace) -> int:
    from omarchy_runtime import scrub  # MuJoCo: in omarchy-runtime-tools only

    out = Path(args.out)
    provenance = scrub.render(Path(args.run_dir), out, fps=args.fps)
    out.with_suffix(".json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"{out} and its provenance, {out.with_suffix('.json')}")
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
    runs.add_argument("--ttft-file", help="or a measured TTFT distribution: a path, or a name in data/latency (ttft-measure)")
    runs.add_argument("--tag", action="append", default=[], metavar="KEY=VALUE", help="recorded in the trace header (repeatable)")
    p = sub.add_parser("demo", parents=[runs], help=f"run {' and '.join(DEMO)}, and tell what happened")
    p.set_defaults(func=lambda a: cmd_eval(a, DEMO, "demo"))
    p = sub.add_parser("eval", parents=[runs], help="run scenarios (default: all) and print the results table")
    p.add_argument("scenarios", nargs="*", help=f"from: {', '.join(config.scenario_names())}")
    p.set_defaults(func=lambda a: cmd_eval(a, a.scenarios or runnable(), "eval"))
    p = sub.add_parser("run", parents=[runs], help="run one scenario (a name, or a path to a .toml file)")
    p.add_argument("scenario")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("s2", help="be S2 for one robot run: listen, run the scripted S2 on the wall clock, report")
    p.add_argument(
        "--listen", metavar="HOST:PORT", help="where to wait for the robot (default: the S2 link's tunnel address, else 127.0.0.1:7447)"
    )
    p.add_argument("--ttft", type=float, help="S2's median time to first token, s (default: the scenario's latency)")
    p.add_argument("--ttft-spread", type=float, default=0.0, help="lognormal spread of the time to first token (default 0: fixed)")
    p.add_argument("--ttft-file", help="or a measured TTFT distribution: a path, or a name in data/latency (ttft-measure)")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for the robot (default 600)")
    p.set_defaults(func=cmd_s2)
    p = sub.add_parser("placement", help="gather S2 placement cells (their results.json) into one table")
    p.add_argument("results", nargs="+", help="results.json of each cell's evaluation")
    p.add_argument("--out", help="write results.json, table.txt and table.md there")
    p.set_defaults(func=cmd_placement)
    p = sub.add_parser("ttft-measure", help="measure an S2 backend's time to first token: a representative prompt, N times, streaming")
    p.add_argument("--url", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8080/v1 (llama-server)")
    p.add_argument("--model", required=True, help="the model name the endpoint expects")
    p.add_argument("--n", type=int, default=50, help="measured requests (default 50)")
    p.add_argument("--warmup", type=int, default=1, help="requests first, not counted (default 1)")
    p.add_argument(
        "--backend", required=True, help='what serves it, for provenance, e.g. "llama.cpp llama-server 9190 (nixpkgs), 8 threads"'
    )
    p.add_argument("--model-name", help="the model's name, for provenance")
    p.add_argument("--model-file", help="the weights file: its SHA-256 goes into the provenance")
    p.add_argument("--model-source", help="where the weights came from (an official URL)")
    p.add_argument("--quantization", help="e.g. Q4_K_M")
    p.add_argument("--license", help="the weights' license")
    p.add_argument("--no-prompt-cache", action="store_true", help="llama-server: process the whole prompt every time (cache_prompt false)")
    p.add_argument("--network-path", help="how the requests travel, when the URL does not say it (an SSH tunnel)")
    p.add_argument("--api-key-env", default="OMARCHY_S2_API_KEY", help="environment variable with the API key (default OMARCHY_S2_API_KEY)")
    p.add_argument("--api-key-file", help="or a file with the API key, outside the repository")
    p.add_argument("--out", help="write the distribution file (JSON) there")
    p.set_defaults(func=cmd_ttft_measure)

    p = sub.add_parser("ttft-docs", help="the committed TTFT distributions (data/latency) as a table, for the docs")
    p.add_argument("names", nargs="*", help="distributions by name or path (default: every s2_ttft file in data/latency)")
    p.add_argument("--docs", nargs="+", metavar="FILE", help="also write the table into these files, between their markers")
    p.set_defaults(func=cmd_ttft_docs)

    p = sub.add_parser("placement-merge", help="merge placement results (the full matrix split over CI jobs) into one table")
    p.add_argument("results", nargs="+", help="results.json of each part (placement --out), with its runs/ beside placement/")
    p.add_argument("--out", required=True, help="write results.json, table.txt and table.md there")
    p.set_defaults(func=cmd_placement_merge)

    p = sub.add_parser("placement-compare", help="compare two placement results (before and after), from their results and traces")
    p.add_argument("before", help="results.json of the earlier run (placement --out), with its runs/ beside placement/")
    p.add_argument("after", help="results.json of the later run")
    p.add_argument("--before-source", required=True, help="where the earlier results come from, for the caption")
    p.add_argument("--after-source", required=True, help="where the later results come from")
    p.add_argument("--docs", nargs="+", metavar="FILE", help="also write the table into these files, between their markers")
    p.set_defaults(func=cmd_placement_compare)

    p = sub.add_parser("placement-docs", help="write a placement results.json's table into documentation files, between its markers")
    p.add_argument("results", help="the results.json written by `placement --out`")
    p.add_argument("files", nargs="+", help="for example README.md manual/runtime.md")
    p.add_argument("--source", help='where the results come from, for the caption, e.g. "CI run 123, GitHub runner, 2 CPUs, KVM"')
    p.set_defaults(func=cmd_placement_docs)
    p = sub.add_parser(
        "inference-matrix",
        help="S1 inference delay x chunk corrector x inference trigger, simulated; a baseline for asynchronous inference",
    )
    p.add_argument("--runs", help="where run directories go (default: $OMARCHY_RUNTIME_RUNS, else ./runs)")
    p.add_argument("--docs", nargs="+", metavar="FILE", help="also write the table into these files, between their markers")
    p.add_argument(
        "--delay-file",
        nargs="+",
        metavar="FILE",
        help="measured S1 inference delays (latency format, kind s1_inference): one more delay each",
    )
    p.set_defaults(func=cmd_inference_matrix)
    p = sub.add_parser(
        "scrub-experiment",
        help="the scrubbing experiment on the simulated Panda: motion only and with contact x plate height errors x seeds (tools package)",
        parents=[runs],
    )
    p.add_argument("--seeds", type=int, default=5, help="runs per condition and error (seeds 1..N)")
    p.add_argument("--jobs", type=int, default=1, help="runs at a time")
    p.add_argument("--docs", nargs="*", help="write the table into these files, between <!-- scrub:begin --> and <!-- scrub:end -->")
    p.set_defaults(func=cmd_scrub_experiment)
    p = sub.add_parser("scrub-docs", help="write a scrubbing results.json's table into documentation files, between its markers")
    p.add_argument("results")
    p.add_argument("--docs", nargs="+", required=True, metavar="FILE")
    p.set_defaults(func=cmd_scrub_docs)
    p = sub.add_parser("scrub-render", help="an mp4 of a recorded scrubbing run (run --record-s0), with its provenance (tools package)")
    p.add_argument("run_dir")
    p.add_argument("--out", required=True, help="the .mp4 to write; its provenance goes next to it (.json)")
    p.add_argument("--fps", type=int, default=30)
    p.set_defaults(func=cmd_scrub_render)
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
