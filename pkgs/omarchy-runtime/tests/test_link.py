"""The link to an S2 in another process: the transport's framing and failures,
and one scenario with S2 in its own process over TCP on the loopback
interface, on the wall clock."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path

from omarchy_runtime.contracts import LinkError, State, Update, ns
from omarchy_runtime.evaluate import scenario_result
from omarchy_runtime.link import RemoteRuntime, TcpTransport, address
from omarchy_runtime.scenario import run
from omarchy_runtime.trace import validate_run
from omarchy_runtime.wallclock import monotonic_ns, sleep_until
from tests.helpers import scenario, tmpdir

HERE = Path(__file__).parent.parent


def frames(t: TcpTransport, n: int) -> list[dict[str, object]]:
    """Receive until n frames arrived (or two seconds passed)."""
    got: list[dict[str, object]] = []
    deadline = monotonic_ns() + ns(2.0)
    while len(got) < n and monotonic_ns() < deadline:
        got += t.receive()
        sleep_until(monotonic_ns() + ns(0.001))
    return got


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

    def test_addresses(self) -> None:
        self.assertEqual(address("edge:7447"), ("edge", 7447))
        self.assertEqual(address("[::1]:7447"), ("::1", 7447))
        with self.assertRaises(LinkError):
            address("edge")


class TestRemoteRuntime(unittest.TestCase):
    def test_heartbeats_round_trips_and_leaving(self) -> None:
        raw, other = socket.socketpair()
        api = RemoteRuntime(TcpTransport(other), ns(0.1))
        self.addCleanup(raw.close)
        self.addCleanup(api.transport.close)
        api.step(0)
        api.step(ns(0.05))
        robot = TcpTransport(raw)
        first = frames(robot, 1)
        self.assertEqual([(f["type"], f["seq"], f["rtt"], f["final"]) for f in first], [("heartbeat", 1, [], False)])
        robot.send({"type": "echo", "seq": 1, "sent": 0, "t": 5})
        robot.send(Update("r1", "x1", "t1", State.PENDING, 5, None).to_json())
        sleep_until(monotonic_ns() + ns(0.01))
        api.step(ns(0.1))  # handles the echo, then the next heartbeat carries its round trip
        self.assertEqual([u.request_id for u in api.poll()], ["r1"])
        self.assertEqual(frames(robot, 1)[0]["rtt"], [ns(0.1)])
        api.leave(ns(0.15))
        self.assertTrue(frames(robot, 1)[0]["final"])
        api.step(ns(0.3))
        sleep_until(monotonic_ns() + ns(0.01))
        self.assertEqual(robot.receive(), [])  # nothing after the final heartbeat
        robot.send({"type": "launch"})
        sleep_until(monotonic_ns() + ns(0.01))
        api.step(ns(0.4))
        self.assertIn("unexpected frame", api.closed or "")


class TestRemoteS2(unittest.TestCase):
    """nominal_a with S2 in another process over TCP (loopback), on the wall
    clock. Wall-clock runs are not bit-identical: the outcome, the claim, the
    verdict, the ground truth and the safety invariants must be those of the
    simulated run exactly; the task time within 15 % of it."""

    def test_s2_in_another_process(self) -> None:
        env = os.environ | {"PYTHONPATH": str(HERE)}
        command = [sys.executable, "-m", "omarchy_runtime", "s2", "--listen", "127.0.0.1:0", "--ttft", "0.1", "--timeout", "60"]
        s2 = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(s2.kill)
        assert s2.stdout is not None
        port = int(s2.stdout.readline().rsplit(":", 1)[1])
        r = run("nominal_a", tmpdir() / "remote", clock="wall", s2=("127.0.0.1", port), s2_timeout=ns(30.0), tags={"profile": "test"})
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
        expected = scenario_result(simulated, validate_run(simulated.dir))["task_time_s"]
        self.assertAlmostEqual(result["task_time_s"], expected, delta=0.15 * expected)

        header = json.loads((r.dir / "run.json").read_text())
        self.assertEqual(header["clock"], "wall")
        self.assertEqual(header["s2"]["placement"], "remote")
        self.assertEqual(header["s2"]["hello"]["role"], "s2")
        self.assertEqual(header["extensions"]["s2_latency"], {"id": "fixed", "version": "1", "seconds": 0.1, "spread": 0.0})
        self.assertEqual(header["tags"], {"profile": "test"})

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
        self.assertEqual(report["refused"], [])
        self.assertGreater(report["rtt_ns"]["n"], 50)
        self.assertIn("soft real-time, Python", report["loop"]["label"])


if __name__ == "__main__":
    unittest.main()
