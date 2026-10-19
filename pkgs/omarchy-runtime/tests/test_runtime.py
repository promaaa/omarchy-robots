"""The runtime against fakes: every failure path of the lifecycle, the
verification phase, fallbacks, cancellation and the public API."""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import Any

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.belief import WorldState
from omarchy_runtime.contracts import (
    ActionChunk,
    BodySpec,
    Cancel,
    ChunkMeta,
    ChunkRejected,
    Claim,
    ContractError,
    Decision,
    FallbackSpec,
    Goal,
    GoalRequest,
    Heartbeat,
    InstallFallback,
    IntentChunk,
    Json,
    Ns,
    Observation,
    Rejection,
    Report,
    S0Counters,
    S0History,
    S0Mode,
    S0Status,
    S1Policy,
    State,
    Update,
    Verdict,
    VerdictKind,
    ns,
)
from omarchy_runtime.extensions import FixedLatency, GeometricVerifier, IdentityFilter, LiftHeld
from omarchy_runtime.runtime import Params, Runtime
from omarchy_runtime.s2 import ScriptedS2
from omarchy_runtime.sched import Mailbox
from omarchy_runtime.trace import TraceWriter, validate_run
from tests.helpers import GANTRY, GANTRY_CFG, IN_RACK, RACK_SPOT, TABLE_SPOT, belief, observation, tmpdir

STEP = ns(0.05)


