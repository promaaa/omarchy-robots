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

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from omarchy_runtime.belief import WorldState
from omarchy_runtime.contracts import (
    GiveUp,
    Goal,
    Json,
    LatencyModel,
    Ns,
    Observation,
    ObservationFilter,
    RecoveryPolicy,
    Retry,
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


class FixedLatency:
    """S2 takes exactly `seconds` to decide, every time."""

    id = "fixed"
    version = "1"

    def __init__(self, seconds: float) -> None:
        self.delay = ns(seconds)

    def sample(self, rng: np.random.Generator) -> Ns:
        return self.delay


FILTERS: Mapping[str, Callable[[], ObservationFilter]] = {"identity": IdentityFilter}
VERIFIERS: Mapping[str, Callable[[], Verifier]] = {"geometric": GeometricVerifier}
RECOVERY: Mapping[str, Callable[[], RecoveryPolicy]] = {"retry_once": RetryOnce}
LATENCY: Mapping[str, Callable[[float], LatencyModel]] = {"fixed": FixedLatency}


def describe(extension: Any) -> Json:
    return {"id": extension.id, "version": extension.version}
