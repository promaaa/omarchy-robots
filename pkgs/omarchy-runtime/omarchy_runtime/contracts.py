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
    """

    seq: int
    action_space: str
    dims: tuple[str, ...]
    t_start: Ns
    dt: Ns
    actions: Array
    valid_until: Ns
    source: str

    @staticmethod
    def make(seq: int, action_space: str, dims: Sequence[str], t_start: Ns, dt: Ns, rows: Any, valid_until: Ns, source: str) -> ActionChunk:
        if dt <= 0:
            raise ChunkRejected(Rejection.SHAPE, f"chunk dt is {dt} ns")
        return ActionChunk(seq, action_space, tuple(dims), t_start, dt, _actions(rows, dims, "chunk"), valid_until, source)

    @property
    def horizon(self) -> int:
        return int(self.actions.shape[0])


@dataclass(frozen=True, eq=False)
class FallbackSpec:
    """A short trajectory S0 runs instead of HOLD when its chunk expires.

    S2 may install one ahead of time, so S0 always has a safe behaviour and
    never waits for a slower layer. Row k is the target at te + (k+1)*dt, where
    te is the tick at which S0 engages the fallback: the trajectory starts from
    wherever S0 is at te. S0 engages it at most once, only while
    valid_from <= now <= valid_until, runs it to its last row, then returns to
    HOLD. A new valid chunk takes over at once.
    """

    action_space: str
    dims: tuple[str, ...]
    dt: Ns
    actions: Array
    valid_from: Ns
    valid_until: Ns
    source: str

    @staticmethod
    def make(action_space: str, dims: Sequence[str], dt: Ns, rows: Any, valid_from: Ns, valid_until: Ns, source: str) -> FallbackSpec:
        if dt <= 0 or valid_until < valid_from:
            raise ChunkRejected(Rejection.SHAPE, f"fallback dt {dt} ns, window [{valid_from}, {valid_until}]")
        return FallbackSpec(action_space, tuple(dims), dt, _actions(rows, dims, "fallback"), valid_from, valid_until, source)

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

    def to_json(self) -> Json:
        return {
            "seq": self.seq,
            "source": self.source,
            "t_start": self.t_start,
            "dt": self.dt,
            "horizon": self.horizon,
            "valid_until": self.valid_until,
            "received": self.received,
        }


@dataclass(frozen=True)
class S0Counters:
    """Cumulative since S0 started. A starvation event is S0 leaving TRACKING
    because its chunk expired with no successor."""

    ticks: int = 0
    starvation_events: int = 0
    limit_clamps: int = 0
    rejected_chunks: int = 0
    fallback_ns: Ns = 0
    hold_ns: Ns = 0
    max_tracking_error: float = 0.0

    def since(self, start: S0Counters) -> S0Counters:
        """Counts accumulated after `start` (max_tracking_error is the running maximum)."""
        return S0Counters(
            self.ticks - start.ticks,
            self.starvation_events - start.starvation_events,
            self.limit_clamps - start.limit_clamps,
            self.rejected_chunks - start.rejected_chunks,
            self.fallback_ns - start.fallback_ns,
            self.hold_ns - start.hold_ns,
            self.max_tracking_error,
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
        }


@dataclass(frozen=True)
class S0Status:
    """S0 as of its last tick. Plain scalars, so another process can produce it.
    `active_seq` is the chunk being tracked (None unless TRACKING); `last_chunk`
    the last one S0 accepted."""

    t: Ns
    mode: S0Mode
    active_seq: int | None
    position: tuple[float, ...]
    last_chunk: ChunkMeta | None
    fallback_installed: bool
    counters: S0Counters

    def to_json(self) -> Json:
        return {
            "t": self.t,
            "mode": self.mode,
            "active_seq": self.active_seq,
            "position": list(self.position),
            "last_chunk": self.last_chunk.to_json() if self.last_chunk else None,
            "fallback_installed": self.fallback_installed,
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
# agent, or S2 on another machine, can drive the same API.


@dataclass(frozen=True)
class GoalRequest:
    """Ask for a goal. Executions of one task share `task_id`; a retry names the
    execution it retries in `retry_of`. `conditioning` is passed to S1 as is."""

    goal: Goal
    task_id: str
    retry_of: str | None = None
    conditioning: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def to_json(self) -> Json:
        return {
            "type": "request",
            "goal": self.goal.to_json(),
            "task_id": self.task_id,
            "retry_of": self.retry_of,
            "conditioning": dict(self.conditioning),
        }


@dataclass(frozen=True)
class InstallFallback:
    """Install a fallback in S0 for a running execution; it is cleared when the execution stops running."""

    execution_id: str
    spec: FallbackSpec

    def to_json(self) -> Json:
        return {"type": "install_fallback", "execution_id": self.execution_id, "spec": self.spec.to_json()}


@dataclass(frozen=True)
class Cancel:
    execution_id: str

    def to_json(self) -> Json:
        return {"type": "cancel", "execution_id": self.execution_id}


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


Message = GoalRequest | InstallFallback | Cancel | Decision


@dataclass(frozen=True)
class Update:
    """An execution changed state. `summary` (the executions.jsonl record) comes with the terminal state."""

    execution_id: str
    task_id: str
    state: State
    summary: Json | None

    def to_json(self) -> Json:
        return {
            "type": "execution",
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "state": self.state,
            "summary": self.summary,
        }


# Interfaces. Every implementation reports an `id` and a `version` where traces
# need to name it.


class RuntimeApi(Protocol):
    """The runtime's public API, as applications and S2 see it."""

    def submit(self, message: Message) -> str | None:
        """Queue a message; a GoalRequest returns the id of the execution it creates."""

    def poll(self) -> list[Update]:
        """Updates since the last poll, oldest first."""


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


class S1Host(Protocol):
    """Where S1 runs: it steps the attached policy and delivers its chunks to S0."""

    def attach(self, policy: S1Policy) -> None: ...

    def detach(self) -> None: ...

    def error(self) -> str | None:
        """Why the attached policy stopped working (it raised), or None."""


class S0(Protocol):
    """Real-time execution (S0): the only layer that commands actuators.

    At every tick S0 executes a valid chunk, else a valid installed fallback,
    else its built-in HOLD; it never waits for a slower layer, and it enforces
    the body's position, velocity and workspace limits itself.
    """

    def submit_chunk(self, chunk: ActionChunk, now: Ns) -> None:
        """Non-blocking. Raises ChunkRejected for another action space, an expired chunk or a stale one."""

    def install_fallback(self, spec: FallbackSpec, now: Ns) -> None:
        """Replace the installed fallback. Raises ChunkRejected if it does not fit the body."""

    def hold(self) -> None:
        """Stop executing the active chunk and drop the installed fallback: HOLD
        from the next tick until a new chunk arrives. The runtime calls it when
        an execution stops running."""

    def status(self) -> S0Status: ...


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
    """How long S2 takes to decide: the delay between what S2 sees and what it sends."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def sample(self, rng: np.random.Generator) -> Ns: ...


def finite(values: Sequence[float]) -> bool:
    return all(math.isfinite(v) for v in values)
