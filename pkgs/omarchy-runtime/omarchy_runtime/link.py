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
"""

from __future__ import annotations

import json
import platform
import socket
from collections import deque

from omarchy_runtime.contracts import (
    ContractError,
    Heartbeat,
    Json,
    LinkError,
    Message,
    Ns,
    RuntimeApi,
    Transport,
    Update,
    message_from_json,
    ns,
)
from omarchy_runtime.trace import TraceWriter, dumps
from omarchy_runtime.wallclock import monotonic_ns, sleep_until

PROTOCOL = 1
MAX_FRAME = 1 << 22  # bytes; a longer line is a protocol error
CLOSE_TIMEOUT = ns(5.0)  # to send what is still buffered when closing


def address(text: str) -> tuple[str, int]:
    """HOST:PORT, as given on the command line."""
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise LinkError(f"{text!r} is not HOST:PORT")
    return host.strip("[]"), int(port)


class TcpTransport:
    """Newline-delimited JSON over a connected stream socket, never blocking
    (TCP_NODELAY on TCP: a frame leaves at once). Outgoing bytes the kernel
    cannot take yet wait in a buffer and leave at the next send or receive."""

    def __init__(self, sock: socket.socket) -> None:
        sock.setblocking(False)
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
        Frames after it stay queued for receive(). For the hellos only."""
        deadline = monotonic_ns() + timeout
        while not self._frames:
            if self.closed is not None:
                raise LinkError(f"link closed before {kind}: {self.closed}")
            if monotonic_ns() > deadline:
                raise LinkError(f"no {kind} within {timeout / 1e9:.0f} s")
            sleep_until(monotonic_ns() + ns(0.005))
            self._flush()
            self._read()
        frame = self._frames.popleft()
        if frame.get("type") == "error":
            raise LinkError(f"the peer refused: {frame.get('reason')}")
        if frame.get("type") != kind:
            raise LinkError(f"expected {kind}, received {frame.get('type')!r}")
        return frame

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


def listen(host: str, port: int, timeout: Ns, announce: bool = False) -> TcpTransport:
    """S2's end: accept one connection. With `announce`, print the address
    bound (port 0 picks a free one) once listening."""
    with socket.create_server((host, port)) as server:
        server.settimeout(timeout / 1e9)
        if announce:
            bound = server.getsockname()
            print(f"listening on {bound[0]}:{bound[1]}", flush=True)
        try:
            conn, _ = server.accept()
        except TimeoutError as e:
            raise LinkError(f"no robot connected to {host}:{port} within {timeout / 1e9:.0f} s") from e
    return TcpTransport(conn)


def connect(host: str, port: int, timeout: Ns) -> TcpTransport:
    """The robot's end: connect, trying again until `timeout` (S2 may start later)."""
    deadline = monotonic_ns() + timeout
    while True:
        try:
            return TcpTransport(socket.create_connection((host, port), timeout=5.0))
        except OSError as e:
            if monotonic_ns() > deadline:
                raise LinkError(f"cannot connect to S2 at {host}:{port}: {e}") from e
        sleep_until(monotonic_ns() + ns(0.2))


def hello(role: str, **fields: object) -> Json:
    return {"type": "hello", "protocol": PROTOCOL, "role": role, "host": platform.node()} | fields


def check_hello(frame: Json, role: str) -> Json:
    if frame.get("protocol") != PROTOCOL or frame.get("role") != role:
        raise LinkError(f"expected a {role} speaking protocol {PROTOCOL}, got {frame.get('role')!r} with {frame.get('protocol')!r}")
    return frame


class RobotLink:
    """The robot's end: S2's messages into the runtime's API, its Updates and
    the heartbeat echoes back. Stepped at S0's rate, so an echo leaves at
    most one S0 period after its heartbeat arrived; the runtime handles the
    messages at its own next step. A message the API refuses is answered
    with an error frame and recorded; a closed link is recorded, and the
    robot carries on without S2: S0 never waits."""

    def __init__(self, transport: Transport, api: RuntimeApi, trace: TraceWriter) -> None:
        self.transport = transport
        self.api = api
        self.trace = trace
        self.s2_done = False  # S2 sent its final heartbeat
        self.closed = False

    def step(self, now: Ns) -> None:
        updates = self.api.poll()
        if self.closed:
            return
        try:
            for frame in self.transport.receive():
                self._frame(frame, now)
            for update in updates:
                self.transport.send(update.to_json())
        except LinkError as e:
            self.closed = True
            self.trace.event(now, "s2.link", None, {"state": "closed", "reason": str(e)})

    def _frame(self, frame: Json, now: Ns) -> None:
        try:
            message = message_from_json(frame)
            if isinstance(message, Heartbeat):
                self.transport.send({"type": "echo", "seq": message.seq, "sent": message.sent, "t": now})
                self.s2_done = self.s2_done or message.final
            self.api.submit(message)
        except ContractError as e:
            self.trace.event(now, "error", None, {"category": "link", "reason": f"refused from S2: {e}"})
            self.transport.send({"type": "error", "reason": str(e)})

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.transport.close()


class RemoteRuntime:
    """S2's end: the runtime's API over a transport, so S2 runs the same code
    as in the runtime's process. Every heartbeat period it sends a heartbeat
    with the round trips measured since the last one. A round trip is the
    time from sending a heartbeat to handling its echo, on S2's monotonic
    clock: the network both ways, plus up to one loop period at each end."""

    def __init__(self, transport: Transport, heartbeat: Ns) -> None:
        self.transport = transport
        self.heartbeat = heartbeat
        self.rtt: list[Ns] = []  # every round trip, for S2's own report
        self.errors: list[str] = []  # what the robot refused
        self.closed: str | None = None
        self._updates: list[Update] = []
        self._measured: list[Ns] = []
        self._seq = 0
        self._next: Ns = 0
        self._left = False

    def submit(self, message: Message) -> None:
        """Send at once. On a closed link the message is lost and `closed` says why."""
        try:
            self.transport.send(message.to_json())
        except LinkError as e:
            self.closed = str(e)

    def poll(self) -> list[Update]:
        updates, self._updates = self._updates, []
        return updates

    def step(self, now: Ns) -> None:
        if self.closed is not None:
            return
        try:
            for frame in self.transport.receive():
                kind = frame.get("type")
                if kind == "execution":
                    self._updates.append(Update.from_json(frame))
                elif kind == "echo":
                    sent = frame.get("sent")
                    if not isinstance(sent, int):
                        raise ContractError(f"echo without its heartbeat's time: {frame}")
                    self._measured.append(now - sent)
                elif kind == "error":
                    self.errors.append(str(frame.get("reason")))
                else:
                    raise ContractError(f"unexpected frame {kind!r} from the robot")
            if now >= self._next and not self._left:
                self._beat(now, final=False)
                self._next = now + self.heartbeat
        except (LinkError, ContractError) as e:
            self.closed = str(e)

    def _beat(self, now: Ns, final: bool) -> None:
        self._seq += 1
        self.transport.send(Heartbeat(self._seq, now, tuple(self._measured), final).to_json())
        self.rtt += self._measured
        self._measured = []

    def leave(self, now: Ns) -> None:
        """The final heartbeat: S2 is done and the robot may end the run. No heartbeat follows."""
        self._left = True
        if self.closed is None:
            try:
                self._beat(now, final=True)
            except LinkError as e:
                self.closed = str(e)
