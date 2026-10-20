"""Contracts between the components of the Omarchy Robots runtime.

Plain data and small interfaces, nothing else. This module imports no
simulator or backend: a native S0 process, a vendor controller, a learned
policy or a remote S2 can implement these as long as it exchanges the same
data.

Time is an integer number of nanoseconds (`Ns`) on the clock the scheduler
injects into every step; in R1 it is simulated time. Positions are metres in
the vertical x-z plane (x to the right, z up), or in 3D (y as well) for a body
that declares a 3D frame.
"""

from __future__ import annotations

import dataclasses
import enum
import functools
import math
from collections.abc import Hashable, Mapping, Sequence
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
    BOUNDS = enum.auto()  # a contact channel's target outside its own bounds, or bounds the wrong way round
    UNSUPPORTED_INTENT = enum.auto()  # a channel, frame or range the S0 controller does not declare


class ChunkRejected(ContractError):
    """S0 refused an action chunk, an intent chunk or a fallback; `code` says
    why, `channel` which channel (motion, object, contact) when one did."""

    def __init__(self, code: Rejection, detail: str, channel: str | None = None) -> None:
        super().__init__(f"{code}{f' ({channel})' if channel else ''}: {detail}")
        self.code = code
        self.channel = channel
        self.detail = detail

    def to_json(self) -> Json:
        return {"code": self.code, "channel": self.channel, "detail": self.detail}


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
    """One entity seen by a sensor: its estimated position and the detector's
    confidence (0 to 1). `y` is the third axis of a 3D scene; the planar
    scenes leave it at 0."""

    entity: str
    x: float
    z: float
    confidence: float
    y: float = 0.0

    def to_json(self) -> Json:
        return {"entity": self.entity, "x": self.x, "y": self.y, "z": self.z, "confidence": self.confidence}


@dataclass(frozen=True)
class Coverage:
    """What a surface sensor saw of one entity's surface, a grid of `cells`:
    how many it could see (not occluded), and how many of those looked clean.
    A sensor of dirt, noisy and occludable; no images."""

    entity: str
    cells: int
    seen: int
    clean: int

    def to_json(self) -> Json:
        return {"entity": self.entity, "cells": self.cells, "seen": self.seen, "clean": self.clean}


@dataclass(frozen=True)
class Proprioception:
    """What the body reports about itself: its position over `dims` (the action
    space's dimensions, gripper included) and the end-effector's reference
    point, (x, z) on a planar body, (x, y, z) in 3D."""

    dims: tuple[str, ...]
    position: tuple[float, ...]
    ee: tuple[float, ...]

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
    coverage: tuple[Coverage, ...] = ()

    def to_json(self) -> Json:
        return {
            "id": self.id,
            "t": self.t,
            "source": self.source,
            "detections": [d.to_json() for d in self.detections],
            "proprio": self.proprio.to_json() if self.proprio else None,
            "coverage": [c.to_json() for c in self.coverage],
        }


