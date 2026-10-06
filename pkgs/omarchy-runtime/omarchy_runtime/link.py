"""The link between the runtime and an S2 in another process, maybe on another machine.

One transport, TcpTransport: newline-delimited JSON over TCP, standard
library only. Only control-plane traffic crosses it: the two hellos; the
API's messages from S2 (requests, fallback installs, cancels, decisions,
heartbeats); execution updates with their summaries, heartbeat echoes and
errors from the robot. S1's chunks never do: they go from S1 to S0 inside
the robot's process.

S2 listens and the robot connects out, so the robot's firewall keeps
admitting SSH only. The robot speaks first: its hello carries the protocol,
the run's identity (run id, scenario, seed, configuration hash, the runtime
and OS builds), the body spec S2 builds fallbacks from, and the heartbeat
period. S2 answers with its own identity, latency model and recovery policy.

A run survives a broken connection. Every message and every update carries
a link number `n`, per direction; heartbeats and echoes carry `ack`, the
highest number applied. After a connection breaks, the robot connects again
with a bounded backoff, and both ends send again, in order, what the other
has not acknowledged; a number already applied is ignored. So a resumed
session neither repeats nor loses a goal. The robot drops a connection on
which S2 has been silent for `reconnect` (application heartbeats, not TCP
timing), and TCP_USER_TIMEOUT bounds how long the kernel keeps unacknowledged
data: TCP's retransmission backoff no longer stretches an outage.
"""

from __future__ import annotations

import errno
import json
import platform
import select
import socket
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omarchy_runtime.contracts import (
    ContractError,
    Heartbeat,
    Json,
    LinkError,
    Message,
    Ns,
    RuntimeApi,
    Update,
    message_from_json,
    ns,
)
from omarchy_runtime.trace import TraceWriter, dumps
from omarchy_runtime.wallclock import monotonic_ns, sleep_until

PROTOCOL = 2
MAX_FRAME = 1 << 22  # bytes; a longer line is a protocol error
CLOSE_TIMEOUT = ns(5.0)  # to send what is still buffered when closing
USER_TIMEOUT_MS = 3000  # TCP_USER_TIMEOUT: unacknowledged data older than this aborts the connection


def address(text: str) -> tuple[str, int]:
    """HOST:PORT, as given on the command line."""
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise LinkError(f"{text!r} is not HOST:PORT")
    return host.strip("[]"), int(port)