class FakeS0:
    def __init__(self) -> None:
        self.mode = S0Mode.HOLD
        self.meta: ChunkMeta | None = None
        self.installed: list[FallbackSpec] = []  # S2's
        self.bases: list[FallbackSpec] = []  # the capability's default
        self.cleared = 0
        self.t: Ns = 0
        self.position = tuple(GANTRY.home)  # the gripper open

    def submit_chunk(self, chunk: ActionChunk | IntentChunk, now: Ns) -> None:
        raise AssertionError("the runtime never submits chunks")

    def install_fallback(self, spec: FallbackSpec, now: Ns, base: bool = False) -> None:
        if spec.valid_until < now:
            raise ChunkRejected(Rejection.EXPIRED, "expired")
        (self.bases if base else self.installed).append(spec)

    def hold(self, fallback: bool = False) -> None:
        self.cleared += 1

    def status(self) -> S0Status:
        return S0Status(self.t, self.mode, None, self.position, self.meta, bool(self.installed or self.bases), S0Counters())

    def history(self, since: Ns) -> S0History:
        raise AssertionError("the runtime never reads S0's history")


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

    def __init__(
        self, policy: FakePolicy | None = None, verifier: Any = None, observation_filter: Any = None, factory: Any = None, **params: float
    ) -> None:
        self.policy = policy or FakePolicy()
        self.s0, self.host, self.inbox = FakeS0(), FakeHost(), Mailbox[Observation]()
        self.dir = tmpdir() / "run"
        p = {
            "deadline": 20.0,
            "s1_stall_timeout": 3.0,
            "freshness": 1.0,
            "settle": 0.3,
            "verify_budget": 2.0,
            "s2_lost": 0.5,
            "s2_long": 2.0,
        }
        p |= params
        self.runtime = Runtime(
            body=GANTRY,
            capabilities=config.capabilities(GANTRY_CFG),
            bindings=config.bindings(GANTRY_CFG),
            implementations={"pick_place.arm3": FakePolicy, "pick_place.gantry": factory or (lambda: self.policy)},
            params=Params(**{k: ns(v) for k, v in p.items()}),  # type: ignore[arg-type]  # the fidelity limits keep their default
            belief=belief(),
            s0=self.s0,
            s1_host=self.host,
            observations=self.inbox,
            observation_filter=observation_filter or IdentityFilter(),
            verifiers={"in": verifier or GeometricVerifier()},
            trace=TraceWriter(self.dir, {"scenario": "rig"}),
            run_id="rig",
            config_hash="0",
            seed=0,
            default_fallbacks={"lift": LiftHeld()},
        )
        self.runtime._trace.event(0, "run.start", None, {})
        self.closed = False
        unittest.addModuleCleanup(self.close)
        self.t: Ns = 0
        self.n = 0
        self.cups: dict[str, tuple[float, float]] = {"cup_1": TABLE_SPOT}
        self.updates: list[Update] = []
        self.requests = 0

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

    def request(self, goal: Goal = IN_RACK, fallback: FallbackSpec | None = None) -> str:
        """Submit a request; returns its request_id."""
        self.requests += 1
        request_id = f"r{self.requests}"
        self.runtime.submit(GoalRequest(goal, "task", request_id, fallback=fallback))
        return request_id

    def execution(self, request_id: str) -> str:
        """The execution of a request, from the Updates seen so far."""
        return next(u.execution_id for u in self.updates if u.request_id == request_id)

    def summary(self, request_id: str) -> Json:
        return next(s for s in self.runtime.finished if s["request_id"] == request_id)

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
        self.assertIs(rig.runtime.executions[rig.execution(first)].state, State.RUNNING)
        self.assertIs(rig.runtime.executions[rig.execution(second)].state, State.PENDING)
        rig.runtime.submit(Cancel(rig.execution(first)))
        rig.run(0.1, chunks=True)
        self.assertEqual(rig.summary(first)["category"], "CANCELLED")
        self.assertIs(rig.runtime.executions[rig.execution(second)].state, State.RUNNING)
        rig.runtime.submit(Cancel(rig.execution(second)))
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
        rig.request()
        rig.runtime.submit(InstallFallback("rig-x01", self.spec()))  # still PENDING when handled: rejected
        rig.run(0.2, chunks=True)
        x = rig.execution("r1")
        self.assertEqual(x, "rig-x01")
        rig.runtime.submit(InstallFallback(x, self.spec()))
        rig.runtime.submit(InstallFallback(x, self.spec(until=0.0)))  # expired: S0 refuses it
        rig.run(0.1, chunks=True)
        self.assertEqual(len(rig.s0.installed), 1)
        rig.policy.result = Report(Claim.DONE, "done")
        rig.run(0.1, chunks=True)
        self.assertEqual(rig.s0.cleared, 2)  # the previous execution's fallbacks at its start; all at its end
        self.assertEqual([f["kind"] for f in rig.runtime.executions[x].fallbacks], ["capability default", "s2 refined"])
        kinds = [e["type"] for e in rig.events()]
        self.assertEqual(kinds.count("fallback.rejected"), 2)
        self.assertEqual(kinds.count("fallback.install"), 2)
        self.assertEqual(kinds.count("s0.hold"), 1)

    def test_s2_predicted_fallback_arrives_with_the_goal_before_s1(self) -> None:
        rig = Rig()
        predicted = FallbackSpec.make(GANTRY.action_space, GANTRY.dims, ns(1.5), [list(GANTRY.safe_pose)], 0, ns(60), "s2.test")
        r = rig.request(fallback=predicted)
        rig.run(0.05)  # one step: the execution starts, S1 is attached, no chunk yet
        x = rig.runtime.executions[rig.execution(r)]
        assert x.started is not None
        self.assertEqual(len(rig.host.attached), 1)
        (base,) = rig.s0.bases
        (s2,) = rig.s0.installed
        # The window sent relative to the start, moved onto the runtime's clock.
        self.assertEqual((s2.valid_from, s2.valid_until), (x.started, x.started + ns(60)))
        self.assertEqual(base.relative, (True,) * len(GANTRY.dims))
        self.assertEqual(base.source, "default.lift@1")
        self.assertEqual([(f["slot"], f["kind"]) for f in x.fallbacks], [("base", "capability default"), ("s2", "s2 predicted")])
        installs = [e["payload"] for e in rig.events() if e["type"] == "fallback.install"]
        self.assertEqual(
            [(p["slot"], p["kind"], p["valid_from"]) for p in installs],
            [("base", "capability default", x.started)] * 1 + [("s2", "s2 predicted", x.started)],
        )

    def test_the_base_fallback_is_renewed_when_the_grasp_changes(self) -> None:
        rig = Rig()
        r = rig.request()
        rig.run(0.2, chunks=True)
        self.assertEqual(len(rig.s0.bases), 1)
        rig.run(0.2, chunks=True)
        self.assertEqual(len(rig.s0.bases), 1)  # the same motion: not installed again
        rig.s0.position = (*GANTRY.home[:-1], 0.0)  # the gripper closed: holding
        rig.run(0.1, chunks=True)
        first, lifted = rig.s0.bases
        z = GANTRY.dims.index("z")
        self.assertEqual((first.actions[0, z], lifted.actions[0, z]), (0.0, 0.02))  # hold, then lift 2 cm and hold
        self.assertEqual(lifted.valid_from, first.valid_from)  # the same window, from the execution's start
        reasons = [e["payload"].get("reason") for e in rig.events() if e["type"] == "fallback.install"]
        self.assertEqual(reasons, ["the execution starts", "the grasp changed"])
        self.assertEqual(rig.execution(r), "rig-x01")

    def test_a_fallback_for_another_body_is_refused(self) -> None:
        rig = Rig()
        r = rig.request()
        rig.run(0.2, chunks=True)
        rig.runtime.submit(InstallFallback(rig.execution(r), self.spec(config.body(config.effective("nominal_a")))))
        rig.run(0.1, chunks=True)
        self.assertEqual(rig.s0.installed, [])

    def test_only_runtime_messages_are_accepted(self) -> None:
        with self.assertRaises(ContractError):
            Rig().runtime.submit(IN_RACK)  # type: ignore[arg-type]

    def test_messages_must_be_json(self) -> None:
        with self.assertRaises(ContractError):
            Rig().runtime.submit(GoalRequest(IN_RACK, "task", "r1", conditioning={"speed": object()}))
        with self.assertRaises(ContractError):
            Rig().runtime.submit(GoalRequest(IN_RACK, "task", "r1", conditioning={"speed": float("nan")}))

    def test_a_request_is_idempotent_by_its_id(self) -> None:
        # A client resending after a lost connection: no second execution, the latest Update again.
        rig = Rig()
        rig.runtime.submit(GoalRequest(IN_RACK, "task", "r1"))
        rig.runtime.submit(GoalRequest(IN_RACK, "task", "r1"))  # still in the inbox
        rig.run(0.2, chunks=True)
        rig.runtime.submit(GoalRequest(IN_RACK, "task", "r1"))  # executing
        rig.run(0.1, chunks=True)
        self.assertEqual(len(rig.runtime.executions), 1)
        states = [u.state for u in rig.updates if u.request_id == "r1"]
        self.assertEqual(states, [State.PENDING, State.PENDING, State.RUNNING, State.RUNNING])  # each repeat: the latest again
        repeats = [e for e in rig.events() if e["type"] == "request" and e["payload"].get("repeat")]
        self.assertEqual(len(repeats), 2)
        with self.assertRaises(ContractError):
            rig.runtime.submit(GoalRequest(Goal("in", ("cup_2", "drying_rack")), "task", "r1"))  # another request, same id

    def test_updates_decisions_and_trace(self) -> None:
        rig = Rig()
        r = rig.request()
        rig.run(0.2, chunks=True)
        rig.policy.result = Report(Claim.DONE, "done")
        rig.cups = {"cup_1": RACK_SPOT}
        rig.run(1.0, chunks=True)
        x = rig.execution(r)
        rig.runtime.submit(Decision("task", x, "give_up", "nothing to do", "test@1"))
        rig.run(0.1)
        mine = [u for u in rig.updates if u.request_id == r]
        self.assertEqual([u.state for u in mine], [State.PENDING, State.RUNNING, State.VERIFYING, State.SUCCEEDED])
        self.assertEqual({u.execution_id for u in mine}, {x})
        self.assertEqual([u.t for u in mine], sorted(u.t for u in mine))
        self.assertEqual(mine[0].t, STEP)  # handled at the first step after submission
        self.assertIsNotNone(rig.updates[-1].summary)
        json.dumps([u.to_json() for u in rig.updates])
        kinds = [e["type"] for e in rig.events()]
        self.assertIn("decision", kinds)
        check = validate_run(rig.dir)
        self.assertEqual([e for e in check.errors if not e.startswith("run.json")], [])  # the rig's header is minimal


