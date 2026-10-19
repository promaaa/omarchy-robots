"""The collaborator extension points: default implementations, and the
registries configuration selects them from by id.

To add an ObservationFilter, a Verifier, a RecoveryPolicy, an S2
LatencyModel or a capability's FallbackGenerator: write a class with `id`, `version` and the method its interface
in contracts.py names, add it to the registry below, and select it in
config/runtime.toml ([extensions]) or in a scenario's [extensions] table. The
runtime core does not change. tests/contracts.py holds the contract tests a
new implementation should pass.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Mapping
from typing import Any

import numpy as np

from omarchy_runtime.belief import WorldState, free_spots
from omarchy_runtime.contracts import (
    CARTESIAN,
    ActionChunk,
    BodySpec,
    ChunkCorrector,
    ConfigError,
    ContactIntent,
    ContactMode,
    FallbackGenerator,
    FallbackSpec,
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
    Timing,
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


class CoverageVerifier:
    """Judges clean(surface) from the latest coverage of the surface seen at
    or after not_before (a dirt sensor): fewer than VISIBLE of its cells seen
    (occluded) is INCONCLUSIVE; else at least CLEAN of those seen looking
    clean is SATISFIED, fewer is VIOLATED. No such observation: INCONCLUSIVE.
    A simulated sensor feeds it; a real cleanliness verifier is future work."""

    id = "coverage"
    version = "1"
    CLEAN = 0.9
    VISIBLE = 0.8

    def verify(self, goal: Goal, belief: WorldState, not_before: Ns) -> Verdict:
        if goal.predicate != "clean" or len(goal.args) != 1:
            return Verdict(VerdictKind.INCONCLUSIVE, (), f"coverage judges clean(surface), not {goal}")
        seen = belief.coverage(goal.args[0], not_before)
        if seen is None:
            return Verdict(VerdictKind.INCONCLUSIVE, (), f"no coverage of {goal.args[0]} seen since t={not_before} ns")
        c, obs = seen
        if c.seen < self.VISIBLE * c.cells:
            return Verdict(VerdictKind.INCONCLUSIVE, (obs,), f"occluded: {c.seen} of {c.cells} cells of {c.entity} seen [{obs}]")
        share = c.clean / c.seen
        kind = VerdictKind.SATISFIED if share >= self.CLEAN else VerdictKind.VIOLATED
        return Verdict(kind, (obs,), f"{c.clean} of {c.seen} cells of {c.entity} seen look clean ({100 * share:.0f} %) [{obs}]")


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


class NoRetry:
    """Never retry: one execution per goal (the scrubbing experiment compares first attempts)."""

    id = "give_up"
    version = "1"

    def decide(self, goal: Goal, summary: Json, belief: Json) -> Retry | GiveUp:
        return GiveUp(f"no retry after {summary['outcome']}/{summary['category']}: this S2 never retries")


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


class Hold:
    """A capability's default fallback: hold where S0 is. Every dimension, the
    gripper included, takes a relative row of zeros: S0 keeps the position it
    has when it engages, so a held object stays held and an open gripper
    stays open. It lasts its whole window. (A gripper moved by a fallback
    could reopen on an object S1 was closing on: never.)"""

    id = "hold"
    version = "1"

    def motion_key(self, body: BodySpec, status: S0Status) -> Hashable:
        return None  # always the same motion

    def offsets(self, body: BodySpec, status: S0Status) -> list[float]:
        return [0.0] * len(body.dims)

    def fallback(self, body: BodySpec, status: S0Status, valid_from: Ns, valid_until: Ns) -> FallbackSpec:
        source = f"default.{self.id}@{self.version}"
        rows = [self.offsets(body, status)]
        return FallbackSpec.make(
            body.action_space,
            body.dims,
            FALLBACK_DT,
            rows,
            valid_from,
            valid_until,
            source,
            max(valid_until - valid_from, FALLBACK_DT),  # all of its window
            [True] * len(body.dims),
        )


class LiftHeld(Hold):
    """Hold, and with the gripper closed (below the middle of its range) on a
    body that has a "z" dimension, rise by `LIFT_M` first: a held object
    leaves whatever it is near. On another body (an arm commanded in joint
    space) it is Hold. Its motion changes with the grasp, so the runtime
    renews it after a grasp and after a release."""

    id = "lift"
    version = "1"

    def motion_key(self, body: BodySpec, status: S0Status) -> Hashable:
        return self._holding(body, status)

    def _holding(self, body: BodySpec, status: S0Status) -> bool:
        """A closed gripper on a body that can rise."""
        if "z" not in body.dims or "gripper" not in body.dims:
            return False
        g = body.dims.index("gripper")
        return status.position[g] < (body.lower[g] + body.upper[g]) / 2

    def offsets(self, body: BodySpec, status: S0Status) -> list[float]:
        row = super().offsets(body, status)
        if self._holding(body, status):
            row[body.dims.index("z")] = LIFT_M
        return row


class Retreat:
    """A Cartesian body's default fallback for contact: leave contact upwards,
    compliant along the normal. The end-effector rises RETREAT_M from where S0
    is, its orientation kept, with a contact channel that breaks contact and
    then stays free, soft along z (RETREAT_STIFFNESS). The body names its
    Cartesian space, frame and end-effector (`kinematics.cartesian`)."""

    id = "retreat"
    version = "1"

    def motion_key(self, body: BodySpec, status: S0Status) -> Hashable:
        return None  # always the same motion

    def fallback(self, body: BodySpec, status: S0Status, valid_from: Ns, valid_until: Ns) -> FallbackSpec:
        cart = body.kinematics.get("cartesian")
        if cart is None:
            raise ConfigError(f"body {body.id}: the retreat fallback needs kinematics.cartesian")
        source = f"default.{self.id}@{self.version}"
        lasts = max(valid_until - valid_from, RETREAT_DT)
        timing = Timing(0, RETREAT_DT, lasts, source, 0)
        modes = [ContactMode.BREAK, ContactMode.FREE]
        contact = ContactIntent.make(
            timing, cart["ee"], cart["frame"], (0, 0, 1), modes, [[0.0], [0.0]], [0.0], [RETREAT_MAX_N], RETREAT_STIFFNESS
        )
        rows = [[0.0, 0.0, RETREAT_M, 1.0, 0.0, 0.0, 0.0]]
        return FallbackSpec.make(
            cart["action_space"], CARTESIAN, RETREAT_DT, rows, valid_from, valid_until, source, lasts, [True] * 7, cart["frame"], contact
        )


FALLBACK_DT = ns(0.2)  # the default fallback's one row, reached over this time
LIFT_M = 0.02
RETREAT_DT, RETREAT_M, RETREAT_MAX_N = ns(0.5), 0.03, 20.0
RETREAT_STIFFNESS = (1000.0, 1000.0, 200.0, 50.0, 50.0, 50.0)

FILTERS: Mapping[str, Callable[[], ObservationFilter]] = {"identity": IdentityFilter}
VERIFIERS: Mapping[str, Callable[[], Verifier]] = {"geometric": GeometricVerifier, "coverage": CoverageVerifier}
RECOVERY: Mapping[str, Callable[[], RecoveryPolicy]] = {"retry_once": RetryOnce, "retry_elsewhere": RetryElsewhere, "give_up": NoRetry}
# Robot-side S1 extensions take their parameter table, [s1_host] in runtime.toml.
CORRECTORS: Mapping[str, Callable[[Json], ChunkCorrector]] = {
    "none": lambda p: NoCorrection(),
    "blend": lambda p: Blend(ns(float(p["blend_s"]))),
}
INFERENCE_TRIGGERS: Mapping[str, Callable[[Json], InferenceTrigger]] = {
    "periodic": lambda p: Periodic(),
    "mismatch": lambda p: Mismatch(float(p["mismatch"])),
}
# A capability names its default fallback generator in capabilities.toml (default_fallback).
DEFAULT_FALLBACKS: Mapping[str, Callable[[], FallbackGenerator]] = {"hold": Hold, "lift": LiftHeld, "retreat": Retreat}
# Latency models take the median time to first token (s) and a spread (0 for fixed).
LATENCY: Mapping[str, Callable[[float, float], LatencyModel]] = {"fixed": FixedLatency, "lognormal": LognormalLatency}


def describe(extension: Any) -> Json:
    return {"id": extension.id, "version": extension.version}
