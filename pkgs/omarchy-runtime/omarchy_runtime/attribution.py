"""Two-level verification: how faithfully S0 executed the intent (fidelity),
beside the outcome verifier, and to which layer a failure belongs.

Fidelity comes from S0's own counters (S0Counters), accumulated tick by tick
on the data plane and read once per execution: the tracking error, the share
of time in the contact mode requested, the share of time with the force
within its bounds, the object's error against its intent, and the guard's
interventions. The outcome verifier is unchanged; fidelity never makes a
success.

Attribution of an execution that ran and did not succeed (FAILED,
UNVERIFIED), the first rule that holds:

1. INFRASTRUCTURE: S1 stalled, S0 starved (a chunk ran out with no
   successor) or ticked late, or S2's link was lost during the execution.
2. PERCEPTION: the verdict is inconclusive, or the belief was
   contradictory on one of the goal's entities during the execution: two
   sources saw it at about the same time, confidently, too far apart.
3. EXECUTION: S0 did not achieve the intent: the fidelity check failed (a
   threshold missed, a command the guard replaced, a degraded chunk), or S0
   reported a fault.
4. INTENT: S0 executed the intent faithfully, and the goal was not reached.

An intent that cannot express something (a grasp, a contact) cannot be
checked for it: with motion-only intents, a slipped grasp is an INTENT
failure. The attribution is only as precise as the intent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from omarchy_runtime.contracts import Category, Json, S0Counters, State

ATTRIBUTIONS = ("INTENT", "EXECUTION", "PERCEPTION", "INFRASTRUCTURE")


@dataclass(frozen=True)
class FidelityLimits:
    """The fidelity check's thresholds (runtime.toml [fidelity])."""

    tracking: float  # the largest mean tracking error, in the motion's units (rad, m)
    contact_mode: float  # the least share of steady contact ticks in the mode requested
    force: float  # the least share of MAINTAIN ticks with the force within its bounds
    object: float  # the largest mean object error, m


def _mean(total: float, n: int) -> float | None:
    return total / n if n else None


def fidelity(c: S0Counters, limits: FidelityLimits) -> Json:
    """The execution's fidelity from S0's counters since it started; a
    measure is None when the intents had no such channel."""
    tracking = _mean(c.motion_error_sum, c.motion_ticks)
    mode = _mean(c.contact_mode_ticks, c.contact_ticks)
    force = _mean(c.force_in_bounds_ticks, c.force_ticks)
    obj = _mean(c.object_error_sum, c.object_ticks)
    failed = []
    if tracking is not None and tracking > limits.tracking:
        failed.append(f"mean tracking error {tracking:.4f} above {limits.tracking}")
    if mode is not None and mode < limits.contact_mode:
        failed.append(f"in the contact mode requested {100 * mode:.1f} % of the time, below {100 * limits.contact_mode:.0f} %")
    if force is not None and force < limits.force:
        failed.append(f"force within bounds {100 * force:.1f} % of the time in MAINTAIN, below {100 * limits.force:.0f} %")
    if obj is not None and obj > limits.object:
        failed.append(f"mean object error {obj:.4f} m above {limits.object} m")
    if c.guard_replacements:
        failed.append(f"the guard replaced {c.guard_replacements} command(s)")
    if c.degraded_chunks:
        failed.append(f"{c.degraded_chunks} chunk(s) executed in a degraded mode")
    return {
        "tracking_error": tracking,
        "contact_mode_share": mode,
        "force_share": force,
        "object_error": obj,
        "force_peak": c.force_peak,
        "guard_clamps": c.limit_clamps,
        "guard_replacements": c.guard_replacements,
        "degraded_chunks": c.degraded_chunks,
        "faithful": not failed,
        "failed": failed,
    }


def attribute(
    outcome: State,
    category: Category | None,
    reason: str,
    fid: Json | None,
    c: S0Counters,
    contradictions: Sequence[Json],
    s2_lost: bool,
    s0_fault: bool,
) -> Json | None:
    """The attribution of a FAILED or UNVERIFIED execution (the module's
    rules), with the evidence for it; None for any other outcome."""
    if outcome not in (State.FAILED, State.UNVERIFIED):
        return None
    infra = []
    if category is Category.S1_STALL:
        infra.append(reason)
    if c.starvation_events:
        infra.append(f"S0 starved {c.starvation_events} time(s): a chunk ran out with no successor")
    if c.late_ticks:
        infra.append(f"S0 ticked late {c.late_ticks} time(s)")
    if s2_lost:
        infra.append("S2's link was lost")
    if infra:
        return {"category": "INFRASTRUCTURE", "reason": "; ".join(infra)}
    if category is Category.VERIFICATION_INCONCLUSIVE:
        return {"category": "PERCEPTION", "reason": f"verdict inconclusive: {reason}"}
    if contradictions:
        seen = "; ".join(
            f"{x['entity']}: {x['sources'][0]} and {x['sources'][1]} {x['distance_m']:.3f} m apart" for x in contradictions[:3]
        )
        return {"category": "PERCEPTION", "reason": f"belief contradictory: {seen}"}
    if s0_fault or (fid is not None and not fid["faithful"]):
        why = ["S0 reported a fault"] if s0_fault else list(fid["failed"]) if fid else []
        return {"category": "EXECUTION", "reason": "S0 did not achieve the intent: " + "; ".join(why)}
    return {"category": "INTENT", "reason": f"S0 executed the intent faithfully; the goal was not reached ({reason})"}
