"""Contracts between the components of the Omarchy Robots runtime.

Plain data and small interfaces, nothing else. This module imports no
simulator or backend: a native S0 process, a vendor controller, a learned
policy or a remote S2 can implement these as long as it exchanges the same
data.

Time is an integer number of nanoseconds (`Ns`) on the clock the scheduler
injects into every step; in R1 it is simulated time. Positions are metres in
the vertical x-z plane (x to the right, z up).
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import NoneType
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from omarchy_runtime.belief import WorldState

Ns = int
Json = dict[str, Any]
Array = NDArray[np.float64]

SECOND: Ns = 1_000_000_000


def ns(seconds: float) -> Ns:
    return round(seconds * SECOND)


def seconds(t: Ns) -> float:
    return t / SECOND


# Errors: one hierarchy; `category` names the kind in traces and messages.


class OmarchyError(Exception):
    category = "error"


class ConfigError(OmarchyError):
    """A configuration file or value is invalid."""

    category = "config"


class ContractError(OmarchyError):
    """A value does not satisfy a contract of this module."""

    category = "contract"


class LifecycleError(OmarchyError):
    """An execution was asked to make a transition its state machine forbids."""

    category = "lifecycle"


class TraceError(OmarchyError):
    """A run directory cannot be written or read."""

    category = "trace"


class LinkError(OmarchyError):
    """The link between two processes is closed, or the peer broke the protocol."""

    category = "link"


class Name(enum.StrEnum):
    """A string enum whose values are the member names, as written in traces."""

    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> str:
        return name


class Rejection(Name):
    ACTION_SPACE = enum.auto()
    SHAPE = enum.auto()
    NOT_FINITE = enum.auto()
    EXPIRED = enum.auto()
    STALE = enum.auto()


class ChunkRejected(ContractError):
    """S0 refused an action chunk or a fallback; `code` says why."""

    def __init__(self, code: Rejection, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class Truth(Name):
    TRUE = enum.auto()
    FALSE = enum.auto()
    UNKNOWN = enum.auto()


class S0Mode(Name):
    TRACKING = enum.auto()  # executing a valid chunk from S1
    FALLBACK = enum.auto()  # executing an installed fallback
    HOLD = enum.auto()  # built-in: keep the joints, leave the gripper as it is
    FAULT = enum.auto()  # the body reports a fault (the simulated S0 never does)


class Claim(Name):
    DONE = enum.auto()
    GAVE_UP = enum.auto()


class VerdictKind(Name):
    SATISFIED = enum.auto()
    VIOLATED = enum.auto()
    INCONCLUSIVE = enum.auto()


class State(Name):
    PENDING = enum.auto()
    RUNNING = enum.auto()
    VERIFYING = enum.auto()
    # Terminal states are the outcomes.
    SUCCEEDED = enum.auto()
    FAILED = enum.auto()
    UNVERIFIED = enum.auto()
    REJECTED = enum.auto()
    ABORTED = enum.auto()


TERMINAL = frozenset({State.SUCCEEDED, State.FAILED, State.UNVERIFIED, State.REJECTED, State.ABORTED})


class Category(Name):
    NO_CAPABILITY = enum.auto()
    PRECONDITION_UNMET = enum.auto()
    IMPLEMENTATION_ERROR = enum.auto()
    TIMEOUT = enum.auto()
    S1_STALL = enum.auto()
    VERIFICATION_FAILED = enum.auto()
    VERIFICATION_INCONCLUSIVE = enum.auto()
    CANCELLED = enum.auto()


# The failure categories each outcome admits; SUCCEEDED has none.
OUTCOME_CATEGORIES: Mapping[State, frozenset[Category]] = {
    State.SUCCEEDED: frozenset(),
    State.REJECTED: frozenset({Category.NO_CAPABILITY, Category.PRECONDITION_UNMET}),
    State.FAILED: frozenset({Category.IMPLEMENTATION_ERROR, Category.TIMEOUT, Category.S1_STALL, Category.VERIFICATION_FAILED}),
    State.UNVERIFIED: frozenset({Category.VERIFICATION_INCONCLUSIVE}),
    State.ABORTED: frozenset({Category.CANCELLED}),
}


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
        raise ContractError(f"{what} must be a list of strings, not {value!r}")
    return tuple(value)


def _field(value: Any, key: str, kind: type | tuple[type, ...], what: str) -> Any:
    """value[key], refused with a ContractError unless it is a `kind` (a bool is never an int)."""
    if not isinstance(value, dict) or key not in value:
        raise ContractError(f"{what}: missing {key}")
    item = value[key]
    if not isinstance(item, kind) or (isinstance(item, bool) and bool not in (kind if isinstance(kind, tuple) else (kind,))):
        raise ContractError(f"{what}: {key} is {type(item).__name__}")
    return item


def _floats(value: Any, what: str) -> tuple[float, ...]:
    if not isinstance(value, list) or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value):
        raise ContractError(f"{what} must be a list of numbers, not {value!r}")
    return tuple(float(v) for v in value)


@dataclass(frozen=True)
class Goal:
    """A goal predicate over entities, such as in(cup_1, drying_rack).

    Applications and S2 request goals, never robot verbs: a capability is how
    a goal gets achieved. JSON form: {"predicate": "in", "args": ["cup_1", "drying_rack"]}.
    """

    predicate: str
    args: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.predicate}({', '.join(self.args)})"

    def to_json(self) -> Json:
        return {"predicate": self.predicate, "args": list(self.args)}

    @staticmethod
    def from_json(value: Any) -> Goal:
        if not isinstance(value, dict) or set(value) != {"predicate", "args"} or not isinstance(value["predicate"], str):
            raise ContractError(f'a goal is {{"predicate": str, "args": [str, ...]}}, not {value!r}')
        return Goal(value["predicate"], _strings(value["args"], "goal args"))


@dataclass(frozen=True)
class Detection:
    """One entity seen by a sensor: its estimated position and the detector's confidence (0 to 1)."""

    entity: str
    x: float
    z: float
    confidence: float

    def to_json(self) -> Json:
        return {"entity": self.entity, "x": self.x, "z": self.z, "confidence": self.confidence}