def _tune(sock: socket.socket) -> None:
    """Frames leave at once; a dead peer is noticed in seconds, not minutes."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, USER_TIMEOUT_MS)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)


class TcpTransport:
    """Newline-delimited JSON over a connected stream socket, never blocking.
    Outgoing bytes the kernel cannot take yet wait in a buffer and leave at
    the next send or receive."""

    def __init__(self, sock: socket.socket) -> None:
        sock.setblocking(False)
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            _tune(sock)
        self._sock = sock
        self._in = bytearray()
        self._out = bytearray()
        self._frames: deque[Json] = deque()
        self.closed: str | None = None  # why the link is closed

    def _close(self, reason: str) -> None:
        if self.closed is None:
            self.closed = reason
            self._sock.close()

    def _flush(self) -> None:
        while self._out and self.closed is None:
            try:
                sent = self._sock.send(self._out)
            except BlockingIOError:
                return
            except OSError as e:
                self._close(f"send failed: {e}")
                return
            del self._out[:sent]

    def send(self, frame: Json) -> None:
        if self.closed is not None:
            raise LinkError(f"link closed: {self.closed}")
        self._out += (dumps(frame) + "\n").encode()
        self._flush()

    def _read(self) -> None:
        """Move every complete frame that has arrived into the queue."""
        while self.closed is None:
            try:
                data = self._sock.recv(1 << 16)
            except BlockingIOError:
                break
            except OSError as e:
                self._close(f"receive failed: {e}")
                break
            if not data:
                self._close("closed by the peer")
                break
            self._in += data
        *lines, rest = self._in.split(b"\n")
        self._in = bytearray(rest)
        for line in lines:
            try:
                frame = json.loads(line)
            except ValueError:
                frame = None
            if not isinstance(frame, dict):
                self._close(f"protocol error: a line that is not a JSON object: {bytes(line[:80])!r}")
                return
            self._frames.append(frame)
        if len(self._in) > MAX_FRAME:
            self._close(f"protocol error: a frame longer than {MAX_FRAME} bytes")

    def receive(self) -> list[Json]:
        self._flush()
        self._read()
        if not self._frames and self.closed is not None:
            raise LinkError(f"link closed: {self.closed}")
        frames = list(self._frames)
        self._frames.clear()
        return frames

    def wait(self, kind: str, timeout: Ns) -> Json:
        """Block (polling) until the next frame arrives; it must be of `kind`.
        Frames after it stay queued for receive(). Before a run starts only."""
        deadline = monotonic_ns() + timeout
        while not self._frames:
            if self.closed is not None:
                raise LinkError(f"link closed before {kind}: {self.closed}")
            if monotonic_ns() > deadline:
                raise LinkError(f"no {kind} within {timeout / 1e9:.0f} s")
            sleep_until(monotonic_ns() + ns(0.005))
            self._flush()
            self._read()
        return expect(self._frames.popleft(), kind)

    def close(self, timeout: Ns = CLOSE_TIMEOUT) -> None:
        """Send what is buffered (for up to `timeout`), then close."""
        if self.closed is None:
            reason = "closed by this end"
            try:
                self._sock.settimeout(timeout / 1e9)
                self._sock.sendall(self._out)
                self._sock.shutdown(socket.SHUT_WR)
            except OSError as e:
                reason += f", {len(self._out)} bytes undelivered: {e}"
            self._close(reason)


def expect(frame: Json, kind: str) -> Json:
    if frame.get("type") == "error":
        raise LinkError(f"the peer refused: {frame.get('reason')}")
    if frame.get("type") != kind:
        raise LinkError(f"expected {kind}, received {frame.get('type')!r}")
    return frame


class Listener:
    """S2's end: a listening socket kept for the whole run, so the robot can
    connect again after a broken connection."""

    def __init__(self, host: str, port: int, announce: bool = False) -> None:
        self._server = socket.create_server((host, port))
        self.where = f"{host}:{port}"
        if announce:
            bound = self._server.getsockname()
            print(f"listening on {bound[0]}:{bound[1]}", flush=True)

    def accept(self, timeout: Ns) -> TcpTransport:
        """Wait up to `timeout` for a connection (before a run only)."""
        self._server.settimeout(timeout / 1e9)
        try:
            conn, _ = self._server.accept()
        except TimeoutError as e:
            raise LinkError(f"no robot connected to {self.where} within {timeout / 1e9:.0f} s") from e
        return TcpTransport(conn)

    def poll(self) -> TcpTransport | None:
        """A connection waiting to be accepted, or None; never blocks."""
        if not select.select([self._server], [], [], 0)[0]:
            return None
        conn, _ = self._server.accept()
        return TcpTransport(conn)

    def close(self) -> None:
        self._server.close()


def connect(host: str, port: int, timeout: Ns, bind: str | None = None) -> TcpTransport:
    """The robot's first connection: try again until `timeout` (S2 may start
    later). Before the run only; reconnections never block (Dialer)."""
    deadline = monotonic_ns() + timeout
    while True:
        try:
            return TcpTransport(socket.create_connection((host, port), timeout=5.0, source_address=(bind, 0) if bind else None))
        except OSError as e:
            if monotonic_ns() > deadline:
                raise LinkError(f"cannot connect to S2 at {host}:{port}: {e}") from e
        sleep_until(monotonic_ns() + ns(0.2))


class Dialer:
    """One non-blocking connection attempt to a resolved address."""

    def __init__(self, sockaddr: Any, family: int, bind: str | None) -> None:
        """`sockaddr` as socket.getaddrinfo gives it, for `family`."""
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        if bind:
            self.sock.bind((bind, 0))
        self.sock.connect_ex(sockaddr)

    def done(self) -> TcpTransport | str | None:
        """The transport once connected, a reason once failed, None meanwhile."""
        if not select.select([], [self.sock], [], 0)[1]:
            return None
        error = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if error:
            self.sock.close()
            return f"connect failed: {errno.errorcode.get(error, error)}"
        return TcpTransport(self.sock)

    def abandon(self) -> None:
        self.sock.close()


def hello(role: str, **fields: object) -> Json:
    return {"type": "hello", "protocol": PROTOCOL, "role": role, "host": platform.node()} | fields


def check_hello(frame: Json, role: str) -> Json:
    if frame.get("protocol") != PROTOCOL or frame.get("role") != role:
        raise LinkError(f"expected a {role} speaking protocol {PROTOCOL}, got {frame.get('role')!r} with {frame.get('protocol')!r}")
    return frame


class Outbox:
    """Numbered frames kept until the peer acknowledges them."""

    def __init__(self) -> None:
        self.n = 0
        self.pending: deque[Json] = deque()

    def push(self, frame: Json) -> Json:
        self.n += 1
        numbered = frame | {"n": self.n}
        self.pending.append(numbered)
        return numbered

    def ack(self, n: object) -> None:
        if not isinstance(n, int):
            raise ContractError(f"ack {n!r} is not a link number")
        while self.pending and self.pending[0]["n"] <= n:
            self.pending.popleft()


class Inbox:
    """The highest link number applied; refuses gaps and drops repeats."""

    def __init__(self) -> None:
        self.applied = 0

    def accept(self, frame: Json) -> bool:
        """True for the next frame in order, False for a repeat. A gap is a protocol error."""
        n = frame.get("n")
        if not isinstance(n, int):
            raise ContractError(f"a frame without its link number: {frame.get('type')!r}")
        if n <= self.applied:
            return False
        if n != self.applied + 1:
            raise LinkError(f"link number {n} after {self.applied}: frames are missing")
        self.applied = n
        return True


@dataclass(frozen=True)
class Reconnect:
    """When and how the robot connects again: it drops a connection on which
    S2 has been silent for `after`, then tries every `backoff` (doubling, up
    to `backoff_max`), each attempt for at most `attempt`."""

    after: Ns
    backoff: Ns
    backoff_max: Ns
    attempt: Ns


class RobotLink:
    """The robot's end: S2's messages into the runtime's API, its Updates and
    the heartbeat echoes back. Stepped at its own rate (1 kHz), so an echo
    leaves at most one period after its heartbeat arrived; the runtime
    handles the messages at its own next step. A message the API refuses is
    answered with an error frame and recorded. A broken connection is
    recorded and dialled again, without blocking: the robot carries on
    without S2 meanwhile, and S0 never waits."""

    def __init__(
        self,
        transport: TcpTransport,
        where: tuple[str, int],
        robot_hello: Json,
        api: RuntimeApi,
        trace: TraceWriter,
        reconnect: Reconnect,
        *,
        bind: str | None = None,
    ) -> None:
        self.transport: TcpTransport | None = transport
        self.api = api
        self.trace = trace
        self.policy = reconnect
        self.bind = bind
        self.robot_hello = robot_hello
        info = socket.getaddrinfo(where[0], where[1], type=socket.SOCK_STREAM)[0]
        self.family, self.sockaddr = info[0], info[4]
        self.where = f"{where[0]}:{where[1]}"
        self.out = Outbox()
        self.inbox = Inbox()
        self.s2_done = False  # S2 sent its final heartbeat
        self.closed = False  # the run is over
        self.session = 1
        self._heard: Ns | None = None  # when S2 was last heard on this connection
        self._dialer: Dialer | None = None
        self._dialed: Ns = 0
        self._handshake = False  # connected, waiting for S2's hello
        self._next: Ns = 0
        self._backoff = reconnect.backoff
        self._attempts = 0
        self._down: Ns | None = None

    def step(self, now: Ns) -> None:
        for update in self.api.poll():
            frame = self.out.push(update.to_json())
            if self.transport is not None and not self._handshake:
                self._send(frame, now)
        if self.closed:
            return
        if self.transport is None:
            self._dial(now)
            return
        try:
            for frame in self.transport.receive():
                self._heard = now
                if self._handshake:
                    self._resume(frame, now)
                else:
                    self._frame(frame, now)
        except (LinkError, ContractError) as e:
            self._drop(now, str(e))
            return
        if self._heard is None:
            self._heard = now
        if now - self._heard > self.policy.after:
            self._drop(now, f"S2 silent for {(now - self._heard) / 1e9:.1f} s")

    def _send(self, frame: Json, now: Ns) -> None:
        assert self.transport is not None
        try:
            self.transport.send(frame)
        except LinkError as e:
            self._drop(now, str(e))

    def _drop(self, now: Ns, reason: str) -> None:
        """A session ends: dial again at once; a failed attempt waits the backoff, which doubles."""
        if self.transport is not None:
            self.transport.close(timeout=0)
        if self._down is None:
            self._down, self._next = now, now
        else:
            self._next, self._backoff = now + self._backoff, min(2 * self._backoff, self.policy.backoff_max)
        self.transport, self._handshake, self._heard = None, False, None
        self.trace.event(now, "s2.link", None, {"state": "closed", "reason": reason, "unacknowledged": len(self.out.pending)})

    def _dial(self, now: Ns) -> None:
        if self._dialer is None:
            if now < self._next:
                return
            self._attempts += 1
            self._dialer, self._dialed = Dialer(self.sockaddr, self.family, self.bind), now
            return
        result = self._dialer.done()
        if result is None and now - self._dialed < self.policy.attempt:
            return
        if not isinstance(result, TcpTransport):
            if result is None:
                self._dialer.abandon()
            self._dialer = None
            self._next, self._backoff = now + self._backoff, min(2 * self._backoff, self.policy.backoff_max)
            return
        self._dialer = None
        self.session += 1
        self.transport, self._handshake, self._heard = result, True, now
        self._send(self.robot_hello | {"session": self.session, "ack": self.inbox.applied}, now)

    def _resume(self, frame: Json, now: Ns) -> None:
        """S2's hello on a new connection: what it acknowledged is dropped, the rest sent again."""
        check_hello(expect(frame, "hello"), "s2")
        self.out.ack(frame.get("ack"))
        self._handshake = False
        assert self._down is not None
        self.trace.event(
            now,
            "s2.link",
            None,
            {
                "state": "reconnected",
                "session": self.session,
                "attempts": self._attempts,
                "down_ns": now - self._down,
                "resent": len(self.out.pending),
            },
        )
        self._down, self._attempts, self._backoff = None, 0, self.policy.backoff
        for pending in list(self.out.pending):
            self._send(pending, now)

    def _frame(self, frame: Json, now: Ns) -> None:
        assert self.transport is not None
        try:
            message = message_from_json(frame)
            if isinstance(message, Heartbeat):
                self.out.ack(frame.get("ack", 0))
                self.transport.send({"type": "echo", "seq": message.seq, "sent": message.sent, "t": now, "ack": self.inbox.applied})
                self.s2_done = self.s2_done or message.final
            elif not self.inbox.accept(frame):
                return  # sent again after a reconnection, applied already
            self.api.submit(message)
        except ContractError as e:
            self.trace.event(now, "error", None, {"category": "link", "reason": f"refused from S2: {e}"})
            self.transport.send({"type": "error", "reason": str(e)})

    def close(self, now: Ns) -> None:
        """The run is over: tell S2 (it stops at its own timeout otherwise) and close."""
        if self.closed:
            return
        self.closed = True
        if self._dialer is not None:
            self._dialer.abandon()
        if self.transport is None or self._handshake:
            self.trace.event(now, "s2.link", None, {"state": "ended", "reason": "no connection to say bye on"})
            return
        try:
            self.transport.send({"type": "bye"})
        except LinkError as e:
            self.trace.event(now, "s2.link", None, {"state": "ended", "reason": f"bye not sent: {e}"})
        self.transport.close()