class TestS2Liveness(unittest.TestCase):
    """The supervisor watches S2's heartbeats from the first to the final one,
    and classifies each outage by its duration against the deadlines."""

    def beat(self, rig: Rig, seconds: float) -> None:
        for _ in range(round(seconds / 0.1)):  # every 0.1 s, each handled at the runtime's next step
            self.seq += 1
            rig.runtime.submit(Heartbeat(self.seq, rig.t, (ns(0.002),)))
            rig.run(0.1)

    def test_outages_classified_by_the_deadlines(self) -> None:
        rig, self.seq = Rig(), 0
        rig.run(1.0)  # no heartbeat yet: S2 is not watched
        self.beat(rig, 1.0)
        rig.run(1.0)  # silent 1.1 s: lost after 0.5 s, then a short outage
        self.beat(rig, 0.5)
        rig.run(3.0)  # silent 3.1 s: a long outage
        self.beat(rig, 0.5)
        rig.run(0.4)  # silent 0.5 s: not more than the lost deadline, no outage
        self.beat(rig, 0.2)
        rig.runtime.submit(Heartbeat(self.seq + 1, rig.t, final=True))
        rig.run(2.0)  # S2 left: silence is not an outage
        link = rig.runtime.s2_link(rig.t)
        assert link is not None
        self.assertEqual([(o["class"], round(o["duration_ns"] / 1e9, 2)) for o in link["outages"]], [("short", 1.1), ("long", 3.1)])
        self.assertEqual(link["max_gap_ns"], ns(3.1))
        self.assertEqual(link["heartbeats"], 23)
        self.assertEqual(link["rtt_ns"]["n"], 22)
        self.assertEqual(link["deadlines_ns"], {"lost": ns(0.5), "long": ns(2.0)})
        stream = [e for e in rig.events() if e["type"] == "s2.link"]
        self.assertEqual([e["payload"]["state"] for e in stream], ["up", "lost", "recovered", "lost", "recovered", "left"])
        lost = stream[1]
        self.assertGreater(lost["payload"]["silent_ns"], ns(0.5))
        self.assertLessEqual(lost["payload"]["silent_ns"], ns(0.55))  # noticed at the first step past the deadline
        self.assertEqual(rig.events()[-1]["payload"]["s2_link"]["outages"], link["outages"])

    def test_an_outage_still_open_at_the_end_is_unrecovered(self) -> None:
        rig, self.seq = Rig(), 0
        self.beat(rig, 0.3)
        rig.run(1.0)
        link = rig.runtime.s2_link(rig.t)
        assert link is not None
        self.assertEqual([o["class"] for o in link["outages"]], ["unrecovered"])
        self.assertIsNone(link["outages"][0]["to"])

    def test_no_heartbeat_no_watch(self) -> None:
        rig = Rig()
        rig.run(2.0)
        self.assertIsNone(rig.runtime.s2_link(rig.t))
        self.assertNotIn("s2_link", rig.events()[-1]["payload"])


