"""The collaborator extension points: default implementations, and the
registries configuration selects them from by id.

To add an ObservationFilter, a Verifier, a RecoveryPolicy or an S2
LatencyModel: write a class with `id`, `version` and the method its interface
in contracts.py names, add it to the registry below, and select it in
config/runtime.toml ([extensions]) or in a scenario's [extensions] table. The
runtime core does not change. tests/contracts.py holds the contract tests a
new implementation should pass.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from omarchy_runtime.belief import WorldState, free_spots
from omarchy_runtime.contracts import (
    ActionChunk,
    ChunkCorrector,
    ConfigError,
    GiveUp,
    Goal,
    InferenceContext,
    InferenceTrigger,
    Json,
    LatencyModel,
    Ns,
    Observation,
    ObservationFilter,
    RecoveryPolicy,
    Retry,
    S0History,
    S0Mode,
    S0Status,
    Truth,
    Verdict,
    VerdictKind,
    Verifier,
    ns,
)


class IdentityFilter:
    """Changes nothing."""

    id = "identity"
    version = "1"

    def apply(self, observation: Observation) -> tuple[Observation, tuple[str, ...]]:
        return observation, ()


VERDICTS = {Truth.TRUE: VerdictKind.SATISFIED, Truth.FALSE: VerdictKind.VIOLATED, Truth.UNKNOWN: VerdictKind.INCONCLUSIVE}


class GeometricVerifier:
    """Judges in, on and held from the belief's geometry, using only estimates
    from observations taken at or after not_before. No fresh, confident
    estimate, or a position too close to a boundary, is INCONCLUSIVE."""

    id = "geometric"
    version = "1"

    def verify(self, goal: Goal, belief: WorldState, not_before: Ns) -> Verdict:
        e = belief.evaluate(goal, since=not_before)
        return Verdict(VERDICTS[e.truth], e.evidence, e.reason)


class RetryOnce:
    """Retry once, unchanged, after the verifier saw the goal violated; give up
    after anything else (rejection, inconclusive verification, stall, timeout)."""

    id = "retry_once"
    version = "1"

    def decide(self, goal: Goal, summary: Json, belief: Json) -> Retry | GiveUp:
        if summary["category"] != "VERIFICATION_FAILED":
            return GiveUp(f"no retry after {summary['outcome']}/{summary['category']}")
        if summary["retry_of"] is not None:
            return GiveUp("already retried once")
        return Retry(goal, {}, f"verification failed ({summary['verdict']['reason']}): retry once")


class RetryElsewhere:
    """S2 choosing another place: after the executor gave up because its place
    spot was blocked, retry with the first spot of the container the final
    belief shows free ({"place_x": x}), at most twice; after a violated
    verification, retry once unchanged (as retry_once); give up otherwise."""

    id = "retry_elsewhere"
    version = "1"

    def decide(self, goal: Goal, summary: Json, belief: Json) -> Retry | GiveUp:
        claim = summary["claim"] or {}
        if claim.get("claim") == "GAVE_UP" and str(claim.get("reason", "")).startswith("blocked"):
            tries = int(summary["conditioning"].get("tries", 0))
            if tries >= 2:
                return GiveUp(f"the place spot was blocked {tries + 1} times")
            obj, container = goal.args
            entities = belief["entities"]
            others = [e["estimate"] for n, e in entities.items() if n != obj and e["kind"] == "object" and e["estimate"]]
            x = free_spots(entities[container]["geometry"], entities[obj]["geometry"]["width"], others)[0]
            return Retry(goal, {"place_x": x, "tries": tries + 1}, f"{claim['reason']}: place at x = {x:.3f} instead")
        return RetryOnce().decide(goal, summary, belief)


class NoCorrection:
    """S0 executes the chunk as S1 computed it."""

    id = "none"
    version = "1"

    def correct(self, chunk: ActionChunk, history: S0History, state: S0Status, now: Ns) -> ActionChunk:
        return chunk


class Blend:
    """A naive linear blend from where S0 is into the new chunk: over
    `window`, the target moves from S0's current position to the chunk's
    trajectory, rows at the chunk's dt from now. A simple stand-in for
    smoothing methods such as Real-Time Chunking, not one of them."""

    id = "blend"
    version = "1"

    def __init__(self, window: Ns) -> None:
        if window <= 0:
            raise ConfigError(f"blend window {window} ns: must be positive")
        self.window = window

    def correct(self, chunk: ActionChunk, history: S0History, state: S0Status, now: Ns) -> ActionChunk:
        a, here = chunk.actions, np.array(state.position)
        times = np.arange(now, max(chunk.valid_until, now) + 1, chunk.dt)
        f = np.clip((times - chunk.t_start) / chunk.dt, 0.0, chunk.horizon - 1.0)
        i = np.minimum(f.astype(int), chunk.horizon - 1)
        j = np.minimum(i + 1, chunk.horizon - 1)
        target = a[i] + (a[j] - a[i]) * (f - i)[:, None]
        alpha = np.minimum(1.0, (times - now) / self.window)[:, None]
        rows = (1 - alpha) * here + alpha * target
        return ActionChunk.make(
            chunk.seq,
            chunk.action_space,
            chunk.dims,
            now,
            chunk.dt,
            rows,
            chunk.valid_until,
            chunk.source,
            observed_at=chunk.observed_at,
            inference_ns=chunk.inference_ns,
        )


class Periodic:
    """An inference at every S1 step: the behaviour before R3."""

    id = "periodic"
    version = "1"

    def due(self, context: InferenceContext) -> bool:
        return True


class Mismatch:
    """An inference when S0's state leaves the position it is tracking by
    more than `threshold` (action-space units), or when the chunk S0 runs
    would end before a new one, started now, could arrive; never while one
    is on its way."""

    id = "mismatch"
    version = "1"

    def __init__(self, threshold: float) -> None:
        if threshold <= 0:
            raise ConfigError(f"mismatch threshold {threshold}: must be positive")
        self.threshold = threshold

    def due(self, context: InferenceContext) -> bool:
        if context.in_flight:
            return False
        s, h = context.status, context.history
        chunk = s.last_chunk
        if s.mode is not S0Mode.TRACKING or chunk is None:
            return True
        if chunk.valid_until - context.now <= context.latency + context.period:
            return True
        return len(h.t) > 0 and float(np.abs(h.desired[-1] - h.command[-1]).max()) > self.threshold


class FixedLatency:
    """S2 takes exactly `seconds` to decide, every time: a fixed time to first token."""

    id = "fixed"
    version = "1"

    def __init__(self, seconds: float, spread: float = 0.0) -> None:
        if seconds < 0 or spread != 0:
            raise ConfigError(f"fixed latency: {seconds} s with spread {spread}; it takes seconds >= 0 and no spread")
        self.delay = ns(seconds)

    def sample(self, rng: np.random.Generator) -> Ns:
        return self.delay


class LognormalLatency:
    """S2's time to first token, sampled for every decision from a lognormal
    distribution: median `seconds`, `spread` the standard deviation of its
    logarithm (0.5 puts 90 % of the samples between 0.44 and 2.3 times the
    median). With a median of 0.1, 1 or 5 s it stands in for a model on the
    robot, on an edge server or in the cloud, whose latency varies."""

    id = "lognormal"
    version = "1"

    def __init__(self, seconds: float, spread: float) -> None:
        if seconds <= 0 or spread < 0:
            raise ConfigError(f"lognormal latency: median {seconds} s, spread {spread}; both must be positive")
        self.median, self.spread = seconds, spread

    def sample(self, rng: np.random.Generator) -> Ns:
        return ns(float(rng.lognormal(math.log(self.median), self.spread)))


FILTERS: Mapping[str, Callable[[], ObservationFilter]] = {"identity": IdentityFilter}
VERIFIERS: Mapping[str, Callable[[], Verifier]] = {"geometric": GeometricVerifier}
RECOVERY: Mapping[str, Callable[[], RecoveryPolicy]] = {"retry_once": RetryOnce, "retry_elsewhere": RetryElsewhere}
# Robot-side S1 extensions take their parameter table, [s1_host] in runtime.toml.
CORRECTORS: Mapping[str, Callable[[Json], ChunkCorrector]] = {
    "none": lambda p: NoCorrection(),
    "blend": lambda p: Blend(ns(float(p["blend_s"]))),
}
INFERENCE_TRIGGERS: Mapping[str, Callable[[Json], InferenceTrigger]] = {
    "periodic": lambda p: Periodic(),
    "mismatch": lambda p: Mismatch(float(p["mismatch"])),
}
# Latency models take the median time to first token (s) and a spread (0 for fixed).
LATENCY: Mapping[str, Callable[[float, float], LatencyModel]] = {"fixed": FixedLatency, "lognormal": LognormalLatency}


def describe(extension: Any) -> Json:
    return {"id": extension.id, "version": extension.version}
