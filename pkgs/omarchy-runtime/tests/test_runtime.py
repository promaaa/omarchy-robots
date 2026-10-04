"""The runtime against fakes: every failure path of the lifecycle, the
verification phase, fallbacks, cancellation and the public API."""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import Any

from omarchy_runtime import config
from omarchy_runtime.belief import WorldState
from omarchy_runtime.contracts import (
    ActionChunk,
    BodySpec,
    Cancel,
    ChunkMeta,
    ChunkRejected,
    Claim,
    Decision,
    FallbackSpec,
    Goal,
    GoalRequest,
    InstallFallback,
    Json,
    Ns,
    Observation,
    Rejection,
    Report,
    S0Counters,
    S0Mode,
    S0Status,
    S1Policy,
    State,
    Verdict,
    VerdictKind,
    ns,
)
from omarchy_runtime.extensions import GeometricVerifier, IdentityFilter
from omarchy_runtime.runtime import Params, Runtime
from omarchy_runtime.sched import Mailbox
from omarchy_runtime.trace import TraceWriter, validate_run
from tests.helpers import GANTRY, GANTRY_CFG, IN_RACK, RACK_SPOT, TABLE_SPOT, belief, observation, tmpdir

STEP = ns(0.05)


class FakeS0:
    def __init__(self) -> None:
        self.mode = S0Mode.HOLD
        self.meta: ChunkMeta | None = None
        self.installed: list[FallbackSpec] = []
        self.cleared = 0
        self.t: Ns = 0

    def submit_chunk(self, chunk: ActionChunk, now: Ns) -> None:
        raise AssertionError("the runtime never submits chunks")

    def install_fallback(self, spec: FallbackSpec, now: Ns) -> None:
        if spec.valid_until < now:
            raise ChunkRejected(Rejection.EXPIRED, "expired")
        self.installed.append(spec)

    def clear_fallback(self) -> None:
        self.cleared += 1

    def status(self) -> S0Status:
        return S0Status(self.t, self.mode, None, tuple(GANTRY.home), self.meta, bool(self.installed), S0Counters())


class FakeHost:
    def __init__(self) -> None:
        self.attached: list[S1Policy] = []
        self.detached = 0
        self.failure: str | None = None

    def attach(self, policy: S1Policy) -> None:
        self.attached.append(policy)

    def detach(self) -> None:
        self.detached += 1

    def error(self) -> str | None:
        return self.failure


class FakePolicy:
    def __init__(self, fails_to_start: bool = False, report_raises: bool = False) -> None:
        self.result: Report | None = None
        self.fails_to_start = fails_to_start
        self.report_raises = report_raises

    def start(self, goal: Goal, belief: Json, body: BodySpec, conditioning: Mapping[str, Any]) -> None:
        if self.fails_to_start:
            raise ValueError("no plan")

    def step(self, now: Ns, observation: Observation | None) -> ActionChunk | None:
        return None

    def report(self) -> Report | None:
        if self.report_raises:
            raise RuntimeError("lost")
        return self.result


class StaleVerifier:
    """Says SATISFIED citing the first observation it ever saw: a buggy verifier."""

    id = "stale"
    version = "0"

    def verify(self, goal: Goal, belief: WorldState, not_before: Ns) -> Verdict:
        return Verdict(VerdictKind.SATISFIED, ("obs-1",), "trust me")


class Rig:
    """A runtime on the gantry with fake S0 and S1, stepped at 20 Hz."""

    def __init__(self, policy: FakePolicy | None = None, verifier: Any = None, **params: float) -> None:
        self.policy = policy or FakePolicy()
        self.s0, self.host, self.inbox = FakeS0(), FakeHost(), Mailbox[Observation]()
        self.dir = tmpdir() / "run"
        p = {"deadline": 20.0, "s1_stall_timeout": 3.0, "freshness": 1.0, "settle": 0.3, "verify_budget": 2.0} | params
        self.runtime = Runtime(
            body=GANTRY,
            capabilities=config.capabilities(GANTRY_CFG),
            bindings=config.bindings(GANTRY_CFG),
            implementations={"pick_place.arm3": FakePolicy, "pick_place.gantry": lambda: self.policy},
            params=Params(**{k: ns(v) for k, v in p.items()}),
            belief=belief(),
            s0=self.s0,
            s1_host=self.host,
            observations=self.inbox,
            observation_filter=IdentityFilter(),
            verifiers={"in": verifier or GeometricVerifier()},
            trace=TraceWriter(self.dir, {"scenario": "rig"}),
            run_id="rig",
            config_hash="0",
            seed=0,
        )
        self.runtime._trace.event(0, "run.start", None, {})
        self.closed = False
        unittest.addModuleCleanup(self.close)
        self.t: Ns = 0
        self.n = 0
        self.cups: dict[str, tuple[float, float]] = {"cup_1": TABLE_SPOT}
        self.updates: list[Any] = []

    def run(self, seconds: float, chunks: bool = False) -> None:
        for _ in range(round(seconds / 0.05)):
            self.t += STEP
            self.n += 1
            self.inbox.put(observation(self.n, self.t / 1e9, self.cups))
            if chunks:
                self.s0.meta = ChunkMeta(self.n, "fake", self.t, STEP, 10, self.t + 9 * STEP, self.t)
            self.s0.t = self.t
            self.runtime.step(self.t)
            self.updates += self.runtime.poll()

    def request(self, goal: Goal = IN_RACK) -> str:
        execution = self.runtime.submit(GoalRequest(goal, "task"))
        assert execution is not None
        return execution

    def summary(self, execution: str) -> Json:
        return next(s for s in self.runtime.finished if s["execution_id"] == execution)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.runtime.close(self.t, "test")

    def events(self) -> list[Json]:
        self.close()
        return [json.loads(line) for line in (self.dir / "events.jsonl").read_text().splitlines()]