class RemoteRuntime:
    """S2's end: the runtime's API over the link, so S2 runs the same code as
    in the runtime's process. Every heartbeat period it sends a heartbeat
    with the round trips measured since the last one. A round trip is the
    time from sending a heartbeat to handling its echo, on S2's monotonic
    clock: the network both ways, plus up to one loop period at each end.
    While the connection is broken, messages wait; a robot connecting again
    with the same run id resumes the session."""

    def __init__(self, transport: TcpTransport, listener: Listener, run_id: str, heartbeat: Ns, reply: Callable[[int], Json]) -> None:
        """`reply(ack)` makes S2's hello for a resumed session."""
        self.transport: TcpTransport | None = transport
        self.listener = listener
        self.run_id = run_id
        self.heartbeat = heartbeat
        self.reply = reply
        self.rtt: list[Ns] = []  # every round trip, for S2's own report
        self.errors: list[str] = []  # what the robot refused
        self.sessions = 1
        self.finished = False  # the robot said bye
        self.out = Outbox()
        self.inbox = Inbox()
        self._updates: list[Update] = []
        self._measured: list[Ns] = []
        self._seq = 0
        self._next: Ns = 0
        self._left = False
        self._final_due = False  # resumed after leaving: the final heartbeat again
        self._candidate: TcpTransport | None = None  # a new connection, before its hello

    def submit(self, message: Message) -> None:
        """Send now, or when the robot is reachable again."""
        frame = self.out.push(message.to_json())
        self._send(frame)

    def poll(self) -> list[Update]:
        updates, self._updates = self._updates, []
        return updates

    def _send(self, frame: Json) -> None:
        """Send if connected; numbered frames stay pending until acknowledged either way."""
        if self.transport is None:
            return
        try:
            self.transport.send(frame)
        except LinkError as e:
            self.errors.append(f"connection lost: {e}")
            self.transport = None

    def step(self, now: Ns) -> None:
        if self.finished:
            return
        self._accept(now)
        if self.transport is None:
            return
        try:
            for frame in self.transport.receive():
                self._frame(frame, now)
        except (LinkError, ContractError) as e:
            self.errors.append(f"connection lost: {e}")
            self.transport = None
            return
        if now >= self._next and (not self._left or self._final_due):
            self._beat(now, final=self._left)
            self._next = now + self.heartbeat

    def _frame(self, frame: Json, now: Ns) -> None:
        kind = frame.get("type")
        if kind == "execution":
            if self.inbox.accept(frame):
                self._updates.append(Update.from_json(frame))
        elif kind == "echo":
            sent = frame.get("sent")
            if not isinstance(sent, int):
                raise ContractError(f"echo without its heartbeat's time: {frame}")
            self._measured.append(now - sent)
            self.out.ack(frame.get("ack", 0))
        elif kind == "error":
            self.errors.append(str(frame.get("reason")))
        elif kind == "bye":
            self.finished = True
        else:
            raise ContractError(f"unexpected frame {kind!r} from the robot")

    def _accept(self, now: Ns) -> None:
        """A robot connecting again: its hello must name this run."""
        if self._candidate is None:
            self._candidate = self.listener.poll()
        if self._candidate is None:
            return
        try:
            frames = self._candidate.receive()
        except LinkError:
            self._candidate = None
            return
        if not frames:
            return
        candidate, self._candidate = self._candidate, None
        robot = check_hello(expect(frames[0], "hello"), "robot")
        if robot.get("run_id") != self.run_id:
            candidate.send({"type": "error", "reason": f"this S2 serves run {self.run_id}, not {robot.get('run_id')}"})
            candidate.close(timeout=0)
            return
        if self.transport is not None:
            self.transport.close(timeout=0)
        self.transport, self.sessions = candidate, self.sessions + 1
        self.out.ack(robot.get("ack", 0))
        candidate.send(self.reply(self.inbox.applied))
        for pending in list(self.out.pending):
            self._send(pending)
        self._final_due = self._left
        for frame in frames[1:]:
            self._frame(frame, now)

    def _beat(self, now: Ns, final: bool) -> None:
        self._seq += 1
        self._send(Heartbeat(self._seq, now, tuple(self._measured), final).to_json() | {"ack": self.inbox.applied})
        self.rtt += self._measured
        self._measured = []
        self._final_due = False

    def leave(self, now: Ns) -> None:
        """The final heartbeat: S2 is done and the robot may end the run. It is
        sent again on a resumed session; no other heartbeat follows."""
        self._left = True
        self._beat(now, final=True)
