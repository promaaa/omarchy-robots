"""Measured latencies: the distribution file, the TTFT measurement against
a stand-in streaming server, S2 and S1 drawing their delays from a file, and
the repeated placement matrix's medians and intervals."""

from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from omarchy_runtime import config, latency, placement, ttft
from omarchy_runtime.contracts import ConfigError, ns
from omarchy_runtime.evaluate import inference_matrix, scenario_result
from omarchy_runtime.scenario import run, s2_parts
from omarchy_runtime.trace import validate_run
from tests.helpers import tmpdir

KEY = "test-key-not-a-secret-7f3a"


def distribution(kind: str = "s2_ttft", samples: list[float] | None = None, **change: Any) -> dict[str, Any]:
    """A file in the format; its delays are test inputs, not a measurement of anything."""
    provenance = {"date": "2026-10-07", "host": {"cpu": "test"}, "model": {"name": "test"}, "method": "test input"}
    provenance |= {"backend": "test", "prompt_sha256": "0" * 64, "network_path": "none"} if kind == "s2_ttft" else {"input": "test"}
    d = {"format": latency.FORMAT, "kind": kind, "label": "measured on host", "unit": "s"}
    return d | {"samples": samples or [0.4, 0.5, 0.6, 0.7, 0.8], "provenance": provenance} | change


def saved(d: dict[str, Any], name: str = "dist.json") -> str:
    path = tmpdir() / name
    path.write_text(json.dumps(d))
    return str(path)


class TestDistributionFile(unittest.TestCase):
    def test_loads_with_its_name_hash_and_summary(self) -> None:
        d = latency.load(saved(distribution()), "s2_ttft")
        self.assertEqual((d["name"], d["summary"]["median"], d["summary"]["n"]), ("dist", 0.6, 5))
        self.assertEqual(len(d["sha256"]), 64)

    def test_refuses_what_is_not_a_measurement(self) -> None:
        bad = {
            "another kind": distribution(kind="s1_inference"),
            "no label": distribution(label="modelled"),
            "four samples": distribution(samples=[0.1, 0.2, 0.3, 0.4]),
            "a negative delay": distribution(samples=[0.1, 0.2, 0.3, 0.4, -1.0]),
            "no provenance": distribution(provenance={"date": "2026-10-07"}),
            "another format": distribution(format="x/1"),
        }
        for why, d in bad.items():
            with self.subTest(why), self.assertRaises(ConfigError):
                latency.load(saved(d), "s2_ttft")

    def test_draws_measured_values_reproducibly(self) -> None:
        model = latency.EmpiricalLatency(latency.load(saved(distribution()), "s2_ttft"))
        a = [model.sample(np.random.default_rng(3)) for _ in range(20)]
        self.assertEqual(a, [model.sample(np.random.default_rng(3)) for _ in range(20)])
        draws = [model.sample(rng) for rng in [np.random.default_rng(7)] for _ in range(200)]
        self.assertEqual(set(draws), {ns(v) for v in (0.4, 0.5, 0.6, 0.7, 0.8)})