class TestFailures(unittest.TestCase):
    def test_executor_gives_up(self) -> None:
        rig = Rig()
        x = rig.request()
        rig.run(0.2, chunks=True)
        rig.policy.result = Report(Claim.GAVE_UP, "cannot")
        rig.run(0.1, chunks=True)
        s = rig.summary(x)
        self.assertEqual((s["outcome"], s["category"], s["claim"]["claim"]), ("FAILED", "IMPLEMENTATION_ERROR", "GAVE_UP"))
        self.assertEqual(rig.host.detached, 1)

    def test_policy_errors_fail_the_execution_not_the_runtime(self) -> None:
        for policy, host_error in ((FakePolicy(fails_to_start=True), None), (FakePolicy(report_raises=True), None), (None, "boom")):
            rig = Rig(policy)
            rig.host.failure = host_error
            x = rig.request()
            rig.run(0.3, chunks=True)
            self.assertEqual((rig.summary(x)["outcome"], rig.summary(x)["category"]), ("FAILED", "IMPLEMENTATION_ERROR"))

    def test_s0_fault(self) -> None:
        rig = Rig()
        x = rig.request()
        rig.run(0.2, chunks=True)
        rig.s0.mode = S0Mode.FAULT
        rig.run(0.1, chunks=True)
        self.assertEqual(rig.summary(x)["reason"], "S0 reports a fault")

    def test_stall_and_timeout(self) -> None:
        rig = Rig()
        x = rig.request()
        rig.run(3.2)  # no chunk ever reaches S0
        s = rig.summary(x)
        self.assertEqual((s["outcome"], s["category"]), ("FAILED", "S1_STALL"))
        self.assertAlmostEqual(s["timing"]["ended"] / 1e9, 3.1)  # started at 0.05: first step with more than 3 s of silence
        rig = Rig(deadline=1.0)
        x = rig.request()
        rig.run(1.3, chunks=True)
        self.assertEqual(rig.summary(x)["category"], "TIMEOUT")

    def test_only_one_execution_runs_at_a_time(self) -> None:
        rig = Rig()
        first, second = rig.request(), rig.request()
        rig.run(0.2, chunks=True)
        self.assertIs(rig.runtime.executions[first].state, State.RUNNING)
        self.assertIs(rig.runtime.executions[second].state, State.PENDING)
        rig.runtime.submit(Cancel(first))
        rig.run(0.1, chunks=True)
        self.assertEqual(rig.summary(first)["category"], "CANCELLED")
        self.assertIs(rig.runtime.executions[second].state, State.RUNNING)
        rig.runtime.submit(Cancel(second))
        rig.run(0.1, chunks=True)
        self.assertEqual(rig.host.detached, 2)
        self.assertTrue(rig.runtime.idle())