@dataclass(frozen=True)
class Proprioception:
    """What the body reports about itself: its position over `dims` (the action
    space's dimensions, gripper included) and the gripper's reference point."""

    dims: tuple[str, ...]
    position: tuple[float, ...]
    ee: tuple[float, float]

    def value(self, dim: str) -> float:
        return self.position[self.dims.index(dim)]

    def to_json(self) -> Json:
        return {"dims": list(self.dims), "position": list(self.position), "ee": list(self.ee)}


@dataclass(frozen=True)
class Observation:
    """A timestamped set of structured detections, plus proprioception. No images.

    `t` is when the world was observed, not when the observation was used.
    """

    id: str
    t: Ns
    source: str
    detections: tuple[Detection, ...]
    proprio: Proprioception | None

    def to_json(self) -> Json:
        return {
            "id": self.id,
            "t": self.t,
            "source": self.source,
            "detections": [d.to_json() for d in self.detections],
            "proprio": self.proprio.to_json() if self.proprio else None,
        }


@dataclass(frozen=True)
class BodySpec:
    """A robot body as the runtime sees it, from configuration.

    `dims` names the action space's dimensions; "gripper" is one of them (0 is
    closed, 1 is open). `lower`, `upper` and `max_velocity` are per dimension.
    `workspace` (x0, x1, z0, z1) is where the gripper's reference point may go.
    `kinematics` holds the kind-specific parameters as plain data.
    """

    id: str
    kind: str
    action_space: str
    dims: tuple[str, ...]
    lower: tuple[float, ...]
    upper: tuple[float, ...]
    max_velocity: tuple[float, ...]
    workspace: tuple[float, float, float, float]
    features: tuple[str, ...]
    home: tuple[float, ...]
    safe_pose: tuple[float, ...]
    kinematics: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def reaches(self, x: float, z: float, tolerance: float = 0.0) -> bool:
        x0, x1, z0, z1 = self.workspace
        return x0 - tolerance <= x <= x1 + tolerance and z0 - tolerance <= z <= z1 + tolerance

    def to_json(self) -> Json:
        return {
            "id": self.id,
            "kind": self.kind,
            "action_space": self.action_space,
            "dims": list(self.dims),
            "lower": list(self.lower),
            "upper": list(self.upper),
            "max_velocity": list(self.max_velocity),
            "workspace": list(self.workspace),
            "features": list(self.features),
            "home": list(self.home),
            "safe_pose": list(self.safe_pose),
            "kinematics": dict(self.kinematics),
        }

    @staticmethod
    def from_json(value: Any) -> BodySpec:
        def numbers(key: str) -> tuple[float, ...]:
            return _floats(_field(value, key, list, "body"), f"body {key}")

        workspace = numbers("workspace")
        if len(workspace) != 4:
            raise ContractError(f"body workspace has {len(workspace)} values, not 4")
        return BodySpec(
            id=_field(value, "id", str, "body"),
            kind=_field(value, "kind", str, "body"),
            action_space=_field(value, "action_space", str, "body"),
            dims=_strings(_field(value, "dims", list, "body"), "body dims"),
            lower=numbers("lower"),
            upper=numbers("upper"),
            max_velocity=numbers("max_velocity"),
            workspace=(workspace[0], workspace[1], workspace[2], workspace[3]),
            features=_strings(_field(value, "features", list, "body"), "body features"),
            home=numbers("home"),
            safe_pose=numbers("safe_pose"),
            kinematics=_field(value, "kinematics", dict, "body"),
        )


