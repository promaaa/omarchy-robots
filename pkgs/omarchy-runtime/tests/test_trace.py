"""Trace schema 5: what validate-trace accepts and what it refuses, and P8
(the same scenario and seed give the same trace, wall-clock fields aside)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime.scenario import run
from omarchy_runtime.trace import NO_OS, os_identity, validate_run
from tests.helpers import scenario, tmpdir

HERE = Path(__file__).parent.parent


def without_wall(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: without_wall(v) for k, v in value.items() if not k.startswith("wall")}
    if isinstance(value, list):
        return [without_wall(v) for v in value]
    return value


def trace(run_dir: Path) -> Any:
    return without_wall(
        {
            "run": json.loads((run_dir / "run.json").read_text()),
            "events": [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()],
            "executions": [json.loads(line) for line in (run_dir / "executions.jsonl").read_text().splitlines()],
        }
    )


class TestValidate(unittest.TestCase):
    def copy(self, name: str) -> Path:
        target = tmpdir() / name
        shutil.copytree(scenario(name).dir, target)
        return target

    def test_every_written_trace_is_complete(self) -> None:
        check = validate_run(scenario("slip_retry").dir)
        self.assertEqual(check.errors, [])
        self.assertTrue(check.complete)
        self.assertEqual(len(check.executions), 2)
        self.assertEqual(len(list(scenario("slip_retry").dir.glob("s0_*.npz"))), 2)

    def test_header_names_extensions_and_identity(self) -> None:
        header = json.loads((scenario("nominal_a").dir / "run.json").read_text())
        self.assertEqual(header["trace_schema"], 5)
        self.assertEqual(header["extensions"]["default_fallbacks"], {"place_in": {"id": "lift", "version": "1"}})
        self.assertEqual(header["extensions"]["filter"], {"id": "identity", "version": "1"})
        self.assertEqual(header["extensions"]["verifiers"]["in"], {"id": "geometric", "version": "1"})
        self.assertEqual(header["extensions"]["recovery"]["id"], "retry_once")
        self.assertEqual(header["clock"], "simulated")
        if not Path("/etc/omarchy-robot/build.json").exists():
            self.assertEqual((header["os"], header["os_note"]), (None, NO_OS))

    def test_a_crash_leaves_a_readable_prefix(self) -> None:
        run_dir = self.copy("nominal_a")
        lines = (run_dir / "events.jsonl").read_text().splitlines()
        (run_dir / "events.jsonl").write_text("\n".join(lines[:40]) + "\n" + lines[40][:25])
        check = validate_run(run_dir)
        self.assertFalse(check.complete)
        self.assertEqual(check.events, 41)
        self.assertTrue(any("truncated last line" in e for e in check.errors))

    def test_refuses_inconsistent_summaries(self) -> None:
        run_dir = self.copy("slip_retry")
        summaries = [json.loads(line) for line in (run_dir / "executions.jsonl").read_text().splitlines()]
        summaries[0]["outcome"], summaries[0]["category"] = "SUCCEEDED", None  # with its VIOLATED verdict
        summaries[1]["verdict"]["evidence_t"] = [0]  # evidence older than not_before
        (run_dir / "executions.jsonl").write_text("".join(json.dumps(s) + "\n" for s in summaries[:2]))
        errors = validate_run(run_dir).errors
        self.assertTrue(any("needs a SATISFIED verdict" in e for e in errors), errors)
        self.assertTrue(any("older than not_before" in e for e in errors), errors)

    def test_refuses_missing_summaries_and_unknown_events(self) -> None:
        run_dir = self.copy("slip_retry")
        (run_dir / "executions.jsonl").write_text((run_dir / "executions.jsonl").read_text().splitlines()[0] + "\n")
        with (run_dir / "events.jsonl").open("a") as f:
            f.write(json.dumps({"seq": 10**6, "t": 10**12, "wall_ns": 0, "type": "s0.tick", "execution": None, "payload": {}}) + "\n")
        errors = validate_run(run_dir).errors
        self.assertTrue(any("has events but no summary" in e for e in errors), errors)
        self.assertTrue(any("unknown event type s0.tick" in e for e in errors), errors)

    def test_reports_garbage_instead_of_crashing(self) -> None:
        run_dir = self.copy("slip_retry")
        events = (run_dir / "events.jsonl").read_text().splitlines()
        (run_dir / "events.jsonl").write_text("\n".join(["{garbage", *events[1:]]) + "\n")
        summaries = (run_dir / "executions.jsonl").read_text().splitlines()
        (run_dir / "executions.jsonl").write_text(summaries[0][:30] + "\n" + summaries[1] + "\n")
        check = validate_run(run_dir)
        self.assertFalse(check.complete)
        self.assertIn("events.jsonl: does not begin with run.start", check.errors)
        self.assertIn("executions.jsonl line 1: not JSON", check.errors)
        self.assertEqual(len(check.executions), 1)  # the other summary is still read

    def test_decisive_verdicts_need_fresh_evidence_after_the_claim(self) -> None:
        run_dir = self.copy("nominal_a")
        s = json.loads((run_dir / "executions.jsonl").read_text())
        s["verdict"]["not_before"], s["verdict"]["evidence_t"] = 0, []
        (run_dir / "executions.jsonl").write_text(json.dumps(s) + "\n")
        errors = validate_run(run_dir).errors
        self.assertTrue(any("verification starts before the executor's claim" in e for e in errors), errors)
        self.assertTrue(any("evidence ids, 0 times" in e for e in errors), errors)

    def test_refuses_a_broken_s0_record(self) -> None:
        run_dir = self.copy("nominal_a")
        path = next(run_dir.glob("s0_*.npz"))
        with np.load(path) as rec:
            columns = dict(rec)
        columns["mode"][5] = 9
        np.savez(path, **columns)
        self.assertTrue(any("a tick without a mode" in e for e in validate_run(run_dir).errors))

    def test_os_identity(self) -> None:
        path = tmpdir() / "build.json"
        self.assertEqual(os_identity(path), (None, NO_OS))
        path.write_text('{"version": "0.0.2", "robot": {"board": "vm"}}')
        self.assertEqual(os_identity(path)[0], {"version": "0.0.2", "robot": {"board": "vm"}})
        path.write_text("[1]")
        self.assertIsNone(os_identity(path)[0])


class TestDeterminism(unittest.TestCase):
    """P8: same scenario, same seed, same trace (wall-clock fields excluded)."""

    def test_same_process(self) -> None:
        for name in ("slip_retry", "fallback_installed"):
            with self.subTest(name):
                again = run(name, tmpdir() / name, record_s0=True)
                self.assertEqual(trace(scenario(name).dir), trace(again.dir))
                for path in scenario(name).dir.glob("s0_*.npz"):
                    with np.load(path) as a, np.load(again.dir / path.name) as b:
                        self.assertEqual(sorted(a), sorted(b))
                        for key in a:
                            self.assertTrue(np.array_equal(a[key], b[key]), key)

    def test_separate_processes_with_different_hash_seeds(self) -> None:
        traces = []
        for seed in ("1", "2"):
            runs = tmpdir()
            env = os.environ | {"PYTHONHASHSEED": seed, "PYTHONPATH": str(HERE)}
            subprocess.run(
                [sys.executable, "-m", "omarchy_runtime", "run", "s2_latency_1", "--runs", str(runs)],
                env=env,
                check=True,
                capture_output=True,
            )
            traces.append(trace(next(runs.glob("run-*/s2_latency_1"))))
        self.assertEqual(traces[0], traces[1])


if __name__ == "__main__":
    unittest.main()