class Raises:
    """An extension that fails: a filter, a verifier, or a policy factory."""

    id = "raises"
    version = "0"

    def apply(self, observation: Observation) -> tuple[Observation, tuple[str, ...]]:
        raise RuntimeError("filter broke")

    def verify(self, goal: Goal, belief: WorldState, not_before: Ns) -> Verdict:
        raise ZeroDivisionError("verifier broke")

    def __call__(self) -> S1Policy:
        raise RuntimeError("no policy")


class TestFailingExtensions(unittest.TestCase):
    """A failing extension fails an execution or loses an observation, never the run."""

    def test_a_failing_verifier_never_gives_success(self) -> None:
        s = TestVerification().done(Rig(verifier=Raises()), {"cup_1": RACK_SPOT})
        self.assertEqual((s["outcome"], s["category"]), ("UNVERIFIED", "VERIFICATION_INCONCLUSIVE"))
        self.assertIn("ZeroDivisionError", s["verdict"]["reason"])

    def test_a_failing_filter_drops_observations_from_the_belief(self) -> None:
        rig = Rig(observation_filter=Raises())
        x = rig.request()
        rig.run(0.3)
        self.assertEqual(rig.runtime.belief.version, 0)  # nothing unfiltered got in
        self.assertEqual(rig.summary(x)["category"], "PRECONDITION_UNMET")
        errors = [e for e in rig.events() if e["type"] == "error"]
        self.assertTrue(errors and all("filter broke" in e["payload"]["reason"] for e in errors))

    def test_a_failing_policy_factory_fails_the_execution(self) -> None:
        rig = Rig(factory=Raises())
        rig.run(0.1)
        x = rig.request()
        rig.run(0.2)
        self.assertEqual((rig.summary(x)["outcome"], rig.summary(x)["category"]), ("FAILED", "IMPLEMENTATION_ERROR"))
        self.assertTrue(rig.runtime.idle())


class TestScriptedS2(unittest.TestCase):
    def test_a_failing_recovery_policy_means_giving_up(self) -> None:
        class Api:
            sent: list[Any] = []

            def submit(self, message: Any) -> None:
                self.sent.append(message)

            def poll(self) -> list[Update]:
                return []

        class Broken:
            id, version = "broken", "0"

            def decide(self, goal: Goal, summary: Json, belief: Json) -> Any:
                raise KeyError("verdict")

        api = Api()
        s2 = ScriptedS2(api, [IN_RACK], GANTRY, Broken(), FixedLatency(0.0), np.random.default_rng(0), fallback=False, task_prefix="t")
        s2.step(0)
        s2.step(STEP)
        failed = {"outcome": "FAILED", "category": "S1_STALL", "belief_final": {}}
        self.assertEqual(api.sent[0].request_id, "t-t01-r01")
        s2._see(Update("t-t01-r01", "x1", "t-t01", State.FAILED, 2 * STEP, failed), 2 * STEP)
        s2.step(3 * STEP)
        s2.step(4 * STEP)
        decision = api.sent[-1]
        self.assertIsInstance(decision, Decision)
        self.assertEqual(decision.action, "give_up")
        self.assertIn("broken@0 failed: KeyError", decision.reason)
        self.assertTrue(s2.done)


if __name__ == "__main__":
    unittest.main()