def _actions(rows: Any, dims: Sequence[str], what: str) -> Array:
    actions = np.array(rows, dtype=np.float64)
    if actions.ndim != 2 or actions.shape[0] < 1 or actions.shape[1] != len(dims):
        raise ChunkRejected(Rejection.SHAPE, f"{what} actions are {actions.shape}, expected (H >= 1, {len(dims)})")
    if not np.all(np.isfinite(actions)):
        raise ChunkRejected(Rejection.NOT_FINITE, f"{what} actions contain NaN or infinity")
    actions.setflags(write=False)
    return actions


@dataclass(frozen=True, eq=False)
class ActionChunk:
    """A short timestamped trajectory over a body's action space, from S1 to S0.

    Row k of `actions` (H x D, D = len(dims)) is the target at t_start + k*dt.
    S0 interpolates linearly between rows and holds the last row until
    `valid_until`; after valid_until the chunk must not be executed. A chunk
    replaces the active one at once (the latest sequence wins), unless the
    active chunk is still valid, comes from the same `source` and has the same
    or a higher `seq`: then it is stale. Each S1 run numbers its chunks from 1.
    Chunks flow from S1 to S0 directly, never through the runtime; the
    runtime sees only their metadata, in S0's status.

    `observed_at` is when the observation the chunk was computed from was
    taken, and `inference_ns` the inference's measured latency, from its
    start to the chunk's hand-off to S0 (set by S1's host): what an
    asynchronous correction needs to know how old the chunk is.
    """

    seq: int
    action_space: str
    dims: tuple[str, ...]
    t_start: Ns
    dt: Ns
    actions: Array
    valid_until: Ns
    source: str
    observed_at: Ns = 0
    inference_ns: Ns = 0

    @staticmethod
    def make(
        seq: int,
        action_space: str,
        dims: Sequence[str],
        t_start: Ns,
        dt: Ns,
        rows: Any,
        valid_until: Ns,
        source: str,
        *,
        observed_at: Ns | None = None,
        inference_ns: Ns = 0,
    ) -> ActionChunk:
        """`observed_at` defaults to `t_start`."""
        if dt <= 0:
            raise ChunkRejected(Rejection.SHAPE, f"chunk dt is {dt} ns")
        seen = t_start if observed_at is None else observed_at
        return ActionChunk(
            seq, action_space, tuple(dims), t_start, dt, _actions(rows, dims, "chunk"), valid_until, source, seen, inference_ns
        )

    @property
    def horizon(self) -> int:
        return int(self.actions.shape[0])


