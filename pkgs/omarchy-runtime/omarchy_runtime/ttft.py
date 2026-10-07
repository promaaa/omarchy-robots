"""Measure an S2 backend's latency: a representative S2 prompt sent N times,
streaming, to an OpenAI-compatible chat completions endpoint (llama.cpp's
llama-server on the host; a cloud API given a key). For each request: the
time to first token (TTFT, from sending the request on an open connection
to the first token of the answer), the total latency (to the end of the
stream), and whether it failed. The result is a latency distribution file
(latency.py) labelled "measured on host": the host that runs this command,
never a robot.

The prompt asks S2 to choose the next rack spot for a cup from the belief
it has when S1 gave up in slot_blocked (data/s2_belief.json), and to answer
in JSON. Its hash covers the messages, the token limit and the temperature.
An API key is read from an environment variable or a file outside the
repository, sent only in the request's Authorization header, and never
printed, logged or written.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from omarchy_runtime import latency
from omarchy_runtime.contracts import ConfigError, Json
from omarchy_runtime.evaluate import md_table
from omarchy_runtime.latency import FORMAT, summary
from omarchy_runtime.trace import runtime_identity, virtualization
from omarchy_runtime.wallclock import perf_ns, stamp

BELIEF = Path(__file__).parent / "data" / "s2_belief.json"
SPOTS = (0.27, 0.36)
SYSTEM = "You are S2, the slow planner of a robot that puts cups into a drying rack. Answer with one JSON object and nothing else."
INSTRUCTION = (
    "The robot's belief follows, as JSON. The spot where the robot was placing cup_1 is taken. Spots in the drying rack "
    "are at x = {spots} m; an object whose centre is closer than 0.06 m to a spot takes it. Choose the spot for cup_1. "
    'Answer exactly as {{"place_x": <metres>, "reason": "<at most ten words>"}}.\n\n{belief}'
)
MAX_TOKENS = 48
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def messages() -> list[Json]:
    belief = json.dumps(json.loads(BELIEF.read_text()), sort_keys=True, separators=(",", ":"))
    user = INSTRUCTION.format(spots=" and ".join(f"{s:.2f}" for s in SPOTS), belief=belief)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def prompt_sha256(msgs: list[Json], max_tokens: int, temperature: float) -> str:
    canonical = json.dumps({"messages": msgs, "max_tokens": max_tokens, "temperature": temperature}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def api_key(env: str, file: str | None) -> str | None:
    if file:
        try:
            return Path(file).read_text().strip() or None
        except OSError as e:
            raise ConfigError(f"API key file {file}: {e.strerror}") from e
    return os.environ.get(env) or None


def valid_answer(text: str) -> bool:
    """The answer is one JSON object with a numeric place_x (code fences allowed)."""
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        answer = json.loads(body)
    except ValueError:
        return False
    return isinstance(answer, dict) and isinstance(answer.get("place_x"), int | float) and not isinstance(answer.get("place_x"), bool)


class Endpoint:
    """A connection to {base}/chat/completions, kept open between requests.
    A server may close it after a streamed answer (llama-server does): a
    request that finds it closed, before any response, goes again on a new
    connection, its clock restarted. That is not a failure of the backend."""

    def __init__(self, base: str, key: str | None, timeout: float) -> None:
        url = urlsplit(base.rstrip("/"))
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ConfigError(f"backend URL {base!r}: expected http(s)://host[:port]/path")
        self.host, self.path, self.key = url.hostname, f"{url.path}/chat/completions", key
        self.tls, self.port, self.timeout = url.scheme == "https", url.port, timeout
        self.conn = self._new()
        self.reconnects = 0

    def _new(self) -> http.client.HTTPConnection:
        if self.tls:
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _send(self, data: bytes, headers: dict[str, str]) -> tuple[int, http.client.HTTPResponse]:
        for attempt in (1, 2):
            start = perf_ns()
            try:
                self.conn.request("POST", self.path, data, headers)
                return start, self.conn.getresponse()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                self.conn.close()
                self.conn = self._new()
                self.reconnects += 1
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def stream(self, body: Json) -> Json:
        """Send one request; read its stream. Times in ns from sending it."""
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        start, response = self._send(json.dumps(body).encode(), headers)
        if response.status != 200:
            detail = response.read(300).decode(errors="replace")
            raise OSError(f"HTTP {response.status}: {detail}")
        first, text, timings = None, [], None
        while True:
            line = response.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            chunk = json.loads(payload)
            timings = chunk.get("timings", timings)  # llama-server's own prompt and generation times
            for choice in chunk.get("choices", []):
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    if first is None:
                        first = perf_ns() - start
                    text.append(piece)
        total = perf_ns() - start
        response.read()
        if first is None:
            raise OSError("the stream ended without a token")
        return {"ttft_ns": first, "total_ns": total, "text": "".join(text), "server_timings": timings}

    def close(self) -> None:
        self.conn.close()


def host() -> Json:
    """The measuring host, without its name."""
    cpu = next((line.split(":", 1)[1].strip() for line in _lines("/proc/cpuinfo") if line.startswith("model name")), platform.processor())
    mem = next((int(line.split()[1]) for line in _lines("/proc/meminfo") if line.startswith("MemTotal:")), 0)
    os_name = next(
        (line.split("=", 1)[1].strip('"') for line in _lines("/etc/os-release") if line.startswith("PRETTY_NAME=")), platform.system()
    )
    return {
        "cpu": cpu,
        "threads": os.cpu_count(),
        "memory_gib": round(mem / 2**20, 1),
        "os": os_name,
        "kernel": platform.release(),
        "virtualization": virtualization(),
        "kvm": Path("/dev/kvm").exists(),
    }


def _lines(path: str) -> list[str]:
    try:
        return Path(path).read_text().splitlines()
    except OSError:
        return []


def measure(
    base: str,
    model: str,
    n: int,
    *,
    key: str | None,
    backend: str,
    model_info: Json,
    warmup: int = 1,
    timeout: float = 120.0,
    no_prompt_cache: bool = False,
    temperature: float = 0.0,
) -> Json:
    """`warmup` requests first, not counted (model load, first allocation),
    then `n` requests, one after the other."""
    if n < 5:
        raise ConfigError("ttft-measure: at least 5 requests")
    host_name = urlsplit(base).hostname or ""
    if host_name not in LOOPBACK and not key:
        raise ConfigError("no API key for a remote backend")
    msgs = messages()
    body: Json = {"model": model, "messages": msgs, "stream": True, "max_tokens": MAX_TOKENS, "temperature": temperature}
    if no_prompt_cache:
        body["cache_prompt"] = False  # llama-server: process the whole prompt every time, as a fresh S2 decision would
    endpoint = Endpoint(base, key, timeout)
    ttft, total, failures, answers, server, reconnects = [], [], [], 0, [], 0
    try:
        for k in range(warmup + n):
            try:
                result = endpoint.stream(body)
            except (OSError, ValueError, http.client.HTTPException) as e:
                if k >= warmup:
                    failures.append({"request": k - warmup, "reason": f"{type(e).__name__}: {e}"[:200]})
                endpoint.close()
                reconnects += endpoint.reconnects
                endpoint = Endpoint(base, key, timeout)
                continue
            if k < warmup:
                continue
            ttft.append(result["ttft_ns"] / 1e9)
            total.append(result["total_ns"] / 1e9)
            answers += valid_answer(result["text"])
            if result["server_timings"]:
                server.append({k: result["server_timings"].get(k) for k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")})
    finally:
        endpoint.close()
        reconnects += endpoint.reconnects
    if len(ttft) < 5:
        raise ConfigError(f"ttft-measure: {len(failures)} of {n} requests failed; first: {failures[0]['reason'] if failures else '-'}")
    path = "loopback on the measuring host" if host_name in LOOPBACK else f"the Internet, from the measuring host to {host_name}"
    return {
        "format": FORMAT,
        "kind": "s2_ttft",
        "label": "measured on host",
        "unit": "s",
        "samples": ttft,
        "total_s": total,
        "failures": failures,
        "reconnects": reconnects,  # requests sent again on a new connection: the server had closed the kept one
        "answers": {"valid_json": answers, "of": len(ttft)},
        "server_timings": server,
        "summary": {"ttft": summary(ttft), "total": summary(total)},
        "provenance": {
            "date": f"{stamp()[:4]}-{stamp()[4:6]}-{stamp()[6:8]}",
            "host": host(),
            "backend": backend,
            "endpoint": f"{urlsplit(base).scheme}://{host_name}{urlsplit(base).path}",
            "model": model_info | {"requested_as": model},
            "method": (
                f"omarchy-runtime ttft-measure: {warmup} warm-up request(s) not counted, then {n} requests one after the other on "
                f"one open connection, streaming; TTFT from sending a request to the first token of the answer, total to the end "
                f"of the stream; temperature {temperature}, at most {MAX_TOKENS} tokens"
                + ("; prompt cache off (cache_prompt false)" if no_prompt_cache else "")
            ),
            "prompt_sha256": prompt_sha256(msgs, MAX_TOKENS, temperature),
            "prompt": "choose the next rack spot for cup_1 from the belief S2 has when S1 gave up in slot_blocked (data/s2_belief.json)",
            "network_path": path,
            "runtime": runtime_identity(),
        },
    }


def report(d: Json) -> str:
    t, s = d["summary"]["ttft"], d["summary"]["total"]
    p = d["provenance"]
    return (
        f"{d['label']}: {p['backend']}, {p['model'].get('name', p['model']['requested_as'])}, {p['network_path']}\n"
        f"TTFT s: median {t['median']:.3f}, p05 {t['p05']:.3f}, p95 {t['p95']:.3f}, min {t['min']:.3f}, max {t['max']:.3f} (n {t['n']})\n"
        f"total s: median {s['median']:.3f}, p95 {s['p95']:.3f}; failures {len(d['failures'])}; "
        f"valid JSON answers {d['answers']['valid_json']} of {d['answers']['of']}"
    )


def as_text(value: Any) -> str:
    return json.dumps(value, indent=2) + "\n"


def docs_table(names: list[str]) -> str:
    """The committed TTFT distributions, one row each, for the docs."""
    header = ["distribution", "label", "backend", "model", "TTFT median s", "p05 s", "p95 s", "n", "failures", "network path", "date"]
    rows = []
    for name in names:
        d = latency.load(name, "s2_ttft")
        t, p = d["summary"], d["provenance"]
        m = p["model"]
        model = " ".join(str(v) for v in (m.get("name"), m.get("quantization")) if v)
        rows.append(
            [
                d["name"],
                d["label"],
                p["backend"],
                model,
                f"{t['median']:.3f}",
                f"{t['p05']:.3f}",
                f"{t['p95']:.3f}",
                str(t["n"]),
                str(len(d.get("failures", []))),
                p["network_path"],
                p["date"],
            ]
        )
    text = (
        "S2's time to first token, measured with `omarchy-runtime ttft-measure` (from sending a request to the first token "
        "of the answer; the prompt and the method are in each file's provenance, with the model file's SHA-256). Measured on "
        "the development host, never on a robot."
    )
    return "\n".join([text, "", *md_table(header, rows)])
