"""A fake AgileX PiPER on a fake CAN bus, for tests: the official SDK
(piper_sdk) talks to it through python-can, and no socket opens.

`fake_socketcan()` puts FakeCanBus in python-can's place for "socketcan"
for the time it lasts: the SDK, which opens its buses as socketcan, then
reaches the FakePiper registered for its channel. The arm decodes the
frames the SDK's protocol (V2) defines, by their CAN ids and big-endian
layouts, and answers as its documentation says, at the rates it implies:
status (0x2A1), joints (0x2A5 to 0x2A7, 0.001 degree) and motors (0x251 to
0x256: speed, current, position) every 5 ms, drivers (0x261 to 0x266, the
enable bit among them) every 25 ms. It moves only enabled (0x471), in CAN
command mode MOVE J (0x151), towards the last joint targets (0x155 to
0x157) at its speed percentage of MAX_SPEED. A target past a joint's limit
is refused with status 0x04 (target exceeds limit); an emergency stop
(0x150) stops it until resumed; `collide()` stops it with status 0x07.
Each frame carries its receive time on the system clock, as socketcan
stamps it; `silent` stops the feedback (a cable pulled), the arm moving on.
What the arm does when commands stop is not documented: this fake holds
the last target (an assumption to check on the lab's arm). Every frame
received is in `received`. Development tools only: it imports python-can.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

import can
import numpy as np

from omarchy_runtime.contracts import Array
from omarchy_runtime.wallclock import monotonic_ns, sleep_until, wall_ns

DEG = np.pi / 180
LIMITS_DEG = ((-150, 150), (0, 180), (-170, 0), (-100, 100), (-70, 70), (-120, 120))  # the SDK's documented joint limits
MAX_SPEED = 3.0  # rad/s at 100 %: an assumption, no source gives it
CYCLE = 5_000_000  # ns: 200 Hz
STATUS_NORMAL, STATUS_ESTOP, STATUS_LIMIT, STATUS_COLLISION = 0x00, 0x01, 0x04, 0x07


def _i32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "big", signed=True)


class FakePiper:
    def __init__(self, q_deg: Sequence[float] = (0, 30, -30, 0, 0, 0)) -> None:
        self.q = np.array(q_deg, dtype=np.float64) * DEG
        self.target = self.q.copy()
        self.dq = np.zeros(6)
        self.enabled = False
        self.ctrl_mode, self.move_mode, self.speed = 0x00, 0x00, 0
        self.status = STATUS_NORMAL
        self.silent = False
        self.received: list[tuple[int, bytes]] = []
        self.lock = threading.Lock()
        self._pending = list(self.q / DEG * 1000)  # joint targets, 0.001 degree, as they arrive in pairs
        self._last = monotonic_ns()
        self._cycle = 0
        self._queue: list[can.Message] = []

    # Commands.

    def receive(self, can_id: int, data: bytes) -> None:
        with self.lock:
            self.received.append((can_id, bytes(data)))
            if can_id == 0x471:  # motors enable or disable: motor 7 is all
                if data[0] == 7:
                    self.enabled = data[1] == 0x02
            elif can_id == 0x151:  # control mode, move mode, speed
                self.ctrl_mode, self.move_mode, self.speed = data[0], data[1], data[2]
            elif can_id == 0x150 and data[0] in (0x01, 0x02):  # emergency stop, resume
                self.status = STATUS_ESTOP if data[0] == 0x01 else STATUS_NORMAL
            elif 0x155 <= can_id <= 0x157:
                k = 2 * (can_id - 0x155)
                self._pending[k], self._pending[k + 1] = _i32(data, 0), _i32(data, 4)
                if can_id == 0x157:
                    self._target(np.array(self._pending, dtype=np.float64) / 1000)

    def _target(self, degrees: Array) -> None:
        if any(not lo <= v <= hi for v, (lo, hi) in zip(degrees, LIMITS_DEG, strict=True)):
            self.status = STATUS_LIMIT  # refused: the arm keeps its last target
            return
        if self.status == STATUS_LIMIT:
            self.status = STATUS_NORMAL
        self.target = degrees * DEG

    def collide(self) -> None:
        with self.lock:
            self.status = STATUS_COLLISION

    # The arm and its feedback.

    def _move(self, dt: float) -> None:
        moving = self.enabled and self.ctrl_mode == 0x01 and self.move_mode == 0x01 and self.status == STATUS_NORMAL
        step = MAX_SPEED * self.speed / 100 * dt if moving else 0.0
        before = self.q.copy()
        self.q += np.clip(self.target - self.q, -step, step)
        self.dq = (self.q - before) / dt if dt > 0 else np.zeros(6)

    def _frames(self) -> list[can.Message]:
        def frame(can_id: int, *parts: tuple[int, int, bool]) -> can.Message:
            data = b"".join(v.to_bytes(n, "big", signed=signed) for v, n, signed in parts)
            return can.Message(timestamp=wall_ns() / 1e9, arbitration_id=can_id, data=data.ljust(8, b"\0"), is_extended_id=False)

        mdeg = [round(v / DEG * 1000) for v in self.q]
        motion = 0x00 if np.allclose(self.q, self.target, atol=1e-4) else 0x01
        out = [
            frame(0x2A1, (self.ctrl_mode, 1, False), (self.status, 1, False), (self.move_mode, 1, False), (0, 1, False), (motion, 1, False))
        ]
        out += [frame(0x2A5 + k, (mdeg[2 * k], 4, True), (mdeg[2 * k + 1], 4, True)) for k in range(3)]
        for i in range(6):
            speed, pos = round(self.dq[i] * 1000), round(self.q[i] * 1000)
            out.append(frame(0x251 + i, (speed, 2, True), (0, 2, True), (pos, 4, True)))
        if self._cycle % 5 == 0:
            status = (0x40 if self.enabled else 0) | (0x10 if self.status == STATUS_COLLISION else 0)
            out += [frame(0x261 + i, (240, 2, False), (30, 2, True), (30, 1, True), (status, 1, False), (0, 2, False)) for i in range(6)]
        return out

    def next_frame(self, timeout: float | None) -> can.Message | None:
        """The next feedback frame, waiting for its cycle (wall clock) up to `timeout` s."""
        deadline = None if timeout is None else monotonic_ns() + round(timeout * 1e9)
        while True:
            with self.lock:
                if self._queue:
                    return self._queue.pop(0)
                due = self._last + CYCLE
                now = monotonic_ns()
                if now >= due:
                    self._move((now - self._last) / 1e9)
                    self._last, self._cycle = now, self._cycle + 1
                    self._queue = [] if self.silent else self._frames()
                    continue
            if deadline is not None and deadline < due:
                sleep_until(deadline)
                return None
            sleep_until(due)


class FakeCanBus(can.BusABC):
    """python-can's bus for a FakePiper: sent frames go to the arm, received ones come from it."""

    arms: ClassVar[dict[str, FakePiper]] = {}

    def __init__(self, channel: Any = None, **kwargs: Any) -> None:
        self.arm = self.arms[str(channel)]
        self.channel_info = f"fake CAN bus {channel}"
        super().__init__(channel=channel)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.arm.receive(msg.arbitration_id, bytes(msg.data))

    def _recv_internal(self, timeout: float | None) -> tuple[can.Message | None, bool]:
        return self.arm.next_frame(timeout), False


@contextlib.contextmanager
def fake_socketcan(arm: FakePiper, channel: str = "can0") -> Iterator[FakePiper]:
    """python-can's "socketcan" is FakeCanBus, reaching `arm` on `channel`, while this lasts."""
    backends: dict[str, tuple[str, str]] = can.interfaces.BACKENDS
    saved = backends["socketcan"]
    backends["socketcan"] = (__name__, "FakeCanBus")
    FakeCanBus.arms[channel] = arm
    try:
        yield arm
    finally:
        backends["socketcan"] = saved
        FakeCanBus.arms.pop(channel, None)