@dataclass(frozen=True, eq=False)
class FallbackSpec:
    """A short trajectory S0 runs instead of HOLD when its chunk expires, or
    when the execution fails (the runtime engages it then).

    S0 holds two: the base fallback (a capability's default, computed by the
    runtime on the robot side) and S2's (predicted with the goal, or refined
    later), which takes precedence while it is valid. So S0 always has a safe
    behaviour and never waits for a slower layer.

    Row k is the target at te + (k+1)*dt, where te is the tick at which S0
    engages the fallback: the trajectory starts from wherever S0 is at te. A
    dimension marked in `relative` takes its rows as offsets from S0's
    position at te ("hold where you are" is a row of zeros); the others take
    them as targets. S0 engages a fallback only while valid_from <= now <=
    valid_until, runs its rows, then holds the last row, still in FALLBACK,
    until te + `lasts`: then the fallback has expired, S0 drops it and runs the
    next one (S2's expires into the base) or HOLD. `lasts` is at least the
    rows' duration (the default: no holding). A new valid chunk takes over at
    once. S2's fallback, once engaged, is then spent (S2 may install
    another): a brief gap in S1's chunks must not send S0 towards S2's pose
    again and again. The base stays installed and engages again, from its
    start, the next time S0 needs it.
    """

    action_space: str
    dims: tuple[str, ...]
    dt: Ns
    actions: Array
    valid_from: Ns
    valid_until: Ns
    source: str
    lasts: Ns
    relative: tuple[bool, ...]

    @staticmethod
    def make(
        action_space: str,
        dims: Sequence[str],
        dt: Ns,
        rows: Any,
        valid_from: Ns,
        valid_until: Ns,
        source: str,
        lasts: Ns | None = None,
        relative: Sequence[bool] | None = None,
    ) -> FallbackSpec:
        if dt <= 0 or valid_until < valid_from:
            raise ChunkRejected(Rejection.SHAPE, f"fallback dt {dt} ns, window [{valid_from}, {valid_until}]")
        actions = _actions(rows, dims, "fallback")
        duration = int(actions.shape[0]) * dt
        if lasts is not None and lasts < duration:
            raise ChunkRejected(Rejection.SHAPE, f"fallback lasts {lasts} ns, shorter than its {duration} ns of rows")
        rel = tuple(bool(r) for r in relative) if relative is not None else (False,) * len(dims)
        if len(rel) != len(dims):
            raise ChunkRejected(Rejection.SHAPE, f"fallback relative has {len(rel)} entries for {len(dims)} dims")
        return FallbackSpec(
            action_space, tuple(dims), dt, actions, valid_from, valid_until, source, duration if lasts is None else lasts, rel
        )

    def shifted(self, by: Ns) -> FallbackSpec:
        """The same fallback with its window moved by `by` (a window relative to a start, made absolute)."""
        return FallbackSpec(
            self.action_space,
            self.dims,
            self.dt,
            self.actions,
            self.valid_from + by,
            self.valid_until + by,
            self.source,
            self.lasts,
            self.relative,
        )

    def same_motion(self, other: FallbackSpec) -> bool:
        """The same rows, timing, frames and duration (the window and source aside)."""
        return (
            (self.action_space, self.dims, self.dt, self.lasts, self.relative)
            == (other.action_space, other.dims, other.dt, other.lasts, other.relative)
            and self.actions.shape == other.actions.shape
            and bool(np.all(self.actions == other.actions))
        )

    @property
    def duration(self) -> Ns:
        return int(self.actions.shape[0]) * self.dt

    def to_json(self) -> Json:
        return {
            "action_space": self.action_space,
            "dims": list(self.dims),
            "dt": self.dt,
            "actions": self.actions.tolist(),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "source": self.source,
            "lasts": self.lasts,
            "relative": list(self.relative),
        }

    @staticmethod
    def from_json(value: Any) -> FallbackSpec:
        try:
            return FallbackSpec.make(
                str(value["action_space"]),
                _strings(value["dims"], "fallback dims"),
                int(value["dt"]),
                value["actions"],
                int(value["valid_from"]),
                int(value["valid_until"]),
                str(value["source"]),
                int(value["lasts"]),
                [bool(r) for r in value["relative"]],
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ContractError(f"not a fallback: {e}") from e


@dataclass(frozen=True)
class ChunkMeta:
    """An action chunk without its actions: what the runtime may know and log."""

    seq: int
    source: str
    t_start: Ns
    dt: Ns
    horizon: int
    valid_until: Ns
    received: Ns
    observed_at: Ns = 0
    inference_ns: Ns = 0

    def to_json(self) -> Json:
        return {
            "seq": self.seq,
            "source": self.source,
            "t_start": self.t_start,
            "dt": self.dt,
            "horizon": self.horizon,
            "valid_until": self.valid_until,
            "received": self.received,
            "observed_at": self.observed_at,
            "inference_ns": self.inference_ns,
        }


@dataclass(frozen=True, eq=False)
class S0History:
    """S0's latest ticks, oldest first, as plain arrays: tick times (ns), the
    positions S0 commanded and the desired positions it was tracking (n x D).
    For the robot side (S1's host, a chunk corrector, an inference trigger)
    at S1's rate; never streamed into traces."""

    t: NDArray[np.int64]
    command: Array
    desired: Array


@dataclass(frozen=True)
class S0Counters:
    """Cumulative since S0 started. A starvation event is S0 leaving TRACKING
    because its chunk expired with no successor. A late tick is a tick that
    came longer than S0's late threshold after the one before; `max_interval_ns`
    is the longest real interval between two ticks, and `max_step` the largest
    step S0 commanded, as a fraction of its bound (velocity limit times the
    nominal tick period, per dimension): never above 1."""

    ticks: int = 0
    starvation_events: int = 0
    limit_clamps: int = 0
    rejected_chunks: int = 0
    fallback_ns: Ns = 0
    hold_ns: Ns = 0
    max_tracking_error: float = 0.0
    late_ticks: int = 0
    max_interval_ns: Ns = 0
    max_step: float = 0.0
    fallback_expiries: int = 0  # fallbacks whose time (lasts) ran out: S0 dropped them for the next one or HOLD
    fallback_cover_ns: Ns = 0  # time a valid fallback (S2's or the base) was installed, tick by tick

    def since(self, start: S0Counters) -> S0Counters:
        """Counts accumulated after `start` (the maxima are running maxima)."""
        return S0Counters(
            self.ticks - start.ticks,
            self.starvation_events - start.starvation_events,
            self.limit_clamps - start.limit_clamps,
            self.rejected_chunks - start.rejected_chunks,
            self.fallback_ns - start.fallback_ns,
            self.hold_ns - start.hold_ns,
            self.max_tracking_error,
            self.late_ticks - start.late_ticks,
            self.max_interval_ns,
            self.max_step,
            self.fallback_expiries - start.fallback_expiries,
            self.fallback_cover_ns - start.fallback_cover_ns,
        )

    def to_json(self) -> Json:
        return {
            "ticks": self.ticks,
            "starvation_events": self.starvation_events,
            "limit_clamps": self.limit_clamps,
            "rejected_chunks": self.rejected_chunks,
            "fallback_ns": self.fallback_ns,
            "hold_ns": self.hold_ns,
            "max_tracking_error": self.max_tracking_error,
            "late_ticks": self.late_ticks,
            "max_interval_ns": self.max_interval_ns,
            "max_step": self.max_step,
            "fallback_expiries": self.fallback_expiries,
            "fallback_cover_ns": self.fallback_cover_ns,
        }


@dataclass(frozen=True)
class S0Status:
    """S0 as of its last tick. Plain scalars, so another process can produce it.
    `active_seq` is the chunk being tracked (None unless TRACKING); `last_chunk`
    the last one S0 accepted. `fallback_installed` says a valid fallback is
    installed (S2's or the base); `fallback_source` names the one S0 would run
    now, or the one it runs."""

    t: Ns
    mode: S0Mode
    active_seq: int | None
    position: tuple[float, ...]
    last_chunk: ChunkMeta | None
    fallback_installed: bool
    counters: S0Counters
    fallback_source: str | None = None

    def to_json(self) -> Json:
        return {
            "t": self.t,
            "mode": self.mode,
            "active_seq": self.active_seq,
            "position": list(self.position),
            "last_chunk": self.last_chunk.to_json() if self.last_chunk else None,
            "fallback_installed": self.fallback_installed,
            "fallback_source": self.fallback_source,
            "counters": self.counters.to_json(),
        }


@dataclass(frozen=True)
class Report:
    """What the executor (S1) says about its own work. A claim, not a fact."""

    claim: Claim
    reason: str

    def to_json(self) -> Json:
        return {"claim": self.claim, "reason": self.reason}


@dataclass(frozen=True)
class Verdict:
    """A verifier's judgement of a goal from observations, with the ids of the
    observations it rests on. INCONCLUSIVE never becomes success."""

    kind: VerdictKind
    evidence: tuple[str, ...]
    reason: str

    def to_json(self) -> Json:
        return {"kind": self.kind, "evidence": list(self.evidence), "reason": self.reason}


@dataclass(frozen=True)
class Retry:
    goal: Goal
    conditioning: Mapping[str, Any]
    reason: str


@dataclass(frozen=True)
class GiveUp:
    reason: str


# Messages into the runtime's public API (a mailbox, processed at the runtime's
# next step) and updates out of it. All JSON-compatible: an LLM tool-calling
# agent, or S2 on another machine, can drive the same API. The API is
# asynchronous: the sender of a GoalRequest chooses its `request_id`, and every
# Update of the execution it creates carries it back.


@dataclass(frozen=True)
class GoalRequest:
    """Ask for a goal. Executions of one task share `task_id`; a retry names the
    execution it retries in `retry_of`. `conditioning` is passed to S1 as is.
    `request_id` is the sender's, unique within a run.

    `fallback` is S2's predicted fallback, sent with the goal so that it is in
    S0 from the execution's start, before S1's first chunk. Its window is
    relative to that start (the runtime moves it onto its clock). S2 may
    refine it later (InstallFallback)."""

    goal: Goal
    task_id: str
    request_id: str
    retry_of: str | None = None
    conditioning: Mapping[str, Any] = field(default_factory=dict, hash=False)
    fallback: FallbackSpec | None = field(default=None, hash=False)

    def to_json(self) -> Json:
        return {
            "type": "request",
            "request_id": self.request_id,
            "goal": self.goal.to_json(),
            "task_id": self.task_id,
            "retry_of": self.retry_of,
            "conditioning": dict(self.conditioning),
            "fallback": self.fallback.to_json() if self.fallback else None,
        }

    @staticmethod
    def from_json(value: Any) -> GoalRequest:
        what = "request"
        fallback = _field(value, "fallback", (dict, NoneType), what)
        return GoalRequest(
            Goal.from_json(_field(value, "goal", dict, what)),
            _field(value, "task_id", str, what),
            _field(value, "request_id", str, what),
            _field(value, "retry_of", (str, NoneType), what),
            _field(value, "conditioning", dict, what),
            FallbackSpec.from_json(fallback) if fallback is not None else None,
        )


@dataclass(frozen=True)
class InstallFallback:
    """S2's refined fallback for a running execution. It replaces S2's fallback
    in S0 (the predicted one, sent with the goal, if any): refined before
    predicted, before the capability's default, before HOLD. After a failure
    it stays while S0 waits for S2; success or an abort clears it."""

    execution_id: str
    spec: FallbackSpec

    def to_json(self) -> Json:
        return {"type": "install_fallback", "execution_id": self.execution_id, "spec": self.spec.to_json()}

    @staticmethod
    def from_json(value: Any) -> InstallFallback:
        what = "install_fallback"
        return InstallFallback(_field(value, "execution_id", str, what), FallbackSpec.from_json(_field(value, "spec", dict, what)))


@dataclass(frozen=True)
class Cancel:
    execution_id: str

    def to_json(self) -> Json:
        return {"type": "cancel", "execution_id": self.execution_id}

    @staticmethod
    def from_json(value: Any) -> Cancel:
        return Cancel(_field(value, "execution_id", str, "cancel"))


@dataclass(frozen=True)
class Decision:
    """A recovery decision taken outside the runtime (by S2), recorded in the trace."""

    task_id: str
    execution_id: str
    action: str  # "retry" or "give_up"
    reason: str
    policy: str  # "<id>@<version>"

    def to_json(self) -> Json:
        return {
            "type": "decision",
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "action": self.action,
            "reason": self.reason,
            "policy": self.policy,
        }

    @staticmethod
    def from_json(value: Any) -> Decision:
        return Decision(*(_field(value, k, str, "decision") for k in ("task_id", "execution_id", "action", "reason", "policy")))


@dataclass(frozen=True)
class Heartbeat:
    """S2 is alive: sent every heartbeat period when S2 runs in another process.
    The runtime's supervisor starts watching S2 at the first one, and stops at
    a `final` one (S2 is leaving). `sent` is on S2's clock: the link echoes it
    back, and S2 measures the round trip; `rtt` holds the round trips S2
    measured since its previous heartbeat."""

    seq: int
    sent: Ns
    rtt: tuple[Ns, ...] = ()
    final: bool = False

    def to_json(self) -> Json:
        return {"type": "heartbeat", "seq": self.seq, "sent": self.sent, "rtt": list(self.rtt), "final": self.final}

    @staticmethod
    def from_json(value: Any) -> Heartbeat:
        what = "heartbeat"
        rtt = _field(value, "rtt", list, what)
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in rtt):
            raise ContractError(f"{what}: rtt must be non-negative integers (ns)")
        return Heartbeat(_field(value, "seq", int, what), _field(value, "sent", int, what), tuple(rtt), _field(value, "final", bool, what))