class TestMeasuredDelaysInRuns(unittest.TestCase):
    def test_s2_takes_its_ttft_from_a_file(self) -> None:
        path = saved(distribution(samples=[1.0] * 5))
        _, model, named = s2_parts(config.effective("nominal_a"), ttft_file=path)
        self.assertEqual((named["s2_latency"]["id"], named["s2_latency"]["seconds"]), ("empirical", 1.0))
        self.assertEqual(model.sample(np.random.default_rng(0)), ns(1.0))
        r = run("nominal_a", tmpdir() / "file", ttft_file=path)
        self.assertEqual(json.loads((r.dir / "run.json").read_text())["extensions"]["s2_latency"]["distribution"], "dist")
        self.assertEqual(r.summaries[0]["outcome"], "SUCCEEDED")
        with self.assertRaises(ConfigError):
            s2_parts(config.effective("nominal_a"), ttft=1.0, ttft_file=path)

    def test_the_inference_matrix_takes_s1_delays_from_a_file(self) -> None:
        path = saved(distribution("s1_inference", samples=[0.1] * 5), "s1.json")
        results = inference_matrix(tmpdir() / "matrix", [path])
        measured = [c for c in results["cells"] if c["inference"].startswith("s1 (median")]
        self.assertEqual(len(measured), len(results["cells"]) // 4)  # one delay of four
        self.assertTrue(all(c["violations"] == 0 for c in measured))
        r = run("cup_moved", tmpdir() / "s1", overrides={"scenario": {"s1": {"latency_file": path}}})
        res = scenario_result(r, validate_run(r.dir))
        self.assertLessEqual(res["dynamics"]["reaction_s"], 0.1 + 0.05 + 1e-9)  # the next observation, then 100 ms


class StreamingServer(BaseHTTPRequestHandler):
    """A stand-in for llama-server's streaming chat completions: two tokens,
    the second after 30 ms, chunked over HTTP/1.1; then, as llama-server
    does, it closes the connection without saying so. It remembers what it
    was sent."""

    protocol_version = "HTTP/1.1"
    seen: list[dict[str, Any]] = []
    fail_first = 0

    def chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802 - the http.server interface
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        StreamingServer.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if StreamingServer.fail_first > 0:
            StreamingServer.fail_first -= 1
            self.send_response(503)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"busy")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in ('{"place_x": 0.36,', ' "reason": "free"}'):
            self.chunk(b"data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}).encode() + b"\n\n")
            threading.Event().wait(0.03)
        self.chunk(b'data: {"choices": [], "timings": {"prompt_n": 512, "prompt_ms": 20.0}}\n\ndata: [DONE]\n\n')
        self.wfile.write(b"0\r\n\r\n")
        self.close_connection = True  # closed, though the client was told nothing

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def server() -> Iterator[str]:
    StreamingServer.seen, StreamingServer.fail_first = [], 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StreamingServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestTtftMeasure(unittest.TestCase):
    def test_measures_the_first_token_and_the_whole_answer(self) -> None:
        with server() as url:
            StreamingServer.fail_first = 1  # the warm-up request fails: not counted
            d = ttft.measure(url, "m", 5, key=KEY, backend="stand-in", model_info={"name": "m"}, no_prompt_cache=True)
        self.assertEqual(latency.check(d, "s2_ttft"), [])
        self.assertEqual((len(d["samples"]), d["failures"], d["answers"]), (5, [], {"valid_json": 5, "of": 5}))
        self.assertGreater(d["reconnects"], 0)  # the closed connection: sent again, not a failure
        self.assertTrue(all(0 < t < total for t, total in zip(d["samples"], d["total_s"], strict=True)))
        self.assertTrue(all(total - t >= 0.025 for t, total in zip(d["samples"], d["total_s"], strict=True)))  # the 30 ms gap
        sent = StreamingServer.seen[-1]
        self.assertEqual((sent["path"], sent["body"]["stream"], sent["body"]["cache_prompt"]), ("/v1/chat/completions", True, False))
        self.assertEqual(sent["auth"], f"Bearer {KEY}")  # sent to the backend only
        self.assertNotIn(KEY, json.dumps(d))  # never written
        self.assertEqual(d["provenance"]["prompt_sha256"], ttft.prompt_sha256(ttft.messages(), ttft.MAX_TOKENS, 0.0))
        self.assertEqual(d["provenance"]["network_path"], "loopback on the measuring host")
        self.assertEqual(d["server_timings"][0]["prompt_n"], 512)

    def test_counts_failures_and_needs_a_key_for_a_remote_backend(self) -> None:
        with server() as url:
            StreamingServer.fail_first = 3  # the warm-up and two measured requests
            d = ttft.measure(url, "m", 7, key=None, backend="stand-in", model_info={"name": "m"})
            StreamingServer.fail_first = 4  # then only 4 of 7 succeed: fewer than 5 samples, refused
            with self.assertRaises(ConfigError):
                ttft.measure(url, "m", 7, key=None, backend="stand-in", model_info={"name": "m"}, warmup=0)
        self.assertEqual((len(d["samples"]), len(d["failures"])), (5, 2))
        self.assertIn("HTTP 503", d["failures"][0]["reason"])
        with self.assertRaises(ConfigError):
            ttft.measure("https://api.example.com/v1", "m", 5, key=None, backend="x", model_info={})

    def test_the_prompt_is_stable_and_answers_are_checked(self) -> None:
        self.assertEqual(ttft.prompt_sha256(ttft.messages(), 48, 0.0), ttft.prompt_sha256(ttft.messages(), 48, 0.0))
        self.assertIn('"dish_1"', ttft.messages()[1]["content"])
        for text, valid in (('{"place_x": 0.36, "reason": "free"}', True), ('```json\n{"place_x": 0.36}\n```', True), ("0.36", False)):
            self.assertEqual(ttft.valid_answer(text), valid, text)


class TestRepeatedMatrix(unittest.TestCase):
    def test_median_and_an_order_statistics_interval(self) -> None:
        six = placement.median_interval([3.0, 1.0, 2.0, 6.0, 4.0, 5.0])
        self.assertEqual((six["median"], six["low"], six["high"]), (3.5, 1.0, 6.0))
        self.assertAlmostEqual(six["coverage"], 1 - 2 / 64)
        self.assertIsNone(placement.median_interval([1.0, 2.0, 3.0, 4.0, 5.0])["low"])  # 93.75 %: below 95
        nine = placement.median_interval([float(v) for v in range(9)])
        self.assertEqual((nine["low"], nine["high"]), (1.0, 7.0))  # the 2nd smallest and largest: 96.1 %
        self.assertGreaterEqual(nine["coverage"], 0.95)


if __name__ == "__main__":
    unittest.main()