class TestVerification(unittest.TestCase):
    def done(self, rig: Rig, cups: dict[str, tuple[float, float]]) -> Json:
        x = rig.request()
        rig.run(0.5, chunks=True)
        rig.cups = cups
        rig.policy.result = Report(Claim.DONE, "finished")
        rig.run(3.0)
        return rig.summary(x)

    def test_claim_and_verdict_are_recorded_apart(self) -> None:
        s = self.done(Rig(), {"cup_1": RACK_SPOT})
        self.assertEqual((s["outcome"], s["claim"]["claim"], s["verdict"]["kind"]), ("SUCCEEDED", "DONE", "SATISFIED"))
        self.assertTrue(all(t >= s["verdict"]["not_before"] for t in s["verdict"]["evidence_t"]))
        self.assertEqual(s["verdict"]["not_before"], s["claim"]["t"] + ns(0.3))
        s = self.done(Rig(), {"cup_1": TABLE_SPOT})
        self.assertEqual(
            (s["outcome"], s["category"], s["claim"]["claim"], s["verdict"]["kind"]), ("FAILED", "VERIFICATION_FAILED", "DONE", "VIOLATED")
        )

    def test_no_fresh_evidence_within_budget_is_unverified(self) -> None:
        s = self.done(Rig(), {})
        self.assertEqual((s["outcome"], s["category"], s["verdict"]["kind"]), ("UNVERIFIED", "VERIFICATION_INCONCLUSIVE", "INCONCLUSIVE"))
        self.assertGreater(s["verdict"]["attempts"], 30)  # one per fresh observation for 2 s
        self.assertAlmostEqual((s["timing"]["ended"] - s["verdict"]["not_before"]) / 1e9, 2.0)

    def test_the_runtime_overrules_a_verifier_citing_old_observations(self) -> None:
        s = self.done(Rig(verifier=StaleVerifier()), {"cup_1": RACK_SPOT})
        self.assertEqual(s["outcome"], "UNVERIFIED")
        self.assertIn("evidence not fresh", s["verdict"]["reason"])

    def test_already_satisfied_goal_is_verified_without_executing(self) -> None:
        rig = Rig()
        rig.cups = {"cup_1": RACK_SPOT}
        rig.run(0.1)
        x = rig.request()
        rig.run(0.5)
        s = rig.summary(x)
        self.assertEqual((s["outcome"], s["claim"], s["implementation"]), ("SUCCEEDED", None, None))
        self.assertTrue(s["resolution"].startswith("already satisfied"))
        self.assertEqual(rig.host.attached, [])

    def test_rejections(self) -> None:
        rig = Rig()
        rig.run(0.1)
        clean, unseen = rig.request(Goal("clean", ("cup_1",))), rig.request(Goal("in", ("mug_9", "drying_rack")))
        rig.run(0.2)
        self.assertEqual(rig.summary(clean)["category"], "NO_CAPABILITY")
        self.assertEqual(rig.summary(unseen)["category"], "PRECONDITION_UNMET")
        self.assertIn("mug_9 not observed", rig.summary(unseen)["candidates"][1]["reason"])


class TestFallbacksAndApi(unittest.TestCase):
    def spec(self, body: BodySpec = GANTRY, until: float = 60.0) -> FallbackSpec:
        return FallbackSpec.make(body.action_space, body.dims, ns(1.5), [list(body.safe_pose)], 0, ns(until), "test")

    def test_installed_for_a_running_execution_and_cleared_when_it_stops(self) -> None:
        rig = Rig()
        x = rig.request()
        rig.runtime.submit(InstallFallback(x, self.spec()))  # still PENDING when handled: rejected
        rig.run(0.2, chunks=True)
        rig.runtime.submit(InstallFallback(x, self.spec()))
        rig.runtime.submit(InstallFallback(x, self.spec(until=0.0)))  # expired: S0 refuses it
        rig.run(0.1, chunks=True)
        self.assertEqual(len(rig.s0.installed), 1)
        rig.policy.result = Report(Claim.DONE, "done")
        rig.run(0.1, chunks=True)
        self.assertEqual(rig.s0.cleared, 1)
        self.assertEqual(len(rig.runtime.executions[x].fallbacks), 1)
        kinds = [e["type"] for e in rig.events()]
        self.assertEqual(kinds.count("fallback.rejected"), 2)
        self.assertEqual(kinds.count("fallback.install"), 1)
        self.assertEqual(kinds.count("fallback.clear"), 1)

    def test_a_fallback_for_another_body_is_refused(self) -> None:
        rig = Rig()
        x = rig.request()
        rig.run(0.2, chunks=True)
        rig.runtime.submit(InstallFallback(x, self.spec(config.body(config.effective("nominal_a")))))
        rig.run(0.1, chunks=True)
        self.assertEqual(rig.s0.installed, [])

    def test_updates_decisions_and_trace(self) -> None:
        rig = Rig()
        x = rig.request()
        rig.run(0.2, chunks=True)
        rig.policy.result = Report(Claim.DONE, "done")
        rig.cups = {"cup_1": RACK_SPOT}
        rig.run(1.0, chunks=True)
        rig.runtime.submit(Decision("task", x, "give_up", "nothing to do", "test@1"))
        rig.run(0.1)
        states = [u.state for u in rig.updates if u.execution_id == x]
        self.assertEqual(states, [State.PENDING, State.RUNNING, State.VERIFYING, State.SUCCEEDED])
        self.assertIsNotNone(rig.updates[-1].summary)
        json.dumps([u.to_json() for u in rig.updates])
        kinds = [e["type"] for e in rig.events()]
        self.assertIn("decision", kinds)
        check = validate_run(rig.dir)
        self.assertEqual([e for e in check.errors if not e.startswith("run.json")], [])  # the rig's header is minimal


if __name__ == "__main__":
    unittest.main()