Message = GoalRequest | InstallFallback | Cancel | Decision | Heartbeat
MESSAGES: Mapping[str, Any] = {
    "request": GoalRequest,
    "install_fallback": InstallFallback,
    "cancel": Cancel,
    "decision": Decision,
    "heartbeat": Heartbeat,
}


def message_from_json(value: Any) -> Message:
    """Any message of the API from its JSON form; a ContractError for anything else."""
    kind = _field(value, "type", str, "message")
    if kind not in MESSAGES:
        raise ContractError(f"message: unknown type {kind!r}")
    message: Message = MESSAGES[kind].from_json(value)
    return message


@dataclass(frozen=True)
class Update:
    """An execution changed state at `t` (the runtime's clock). `request_id` is
    the one its GoalRequest chose; `summary` (the executions.jsonl record)
    comes with the terminal state."""

    request_id: str
    execution_id: str
    task_id: str
    state: State
    t: Ns
    summary: Json | None

    def to_json(self) -> Json:
        return {
            "type": "execution",
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "state": self.state,
            "t": self.t,
            "summary": self.summary,
        }

    @staticmethod
    def from_json(value: Any) -> Update:
        what = "execution update"
        try:
            state = State(_field(value, "state", str, what))
        except ValueError as e:
            raise ContractError(f"{what}: {e}") from e
        return Update(
            _field(value, "request_id", str, what),
            _field(value, "execution_id", str, what),
            _field(value, "task_id", str, what),
            state,
            _field(value, "t", int, what),
            _field(value, "summary", (dict, NoneType), what),
        )


