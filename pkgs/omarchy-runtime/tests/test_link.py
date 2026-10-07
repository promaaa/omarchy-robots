"""The link to an S2 in another process: the transport's framing and failures,
and one scenario with S2 in its own process over TCP on the loopback
interface, on the wall clock."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from omarchy_runtime import placement
from omarchy_runtime.contracts import ConfigError, ContractError, GoalRequest, LinkError, State, Update, ns
from omarchy_runtime.evaluate import scenario_result
from omarchy_runtime.link import Inbox, Listener, Outbox, Reconnect, RemoteRuntime, RobotLink, TcpTransport, address, through_tunnel
from omarchy_runtime.link import tunnel as link_tunnel
from omarchy_runtime.scenario import run
from omarchy_runtime.trace import TraceWriter, validate_run
from omarchy_runtime.wallclock import monotonic_ns, sleep_until
from tests.helpers import IN_RACK, scenario, tmpdir
from tests.test_runtime import Rig

HERE = Path(__file__).parent.parent


def frames(t: TcpTransport, n: int) -> list[dict[str, object]]:
    """Receive until n frames arrived (or two seconds passed)."""
    got: list[dict[str, object]] = []
    deadline = monotonic_ns() + ns(2.0)
    while len(got) < n and monotonic_ns() < deadline:
        got += t.receive()
        sleep_until(monotonic_ns() + ns(0.001))
    return got


class Proxy:
    """A TCP relay between the robot and S2 that can cut the link: cut()
    breaks every connection and refuses new ones (accepts, then closes) for
    a while, as a link that goes down and comes back."""

    def __init__(self, target: int) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.target = target
        self.down_until = 0
        self.open: list[socket.socket] = []
        self.held: list[socket.socket] = []
        self.blackhole = False
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                robot, _ = self.server.accept()
            except OSError:
                return
            if monotonic_ns() < self.down_until:
                if self.blackhole:
                    self.held.append(robot)  # accepted, never answered
                else:
                    robot.close()
                continue
            s2 = socket.create_connection(("127.0.0.1", self.target))
            with self.lock:
                self.open += [robot, s2]
            for a, b in ((robot, s2), (s2, robot)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, a: socket.socket, b: socket.socket) -> None:
        try:
            while data := a.recv(65536):
                if monotonic_ns() < self.down_until and self.blackhole:
                    continue  # dropped: the connection stays open and silent
                b.sendall(data)
        except OSError:
            pass  # the other pump, or cut(), closed the pair
        for sock in (a, b):
            sock.close()

    def cut(self, seconds: float, blackhole: bool = False) -> None:
        """Down for `seconds`: refuse (close every connection) or, as a
        blackhole, keep connections open and drop everything on them."""
        self.blackhole = blackhole
        self.down_until = monotonic_ns() + ns(seconds)
        if blackhole:
            return
        with self.lock:
            for sock in self.open:
                try:
                    sock.shutdown(socket.SHUT_RDWR)  # the peers see the connection end
                except OSError:
                    pass  # already closed by its pump
                sock.close()
            self.open = []

    def close(self) -> None:
        self.server.close()


class TestTransport(unittest.TestCase):
    def pair(self) -> tuple[socket.socket, TcpTransport]:
        raw, other = socket.socketpair()
        t = TcpTransport(other)
        self.addCleanup(raw.close)
        self.addCleanup(t.close)
        return raw, t

    def test_frames_whatever_the_chunks(self) -> None:
        raw, t = self.pair()
        raw.sendall(b'{"type": "a", "n": 1}\n{"type": "a",')
        self.assertEqual(frames(t, 1), [{"type": "a", "n": 1}])
        self.assertEqual(t.receive(), [])
        raw.sendall(b' "n": 2}\n{"type": "b"}\n')
        self.assertEqual(frames(t, 2), [{"type": "a", "n": 2}, {"type": "b"}])
        t.send({"type": "c", "x": [1.5]})
        self.assertEqual(raw.recv(100), b'{"type":"c","x":[1.5]}\n')

    def test_frames_before_a_close_arrive_then_the_link_is_closed(self) -> None:
        raw, t = self.pair()
        raw.sendall(b'{"type": "last"}\n')
        raw.close()
        self.assertEqual(frames(t, 1), [{"type": "last"}])
        with self.assertRaisesRegex(LinkError, "closed by the peer"):
            t.receive()
        with self.assertRaises(LinkError):
            t.send({"type": "late"})

    def test_a_line_that_is_not_a_json_object_closes_the_link(self) -> None:
        for line in (b"not json\n", b"[1, 2]\n"):
            raw, t = self.pair()
            raw.sendall(line)
            with self.assertRaisesRegex(LinkError, "protocol error"):
                frames(t, 1)

    def test_wait_keeps_the_frames_after_the_one_awaited(self) -> None:
        raw, t = self.pair()
        raw.sendall(b'{"type": "hello"}\n{"type": "heartbeat"}\n')
        self.assertEqual(t.wait("hello", ns(2.0)), {"type": "hello"})
        self.assertEqual(t.receive(), [{"type": "heartbeat"}])
        raw.sendall(b'{"type": "error", "reason": "no"}\n')
        with self.assertRaisesRegex(LinkError, "refused: no"):
            t.wait("hello", ns(2.0))
        with self.assertRaisesRegex(LinkError, "no hello within"):
            t.wait("hello", ns(0.05))

    def test_with_the_s2_link_enabled_only_the_tunnel(self) -> None:
        path = tmpdir() / "s2-link.json"
        self.assertIsNone(link_tunnel(path))
        path.write_text(json.dumps({"role": "robot", "interface": "wg-s2", "address": "10.77.0.1", "peer": "10.77.0.2", "port": 7447}))
        t = link_tunnel(path)
        self.assertEqual(through_tunnel(t, "10.77.0.2", listening=False), "10.77.0.1")  # connect from the tunnel address
        self.assertEqual(through_tunnel(t, "10.77.0.1", listening=True), "10.77.0.1")
        for host, listening in (("edge", False), ("192.168.1.1", False), ("0.0.0.0", True)):
            with self.subTest(host), self.assertRaisesRegex(ConfigError, "the S2 link is enabled"):
                through_tunnel(t, host, listening=listening)
        self.assertIsNone(through_tunnel(None, "edge", listening=False))
        path.write_text("[]")
        with self.assertRaises(ConfigError):
            link_tunnel(path)

    def test_addresses(self) -> None:
        self.assertEqual(address("edge:7447"), ("edge", 7447))
        self.assertEqual(address("[::1]:7447"), ("::1", 7447))
        with self.assertRaises(LinkError):
            address("edge")


class TestNumbering(unittest.TestCase):
    """Link numbers and acknowledgements: what a resumed session sends again."""

    def test_outbox_keeps_what_is_not_acknowledged(self) -> None:
        out = Outbox()
        self.assertEqual([out.push({"type": "x"})["n"] for _ in range(3)], [1, 2, 3])
        out.ack(2)
        self.assertEqual([f["n"] for f in out.pending], [3])
        out.ack(1)  # an older ack changes nothing
        self.assertEqual([f["n"] for f in out.pending], [3])
        with self.assertRaises(ContractError):
            out.ack("3")

    def test_inbox_applies_in_order_and_drops_repeats(self) -> None:
        inbox = Inbox()
        self.assertEqual([inbox.accept({"n": n}) for n in (1, 2, 2, 1, 3)], [True, True, False, False, True])
        with self.assertRaises(LinkError):
            inbox.accept({"n": 5})  # 4 is missing
        with self.assertRaises(ContractError):
            inbox.accept({"type": "request"})


class TestRobotLink(unittest.TestCase):
    def test_connects_again_to_the_address_the_first_connection_reached(self) -> None:
        # Found by vm-s2-placement: "edge" resolved to IPv6 first, S2 listened on IPv4
        # only, and every reconnection to the first resolved address was refused.
        listener = Listener("127.0.0.1", 0)
        self.addCleanup(listener.close)
        port = listener._server.getsockname()[1]
        first = TcpTransport(socket.create_connection(("127.0.0.1", port)))
        self.addCleanup(first.close)
        trace = TraceWriter(tmpdir() / "run", {})
        self.addCleanup(trace.close, 0, {"reason": "test"})
        robot = RobotLink(first, ("::1", port), {}, Rig().runtime, trace, Reconnect(ns(3), ns(0.25), ns(2), ns(1)))
        self.assertEqual((robot.family, robot.sockaddr), (socket.AF_INET, ("127.0.0.1", port)))


class TestRemoteRuntime(unittest.TestCase):
    def test_heartbeats_round_trips_acks_and_leaving(self) -> None:
        raw, other = socket.socketpair()
        listener = Listener("127.0.0.1", 0)
        self.addCleanup(listener.close)
        api = RemoteRuntime(TcpTransport(other), listener, "run", ns(0.1), lambda ack: {"type": "hello", "ack": ack})
        self.addCleanup(raw.close)
        api.step(0)
        api.step(ns(0.05))
        robot = TcpTransport(raw)
        first = frames(robot, 1)
        self.assertEqual([(f["type"], f["seq"], f["rtt"], f["final"], f["ack"]) for f in first], [("heartbeat", 1, [], False, 0)])
        api.submit(GoalRequest(IN_RACK, "t1", "r1"))
        self.assertEqual(frames(robot, 1)[0]["n"], 1)
        robot.send({"type": "echo", "seq": 1, "sent": 0, "t": 5, "ack": 1})
        robot.send(Update("r1", "x1", "t1", State.PENDING, 5, None).to_json() | {"n": 1})
        robot.send(Update("r1", "x1", "t1", State.PENDING, 5, None).to_json() | {"n": 1})  # sent again: dropped
        sleep_until(monotonic_ns() + ns(0.01))
        api.step(ns(0.1))  # handles the echo, then the next heartbeat carries its round trip and the ack
        self.assertEqual([u.request_id for u in api.poll()], ["r1"])
        self.assertEqual(list(api.out.pending), [])  # the robot acknowledged the request
        beat = frames(robot, 1)[0]
        self.assertEqual((beat["rtt"], beat["ack"]), ([ns(0.1)], 1))
        api.leave(ns(0.15))
        self.assertTrue(frames(robot, 1)[0]["final"])
        api.step(ns(0.3))
        sleep_until(monotonic_ns() + ns(0.01))
        self.assertEqual(robot.receive(), [])  # nothing after the final heartbeat
        robot.send({"type": "bye"})
        sleep_until(monotonic_ns() + ns(0.01))
        api.step(ns(0.4))
        self.assertTrue(api.finished)


class TestReconnection(unittest.TestCase):
    """The link breaks 6.5 s into nominal_a and refuses connections for 3 s,
    so the execution's final update is sent during the outage: the robot
    connects again and sends it again, nothing is executed twice or lost,
    the supervisor records the outage, the run succeeds."""

    def test_the_robot_connects_again_and_the_session_resumes(self) -> None:
        env = os.environ | {"PYTHONPATH": str(HERE)}
        command = [sys.executable, "-m", "omarchy_runtime", "s2", "--listen", "127.0.0.1:0", "--ttft", "0.1", "--timeout", "60"]
        s2 = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(s2.kill)
        assert s2.stdout is not None
        proxy = Proxy(int(s2.stdout.readline().rsplit(":", 1)[1]))
        self.addCleanup(proxy.close)
        cut = threading.Timer(6.5, proxy.cut, args=(3.0,))
        cut.start()
        self.addCleanup(cut.cancel)
        r = run("nominal_a", tmpdir() / "reconnect", clock="wall", s2=("127.0.0.1", proxy.port), s2_timeout=ns(30.0))
        out, err = s2.communicate(timeout=60)
        self.assertEqual(s2.returncode, 0, err)
        report = json.loads(out)

        result = scenario_result(r, validate_run(r.dir))
        self.assertEqual(result["problems"], [])
        self.assertEqual([s["outcome"] for s in r.summaries], ["SUCCEEDED"])  # one execution: nothing repeated
        events = [json.loads(line) for line in (r.dir / "events.jsonl").read_text().splitlines()]
        states = [e["payload"]["state"] for e in events if e["type"] == "s2.link"]
        self.assertIn("closed", states)
        self.assertIn("reconnected", states)
        self.assertLess(states.index("closed"), states.index("reconnected"))
        self.assertEqual(states[-1], "left")
        reconnected = next(e["payload"] for e in events if e["type"] == "s2.link" and e["payload"]["state"] == "reconnected")
        self.assertGreaterEqual(reconnected["down_ns"], ns(2.5))  # refused for 3 s
        self.assertLess(reconnected["down_ns"], ns(6.0))  # backoff bounded at 2 s
        self.assertGreaterEqual(reconnected["resent"], 1)  # the SUCCEEDED update, sent again
        link = r.s2_link
        assert link is not None
        self.assertEqual([o["class"] for o in link["outages"]], ["long"])
        self.assertEqual((report["done"], report["bye"], report["sessions"]), (True, True, 2))


class TestRemoteS2(unittest.TestCase):
    """nominal_a with S2 in another process over TCP (loopback), on the wall
    clock. Wall-clock runs are not bit-identical: the outcome, the claim, the
    verdict, the ground truth and the safety invariants must be those of the
    simulated run exactly; the task time within placement.TOLERANCE of it."""

    def s2(self) -> tuple[subprocess.Popen[str], int]:
        env = os.environ | {"PYTHONPATH": str(HERE)}
        command = [sys.executable, "-m", "omarchy_runtime", "s2", "--listen", "127.0.0.1:0", "--ttft", "0.1", "--timeout", "60"]
        s2 = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(s2.kill)
        assert s2.stdout is not None
        return s2, int(s2.stdout.readline().rsplit(":", 1)[1])

    def test_s2_in_another_process(self) -> None:
        s2, port = self.s2()
        r = run("nominal_a", tmpdir() / "remote", clock="wall", s2=("127.0.0.1", port), s2_timeout=ns(30.0), tags={"profile": "local"})
        out, err = s2.communicate(timeout=60)
        self.assertEqual(s2.returncode, 0, err)
        report = json.loads(out)

        result = scenario_result(r, validate_run(r.dir))
        self.assertEqual(result["problems"], [])
        self.assertTrue(result["trace_complete"])
        simulated = scenario("nominal_a")
        self.assertEqual(
            [(s["outcome"], s["claim"]["claim"], s["verdict"]["kind"]) for s in r.summaries],
            [(s["outcome"], s["claim"]["claim"], s["verdict"]["kind"]) for s in simulated.summaries],
        )
        self.assertEqual(r.truths, simulated.truths)
        self.assertEqual(r.world.violations, [])

        header = json.loads((r.dir / "run.json").read_text())
        self.assertEqual(header["clock"], "wall")
        self.assertEqual(header["s2"]["placement"], "remote")
        self.assertEqual(header["s2"]["hello"]["role"], "s2")
        self.assertEqual(header["extensions"]["s2_latency"], {"id": "fixed", "version": "1", "seconds": 0.1, "spread": 0.0})
        self.assertEqual(header["tags"], {"profile": "local"})

        link = r.s2_link
        assert link is not None
        self.assertGreater(link["heartbeats"], 50)
        self.assertEqual(link["outages"], [])
        self.assertGreater(link["rtt_ns"]["n"], 50)
        self.assertLess(link["max_gap_ns"], ns(0.5))
        events = [json.loads(line) for line in (r.dir / "events.jsonl").read_text().splitlines()]
        states = [e["payload"]["state"] for e in events if e["type"] == "s2.link"]
        self.assertEqual(states, ["up", "left"])
        self.assertEqual(events[-1]["payload"]["s2_link"]["heartbeats"], link["heartbeats"])
        self.assertIn("loop", events[-1]["payload"])

        self.assertTrue(report["done"])
        self.assertEqual((report["errors"], report["bye"], report["sessions"]), ([], True, 1))
        self.assertGreater(report["rtt_ns"]["n"], 50)
        self.assertIn("soft real-time, Python", report["loop"]["label"])

        # The experiment's table from this cell: it holds the task time to the simulated
        # run's (placement.TOLERANCE), and rewrites the documentation.
        cell = tmpdir() / "results.json"
        cell.write_text(json.dumps({"scenarios": [result]}))
        gathered = placement.gather([cell])
        self.assertTrue(gathered["passed"], gathered["cells"][0]["problems"])
        c = gathered["cells"][0]
        self.assertEqual((c["profile"], c["ttft_s"], c["s2"], c["outages"]), ("local", 0.1, "remote", []))
        self.assertEqual(c["simulated_task_time_s"], scenario_result(simulated, validate_run(simulated.dir))["task_time_s"])
        self.assertIn("no emulated network, not in VMs, soft real-time", placement.report(gathered))
        doc = tmpdir() / "README.md"
        doc.write_text(f"# Title\n\n{placement.BEGIN}\nold table\n{placement.END}\n\nAfter.\n")
        placement.write_docs(gathered, [doc], "a test")
        text = doc.read_text()
        self.assertIn("Source: a test.", text)
        self.assertNotIn("old table", text)
        self.assertIn("| nominal_a | local | 0.1 | 1 | SUCCEEDED | 0 | 0 |", text)
        self.assertTrue(text.endswith(f"{placement.END}\n\nAfter.\n"))
        self.assertIn('<details markdown="1"><summary>Every cell (1)</summary>', text)
        doc.write_text("no markers\n")
        with self.assertRaises(ConfigError):
            placement.write_docs(gathered, [doc])

    def test_a_recovery_through_s2_in_another_process(self) -> None:
        # slot_blocked: S1 gives up, S0 waits under the fallback, and S2 in the other
        # process chooses another spot. The cell carries the recovery, as the simulated run.
        s2, port = self.s2()
        r = run("slot_blocked", tmpdir() / "blocked", clock="wall", s2=("127.0.0.1", port), s2_timeout=ns(30.0), tags={"profile": "local"})
        self.assertEqual(s2.wait(timeout=60), 0)
        cell = tmpdir() / "results.json"
        cell.write_text(json.dumps({"scenarios": [scenario_result(r, validate_run(r.dir))]}))
        gathered = placement.gather([cell])
        self.assertTrue(gathered["passed"], gathered["cells"][0]["problems"])
        c = gathered["cells"][0]
        assert c["recovery"] is not None
        self.assertTrue(c["recovery"]["fallback_engaged"])
        self.assertFalse(c["recovery"]["fallback_expired"])  # S2 answers in 0.1 s; the fallback lasts 3 s
        self.assertAlmostEqual(c["simulated_recovery_s"], 0.15, places=2)
        self.assertLess(c["recovery"]["recovery_s"], 1.0)
        header, body = placement.summary(gathered)
        row = dict(zip(header, body[0], strict=True))
        self.assertEqual((row["S2 fallbacks expired"], row["runs within tolerance"], row["two-cup task s"]), ("0 of 1", "1 of 1", "-"))
        self.assertTrue(row["slot_blocked recovery s"].endswith("(0.15)"))
        self.assertEqual(row["fallback coverage"], "100.0 %")
        # Before and after, here the same run twice: the table placement-compare writes.
        saved = tmpdir() / "placement" / "results.json"
        saved.parent.mkdir(parents=True)
        saved.write_text(json.dumps(gathered))
        (compared,) = placement.compare(saved, saved)["rows"]
        self.assertEqual((compared["profile"], compared["after"]["engaged"], compared["after"]["hold_s"]), ("local", [True], [0.0]))
        self.assertIn(
            "| local | 0.1 | 100.0 % → 100.0 % | 1 of 1 → 1 of 1 |", placement.compare_markdown(placement.compare(saved, saved), "a", "b")
        )


if __name__ == "__main__":
    unittest.main()
