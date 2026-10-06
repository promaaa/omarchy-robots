"""The simulated S0: a 1 kHz (simulated) tick that never waits.

At every tick it executes the active chunk while now <= valid_until, else an
installed fallback inside its validity window, else HOLD (keep the position,
gripper included). It then enforces the body's limits itself: position
limits, then velocity limits, then the gripper's workspace. A step is never
larger than the velocity limit times the nominal tick period: after a late
tick S0 continues from where it is, at that bound, without catching up. Not
real-time: simulated time, or the wall clock, in Python.
"""

from __future__ import annotations

import numpy as np

from omarchy_runtime.contracts import (
    ActionChunk,
    BodySpec,
    ChunkMeta,
    ChunkRejected,
    FallbackSpec,
    Ns,
    Rejection,
    S0Counters,
    S0Mode,
    S0Status,
    seconds,
)
from omarchy_runtime.sim.kinematics import Kinematics
from omarchy_runtime.sim.world import World
from omarchy_runtime.trace import S0_MODES

CODES = {S0Mode(m): n for n, m in enumerate(S0_MODES)}


class SimS0:
    def __init__(self, body: BodySpec, world: World, capacity: int, nominal: Ns = 1_000_000, late_after: Ns | None = None) -> None:
        """`nominal` is S0's tick period; a tick more than `late_after` (1.5
        periods by default) after the one before is a late tick."""
        self.nominal = nominal
        self.late_after = late_after if late_after is not None else nominal * 3 // 2
        self.bound = np.array(body.max_velocity) * seconds(nominal)  # the largest step per tick
        self.body = body
        self.world = world
        self.kin = Kinematics(body)
        self.joints = [i for i, d in enumerate(body.dims) if d != "gripper"]
        self.lower = np.array(body.lower)
        self.upper = np.array(body.upper)
        self.vmax = np.array(body.max_velocity)
        self.pos = np.array(world.q, dtype=np.float64)
        self.mode = S0Mode.HOLD
        self.chunk: ActionChunk | None = None
        self.meta: ChunkMeta | None = None
        self.fallback: FallbackSpec | None = None
        self.engaged: tuple[Ns, np.ndarray] | None = None  # when the fallback started, and from where
        self.t: Ns = 0
        self._last: Ns | None = None
        self.c = {
            "ticks": 0,
            "starvation_events": 0,
            "limit_clamps": 0,
            "rejected_chunks": 0,
            "fallback_ns": 0,
            "hold_ns": 0,
            "late_ticks": 0,
        }
        self.max_error = 0.0
        self.max_interval: Ns = 0
        self.max_step = 0.0
        # The per-tick record: preallocated, written to disk once at the end of a run.
        d = len(body.dims)
        self.record_t = np.zeros(capacity, dtype=np.int64)
        self.record_mode = np.zeros(capacity, dtype=np.int8)
        self.record_position = np.zeros((capacity, d))
        self.record_desired = np.zeros((capacity, d))
        self.record_seq = np.full(capacity, -1, dtype=np.int64)  # the chunk tracked, -1 when not tracking
        self.record_valid_until = np.full(capacity, -1, dtype=np.int64)

    def _check(self, action_space: str, dims: tuple[str, ...]) -> None:
        if action_space != self.body.action_space or dims != self.body.dims:
            self.c["rejected_chunks"] += 1
            raise ChunkRejected(
                Rejection.ACTION_SPACE, f"{action_space} {list(dims)}; this S0 runs {self.body.action_space} {list(self.body.dims)}"
            )

    def submit_chunk(self, chunk: ActionChunk, now: Ns) -> None:
        self._check(chunk.action_space, chunk.dims)
        if chunk.valid_until < now:
            self.c["rejected_chunks"] += 1
            raise ChunkRejected(Rejection.EXPIRED, f"chunk {chunk.seq} valid until {chunk.valid_until} ns, now {now} ns")
        active = self.chunk
        if active is not None and active.valid_until >= now and chunk.source == active.source and chunk.seq <= active.seq:
            self.c["rejected_chunks"] += 1
            raise ChunkRejected(Rejection.STALE, f"chunk {chunk.seq} while chunk {active.seq} of {active.source} is active")
        self.chunk = chunk
        self.meta = ChunkMeta(chunk.seq, chunk.source, chunk.t_start, chunk.dt, chunk.horizon, chunk.valid_until, now)

    def install_fallback(self, spec: FallbackSpec, now: Ns) -> None:
        self._check(spec.action_space, spec.dims)
        if spec.valid_until < now:
            raise ChunkRejected(Rejection.EXPIRED, f"fallback valid until {spec.valid_until} ns, now {now} ns")
        self.fallback, self.engaged = spec, None

    def hold(self) -> None:
        self.chunk, self.fallback, self.engaged = None, None, None

    def status(self) -> S0Status:
        counters = S0Counters(max_tracking_error=self.max_error, max_interval_ns=self.max_interval, max_step=self.max_step, **self.c)
        active = self.chunk.seq if self.mode is S0Mode.TRACKING and self.chunk is not None else None
        position = tuple(float(v) for v in self.pos)
        return S0Status(self.t, self.mode, active, position, self.meta, self.fallback is not None, counters)

    def _desired(self, now: Ns) -> tuple[S0Mode, np.ndarray]:
        c = self.chunk
        if c is not None and now <= c.valid_until:
            self.fallback = self.fallback if self.engaged is None else None  # a used fallback is spent
            self.engaged = None
            f = min(max((now - c.t_start) / c.dt, 0.0), c.horizon - 1.0)
            i = int(f)
            a = c.actions
            return S0Mode.TRACKING, a[i] if i == c.horizon - 1 else a[i] + (a[i + 1] - a[i]) * (f - i)
        if c is not None:
            self.chunk = None
            if self.mode is S0Mode.TRACKING:
                self.c["starvation_events"] += 1
        fb = self.fallback
        if fb is not None and self.engaged is None and fb.valid_from <= now <= fb.valid_until:
            self.engaged = (now, self.pos.copy())
        if fb is not None and self.engaged is not None:
            te, p0 = self.engaged
            f, rows = (now - te) / fb.dt, fb.actions.shape[0]
            if f <= rows:
                i = min(int(f), rows - 1)
                start = p0 if i == 0 else fb.actions[i - 1]
                return S0Mode.FALLBACK, start + (fb.actions[i] - start) * (f - i)
            self.fallback, self.engaged = None, None
        return S0Mode.HOLD, self.pos

    def tick(self, now: Ns) -> None:
        dt = now - self._last if self._last is not None else 0
        if dt > self.late_after:
            self.c["late_ticks"] += 1
        self.max_interval = max(self.max_interval, dt)
        mode, desired = self._desired(now)
        # Bounded: never more than the nominal period's worth, so a late tick never catches up.
        step = self.vmax * seconds(min(dt, self.nominal))  # nothing moves in no time: the first tick holds
        command = np.clip(np.clip(desired, self.lower, self.upper), self.pos - step, self.pos + step)
        x, z = self.kin.ee([float(command[i]) for i in self.joints])
        if not self.body.reaches(x, z):
            command[self.joints] = self.pos[self.joints]  # do not move into or further out of bounds
        if mode is not S0Mode.HOLD and np.any(np.abs(command - desired) > 1e-9):
            self.c["limit_clamps"] += 1
        if mode is S0Mode.TRACKING:
            self.max_error = max(self.max_error, float(np.max(np.abs(command - desired))))
        moving = self.bound > 0
        self.max_step = max(self.max_step, float(np.max(np.abs(command - self.pos)[moving] / self.bound[moving], initial=0.0)))
        if self.mode is S0Mode.FALLBACK:
            self.c["fallback_ns"] += dt
        elif self.mode is S0Mode.HOLD:
            self.c["hold_ns"] += dt
        self.pos = command
        self.mode, self.t, self._last = mode, now, now
        self.world.apply(now, command.tolist())
        n = self.c["ticks"]
        if n < len(self.record_t):
            self.record_t[n] = now
            self.record_mode[n] = CODES[mode]
            self.record_position[n] = command
            self.record_desired[n] = desired
            if mode is S0Mode.TRACKING and self.chunk is not None:
                self.record_seq[n] = self.chunk.seq
                self.record_valid_until[n] = self.chunk.valid_until
        self.c["ticks"] = n + 1