# Interfaces. Every implementation reports an `id` and a `version` where traces
# need to name it.


class RuntimeApi(Protocol):
    """The runtime's public API, as applications and S2 see it."""

    def submit(self, message: Message) -> None:
        """Queue a message. The execution a GoalRequest creates is named in the
        Updates that carry its request_id."""

    def poll(self) -> list[Update]:
        """Updates since the last poll, oldest first."""


class Transport(Protocol):
    """Frames, each one JSON object, between the runtime's process and another
    one (S2). It never blocks: send queues, receive returns what has arrived.
    Both raise LinkError once the link is closed; frames that arrived before
    the close are returned first."""

    def send(self, frame: Json) -> None: ...

    def receive(self) -> list[Json]: ...

    def close(self) -> None: ...


class S1Policy(Protocol):
    """Action generation (S1): turns a goal into action chunks at its own rate.

    The data plane steps it and sends its chunks straight to S0; the runtime
    only starts it and reads its report at the supervisor rate.
    """

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        """Begin working on `goal` from a belief snapshot."""

    def step(self, now: Ns, observation: Observation | None) -> ActionChunk | None:
        """Compute the next chunk from the latest observation, or nothing."""

    def report(self) -> Report | None:
        """The executor's claim once it has one; None while it is still working."""


@dataclass(frozen=True, eq=False)
class InferenceContext:
    """What an InferenceTrigger sees, at S1's rate: S0's status and its
    recent ticks, how many inferences are on their way, the last measured
    inference latency, and S1's period (how often the trigger is asked)."""

    now: Ns
    status: S0Status
    history: S0History
    in_flight: int
    latency: Ns
    period: Ns