@dataclass(frozen=True)
class BodySpec:
    """A robot body as the runtime sees it, from configuration.

    `dims` names the action space's dimensions; "gripper" is one of them (0 is
    closed, 1 is open) on a body with a gripper. `lower`, `upper` and
    `max_velocity` are per dimension. `workspace` (x0, x1, z0, z1), or (x0,
    x1, y0, y1, z0, z1) in 3D, is where the end-effector's reference point
    may go. `kinematics` holds the kind-specific parameters as plain data.
    """

    id: str
    kind: str
    action_space: str
    dims: tuple[str, ...]
    lower: tuple[float, ...]
    upper: tuple[float, ...]
    max_velocity: tuple[float, ...]
    workspace: tuple[float, ...]
    features: tuple[str, ...]
    home: tuple[float, ...]
    safe_pose: tuple[float, ...]
    kinematics: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def reaches(self, x: float, z: float, tolerance: float = 0.0) -> bool:
        x0, x1, z0, z1 = self.workspace[:2] + self.workspace[-2:]
        return x0 - tolerance <= x <= x1 + tolerance and z0 - tolerance <= z <= z1 + tolerance

    def inside(self, point: Sequence[float] | Array, tolerance: float = 0.0) -> bool:
        """Whether a point, (x, z) or (x, y, z) as the workspace has it, is in the workspace."""
        w = self.workspace
        return all(w[2 * i] - tolerance <= v <= w[2 * i + 1] + tolerance for i, v in enumerate(point))

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
        if len(workspace) not in (4, 6):
            raise ContractError(f"body workspace has {len(workspace)} values, not 4 or 6")
        return BodySpec(
            id=_field(value, "id", str, "body"),
            kind=_field(value, "kind", str, "body"),
            action_space=_field(value, "action_space", str, "body"),
            dims=_strings(_field(value, "dims", list, "body"), "body dims"),
            lower=numbers("lower"),
            upper=numbers("upper"),
            max_velocity=numbers("max_velocity"),
            workspace=workspace,
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


# Intent contract v2: an intent chunk carries optional channels, each validated
# alone and each a timed trajectory with an ActionChunk's semantics. A v1
# ActionChunk is an intent with a motion channel only (IntentChunk.from_v1).


class ContactMode(Name):
    FREE = enum.auto()  # no contact wanted: track the motion
    MAKE = enum.auto()  # establish contact along the normal (a transition)
    MAINTAIN = enum.auto()  # stay in contact, the force at its target, within bounds
    BREAK = enum.auto()  # leave contact along the normal (a transition)


CARTESIAN = ("x", "y", "z", "qw", "qx", "qy", "qz")  # a pose: position (m) and orientation (unit quaternion, w first)
WRENCH = ("fx", "fy", "fz", "tx", "ty", "tz")  # N and Nm


@dataclass(frozen=True)
class Timing:
    """When a channel's rows apply, as for an ActionChunk: row k is the target
    at t_start + k*dt, rows are interpolated linearly (contact modes step),
    the last row holds until valid_until, and nothing is executed after it.
    `observed_at` is when the observation behind the rows was taken,
    `inference_ns` how long computing them took, `source` who computed them."""

    t_start: Ns
    dt: Ns
    valid_until: Ns
    source: str
    observed_at: Ns
    inference_ns: Ns = 0

    def check(self, channel: str) -> None:
        if self.dt <= 0:
            raise ChunkRejected(Rejection.SHAPE, f"dt is {self.dt} ns", channel)

    def index(self, horizon: int, now: Ns) -> tuple[int, float]:
        """The row in force at `now`, and how far towards the next one (0 to 1)."""
        f = min(max((now - self.t_start) / self.dt, 0.0), horizon - 1.0)
        return int(f), f - int(f)

    def shifted(self, by: Ns) -> Timing:
        return Timing(self.t_start + by, self.dt, self.valid_until + by, self.source, self.observed_at + by, self.inference_ns)


def sample(rows: Array, timing: Timing, now: Ns) -> Array:
    """Rows (H x ...) interpolated at `now`, as S0 tracks a chunk."""
    i, frac = timing.index(rows.shape[0], now)
    out: Array = rows[i] if i == rows.shape[0] - 1 else rows[i] + (rows[i + 1] - rows[i]) * frac
    return out


def _rows(rows: Any, tail: tuple[int, ...], channel: str, what: str) -> Array:
    """Rows of shape (H >= 1, *tail), finite and read-only; -1 in `tail` takes any size."""
    a = np.array(rows, dtype=np.float64)
    if a.ndim != 1 + len(tail) or a.shape[0] < 1 or any(t not in (-1, n) for t, n in zip(tail, a.shape[1:], strict=True)):
        raise ChunkRejected(Rejection.SHAPE, f"{what} are {a.shape}, expected (H >= 1, {', '.join(map(str, tail))})", channel)
    if not np.all(np.isfinite(a)):
        raise ChunkRejected(Rejection.NOT_FINITE, f"{what} contain NaN or infinity", channel)
    a.setflags(write=False)
    return a


def _unit(q: Array, channel: str, what: str) -> None:
    if np.any(np.abs(np.linalg.norm(q, axis=-1) - 1.0) > 1e-3):
        raise ChunkRejected(Rejection.SHAPE, f"{what} are not unit vectors", channel)


@dataclass(frozen=True, eq=False)
class MotionIntent:
    """Robot motion: targets over `dims` of `action_space`. With no `frame`,
    the body's own action space (joint targets, as a v1 chunk); with a frame,
    Cartesian end-effector poses in it: dims start with CARTESIAN (others,
    such as a gripper, may follow)."""

    timing: Timing
    action_space: str
    dims: tuple[str, ...]
    rows: Array
    frame: str | None = None

    @staticmethod
    def make(timing: Timing, action_space: str, dims: Sequence[str], rows: Any, frame: str | None = None) -> MotionIntent:
        timing.check("motion")
        a = _rows(rows, (len(dims),), "motion", "motion rows")
        if frame is not None:
            if tuple(dims[: len(CARTESIAN)]) != CARTESIAN:
                raise ChunkRejected(Rejection.SHAPE, f"Cartesian dims are {list(dims)}, expected {list(CARTESIAN)} first", "motion")
            _unit(a[:, 3:7], "motion", "orientations")
        return MotionIntent(timing, action_space, tuple(dims), a, frame)


@dataclass(frozen=True, eq=False)
class ObjectIntent:
    """The intended motion of one object, in `frame`: target poses (H x 7,
    CARTESIAN), or sparse 3D points (P x 3, where they are at
    timing.observed_at) and their future positions (flow, H x P x 3)."""

    timing: Timing
    entity: str
    frame: str
    poses: Array | None = None
    points: Array | None = None
    flow: Array | None = None

    @property
    def form(self) -> str:
        return "pose" if self.poses is not None else "flow"

    @staticmethod
    def make(timing: Timing, entity: str, frame: str, *, poses: Any = None, points: Any = None, flow: Any = None) -> ObjectIntent:
        timing.check("object")
        if (poses is None) == (points is None or flow is None) or (poses is not None and (points, flow) != (None, None)):
            raise ChunkRejected(Rejection.SHAPE, "give poses, or points and their flow", "object")
        if poses is not None:
            p = _rows(poses, (7,), "object", "object poses")
            _unit(p[:, 3:], "object", "object orientations")
            return ObjectIntent(timing, entity, frame, poses=p)
        pts = _rows(points, (3,), "object", "object points")
        f = _rows(flow, (len(pts), 3), "object", "object flow")
        return ObjectIntent(timing, entity, frame, points=pts, flow=f)


@dataclass(frozen=True, eq=False)
class ContactIntent:
    """Contact for one end-effector `ee`: per row a mode, and a target, a
    normal force (H x 1, N, pressing positive) or a wrench (H x 6, WRENCH),
    with `lower` and `upper` bounds (1 or 6 values) that MAKE and MAINTAIN
    rows must respect; the surface normal (unit, out of the surface) in
    `frame`; and the stiffness along each Cartesian axis of `frame` (N/m,
    then Nm/rad), the compliance S0 keeps while it executes the channel."""

    timing: Timing
    ee: str
    frame: str
    normal: tuple[float, float, float]
    modes: tuple[ContactMode, ...]
    target: Array
    lower: tuple[float, ...]
    upper: tuple[float, ...]
    stiffness: tuple[float, ...]

    @property
    def kind(self) -> str:
        return "normal_force" if self.target.shape[1] == 1 else "wrench"

    def mode(self, now: Ns) -> ContactMode:
        return self.modes[self.timing.index(len(self.modes), now)[0]]

    def to_json(self) -> Json:
        t = self.timing
        return {
            "timing": {"t_start": t.t_start, "dt": t.dt, "valid_until": t.valid_until, "source": t.source, "observed_at": t.observed_at},
            "ee": self.ee,
            "frame": self.frame,
            "normal": list(self.normal),
            "modes": list(self.modes),
            "target": self.target.tolist(),
            "lower": list(self.lower),
            "upper": list(self.upper),
            "stiffness": list(self.stiffness),
        }

    @staticmethod
    def from_json(value: Any) -> ContactIntent:
        t = _field(value, "timing", dict, "contact")
        start, dt, until, seen = (_field(t, k, int, "contact timing") for k in ("t_start", "dt", "valid_until", "observed_at"))
        timing = Timing(start, dt, until, _field(t, "source", str, "contact timing"), seen)
        return ContactIntent.make(
            timing,
            _field(value, "ee", str, "contact"),
            _field(value, "frame", str, "contact"),
            _floats(_field(value, "normal", list, "contact"), "contact normal"),
            _strings(_field(value, "modes", list, "contact"), "contact modes"),
            _field(value, "target", list, "contact"),
            _floats(_field(value, "lower", list, "contact"), "contact lower"),
            _floats(_field(value, "upper", list, "contact"), "contact upper"),
            _floats(_field(value, "stiffness", list, "contact"), "contact stiffness"),
        )

    @staticmethod
    def make(
        timing: Timing,
        ee: str,
        frame: str,
        normal: Sequence[float],
        modes: Sequence[ContactMode | str],
        target: Any,
        lower: Sequence[float],
        upper: Sequence[float],
        stiffness: Sequence[float],
    ) -> ContactIntent:
        c = "contact"
        timing.check(c)
        try:
            ms = tuple(ContactMode(m) for m in modes)
        except ValueError as e:
            raise ChunkRejected(Rejection.SHAPE, str(e), c) from e
        t = _rows(target, (-1,), c, "contact targets")
        width = t.shape[1]
        if width not in (1, len(WRENCH)) or len(ms) != t.shape[0]:
            raise ChunkRejected(Rejection.SHAPE, f"{len(ms)} modes and targets {t.shape}: expected H modes and (H, 1 or 6)", c)
        n, lo, hi, k = (
            _rows([v], (want,), c, what)[0]
            for v, want, what in (
                (normal, 3, "normal"),
                (lower, width, "lower bounds"),
                (upper, width, "upper bounds"),
                (stiffness, 6, "stiffness"),
            )
        )
        _unit(n, c, "normal")
        if np.any(lo > hi) or (width == 1 and lo[0] < 0) or np.any(k < 0):
            raise ChunkRejected(Rejection.BOUNDS, f"bounds [{list(lo)}, {list(hi)}], stiffness {list(k)}", c)
        pressing = np.array([m in (ContactMode.MAKE, ContactMode.MAINTAIN) for m in ms])
        if np.any((t[pressing] < lo) | (t[pressing] > hi)):
            raise ChunkRejected(Rejection.BOUNDS, f"a MAKE or MAINTAIN target outside [{list(lo)}, {list(hi)}]", c)
        lows, highs, ks = (tuple(float(v) for v in a) for a in (lo, hi, k))
        return ContactIntent(timing, ee, frame, (float(n[0]), float(n[1]), float(n[2])), ms, t, lows, highs, ks)


CHANNELS = ("motion", "object", "contact")


@dataclass(frozen=True, eq=False)
class IntentChunk:
    """What S1 sends S0 in contract v2: optional channels, at least one. The
    chunk replaces the active one and is stale under the same rules as an
    ActionChunk (seq per source); it is executed only while every channel it
    carries is valid (its valid_until is the earliest). Without a motion
    channel S0 keeps the pose it had when the chunk took over."""

    seq: int
    source: str
    motion: MotionIntent | None = None
    object: ObjectIntent | None = None
    contact: tuple[ContactIntent, ...] = ()

    @staticmethod
    def make(
        seq: int, source: str, motion: MotionIntent | None = None, object: ObjectIntent | None = None, contact: Sequence[ContactIntent] = ()
    ) -> IntentChunk:
        chunk = IntentChunk(seq, source, motion, object, tuple(contact))
        if not chunk.channels:
            raise ChunkRejected(Rejection.SHAPE, "an intent chunk carries at least one channel")
        if len({c.ee for c in chunk.contact}) != len(chunk.contact):
            raise ChunkRejected(Rejection.SHAPE, "two contact channels for one end-effector", "contact")
        return chunk

    @staticmethod
    def from_v1(chunk: ActionChunk) -> IntentChunk:
        """A v1 chunk as a motion-only intent, timing and rows unchanged."""
        timing = Timing(chunk.t_start, chunk.dt, chunk.valid_until, chunk.source, chunk.observed_at, chunk.inference_ns)
        return IntentChunk(chunk.seq, chunk.source, MotionIntent(timing, chunk.action_space, chunk.dims, chunk.actions))

    def inferred(self, latency: Ns) -> IntentChunk:
        """The same chunk with its measured inference latency on every channel (set by S1's host)."""

        def at(c: Any) -> Any:
            return None if c is None else dataclasses.replace(c, timing=dataclasses.replace(c.timing, inference_ns=latency))

        return IntentChunk(self.seq, self.source, at(self.motion), at(self.object), tuple(at(c) for c in self.contact))

    @functools.cached_property
    def channels(self) -> tuple[str, ...]:
        return tuple(c for c, present in zip(CHANNELS, (self.motion, self.object, self.contact), strict=True) if present)

    @functools.cached_property
    def timings(self) -> list[Timing]:
        return [c.timing for c in (self.motion, self.object, *self.contact) if c is not None]

    @functools.cached_property
    def valid_until(self) -> Ns:
        return min(t.valid_until for t in self.timings)

    @functools.cached_property
    def t_start(self) -> Ns:
        return min(t.t_start for t in self.timings)

    @functools.cached_property
    def observed_at(self) -> Ns:
        return min(t.observed_at for t in self.timings)

    @functools.cached_property
    def inference_ns(self) -> Ns:
        return max(t.inference_ns for t in self.timings)

    @property
    def lead(self) -> tuple[Timing, int]:
        """The first channel's timing and horizon (the motion's when there is one), for metadata."""
        c = next(c for c in (self.motion, self.object, *self.contact) if c is not None)
        rows = (
            c.rows
            if isinstance(c, MotionIntent)
            else c.target
            if isinstance(c, ContactIntent)
            else c.poses
            if c.poses is not None
            else c.flow
        )
        assert rows is not None
        return c.timing, int(rows.shape[0])


# What an S0 controller declares it honours. An intent outside it is refused
# (UNSUPPORTED_INTENT), unless its configuration names a degraded mode that
# covers it: then S0 executes the degraded intent and records the degradation.
DEGRADED_MODES: Mapping[str, str] = {
    "drop_object": "the object channel is dropped: this controller does not honour it",
    "drop_contact": "the contact channel is dropped: the motion runs without it",
    "clamp_force": "force targets and bounds above the controller's maximum are clamped to it",
    "clamp_stiffness": "stiffness outside the controller's ranges is clamped into them",
}
COMMANDS = ("position", "velocity", "torque")


@dataclass(frozen=True)
class ContactCapability:
    """The contact a controller honours: end-effectors, frames, modes, the
    largest force (N), the stiffness range of each Cartesian axis, and
    whether it takes 6D wrench targets besides normal forces."""

    ees: tuple[str, ...]
    frames: tuple[str, ...]
    max_force: float
    stiffness: tuple[tuple[float, float], ...]
    wrench: bool = False
    modes: tuple[ContactMode, ...] = tuple(ContactMode)

    def to_json(self) -> Json:
        return {
            "ees": list(self.ees),
            "frames": list(self.frames),
            "max_force": self.max_force,
            "stiffness": [list(r) for r in self.stiffness],
            "wrench": self.wrench,
            "modes": list(self.modes),
        }


@dataclass(frozen=True)
class ControllerCapability:
    """What an S0 controller honours: the command it returns, its rate, the
    row spacing it accepts, the motion action spaces and Cartesian frames
    (which also frame object intents), object intent forms ("pose",
    "flow"), contact, the sensor streams it consumes (`scene`), and the
    degraded modes (DEGRADED_MODES) its configuration allows."""

    id: str
    version: str
    command: str
    rate_hz: int
    dt: tuple[Ns, Ns]
    motion: tuple[str, ...]
    frames: tuple[str, ...] = ()
    object: tuple[str, ...] = ()
    contact: ContactCapability | None = None
    scene: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = set(self.degraded) - set(DEGRADED_MODES)
        if self.command not in COMMANDS or unknown or not 0 < self.dt[0] <= self.dt[1]:
            raise ConfigError(f"controller {self.id}: command {self.command!r}, degraded modes {sorted(unknown)}, dt {self.dt}")

    def to_json(self) -> Json:
        return {
            "id": self.id,
            "version": self.version,
            "command": self.command,
            "rate_hz": self.rate_hz,
            "dt": list(self.dt),
            "motion": list(self.motion),
            "frames": list(self.frames),
            "object": list(self.object),
            "contact": self.contact.to_json() if self.contact else None,
            "scene": list(self.scene),
            "degraded": list(self.degraded),
        }

    def _refuse(self, channel: str, detail: str) -> ChunkRejected:
        return ChunkRejected(Rejection.UNSUPPORTED_INTENT, f"{detail} (controller {self.id}@{self.version})", channel)

    def admit(self, intent: IntentChunk) -> tuple[IntentChunk, tuple[str, ...]]:
        """The intent this controller executes, and the degradations applied
        (each "<mode>: <detail>"); ChunkRejected UNSUPPORTED_INTENT when a
        channel, frame or range is outside the declaration and no allowed
        degraded mode covers it. Nothing is ever dropped silently."""
        degraded: list[str] = []
        named = [("motion", intent.motion), ("object", intent.object), *(("contact", c) for c in intent.contact)]
        for name, channel in named:
            if channel is not None and not self.dt[0] <= channel.timing.dt <= self.dt[1]:
                raise self._refuse(name, f"rows {channel.timing.dt} ns apart, accepted {self.dt[0]} to {self.dt[1]} ns")
        m = intent.motion
        if m is not None and (m.action_space not in self.motion or (m.frame is not None and m.frame not in self.frames)):
            raise self._refuse("motion", f"motion in {m.action_space}" + (f", frame {m.frame}" if m.frame else ""))
        obj = intent.object
        if obj is not None and (obj.form not in self.object or obj.frame not in self.frames):
            if "drop_object" not in self.degraded:
                raise self._refuse("object", f"object intent as {obj.form} in frame {obj.frame}")
            degraded.append(f"drop_object: {obj.form} of {obj.entity} in {obj.frame}")
            obj = None
        contact = list(intent.contact)
        cap = self.contact
        if contact and cap is None:
            if "drop_contact" not in self.degraded:
                raise self._refuse("contact", "contact intent")
            degraded.append(f"drop_contact: {', '.join(c.ee for c in contact)}")
            contact = []
        for n, given in enumerate(contact):
            assert cap is not None
            c = given
            if c.ee not in cap.ees or c.frame not in cap.frames or (c.kind == "wrench" and not cap.wrench) or set(c.modes) - set(cap.modes):
                raise self._refuse("contact", f"{c.kind} for {c.ee} in {c.frame}, modes {sorted(set(c.modes))}")
            force = np.abs(c.target[:, :3]).max() if c.kind == "wrench" else c.target.max()
            limit = max(abs(v) for v in (c.upper[:3] if c.kind == "wrench" else c.upper))
            if max(force, limit) > cap.max_force:
                if "clamp_force" not in self.degraded:
                    raise self._refuse("contact", f"force up to {max(force, limit):.1f} N, at most {cap.max_force:.1f} N")
                degraded.append(f"clamp_force: {c.ee} to {cap.max_force:.1f} N")
                c = _clamp_force(c, cap.max_force)
            k = np.array(c.stiffness)
            lo, hi = np.array([r[0] for r in cap.stiffness]), np.array([r[1] for r in cap.stiffness])
            if np.any((k < lo) | (k > hi)):
                if "clamp_stiffness" not in self.degraded:
                    raise self._refuse("contact", f"stiffness {list(k)} outside {[list(r) for r in cap.stiffness]}")
                degraded.append(f"clamp_stiffness: {c.ee}")
                c = dataclasses.replace(c, stiffness=tuple(float(v) for v in np.clip(k, lo, hi)))
            contact[n] = c
        if not degraded:
            return intent, ()
        if m is None and obj is None and not contact:
            raise self._refuse(intent.channels[0], "nothing left once degraded: " + "; ".join(degraded))
        return IntentChunk(intent.seq, intent.source, m, obj, tuple(contact)), tuple(degraded)


def _clamp_force(c: ContactIntent, most: float) -> ContactIntent:
    if c.kind == "normal_force":
        target, lower, upper = np.minimum(c.target, most), np.minimum(c.lower, most), np.minimum(c.upper, most)
    else:
        f = slice(0, 3)
        target = c.target.copy()
        target[:, f] = np.clip(target[:, f], -most, most)
        lower, upper = np.array(c.lower), np.array(c.upper)
        lower[f], upper[f] = np.clip(lower[f], -most, most), np.clip(upper[f], -most, most)
    target.setflags(write=False)
    return dataclasses.replace(c, target=target, lower=tuple(float(v) for v in lower), upper=tuple(float(v) for v in upper))


@dataclass(frozen=True, eq=False)
class FallbackSpec:
    """A short trajectory S0 runs instead of HOLD when its chunk expires, or
    when the execution fails (the runtime engages it then), expressed in the
    intent language: a motion, and optionally a contact channel ("become
    compliant along z", "leave contact upwards").

    S0 holds two: the base fallback (a capability's default, computed by the
    runtime on the robot side) and S2's (predicted with the goal, or refined
    later), which takes precedence while it is valid. So S0 always has a safe
    behaviour and never waits for a slower layer.

    Row k is the target at te + (k+1)*dt, where te is the tick at which S0
    engages the fallback: the trajectory starts from wherever S0 is at te. A
    dimension marked in `relative` takes its rows as offsets from S0's
    position at te ("hold where you are" is a row of zeros); the others take
    them as targets. With a `frame` the rows are Cartesian poses (CARTESIAN
    first): relative positions add, a relative orientation composes with the
    one at te (the identity quaternion keeps it). `contact`, if any, has its
    timing relative to te. S0 engages a fallback only while valid_from <= now <=
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
    frame: str | None = None
    contact: ContactIntent | None = None

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
        frame: str | None = None,
        contact: ContactIntent | None = None,
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
        if frame is not None:
            MotionIntent.make(Timing(0, dt, 0, source, 0), action_space, dims, actions, frame)  # the Cartesian checks
        return FallbackSpec(
            action_space,
            tuple(dims),
            dt,
            actions,
            valid_from,
            valid_until,
            source,
            duration if lasts is None else lasts,
            rel,
            frame,
            contact,
        )

    def shifted(self, by: Ns) -> FallbackSpec:
        """The same fallback with its window moved by `by` (a window relative to a start, made absolute)."""
        return dataclasses.replace(self, valid_from=self.valid_from + by, valid_until=self.valid_until + by)

    def same_motion(self, other: FallbackSpec) -> bool:
        """The same rows, timing, frames, contact and duration (the window and source aside)."""
        return (
            (self.action_space, self.dims, self.dt, self.lasts, self.relative, self.frame)
            == (other.action_space, other.dims, other.dt, other.lasts, other.relative, other.frame)
            and self.actions.shape == other.actions.shape
            and bool(np.all(self.actions == other.actions))
            and (self.contact.to_json() if self.contact else None) == (other.contact.to_json() if other.contact else None)
        )

    @property
    def duration(self) -> Ns:
        return int(self.actions.shape[0]) * self.dt

    def engage(self, te: Ns, p0: Array) -> IntentChunk:
        """The fallback as the intent S0 executes from tick `te`, where S0's
        position (a pose, with a frame) was `p0`: row 0 is p0 at te, then the
        targets, the last held until te + lasts."""
        rel = np.array(self.relative)
        targets = np.where(rel, p0 + self.actions, self.actions)
        if self.frame is not None and rel[3:7].all():
            targets[:, 3:7] = [quat_mul(q, p0[3:7]) for q in self.actions[:, 3:7]]
        rows = np.vstack([p0, targets])
        rows.setflags(write=False)
        timing = Timing(te, self.dt, te + self.lasts, self.source, te)
        contact = (dataclasses.replace(self.contact, timing=self.contact.timing.shifted(te)),) if self.contact else ()
        return IntentChunk(0, self.source, MotionIntent(timing, self.action_space, self.dims, rows, self.frame), None, contact)

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
            "frame": self.frame,
            "contact": self.contact.to_json() if self.contact else None,
        }

    @staticmethod
    def from_json(value: Any) -> FallbackSpec:
        try:
            contact = value.get("contact")
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
                None if value.get("frame") is None else str(value["frame"]),
                None if contact is None else ContactIntent.from_json(contact),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ContractError(f"not a fallback: {e}") from e


def quat_matrix(q: Array) -> Array:
    """The rotation matrix of a unit quaternion, w first."""
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


def matrix_quat(m: Array) -> Array:
    """The unit quaternion (w first, w >= 0) of a rotation matrix, from its
    largest diagonal term (exact near half turns too)."""
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = 2.0 * math.sqrt(1.0 + trace)
        q = np.array([s / 4, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s])
    else:
        i = int(np.argmax([m[0, 0], m[1, 1], m[2, 2]]))
        j, k = (i + 1) % 3, (i + 2) % 3
        s = 2.0 * math.sqrt(max(0.0, 1.0 + m[i, i] - m[j, j] - m[k, k]))
        q = np.zeros(4)
        q[0] = (m[k, j] - m[j, k]) / s
        q[1 + i], q[1 + j], q[1 + k] = s / 4, (m[j, i] + m[i, j]) / s, (m[k, i] + m[i, k]) / s
    out: Array = (q if q[0] >= 0 else -q) / np.linalg.norm(q)
    return out


def quat_mul(a: Array, b: Array) -> Array:
    """The Hamilton product a b of two quaternions, w first."""
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array(
        [
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ]
    )


@dataclass(frozen=True)
class ChunkMeta:
    """A chunk without its rows: what the runtime may know and log. `dt` and
    `horizon` are its first channel's (the motion's when it has one);
    `channels` the channels it carried, `degraded` the degradations S0
    applied to it (ControllerCapability.admit)."""

    seq: int
    source: str
    t_start: Ns
    dt: Ns
    horizon: int
    valid_until: Ns
    received: Ns
    observed_at: Ns = 0
    inference_ns: Ns = 0
    channels: tuple[str, ...] = ("motion",)
    degraded: tuple[str, ...] = ()

    @staticmethod
    def of(chunk: IntentChunk, received: Ns, degraded: tuple[str, ...] = ()) -> ChunkMeta:
        timing, horizon = chunk.lead
        return ChunkMeta(
            chunk.seq,
            chunk.source,
            chunk.t_start,
            timing.dt,
            horizon,
            chunk.valid_until,
            received,
            chunk.observed_at,
            chunk.inference_ns,
            chunk.channels,
            degraded,
        )

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
            "channels": list(self.channels),
            "degraded": list(self.degraded),
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


@dataclass(frozen=True, eq=False)
class RobotState:
    """What the body reports at one tick, as plain arrays: `position` and
    `velocity` over its dims, measured joint `torque`s, the end-effector's
    pose `ee` (CARTESIAN, in the body's base frame; (x, z) on a planar body)
    and `twist` (6), the external `wrench` on the end-effector (6, WRENCH,
    applied by the environment, base frame: pressing down on a table gives
    a positive fz), and the model terms a torque controller needs: the
    end-effector `jacobian` (6 x n) and `coriolis` torques. The robot (or
    its model library) computes them; a field it does not have is None."""

    t: Ns
    position: Array
    velocity: Array | None = None
    torque: Array | None = None
    ee: Array | None = None
    twist: Array | None = None
    wrench: Array | None = None
    jacobian: Array | None = None
    coriolis: Array | None = None


@dataclass(frozen=True, eq=False)
class Command:
    """A controller's output for tick `t`: one value per joint, of the `kind`
    its capability declares (position, velocity or torque). The guard checks
    it before anything reaches the actuators."""

    t: Ns
    kind: str
    values: Array


class RobotIO(Protocol):
    """The body as S0 reaches it: its state at a tick, the scene inputs (sensor
    streams on the data plane, by name, as the controller's capability
    declares them), and the actuators, which take the guarded command. A
    simulator, a vendor library (libfranka) or a CAN bus behind an adapter."""

    def state(self, now: Ns) -> RobotState: ...

    def scene(self, now: Ns) -> Mapping[str, Array]: ...

    def apply(self, now: Ns, command: Array) -> None: ...


class S0Controller(Protocol):
    """The S0 controller (the guard's other half): at each tick it gets the
    active intent (S1's chunk, or the fallback the guard engaged), the robot
    state and the scene inputs its capability declares, and returns a
    command. It must not block, read ground truth or touch the actuators:
    the guard sits between its output and them, whatever it returns. A
    classical controller today, a learned one later: the contract is the
    same."""

    @property
    def capability(self) -> ControllerCapability: ...

    def command(self, intent: IntentChunk, state: RobotState, scene: Mapping[str, Array], now: Ns) -> Command: ...


@dataclass(frozen=True)
class S0Counters:
    """Cumulative since S0 started. A starvation event is S0 leaving TRACKING
    because its chunk expired with no successor. A late tick is a tick that
    came longer than S0's late threshold after the one before; `max_interval_ns`
    is the longest real interval between two ticks, and `max_step` the largest
    step S0 commanded, as a fraction of its bound (velocity limit times the
    nominal tick period, per dimension): never above 1.

    The guard: `limit_clamps` counts ticks whose command it clamped (a limit,
    the bounded step, the workspace), `guard_replacements` ticks whose command
    it replaced by its own safe one (not finite, stale, a breach no clamp
    fixes); `degraded_chunks` chunks executed in a degraded mode.

    Fidelity, what S0 achieved against the intent it executed from S1 (never
    a fallback), as sums so that an execution's share is a difference:
    `motion_error_sum` over `motion_ticks` (the tracking error in the motion's
    units, rad or m; along a contact normal in MAINTAIN, force rules instead
    of position); `contact_mode_ticks` of `contact_ticks` (ticks of a steady
    mode, FREE or MAINTAIN, in the mode requested: in contact for MAINTAIN,
    out of it for FREE); `force_in_bounds_ticks` of `force_ticks` (MAINTAIN
    ticks); `object_error_sum` over `object_ticks` (m). A channel S0 could
    not measure is counted apart: `contact_unmeasured_ticks` (a contact
    channel and no wrench), `object_unmeasured_ticks` (an object intent and
    no pose of it). The maxima (`max_*`, `force_peak`) run since S0 started."""

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
    guard_replacements: int = 0
    degraded_chunks: int = 0
    motion_ticks: int = 0
    motion_error_sum: float = 0.0
    contact_ticks: int = 0
    contact_mode_ticks: int = 0
    force_ticks: int = 0
    force_in_bounds_ticks: int = 0
    force_peak: float = 0.0
    object_ticks: int = 0
    object_error_sum: float = 0.0
    contact_unmeasured_ticks: int = 0
    object_unmeasured_ticks: int = 0

    RUNNING = frozenset({"max_tracking_error", "max_interval_ns", "max_step", "force_peak"})

    def since(self, start: S0Counters) -> S0Counters:
        """Counts accumulated after `start` (the maxima are running maxima)."""
        return S0Counters(
            **{f.name: getattr(self, f.name) - (0 if f.name in self.RUNNING else getattr(start, f.name)) for f in dataclasses.fields(self)}
        )

    def to_json(self) -> Json:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


@dataclass(frozen=True)
class S0Status:
    """S0 as of its last tick. Plain scalars, so another process can produce it.
    `active_seq` is the chunk being tracked (None unless TRACKING); `last_chunk`
    the last one S0 accepted. `fallback_installed` says a valid fallback is
    installed (S2's or the base); `fallback_source` names the one S0 would run
    now, or the one it runs. `rejection` is the last chunk S0 refused: its
    seq, source, when, and the structured error (ChunkRejected.to_json)."""

    t: Ns
    mode: S0Mode
    active_seq: int | None
    position: tuple[float, ...]
    last_chunk: ChunkMeta | None
    fallback_installed: bool
    counters: S0Counters
    fallback_source: str | None = None
    rejection: Json | None = field(default=None, hash=False)

    def to_json(self) -> Json:
        return {
            "t": self.t,
            "mode": self.mode,
            "active_seq": self.active_seq,
            "position": list(self.position),
            "last_chunk": self.last_chunk.to_json() if self.last_chunk else None,
            "fallback_installed": self.fallback_installed,
            "fallback_source": self.fallback_source,
            "rejection": self.rejection,
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

    def step(self, now: Ns, observation: Observation | None) -> ActionChunk | IntentChunk | None:
        """Compute the next chunk (v1, or an intent chunk) from the latest observation, or nothing."""

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
    generator returns another motion (after a grasp, say). `motion_key` is
    cheap and asked at every supervisor step: it changes whenever
    `fallback` would return another motion, and the runtime asks `fallback`
    only then. Neither may block, read ground truth or call S0; one that
    raises leaves the base fallback as it was and records an error."""

    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def motion_key(self, body: BodySpec, status: S0Status) -> Hashable: ...

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

    def submit_chunk(self, chunk: ActionChunk | IntentChunk, now: Ns) -> None:
        """Non-blocking. A v1 chunk is a motion-only intent. Raises
        ChunkRejected for an intent the controller does not honour
        (UNSUPPORTED_INTENT, unless a degraded mode its configuration allows
        covers it), an expired chunk or a stale one."""

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
