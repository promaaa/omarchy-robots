"""Latency distribution files: measured delays with their provenance, in one
JSON format for an S2 backend's time to first token (kind "s2_ttft",
written by `omarchy-runtime ttft-measure`) and an S1 model's inference delay
(kind "s1_inference", from a collaborator's measurements).

    {
      "format": "omarchy-latency/1",
      "kind": "s2_ttft" | "s1_inference",
      "label": "measured on host" | "measured on board" | ...,
      "unit": "s",
      "samples": [0.81, 0.79, ...],      # the delays, seconds, at least 5
      "provenance": {"date": ..., "host": {...}, "model": {...}, "method": ..., ...}
    }

"samples" holds successful measurements only; a file may add its own keys
(failures, totals, answers). "label" says where the delays were measured
and starts with "measured"; nothing here is invented or modelled. Provenance
must name the date, the host, the model and the method; an s2_ttft file also
the backend, the prompt's hash and the network path, an s1_inference file
the model's input.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime.contracts import ConfigError, Json, Ns, ns

FORMAT = "omarchy-latency/1"
DATA = Path(__file__).parent / "data" / "latency"
PROVENANCE = {
    "s2_ttft": ("date", "host", "model", "method", "backend", "prompt_sha256", "network_path"),
    "s1_inference": ("date", "host", "model", "method", "input"),
}


def path_of(where: str) -> Path:
    """A distribution by path, or by name among the package's (data/latency)."""
    return Path(where) if "/" in where or where.endswith(".json") else DATA / f"{where}.json"


def load(where: str, kind: str) -> Json:
    """A checked distribution file, with its name, sha256 and summary added."""
    path = path_of(where)
    try:
        raw = path.read_bytes()
        d = json.loads(raw)
    except (OSError, ValueError) as e:
        raise ConfigError(f"latency distribution {where}: {e}") from e
    problems = check(d, kind)
    if problems:
        raise ConfigError(f"latency distribution {path}: " + "; ".join(problems))
    out: Json = d | {"name": path.stem, "sha256": hashlib.sha256(raw).hexdigest(), "summary": summary(d["samples"])}
    return out


def check(d: Any, kind: str) -> list[str]:
    if not isinstance(d, dict):
        return ["not a JSON object"]
    problems = []
    if d.get("format") != FORMAT:
        problems.append(f"format {d.get('format')!r}, expected {FORMAT}")
    if d.get("kind") != kind:
        problems.append(f"kind {d.get('kind')!r}, expected {kind}")
    if not str(d.get("label", "")).startswith("measured"):
        problems.append("label must say where it was measured (measured on host, measured on board, ...)")
    if d.get("unit") != "s":
        problems.append("unit must be s")
    samples = d.get("samples")
    if not isinstance(samples, list) or len(samples) < 5:
        problems.append("samples: at least 5 delays")
    elif not all(isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in samples):
        problems.append("samples: finite, non-negative seconds")
    provenance = d.get("provenance")
    if not isinstance(provenance, dict):
        problems.append("provenance: missing")
    else:
        missing = [k for k in PROVENANCE.get(kind, ()) if not provenance.get(k)]
        if missing:
            problems.append(f"provenance: missing {', '.join(missing)}")
    return problems


def summary(samples: list[float]) -> Json:
    a = np.array(samples, dtype=np.float64)
    p05, median, p95 = (float(v) for v in np.percentile(a, [5, 50, 95]))
    return {"n": len(a), "median": median, "p05": p05, "p95": p95, "min": float(a.min()), "max": float(a.max()), "mean": float(a.mean())}


class EmpiricalLatency:
    """A measured distribution: each delay is one of its samples, drawn at
    random with replacement (a bootstrap of the measurements), from the
    run's random generator, so a seed reproduces the sequence."""

    id = "empirical"
    version = "1"

    def __init__(self, distribution: Json) -> None:
        self.distribution = distribution
        self._samples = [ns(float(v)) for v in distribution["samples"]]

    def sample(self, rng: np.random.Generator) -> Ns:
        return self._samples[int(rng.integers(len(self._samples)))]