class FallbackGenerator(Protocol):
    """A capability's default fallback, computed by the runtime on the robot
    side from S0's status: installed in S0 as its base fallback when an
    execution starts, before S1's first chunk, and renewed whenever the
    generator returns another motion (after a grasp, say). It must not block,
    read ground truth or call S0; one that raises leaves the base fallback as
    it was and records an error."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def fallback(self, body: BodySpec, status: S0Status, valid_from: Ns, valid_until: Ns) -> FallbackSpec: ...


class ChunkCorrector(Protocol):
    """Applied on the robot side when S1's host hands a new chunk to S0:
    returns the chunk S0 executes. It receives the new chunk (computed from
    an observation taken at chunk.observed_at, inference_ns ago at most),
    S0's executed history since that observation, and S0's current status.

    It runs in S0's loop: it must return within about 0.5 ms, must not
    block, must not read ground truth or any simulator state, and must not
    call S0; the result keeps the chunk's seq, source, action space and
    observation time."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def correct(self, chunk: ActionChunk, history: S0History, state: S0Status, now: Ns) -> ActionChunk: ...


class InferenceTrigger(Protocol):
    """Decides, at S1's rate, whether S1 starts its next inference now. It
    must answer within about 0.5 ms, must not block, read ground truth or
    call S0, and must start one when S0 has no chunk and none is on its way
    (else S0 starves)."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def due(self, context: InferenceContext) -> bool: ...


class S1Host(Protocol):
    """Where S1 runs: it steps the attached policy and delivers its chunks to S0."""

    def attach(self, policy: S1Policy) -> None: ...

    def detach(self) -> None: ...

    def error(self) -> str | None:
        """Why the attached policy stopped working (it raised), or None."""


class S0(Protocol):
    """Real-time execution (S0): the only layer that commands actuators.

    At every tick S0 executes a valid chunk, else a valid installed fallback
    (S2's before the base), else its built-in HOLD; it never waits for a
    slower layer, and it enforces the body's position, velocity and workspace
    limits itself. A fallback engages when a chunk ran out with no successor,
    or when the runtime asks (hold with `fallback`, after a failure); while an
    execution awaits S1's first chunk, S0 holds. It counts, tick by tick, the time a valid fallback is
    installed (`fallback_cover_ns`): the runtime's fallback coverage.

    Bounded steps, a safety contract for every implementation, hardware
    included: S0 never moves a dimension more in one tick than its velocity
    limit times S0's nominal tick period, whatever the real interval since the
    last tick. A late tick (later than a threshold) never produces a catch-up
    jump: S0 continues from its current state at no more than that bound, and
    its target, if the plan moved on meanwhile, is reached later. S0 counts
    late ticks and reports the longest interval and the largest step
    (S0Counters).
    """

    def submit_chunk(self, chunk: ActionChunk, now: Ns) -> None:
        """Non-blocking. Raises ChunkRejected for another action space, an expired chunk or a stale one."""

    def install_fallback(self, spec: FallbackSpec, now: Ns, base: bool = False) -> None:
        """Replace S2's fallback, or with `base` the base fallback, in one step:
        the one replaced stays valid until then. Raises ChunkRejected if it
        does not fit the body or has expired."""

    def hold(self, fallback: bool = False) -> None:
        """Stop executing the active chunk. Without `fallback`, also drop both
        installed fallbacks: HOLD from the next tick until a new chunk arrives.
        With it, engage a valid installed fallback at the next tick (S2's, else
        the base; else HOLD). The runtime calls it when an execution stops
        running: with `fallback` when the execution failed."""

    def status(self) -> S0Status: ...

    def history(self, since: Ns) -> S0History:
        """Ticks at or after `since`, from a bounded buffer (the last seconds only)."""


class ObservationFilter(Protocol):
    """Applied to every observation before it reaches the belief or a trace.

    It may transform or redact fields but must keep entity ids stable within a
    run (the same input id always gives the same output id), and it returns,
    with the filtered observation, the list of what it changed (empty when
    nothing changed).
    """

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def apply(self, observation: Observation) -> tuple[Observation, tuple[str, ...]]: ...


class Verifier(Protocol):
    """Judges a goal from the belief, using only observations taken at or after
    `not_before`. It is called again on newer observations while the
    execution's verification budget lasts."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def verify(self, goal: Goal, belief: WorldState, not_before: Ns) -> Verdict: ...


class RecoveryPolicy(Protocol):
    """Decides what to do after an execution ends without success: retry
    (a new execution of the same task) or give up. `summary` is the
    execution's executions.jsonl record; `belief` its final belief snapshot."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def decide(self, goal: Goal, summary: Json, belief: Json) -> Retry | GiveUp: ...


class LatencyModel(Protocol):
    """How long S2 takes to decide: the delay between what S2 sees and what it
    sends, its time to first token. Fixed or sampled from `rng`."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def sample(self, rng: np.random.Generator) -> Ns: ...


def finite(values: Sequence[float]) -> bool:
    return all(math.isfinite(v) for v in values)
