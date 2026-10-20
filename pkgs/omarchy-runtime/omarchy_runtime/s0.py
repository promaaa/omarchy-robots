"""S0, assembled: the guard and a controller, one tick at a time (contracts.S0).

At every tick S0 reads the robot's state and the scene inputs its controller
declares (the data plane: never runtime events), the guard picks what S0
executes (S1's chunk, a fallback, or HOLD), the controller turns it into a
command, the guard checks that command, and the robot gets the result. A
chunk enters only if the controller's capability admits it, degraded only in
a mode its configuration allows, and recorded. S0 also counts how faithfully
it executed S1's intents (S0Counters), for the runtime's fidelity check.
Not real-time: simulated time, or the wall clock, in Python.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from omarchy_runtime.contracts import (
    CARTESIAN,
    ActionChunk,
    Array,
    BodySpec,
    ChunkMeta,
    ChunkRejected,
    Command,
    ContactMode,
    FallbackSpec,
    IntentChunk,
    Json,
    Ns,
    Rejection,
    RobotIO,
    RobotState,
    S0Controller,
    S0Counters,
    S0History,
    S0Mode,
    S0Status,
    quat_matrix,
    sample,
)
from omarchy_runtime.guard import Guard, Limits
from omarchy_runtime.trace import S0_FALLBACKS, S0_MODES

CODES = {S0Mode(m): n for n, m in enumerate(S0_MODES)}
SLOTS = {name: n for n, name in enumerate(S0_FALLBACKS)}  # the fallback S0 runs, in its per-tick record
CONTACT_N = 1.0  # N: a measured normal force at least this large is contact
SUMS = ("motion_error_sum", "object_error_sum", "force_peak")


class S0Loop:
    def __init__(
        self,
        body: BodySpec,
        controller: S0Controller,
        robot: RobotIO,
        capacity: int,
        nominal: Ns = 1_000_000,
        late_after: Ns | None = None,
        history: Ns = 2_000_000_000,
        forward: Callable[[Sequence[float]], tuple[float, ...]] | None = None,
        limits: Limits | None = None,
    ) -> None:
        """`nominal` is S0's tick period; a tick more than `late_after` (1.5
        periods by default) after the one before is a late tick. The last
        `history` of ticks stay readable (history()). `forward` maps commanded
        positions to the workspace point (position control)."""
        self.nominal = nominal
        self.late_after = late_after if late_after is not None else nominal * 3 // 2
        self.body = body
        self.controller = controller
        self.capability = controller.capability
        self.robot = robot
        self.c = {f: 0 for f in S0Counters.__dataclass_fields__ if f not in S0Counters.RUNNING and f not in SUMS}
        self.f = dict.fromkeys(SUMS, 0.0)
        self.guard = Guard(body, self.capability.command, nominal, self.c, forward, limits)
        self.bound = self.guard.bound
        self._per_bound = np.divide(
            1.0, self.bound, out=np.zeros_like(self.bound), where=self.bound > 0
        )  # a step as a fraction of its bound
        self.position = self.capability.command == "position"
        self.pos = np.array(robot.state(0).position, dtype=np.float64)
        self.mode = S0Mode.HOLD
        self.meta: ChunkMeta | None = None
        self.rejection: Json | None = None
        self.t: Ns = 0
        self._last: Ns | None = None
        self.max_error = 0.0
        self.max_interval: Ns = 0
        self.max_step = 0.0
        self._object0: tuple[IntentChunk, Array] | None = None  # an object's pose when the chunk took over (flow)
        # The per-tick record: preallocated, written to disk once at the end of a run.
        d = len(body.dims)
        self.record_t = np.zeros(capacity, dtype=np.int64)
        self.record_mode = np.zeros(capacity, dtype=np.int8)
        self.record_position = np.zeros((capacity, d))  # the command (position control), else the measured position
        self.record_desired = np.zeros((capacity, d))  # the controller's command before the guard (positions or torques)
        self.record_seq = np.full(capacity, -1, dtype=np.int64)  # the chunk tracked, -1 when not tracking
        self.record_valid_until = np.full(capacity, -1, dtype=np.int64)
        self.record_observed = np.full(capacity, -1, dtype=np.int64)  # the tracked chunk's observation time
        self.record_fallback = np.zeros(capacity, dtype=np.int8)  # SLOTS: the fallback run in FALLBACK
        # The robot-side history: a ring buffer of the last ticks.
        n = max(2, history // nominal)
        self._h_t = np.zeros(n, dtype=np.int64)
        self._h_command = np.zeros((n, d))
        self._h_desired = np.zeros((n, d))
        self._h_count = 0

    @property
    def chunk(self) -> IntentChunk | None:
        return self.guard.chunk

    @property
    def fallback(self) -> FallbackSpec | None:
        return self.guard.fallback

    @property
    def base(self) -> FallbackSpec | None:
        return self.guard.base

    def _refuse(self, code: Rejection, detail: str, channel: str | None = None) -> ChunkRejected:
        self.c["rejected_chunks"] += 1
        return ChunkRejected(code, detail, channel)

    def _fits(self, action_space: str, dims: tuple[str, ...], frame: str | None) -> None:
        """Joint targets in the body's own action space, or poses in a declared Cartesian space and frame."""
        cap, b = self.capability, self.body
        if frame is None and (action_space, dims) != (b.action_space, b.dims):
            raise self._refuse(
                Rejection.ACTION_SPACE, f"{action_space} {list(dims)}; this S0 runs {b.action_space} {list(b.dims)}", "motion"
            )
        if frame is not None and (action_space not in cap.motion or frame not in cap.frames or dims[: len(CARTESIAN)] != CARTESIAN):
            raise self._refuse(
                Rejection.UNSUPPORTED_INTENT, f"{action_space} in frame {frame} (controller {cap.id}@{cap.version})", "motion"
            )

    def submit_chunk(self, chunk: ActionChunk | IntentChunk, now: Ns) -> None:
        intent = chunk if isinstance(chunk, IntentChunk) else IntentChunk.from_v1(chunk)
        try:
            if intent.motion is not None:
                self._fits(intent.motion.action_space, intent.motion.dims, intent.motion.frame)
            try:
                intent, degraded = self.capability.admit(intent)
                self.guard.submit(intent, now)
            except ChunkRejected:
                self.c["rejected_chunks"] += 1
                raise
        except ChunkRejected as e:
            self.rejection = {"seq": intent.seq, "source": intent.source, "t": now} | e.to_json()
            raise
        if degraded:
            self.c["degraded_chunks"] += 1
        self.meta = ChunkMeta.of(intent, now, degraded)

    def install_fallback(self, spec: FallbackSpec, now: Ns, base: bool = False) -> None:
        self._fits(spec.action_space, spec.dims, spec.frame)
        p0 = np.r_[0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, np.zeros(len(spec.dims) - 7)] if spec.frame else np.zeros(len(spec.dims))
        _, degraded = self.capability.admit(spec.engage(now, p0))
        if degraded:
            raise self._refuse(Rejection.UNSUPPORTED_INTENT, "a fallback is never degraded: " + "; ".join(degraded))
        self.guard.install(spec, now, base)

    def hold(self, fallback: bool = False) -> None:
        self.guard.hold(fallback)

    def restart(self) -> None:
        """The actuators start again from rest, as at each new control loop
        of a Franka: the next tick is no late one, and its torque is zero."""
        self._last = None
        self.guard.restart()

    def history(self, since: Ns) -> S0History:
        n, size = self._h_count, len(self._h_t)
        order = np.arange(max(0, n - size), n) % size
        keep = order[self._h_t[order] >= since]
        return S0History(self._h_t[keep].copy(), self._h_command[keep].copy(), self._h_desired[keep].copy())

    def status(self) -> S0Status:
        sums: dict[str, Any] = {**self.c, **self.f}
        counters = S0Counters(max_tracking_error=self.max_error, max_interval_ns=self.max_interval, max_step=self.max_step, **sums)
        chunk = self.guard.chunk
        active = chunk.seq if self.mode is S0Mode.TRACKING and chunk is not None else None
        position = tuple(float(v) for v in self.pos)
        cover = self.guard.covering(self.t)
        return S0Status(
            self.t, self.mode, active, position, self.meta, cover is not None, counters, cover.source if cover else None, self.rejection
        )

    def _scene(self, now: Ns) -> Mapping[str, Array]:
        """The scene inputs the controller declares, and only those, straight from the robot side."""
        if not self.capability.scene:
            return {}
        streams = self.robot.scene(now)
        return {k: streams[k] for k in self.capability.scene if k in streams}

    def _command(self, intent: IntentChunk | None, state: RobotState, scene: Mapping[str, Array], now: Ns) -> Command | None:
        if intent is None:
            return None  # HOLD: the guard's own
        try:
            return self.controller.command(intent, state, scene, now)
        except Exception:  # noqa: BLE001 - a controller that raises gets the guard's safe command, counted as a replacement
            return None

    def tick(self, now: Ns) -> None:
        dt = now - self._last if self._last is not None else 0
        if dt > self.late_after:
            self.c["late_ticks"] += 1
        self.max_interval = max(self.max_interval, dt)
        state = self.robot.state(now)
        scene = self._scene(now)
        mode, intent = self.guard.select(now, state, self.mode)
        command = self._command(intent, state, scene, now)
        values, wanted = self.guard.check(command, state, mode, now, dt)
        if mode is S0Mode.TRACKING:
            self.max_error = max(self.max_error, float(np.abs(values - wanted).max()))
            assert intent is not None
            self._fidelity(intent, state, scene, now)
        if state.wrench is not None:
            self.f["force_peak"] = max(self.f["force_peak"], float(np.linalg.norm(state.wrench[:3])))
        if self.position:
            self.max_step = max(self.max_step, float((np.abs(values - self.pos) * self._per_bound).max()))
        if self.mode is S0Mode.FALLBACK:
            self.c["fallback_ns"] += dt
        elif self.mode is S0Mode.HOLD:
            self.c["hold_ns"] += dt
        if self.guard.covering(now) is not None:
            self.c["fallback_cover_ns"] += dt
        self.pos = values if self.position else np.array(state.position, dtype=np.float64)
        self.mode, self.t, self._last = mode, now, now
        self.robot.apply(now, values)
        n = self.c["ticks"]
        if n < len(self.record_t):
            self.record_t[n] = now
            self.record_mode[n] = CODES[mode]
            self.record_position[n] = self.pos
            self.record_desired[n] = wanted
            chunk = self.guard.chunk
            if mode is S0Mode.TRACKING and chunk is not None:
                self.record_seq[n] = chunk.seq
                self.record_valid_until[n] = chunk.valid_until
                self.record_observed[n] = chunk.observed_at
            engaged = self.guard.engaged
            if mode is S0Mode.FALLBACK and engaged is not None:
                self.record_fallback[n] = SLOTS["s2" if engaged[0] is self.guard.fallback else "base"]
        h = self._h_count % len(self._h_t)
        self._h_t[h] = now
        self._h_command[h], self._h_desired[h] = (values, wanted) if self.position else (self.pos, self.pos)
        self._h_count += 1
        self.c["ticks"] = n + 1

    def _fidelity(self, intent: IntentChunk, state: RobotState, scene: Mapping[str, Array], now: Ns) -> None:
        """What S0 achieved against S1's intent at this tick (S0Counters)."""
        c, f = self.c, self.f
        pressing = [np.array(ci.normal) for ci in intent.contact if ci.mode(now) is not ContactMode.FREE]
        m = intent.motion
        if m is not None:
            target = sample(m.rows, m.timing, now)
            if m.frame is None:
                error = float(np.abs(state.position - target).max())
            else:
                assert state.ee is not None
                e = target[:3] - state.ee[:3]
                for n in pressing:  # along a contact normal, force rules: that position is no target
                    e = e - float(e @ n) * n
                error = float(np.linalg.norm(e))
            c["motion_ticks"] += 1
            f["motion_error_sum"] += error
        if intent.contact and state.wrench is None:
            c["contact_unmeasured_ticks"] += 1  # no evidence S0 honoured the contact channel
        if state.wrench is not None:
            for ci in intent.contact:
                mode = ci.mode(now)
                fn = float(state.wrench[:3] @ np.array(ci.normal))
                if mode in (ContactMode.FREE, ContactMode.MAINTAIN):
                    c["contact_ticks"] += 1
                    c["contact_mode_ticks"] += (mode is ContactMode.MAINTAIN) == (fn >= CONTACT_N)
                if mode is ContactMode.MAINTAIN:
                    value = np.array([fn]) if ci.kind == "normal_force" else state.wrench
                    c["force_ticks"] += 1
                    c["force_in_bounds_ticks"] += bool(np.all((value >= ci.lower) & (value <= ci.upper)))
        o = intent.object
        pose = scene.get(f"pose:{o.entity}") if o is not None else None
        if o is not None and pose is None:
            c["object_unmeasured_ticks"] += 1
        if o is not None and pose is not None:
            if o.poses is not None:
                error = float(np.linalg.norm(sample(o.poses, o.timing, now)[:3] - pose[:3]))
            else:
                assert o.points is not None and o.flow is not None
                if self._object0 is None or self._object0[0] is not intent:
                    self._object0 = (intent, np.array(pose))
                p0 = self._object0[1]
                moved = quat_matrix(pose[3:7]) @ quat_matrix(p0[3:7]).T
                seen = (o.points - p0[:3]) @ moved.T + pose[:3]
                error = float(np.mean(np.linalg.norm(sample(o.flow, o.timing, now) - seen, axis=1)))
            c["object_ticks"] += 1
            f["object_error_sum"] += error
