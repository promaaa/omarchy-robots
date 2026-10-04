"""Run directories: writing them, and checking them (trace schema 1).

A run directory holds:
  run.json          header: schema, runtime and OS identity, config hash, seed, clock, extensions
  events.jsonl      low-rate events, one JSON object per line, flushed one by one
  executions.jsonl  one summary per finished execution
  s0_<id>.npz       optional: S0's per-tick record for one execution
  results.json      written by the evaluation harness only

Keys that start with "wall" hold wall-clock values; everything else is
determined by the scenario and its seed.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import numpy as np

from omarchy_runtime import __version__
from omarchy_runtime.contracts import (
    OUTCOME_CATEGORIES,
    SECOND,
    TERMINAL,
    Category,
    Json,
    Ns,
    State,
    TraceError,
    VerdictKind,
)
from omarchy_runtime.wallclock import perf_ns, wall_ns

TRACE_SCHEMA = 1
BUILD_JSON = Path("/etc/omarchy-robot/build.json")
NO_OS = "no Omarchy Robots identity: /etc/omarchy-robot/build.json not found"

EVENT_TYPES = frozenset(
    {
        "run.start",
        "run.end",
        "request",
        "decision",
        "cancel",
        "resolution",
        "lifecycle",
        "s0.mode",
        "s0.status",
        "chunk",
        "fallback.install",
        "fallback.rejected",
        "s0.hold",
        "verify.attempt",
        "verify.evidence",
        "verdict",
        "error",
    }
)
# The per-tick S0 record never enters the event stream: at most this many
# events per simulated second, whatever S0's rate.
MAX_EVENTS_PER_SECOND = 200

S0_RECORD = ("t", "mode", "position", "desired", "seq", "valid_until")
S0_MODES = ("TRACKING", "FALLBACK", "HOLD", "FAULT")  # the codes in an S0 record's "mode" column


def os_identity(path: Path = BUILD_JSON) -> tuple[Json | None, str]:
    """The Omarchy Robots build this runs on, or None on any other machine."""
    try:
        build = json.loads(path.read_text())
    except FileNotFoundError:
        return None, NO_OS
    except (OSError, ValueError) as e:
        return None, f"no Omarchy Robots identity: {path} unreadable: {e}"
    if not isinstance(build, dict):
        return None, f"no Omarchy Robots identity: {path} is not a JSON object"
    return build, f"read from {path}"


def runtime_identity() -> Json:
    """Version and source revision. Nix builds set OMARCHY_RUNTIME_REVISION
    ("<rev>" or "<rev>-dirty"); a source checkout asks git; otherwise null."""
    revision = os.environ.get("OMARCHY_RUNTIME_REVISION")
    if revision is None:
        revision = _git_revision(Path(__file__).parent)
    dirty = None if revision is None else revision.endswith("dirty")
    return {"version": __version__, "revision": revision, "dirty": dirty}


def _git_revision(where: Path) -> str | None:
    try:
        rev = subprocess.run(["git", "-C", str(where), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        status = subprocess.run(["git", "-C", str(where), "status", "--porcelain", "."], capture_output=True, text=True, check=False)
    except OSError:
        return None
    if rev.returncode != 0:
        return None
    return rev.stdout.strip() + ("-dirty" if status.stdout.strip() else "")


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


class TraceWriter:
    """Writes one run directory. Events are appended and flushed one at a
    time, so a crashed run leaves a readable prefix."""

    def __init__(self, run_dir: Path, header: Json) -> None:
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            (run_dir / "run.json").write_text(json.dumps({"trace_schema": TRACE_SCHEMA} | header, indent=2, allow_nan=False) + "\n")
            self._events: TextIO = (run_dir / "events.jsonl").open("w", encoding="utf-8")
            self._executions: TextIO = (run_dir / "executions.jsonl").open("w", encoding="utf-8")
        except OSError as e:
            raise TraceError(f"cannot write the run directory {run_dir}: {e}") from e
        self.dir = run_dir
        self._seq = 0
        self.cost: Counter[str | None] = Counter()  # wall-clock ns spent writing, per execution id

    def event(self, t: Ns, kind: str, execution: str | None, payload: Json) -> None:
        start = perf_ns()
        record = {"seq": self._seq, "t": t, "wall_ns": wall_ns(), "type": kind, "execution": execution, "payload": payload}
        self._events.write(dumps(record) + "\n")
        self._events.flush()
        self._seq += 1
        self.cost[execution] += perf_ns() - start

    def execution(self, summary: Json) -> None:
        self._executions.write(dumps(summary) + "\n")
        self._executions.flush()

    def close(self, t: Ns, reason: str) -> None:
        self.event(t, "run.end", None, {"reason": reason})
        self._events.close()
        self._executions.close()


@dataclass
class TraceCheck:
    errors: list[str] = field(default_factory=list)
    complete: bool = False
    events: int = 0
    executions: list[Json] = field(default_factory=list)


def _fields(record: Any, spec: dict[str, Any], where: str, errors: list[str]) -> bool:
    if not isinstance(record, dict):
        errors.append(f"{where}: not a JSON object")
        return False
    ok = True
    for key, kind in spec.items():
        if key not in record:
            errors.append(f"{where}: missing {key}")
            ok = False
        elif not isinstance(record[key], kind) or (kind is int and isinstance(record[key], bool)):
            errors.append(f"{where}: {key} has type {type(record[key]).__name__}")
            ok = False
    return ok


NULL = type(None)
HEADER = {
    "trace_schema": int,
    "runtime": dict,
    "os": (dict, NULL),
    "os_note": str,
    "config_hash": str,
    "scenario": str,
    "seed": int,
    "clock": str,
    "body": str,
    "extensions": dict,
    "rates_hz": dict,
}
EVENT = {"seq": int, "t": int, "wall_ns": int, "type": str, "execution": (str, NULL), "payload": dict}
SUMMARY = {
    "execution_id": str,
    "task_id": str,
    "retry_of": (str, NULL),
    "goal": dict,
    "conditioning": dict,
    "capability": (str, NULL),
    "implementation": (str, NULL),
    "implementation_version": (str, NULL),
    "candidates": list,
    "resolution": str,
    "body": str,
    "action_space": str,
    "belief_initial": dict,
    "belief_final": dict,
    "claim": (dict, NULL),
    "verdict": (dict, NULL),
    "outcome": str,
    "category": (str, NULL),
    "reason": str,
    "timing": dict,
    "wall": dict,
    "s0": dict,
    "fallbacks": list,
    "config_hash": str,
    "seed": int,
}
TIMING = {
    "requested": int,
    "started": (int, NULL),
    "first_chunk": (int, NULL),
    "time_to_first_chunk_ns": (int, NULL),
    "claimed": (int, NULL),
    "ended": int,
    "duration_ns": int,
    "fallback_ns": int,
    "hold_ns": int,
}


def _check_summary(s: Json, where: str, errors: list[str]) -> None:
    if not _fields(s, SUMMARY, where, errors):
        return
    _fields(s["timing"], TIMING, f"{where} timing", errors)
    try:
        outcome = State(s["outcome"])
        category = None if s["category"] is None else Category(s["category"])
    except ValueError as e:
        errors.append(f"{where}: {e}")
        return
    if outcome not in TERMINAL:
        errors.append(f"{where}: outcome {outcome} is not terminal")
        return
    allowed = OUTCOME_CATEGORIES[outcome]
    if (category is None) != (not allowed) or (category is not None and category not in allowed):
        errors.append(f"{where}: {outcome} with category {category}")
    verdict, claim = s["verdict"], s["claim"]
    if verdict is not None:
        if not _fields(
            verdict, {"kind": str, "evidence": list, "evidence_t": list, "reason": str, "not_before": int}, f"{where} verdict", errors
        ):
            return
        if verdict["kind"] != VerdictKind.INCONCLUSIVE:  # a decisive verdict rests on fresh, known observations
            if len(verdict["evidence_t"]) != len(verdict["evidence"]):
                errors.append(f"{where}: {len(verdict['evidence'])} evidence ids, {len(verdict['evidence_t'])} times")
            if any(not isinstance(t, int) or t < verdict["not_before"] for t in verdict["evidence_t"]):
                errors.append(f"{where}: verdict evidence older than not_before {verdict['not_before']}")
        if isinstance(claim, dict) and isinstance(claim.get("t"), int) and verdict["not_before"] < claim["t"]:
            errors.append(f"{where}: verification starts before the executor's claim")
    expected_verdict = {
        (State.SUCCEEDED, None): VerdictKind.SATISFIED,
        (State.FAILED, Category.VERIFICATION_FAILED): VerdictKind.VIOLATED,
        (State.UNVERIFIED, Category.VERIFICATION_INCONCLUSIVE): VerdictKind.INCONCLUSIVE,
    }.get((outcome, category))
    if expected_verdict is not None:
        if verdict is None or verdict["kind"] != expected_verdict:
            errors.append(f"{where}: {outcome} needs a {expected_verdict} verdict, has {verdict and verdict['kind']}")
        elif expected_verdict is not VerdictKind.INCONCLUSIVE and not verdict["evidence"]:
            errors.append(f"{where}: {outcome} without evidence")
    if outcome is State.REJECTED and (claim is not None or verdict is not None):
        errors.append(f"{where}: a rejected execution has a claim or a verdict")


def _check_s0_record(path: Path, errors: list[str]) -> None:
    try:
        with np.load(path) as record:
            missing = [k for k in S0_RECORD if k not in record]
            if missing:
                errors.append(f"{path.name}: missing {', '.join(missing)}")
                return
            n = record["t"].shape[0]
            if any(record[k].shape[0] != n for k in S0_RECORD) or not np.all(np.diff(record["t"]) > 0):
                errors.append(f"{path.name}: columns of different lengths or time not increasing")
            elif n and (record["mode"].min() < 0 or record["mode"].max() >= len(S0_MODES)):
                errors.append(f"{path.name}: a tick without a mode")
    except (OSError, ValueError) as e:
        errors.append(f"{path.name}: unreadable: {e}")


def validate_run(run_dir: Path) -> TraceCheck:
    """Check a run directory against trace schema 1. `complete` means the run
    ended normally and every execution it started has a terminal summary."""
    check = TraceCheck()
    errors = check.errors
    try:
        header = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError) as e:
        errors.append(f"run.json: {e}")
        return check
    if _fields(header, HEADER, "run.json", errors) and header["trace_schema"] != TRACE_SCHEMA:
        errors.append(f"run.json: trace_schema {header['trace_schema']}, this validator reads {TRACE_SCHEMA}")

    seen: set[str] = set()
    ended = False
    per_second: Counter[int] = Counter()
    try:
        lines = (run_dir / "events.jsonl").read_text().split("\n")
    except OSError as e:
        errors.append(f"events.jsonl: {e}")
        lines = []
    if lines and lines[-1] == "":
        lines.pop()
    last_t = -1
    for n, line in enumerate(lines):
        try:
            event = json.loads(line)
        except ValueError:
            errors.append(f"events.jsonl line {n + 1}: not JSON" + (" (truncated last line)" if n == len(lines) - 1 else ""))
            continue
        if not _fields(event, EVENT, f"events.jsonl line {n + 1}", errors):
            continue
        if event["seq"] != n:
            errors.append(f"events.jsonl line {n + 1}: seq {event['seq']}")
        if event["t"] < last_t:
            errors.append(f"events.jsonl line {n + 1}: time goes backwards")
        last_t = event["t"]
        if event["type"] not in EVENT_TYPES:
            errors.append(f"events.jsonl line {n + 1}: unknown event type {event['type']}")
        if event["execution"] is not None:
            seen.add(event["execution"])
        per_second[event["t"] // SECOND] += 1
        ended = event["type"] == "run.end"
    check.events = len(lines)
    try:
        first = json.loads(lines[0]) if lines else {}
    except ValueError:
        first = {}
    if not isinstance(first, dict) or first.get("type") != "run.start":
        errors.append("events.jsonl: does not begin with run.start")
    busiest = max(per_second.values(), default=0)
    if busiest > MAX_EVENTS_PER_SECOND:
        errors.append(f"events.jsonl: {busiest} events in one simulated second, more than {MAX_EVENTS_PER_SECOND}")

    summaries = []
    try:
        summary_lines = (run_dir / "executions.jsonl").read_text().splitlines()
    except OSError as e:
        errors.append(f"executions.jsonl: {e}")
        summary_lines = []
    for n, line in enumerate(summary_lines):
        try:
            summaries.append(json.loads(line))
        except ValueError:
            errors.append(f"executions.jsonl line {n + 1}: not JSON")
    ids = set()
    for n, s in enumerate(summaries):
        _check_summary(s, f"executions.jsonl line {n + 1}", errors)
        if isinstance(s, dict) and isinstance(s.get("execution_id"), str):
            ids.add(s["execution_id"])
    for missing in sorted(seen - ids):
        errors.append(f"execution {missing} has events but no summary")
    for path in sorted(run_dir.glob("s0_*.npz")):
        _check_s0_record(path, errors)
    check.executions = summaries
    check.complete = ended and not errors
    return check
