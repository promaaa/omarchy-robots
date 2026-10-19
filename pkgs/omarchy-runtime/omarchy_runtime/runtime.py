"""The control plane: goals in; executions resolved, supervised, verified and traced.

The runtime owns the application API, capability resolution, the execution
lifecycle, supervision, the belief and verification. It is not in the
high-rate path: S1 sends chunks to S0 directly, and the runtime samples S0's
status once per step, at its own rate.

It never reads the simulator: everything it knows comes from observations
(through the observation filter) and from S0's status.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import assert_never

from omarchy_runtime.attribution import FidelityLimits, attribute, fidelity
from omarchy_runtime.belief import Evaluation, WorldState
from omarchy_runtime.contracts import (
    OUTCOME_CATEGORIES,
    S0,
    TERMINAL,
    BodySpec,
    Cancel,
    Category,
    ChunkMeta,
    Claim,
    ConfigError,
    ContractError,
    Decision,
    FallbackGenerator,
    FallbackSpec,
    Goal,
    GoalRequest,
    Heartbeat,
    InstallFallback,
    Json,
    LifecycleError,
    Message,
    Ns,
    Observation,
    ObservationFilter,
    Report,
    S0Counters,
    S0Mode,
    S0Status,
    S1Host,
    S1Policy,
    State,
    Truth,
    Update,
    Verdict,
    VerdictKind,
    Verifier,
    ns,
    seconds,
)
from omarchy_runtime.sched import Mailbox, distribution
from omarchy_runtime.trace import TraceWriter, dumps
from omarchy_runtime.wallclock import perf_ns

BASE_WINDOW = ns(600.0)  # the base fallback's window from an execution's start: it, and a wait for S2 after a failure


@dataclass(frozen=True)
class Capability:
    """A way to achieve a goal predicate; verbs are capabilities, never goals."""

    id: str
    achieves: str
    params: tuple[str, ...]
    requires: tuple[str, ...]  # body features
    preconditions: tuple[tuple[str, str], ...]  # ("observed" | "reachable", param)
    default_fallback: str | None = None  # a FallbackGenerator id: S0's base fallback while it runs


@dataclass(frozen=True)
class Binding:
    """Which implementation runs a capability, on which bodies (from configuration)."""

    capability: str
    implementation: str
    version: str
    bodies: tuple[str, ...]
    priority: int


@dataclass(frozen=True)
class Params:
    deadline: Ns
    s1_stall_timeout: Ns
    freshness: Ns
    settle: Ns
    verify_budget: Ns
    s2_lost: Ns  # no heartbeat from S2 for longer than this: the link is lost
    s2_long: Ns  # an outage this long or longer is "long", a shorter one "short"
    fidelity: FidelityLimits = FidelityLimits(0.05, 0.9, 0.8, 0.01)


@dataclass(frozen=True)
class Candidate:
    capability: str
    implementation: str | None
    version: str | None
    accepted: bool
    reason: str

    def to_json(self) -> Json:
        return {
            "capability": self.capability,
            "implementation": self.implementation,
            "version": self.version,
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Resolution:
    binding: Binding | None
    category: Category | None  # why nothing was chosen, when nothing was
    reason: str
    candidates: tuple[Candidate, ...]
    satisfied: Evaluation | None = None  # the goal already holds in fresh belief


def resolve(
    goal: Goal,
    belief: WorldState,
    body: BodySpec,
    capabilities: Sequence[Capability],
    bindings: Sequence[Binding],
    since: Ns,
) -> Resolution:
    """Deterministic: the same goal, belief and configuration give the same
    answer, and every candidate considered is recorded with its reason."""
    already = belief.evaluate(goal, since)
    if already.truth is Truth.TRUE:
        return Resolution(None, None, f"already satisfied: {already.reason}", (), already)
    candidates: list[Candidate] = []
    achieving = []
    for cap in sorted(capabilities, key=lambda c: c.id):
        if cap.achieves != goal.predicate:
            candidates.append(Candidate(cap.id, None, None, False, f"achieves {cap.achieves}, not {goal.predicate}"))
        elif len(cap.params) != len(goal.args):
            candidates.append(Candidate(cap.id, None, None, False, f"takes {len(cap.params)} arguments, not {len(goal.args)}"))
        else:
            achieving.append(cap)
    if not achieving:
        return Resolution(None, Category.NO_CAPABILITY, f"no capability achieves {goal}", tuple(candidates))

    chosen: tuple[int, Binding] | None = None
    usable_on_body = False
    for cap in achieving:
        lacking = [f for f in cap.requires if f not in body.features]
        unmet = _unmet(cap, goal.args, belief, body, since)
        for b in sorted((b for b in bindings if b.capability == cap.id), key=lambda b: (-b.priority, b.implementation)):
            if body.id not in b.bodies:
                reason, ok = f"not bound to body {body.id} (bodies: {', '.join(b.bodies)})", False
            elif lacking:
                reason, ok = f"body {body.id} lacks {', '.join(lacking)}", False
            else:
                usable_on_body = True
                reason, ok = ("precondition unmet: " + "; ".join(unmet), False) if unmet else ("eligible", True)
            candidates.append(Candidate(cap.id, b.implementation, b.version, ok, reason))
            if ok and (chosen is None or b.priority > chosen[1].priority):
                chosen = (len(candidates) - 1, b)
    if chosen is not None:
        n, b = chosen
        candidates[n] = Candidate(b.capability, b.implementation, b.version, True, f"chosen (priority {b.priority})")
        return Resolution(b, None, f"{b.capability} by {b.implementation} {b.version}", tuple(candidates))
    if usable_on_body:
        return Resolution(None, Category.PRECONDITION_UNMET, f"preconditions of {goal} unmet", tuple(candidates))
    return Resolution(None, Category.NO_CAPABILITY, f"no capability achieves {goal} on body {body.id}", tuple(candidates))


def _unmet(cap: Capability, args: Sequence[str], belief: WorldState, body: BodySpec, since: Ns) -> list[str]:
    named = dict(zip(cap.params, args, strict=True))
    unmet = []
    for kind, param in cap.preconditions:
        entity = named[param]
        region = belief.scene.regions.get(entity)
        e = belief.estimate(entity, since)
        if kind == "observed" and e is None and region is None:
            unmet.append(f"{entity} not observed with confidence since t={since} ns")
        elif kind == "reachable":
            if region is not None:
                x0, x1, z0, z1 = body.workspace
                if region.x1 < x0 or region.x0 > x1 or region.z1 < z0 or region.z0 > z1:
                    unmet.append(f"{entity} is outside the workspace of {body.id}")
            elif e is None:
                unmet.append(f"{entity}: no estimate to check reach")
            elif not body.reaches(e.x, e.z, belief.margin):
                unmet.append(f"{entity} at ({e.x:.3f}, {e.z:.3f}) is outside the workspace of {body.id}")
    return unmet


# The execution lifecycle. Terminal states are the outcomes.
TRANSITIONS: Mapping[State, frozenset[State]] = {
    State.PENDING: frozenset({State.RUNNING, State.VERIFYING, State.REJECTED, State.ABORTED}),
    State.RUNNING: frozenset({State.VERIFYING, State.FAILED, State.ABORTED}),
    State.VERIFYING: frozenset({State.SUCCEEDED, State.FAILED, State.UNVERIFIED, State.ABORTED}),
}


class Execution:
    """One attempt at one goal. Its state machine raises on an illegal transition."""

    def __init__(self, execution_id: str, request: GoalRequest, requested: Ns, belief: Json) -> None:
        self.id = execution_id
        self.request = request
        self.state = State.PENDING
        self.category: Category | None = None
        self.reason = ""
        self.requested = requested
        self.resolution: Resolution | None = None
        self.policy: S1Policy | None = None
        self.started: Ns | None = None
        self.first_chunk: Ns | None = None
        self.claimed: Ns | None = None
        self.ended: Ns | None = None
        self.report: Report | None = None
        self.verdict: Verdict | None = None
        self.evidence_t: list[Ns] = []
        self.not_before: Ns = 0
        self.verify_until: Ns = 0
        self.attempts = 0
        self.verified_version = -1
        self.belief_initial = belief
        self.belief_final: Json = {}
        self.counters_start: S0Counters | None = None
        self.counters = S0Counters()
        self.wall: Counter[str] = Counter()
        self.fallbacks: list[Json] = []  # every installation: slot, kind, source, window, duration
        self.base: FallbackSpec | None = None  # the capability's default, as installed
        self.base_key: Hashable = None  # its generator's motion key then
        self.s0_started: Ns | None = None  # S0's time when the execution started, for its coverage
        self.s0_stopped: Ns | None = None
        self.base_error: str | None = None  # the default fallback generator's last error, recorded once
        self.s2_lost = False  # S2's link was lost while it was active
        self.s0_fault = False

    def transition(self, to: State, category: Category | None = None, reason: str = "") -> None:
        if to not in TRANSITIONS.get(self.state, frozenset()):
            raise LifecycleError(f"execution {self.id}: {self.state} -> {to} is not a transition")
        if to in TERMINAL:
            allowed = OUTCOME_CATEGORIES[to]
            if (category is None) != (not allowed) or (category is not None and category not in allowed):
                raise LifecycleError(f"execution {self.id}: outcome {to} with category {category}")
        elif category is not None:
            raise LifecycleError(f"execution {self.id}: {to} is not an outcome, it takes no category")
        self.state, self.category = to, category
        if reason:
            self.reason = reason


class Runtime:
    """Stepped by the scheduler at the supervisor rate. The public API is
    `submit` (messages in) and `poll` (updates out); both are JSON-compatible."""

    def __init__(
        self,
        *,
        body: BodySpec,
        capabilities: Sequence[Capability],
        bindings: Sequence[Binding],
        implementations: Mapping[str, Callable[[], S1Policy]],
        params: Params,
        belief: WorldState,
        s0: S0,
        s1_host: S1Host,
        observations: Mailbox[Observation],
        observation_filter: ObservationFilter,
        verifiers: Mapping[str, Verifier],
        trace: TraceWriter,
        run_id: str,
        config_hash: str,
        seed: int,
        default_fallbacks: Mapping[str, FallbackGenerator] | None = None,
    ) -> None:
        self._defaults = default_fallbacks or {}
        for cap in capabilities:
            if cap.default_fallback is not None and cap.default_fallback not in self._defaults:
                raise ConfigError(f"capability {cap.id} names default fallback {cap.default_fallback}, which does not exist")
        for b in bindings:
            if b.implementation not in implementations:
                raise ConfigError(f"binding of {b.capability} names implementation {b.implementation}, which does not exist")
        for cap in capabilities:
            if cap.achieves not in verifiers:
                raise ConfigError(f"capability {cap.id} achieves {cap.achieves}, which has no verifier")
            for kind, param in cap.preconditions:
                if kind not in ("observed", "reachable") or param not in cap.params:
                    raise ConfigError(f"capability {cap.id}: unknown precondition {kind} {param}")
        self.body = body
        self._capabilities = capabilities
        self._bindings = bindings
        self._implementations = implementations
        self.params = params
        self.belief = belief
        self._s0 = s0
        self._s1 = s1_host
        self._observations = observations
        self._filter = observation_filter
        self._verifiers = verifiers
        self._trace = trace
        self._run_id = run_id
        self._config_hash = config_hash
        self._seed = seed
        self._inbox: Mailbox[Message] = Mailbox()
        self._requests: dict[str, GoalRequest] = {}
        self._by_request: dict[str, Execution] = {}
        self._last_update: dict[str, Update] = {}  # per request_id
        self._outbox: list[Update] = []
        self._queue: deque[Execution] = deque()
        self._active: Execution | None = None
        self._count = 0
        self._recent: dict[str, tuple[Observation, tuple[str, ...]]] = {}
        self._status: S0Status | None = None
        self._mode: S0Mode | None = None
        self._chunk: ChunkMeta | None = None
        self._rejection: Json | None = None
        self.executions: dict[str, Execution] = {}
        self.finished: list[Json] = []  # summaries, in the order executions ended
        # S2's liveness, watched from its first heartbeat to its final one.
        self._s2_watch = False
        self._s2_seen: Ns | None = None  # when the last heartbeat was handled
        self._s2_lost: Ns | None = None  # when the current outage was declared
        self._s2_heartbeats = 0
        self._s2_max_gap: Ns = 0
        self._s2_rtt: list[tuple[Ns, Ns]] = []  # (when reported, round trip S2 measured)
        self.s2_outages: list[Json] = []

    # Public API.

    def submit(self, message: Message) -> None:
        """Queue a message for the next step. A GoalRequest's execution is named
        then, in an Update carrying its request_id. A GoalRequest is idempotent
        by request_id: the same request again (a client resending after a lost
        connection) creates no second execution, and its latest Update comes
        again. Raises ContractError for anything but the API's messages, a
        message that is not JSON (it could not be traced), or a request_id
        already used for another request."""
        if not isinstance(message, GoalRequest | InstallFallback | Cancel | Decision | Heartbeat):
            raise ContractError(f"not a runtime message: {type(message).__name__}")
        try:
            dumps(message.to_json())
        except (TypeError, ValueError) as e:
            raise ContractError(f"{type(message).__name__} is not JSON-serializable: {e}") from e
        if isinstance(message, GoalRequest):
            known = self._requests.setdefault(message.request_id, message)
            if known.to_json() != message.to_json():
                raise ContractError(f"request_id {message.request_id} already names another request in this run")
        self._inbox.put(message)

    def poll(self) -> list[Update]:
        updates, self._outbox = self._outbox, []
        return updates

    def idle(self) -> bool:
        """Nothing running, nothing queued, no message waiting."""
        return self._active is None and not self._queue and not self._inbox

    def s2_link(self, now: Ns) -> Json | None:
        """S2's liveness so far: heartbeats, the round trips S2 reported, the
        largest gap between heartbeats, and every outage with its class. None
        if S2 never sent a heartbeat (it runs in this process)."""
        if self._s2_seen is None:
            return None
        outages = list(self.s2_outages)
        if self._s2_lost is not None:
            outages.append(self._outage(self._s2_seen, None, now - self._s2_seen))
        return {
            "deadlines_ns": {"lost": self.params.s2_lost, "long": self.params.s2_long},
            "heartbeats": self._s2_heartbeats,
            "max_gap_ns": self._s2_max_gap,
            "rtt_ns": distribution([rtt for _, rtt in self._s2_rtt]),
            "rtt_samples": [[t, rtt] for t, rtt in self._s2_rtt],
            "outages": outages,
        }

    # The step: observations, S0 status, messages, then supervision.

    def step(self, now: Ns) -> None:
        for observation in self._observations.drain():
            self._observe(observation, now)
        self._sample(now)
        for message in self._inbox.drain():
            self._handle(message, now)
        if self._s2_watch and self._s2_lost is None and self._s2_seen is not None and now - self._s2_seen > self.params.s2_lost:
            self._s2_lost = now
            if self._active is not None:
                self._active.s2_lost = True
            self._event(now, "s2.link", None, {"state": "lost", "last_heartbeat": self._s2_seen, "silent_ns": now - self._s2_seen})
        if self._active is None and self._queue:
            self._start(self._queue.popleft(), now)
        ex = self._active
        if ex is not None and ex.state is State.RUNNING:
            self._supervise(ex, now)
        if ex is not None and ex.state is State.VERIFYING:
            self._verify(ex, now)

    def close(self, now: Ns, reason: str, **extra: Json) -> None:
        """End the trace: run.end with the reason, S2's liveness if it was
        watched, and any `extra` (such as the loop statistics)."""
        link = self.s2_link(now)
        self._trace.close(now, {"reason": reason} | ({"s2_link": link} if link else {}) | extra)

    def _event(self, now: Ns, kind: str, ex: Execution | None, payload: Json) -> None:
        self._trace.event(now, kind, ex.id if ex else None, payload)

    def _observe(self, observation: Observation, now: Ns) -> None:
        """Through the filter, then into the belief. An observation the filter
        cannot handle is dropped: unfiltered data never reaches the belief."""
        try:
            filtered, changes = self._filter.apply(observation)
            if not isinstance(filtered, Observation) or not isinstance(changes, tuple):
                raise ContractError(f"returned {type(filtered).__name__}, {type(changes).__name__}")
        except Exception as e:  # noqa: BLE001 - a failing filter loses an observation, not the run
            self._event(
                now, "error", self._active, {"category": "extension", "reason": f"filter {self._filter.id}: {type(e).__name__}: {e}"}
            )
            return
        self.belief.update(filtered)
        self._recent[filtered.id] = (filtered, changes)
        if len(self._recent) > 400:
            del self._recent[next(iter(self._recent))]

    def _sample(self, now: Ns) -> None:
        before = self._status.counters if self._status is not None else S0Counters()
        status = self._status = self._s0.status()
        ex = self._active
        c = status.counters
        if c.late_ticks > before.late_ticks:
            self._event(now, "s0.late", ex, {"late_ticks": c.late_ticks, "max_interval_ns": c.max_interval_ns, "s0_t": status.t})
        if (c.limit_clamps, c.guard_replacements) != (before.limit_clamps, before.guard_replacements):
            guard = {"clamps": c.limit_clamps - before.limit_clamps, "replacements": c.guard_replacements - before.guard_replacements}
            self._event(now, "s0.guard", ex, guard | {"s0_t": status.t, "mode": status.mode})
        if status.mode != self._mode:
            self._event(now, "s0.mode", ex, {"from": self._mode, "to": status.mode, "s0_t": status.t})
            self._mode = status.mode
        chunk = status.last_chunk
        if chunk is not None and chunk != self._chunk:
            self._chunk = chunk
            self._event(now, "chunk", ex, chunk.to_json())
        if status.rejection is not None and status.rejection != self._rejection:
            self._rejection = status.rejection
            self._event(now, "chunk.rejected", ex, status.rejection)
        if ex is not None:
            self._event(now, "s0.status", ex, status.to_json())

    def _handle(self, message: Message, now: Ns) -> None:
        if isinstance(message, GoalRequest) and message.request_id in self._by_request:
            ex = self._by_request[message.request_id]
            self._event(now, "request", ex, message.to_json() | {"repeat": True})
            self._outbox.append(self._last_update[message.request_id])
        elif isinstance(message, GoalRequest):
            self._count += 1
            ex = Execution(f"{self._run_id}-x{self._count:02d}", message, now, self.belief.snapshot())
            self.executions[ex.id] = ex
            self._by_request[message.request_id] = ex
            self._event(now, "request", ex, message.to_json())
            self._queue.append(ex)
            self._update(ex, now, None)
        elif isinstance(message, Decision):
            self._event(now, "decision", self.executions.get(message.execution_id), message.to_json())
        elif isinstance(message, Cancel):
            self._cancel(message, now)
        elif isinstance(message, InstallFallback):
            self._install(message, now)
        elif isinstance(message, Heartbeat):
            self._heartbeat(message, now)
        else:
            assert_never(message)

    def _outage(self, start: Ns, end: Ns | None, duration: Ns) -> Json:
        kind = "unrecovered" if end is None else "long" if duration >= self.params.s2_long else "short"
        return {"from": start, "to": end, "duration_ns": duration, "class": kind}

    def _heartbeat(self, hb: Heartbeat, now: Ns) -> None:
        """S2 is alive. A gap longer than the lost deadline was an outage; it
        is classified by its duration against the deadlines when it ends."""
        self._s2_heartbeats += 1
        self._s2_rtt += [(now, rtt) for rtt in hb.rtt]
        if self._s2_seen is None:
            self._event(now, "s2.link", None, {"state": "up", "seq": hb.seq})
        else:
            self._s2_max_gap = max(self._s2_max_gap, now - self._s2_seen)
            if self._s2_lost is not None:
                outage = self._outage(self._s2_seen, now, now - self._s2_seen)
                self.s2_outages.append(outage)
                self._event(now, "s2.link", None, {"state": "recovered", "seq": hb.seq} | outage)
                self._s2_lost = None
        self._s2_seen, self._s2_watch = now, not hb.final
        if hb.final:
            self._event(now, "s2.link", None, {"state": "left", "seq": hb.seq})

    def _set(self, ex: Execution, to: State, now: Ns, category: Category | None = None, reason: str = "") -> None:
        before = ex.state
        ex.transition(to, category, reason)
        self._event(now, "lifecycle", ex, {"from": before, "to": to, "category": category, "reason": reason})
        if to in TERMINAL:
            ex.ended = now
            ex.belief_final = self.belief.snapshot()
            if self._active is ex:
                self._active = None
            summary = self._summary(ex)
            self._trace.execution(summary)
            self.finished.append(summary)
            self._update(ex, now, summary)
        else:
            self._update(ex, now, None)

    def _update(self, ex: Execution, now: Ns, summary: Json | None) -> None:
        update = Update(ex.request.request_id, ex.id, ex.request.task_id, ex.state, now, summary)
        self._last_update[ex.request.request_id] = update
        self._outbox.append(update)

    def _start(self, ex: Execution, now: Ns) -> None:
        self._active = ex
        start = perf_ns()
        res = resolve(ex.request.goal, self.belief, self.body, self._capabilities, self._bindings, now - self.params.freshness)
        ex.wall["resolution_ns"] += perf_ns() - start
        ex.resolution = res
        self._event(
            now,
            "resolution",
            ex,
            {
                "goal": ex.request.goal.to_json(),
                "chosen": res.binding.implementation if res.binding else None,
                "category": res.category,
                "reason": res.reason,
                "candidates": [c.to_json() for c in res.candidates],
                "wall_ns": ex.wall["resolution_ns"],
            },
        )
        if res.satisfied is not None:
            # Nothing to execute; the goal is still verified from observations taken from now on.
            ex.not_before, ex.verify_until = now, now + self.params.verify_budget
            self._set(ex, State.VERIFYING, now, reason=res.reason)
            return
        if res.binding is None:
            assert res.category is not None
            self._set(ex, State.REJECTED, now, res.category, res.reason)
            return
        assert self._status is not None
        ex.started, ex.counters_start, ex.s0_started = now, self._status.counters, self._status.t
        self._set(ex, State.RUNNING, now, reason=res.reason)
        # Fallbacks before S1's first chunk: the previous execution's go, then the
        # capability's default (S0's base), then S2's prediction sent with the goal.
        self._s0.hold()
        self._renew_base(ex, now, "the execution starts")
        if ex.request.fallback is not None:
            self._install_s2(ex, ex.request.fallback.shifted(now), now, "s2 predicted")
        try:
            policy = self._implementations[res.binding.implementation]()
            policy.start(ex.request.goal, self.belief.snapshot(), self.body, ex.request.conditioning)
        except Exception as e:  # noqa: BLE001 - a policy that cannot start is a failed execution, not a crashed runtime
            self._fail(ex, now, Category.IMPLEMENTATION_ERROR, f"{res.binding.implementation} failed to start: {type(e).__name__}: {e}")
            return
        ex.policy = policy
        self._s1.attach(policy)

    def _stop_running(self, ex: Execution, now: Ns, fallback: bool = False) -> None:
        """S1 stops producing: what is left of the execution's chunk is not
        executed. A failed execution engages the fallback S2 installed (S0
        waits under it for S2's decision); otherwise the fallback is dropped
        and S0 holds."""
        self._s1.detach()
        self._s0.hold(fallback)
        self._event(now, "s0.hold", ex, {"reason": f"execution {ex.id} stopped running", "fallback": fallback})
        assert self._status is not None and ex.counters_start is not None
        ex.counters = self._status.counters.since(ex.counters_start)
        ex.s0_stopped = self._status.t

    def _capability(self, ex: Execution) -> Capability | None:
        binding = ex.resolution.binding if ex.resolution else None
        return next((c for c in self._capabilities if binding and c.id == binding.capability), None)

    def _renew_base(self, ex: Execution, now: Ns, reason: str) -> None:
        """The capability's default fallback, from S0's status, installed as
        S0's base fallback when its motion differs from the one installed (at
        the start; after a grasp). The one replaced stays until then."""
        cap = self._capability(ex)
        if cap is None or cap.default_fallback is None or self._status is None or ex.started is None:
            return
        generator = self._defaults[cap.default_fallback]
        try:
            key = generator.motion_key(self.body, self._status)
            if ex.base is not None and key == ex.base_key:
                return  # the same motion: nothing to build (asked at every step, so it must stay cheap)
            spec = generator.fallback(self.body, self._status, ex.started, ex.started + BASE_WINDOW)
            if not isinstance(spec, FallbackSpec):
                raise ContractError(f"returned {type(spec).__name__}, not a FallbackSpec")
            ex.base_key = key
            if ex.base is not None and spec.same_motion(ex.base):
                return
            self._s0.install_fallback(spec, now, base=True)
        except Exception as e:  # noqa: BLE001 - a failing generator keeps the base fallback as it was
            error = f"default fallback {generator.id} of {cap.id}: {type(e).__name__}: {e}"
            if error != ex.base_error:
                ex.base_error = error
                self._event(now, "error", ex, {"category": "extension", "reason": error})
            return
        ex.base = spec
        self._installed(ex, spec, now, "base", "capability default", {"capability": cap.id, "reason": reason})

    def _install_s2(self, ex: Execution, spec: FallbackSpec, now: Ns, kind: str) -> None:
        """S2's fallback, predicted with the goal or refined, into S0's S2 slot."""
        reason = ""
        if spec.action_space != self.body.action_space or spec.dims != self.body.dims:
            reason = f"fallback for {spec.action_space} {list(spec.dims)}, body {self.body.id} is {self.body.action_space}"
        else:
            try:
                self._s0.install_fallback(spec, now)
            except ContractError as e:
                reason = str(e)
        if reason:
            self._event(now, "fallback.rejected", ex, {"reason": reason, "source": spec.source, "kind": kind})
            return
        self._installed(ex, spec, now, "s2", kind, {})

    def _installed(self, ex: Execution, spec: FallbackSpec, now: Ns, slot: str, kind: str, extra: Json) -> None:
        record = {"t": now, "slot": slot, "kind": kind, "source": spec.source, "valid_from": spec.valid_from}
        ex.fallbacks.append(record | {"valid_until": spec.valid_until, "lasts_ns": spec.lasts, "duration_ns": spec.duration})
        self._event(now, "fallback.install", ex, spec.to_json() | {"slot": slot, "kind": kind} | extra)

    def _fail(self, ex: Execution, now: Ns, category: Category, reason: str) -> None:
        if ex.state is State.RUNNING:
            self._stop_running(ex, now, fallback=True)
        self._event(now, "error", ex, {"category": category, "reason": reason})
        self._set(ex, State.FAILED, now, category, reason)

    def _supervise(self, ex: Execution, now: Ns) -> None:
        status, policy = self._status, ex.policy
        assert status is not None and policy is not None and ex.started is not None
        self._renew_base(ex, now, "the grasp changed")
        chunk = status.last_chunk
        # S1 is attached after S0's tick at `started`: a chunk received then belongs to the execution before.
        last = chunk.received if chunk is not None and chunk.received > ex.started else None
        if last is not None and ex.first_chunk is None:
            ex.first_chunk = last
        try:
            report = policy.report()
        except Exception as e:  # noqa: BLE001 - see _start
            self._fail(ex, now, Category.IMPLEMENTATION_ERROR, f"report() raised {type(e).__name__}: {e}")
            return
        silent = now - (last if last is not None else ex.started)
        if report is None and (error := self._s1.error()) is not None:
            self._fail(ex, now, Category.IMPLEMENTATION_ERROR, f"S1 raised {error}")
        elif report is not None:
            ex.report, ex.claimed = report, now
            if report.claim is Claim.GAVE_UP:
                self._fail(ex, now, Category.IMPLEMENTATION_ERROR, f"the executor gave up: {report.reason}")
                return
            self._stop_running(ex, now)
            ex.not_before = now + self.params.settle
            ex.verify_until = ex.not_before + self.params.verify_budget
            self._set(ex, State.VERIFYING, now, reason=f"executor claims DONE: {report.reason}")
        elif status.mode is S0Mode.FAULT:
            ex.s0_fault = True
            self._fail(ex, now, Category.IMPLEMENTATION_ERROR, "S0 reports a fault")
        elif silent > self.params.s1_stall_timeout:
            self._fail(ex, now, Category.S1_STALL, f"no chunk reached S0 for {seconds(silent):.2f} s; S0 is in {status.mode}")
        elif now - ex.started > self.params.deadline:
            self._fail(ex, now, Category.TIMEOUT, f"still running after {seconds(self.params.deadline):.1f} s")

    def _verify(self, ex: Execution, now: Ns) -> None:
        if now < ex.not_before or (self.belief.version == ex.verified_version and now < ex.verify_until):
            return
        verifier = self._verifiers.get(ex.request.goal.predicate)
        if verifier is None:
            verdict = Verdict(VerdictKind.INCONCLUSIVE, (), f"no verifier for {ex.request.goal.predicate}")
        else:
            start = perf_ns()
            try:
                verdict = verifier.verify(ex.request.goal, self.belief, ex.not_before)
                if not isinstance(verdict, Verdict):
                    raise ContractError(f"returned {type(verdict).__name__}, not a Verdict")
            except Exception as err:  # noqa: BLE001 - a failing verifier gives no verdict, never a success
                reason = f"verifier {verifier.id}: {type(err).__name__}: {err}"
                self._event(now, "error", ex, {"category": "extension", "reason": reason})
                verdict = Verdict(VerdictKind.INCONCLUSIVE, (), reason)
            ex.wall["verification_ns"] += perf_ns() - start
        ex.attempts += 1
        ex.verified_version = self.belief.version
        # Independent of the verifier: a decisive verdict must rest on known
        # observations taken at or after not_before.
        evidence = [self._recent.get(i) for i in verdict.evidence]
        if verdict.kind is not VerdictKind.INCONCLUSIVE and (not evidence or any(e is None or e[0].t < ex.not_before for e in evidence)):
            verdict = Verdict(VerdictKind.INCONCLUSIVE, verdict.evidence, f"evidence not fresh: {verdict.reason}")
        self._event(now, "verify.attempt", ex, {"attempt": ex.attempts, "verdict": verdict.to_json()})
        if verdict.kind is VerdictKind.INCONCLUSIVE and now < ex.verify_until:
            return
        ex.verdict = verdict
        ex.evidence_t = [e[0].t for e in evidence if e is not None]
        for e in evidence:
            if e is not None:
                self._event(now, "verify.evidence", ex, {"observation": e[0].to_json(), "filter_changes": list(e[1])})
        self._event(now, "verdict", ex, verdict.to_json() | {"not_before": ex.not_before, "attempts": ex.attempts})
        if verdict.kind is VerdictKind.SATISFIED:
            self._set(ex, State.SUCCEEDED, now, reason=verdict.reason)
        elif verdict.kind is VerdictKind.VIOLATED:
            self._set(ex, State.FAILED, now, Category.VERIFICATION_FAILED, verdict.reason)
        else:
            self._set(ex, State.UNVERIFIED, now, Category.VERIFICATION_INCONCLUSIVE, verdict.reason)

    def _cancel(self, message: Cancel, now: Ns) -> None:
        ex = self.executions.get(message.execution_id)
        self._event(now, "cancel", ex, message.to_json() | {"state": ex.state if ex else None})
        if ex is None or ex.state in TERMINAL:
            return
        if ex.state is State.PENDING and ex in self._queue:
            self._queue.remove(ex)
        if ex.state is State.RUNNING:
            self._stop_running(ex, now)
        self._set(ex, State.ABORTED, now, Category.CANCELLED, "cancelled by request")

    def _install(self, message: InstallFallback, now: Ns) -> None:
        ex = self.executions.get(message.execution_id)
        if ex is None or ex.state is not State.RUNNING:
            reason = f"execution {message.execution_id} is {ex.state if ex else 'unknown'}, not RUNNING"
            self._event(now, "fallback.rejected", ex, {"reason": reason, "source": message.spec.source, "kind": "s2 refined"})
            return
        self._install_s2(ex, message.spec, now, "s2 refined")

    def _coverage(self, ex: Execution) -> float | None:
        """The fraction of the time the execution ran (from its start to S1's
        claim or its failure, on S0's clock) during which S0 held a valid
        fallback: S0's own count, tick by tick. None if it never ran."""
        if ex.s0_started is None or ex.s0_stopped is None or ex.s0_stopped <= ex.s0_started:
            return None
        return ex.counters.fallback_cover_ns / (ex.s0_stopped - ex.s0_started)

    def _attribution(self, ex: Execution) -> tuple[Json | None, Json | None]:
        """S0's fidelity to the intents of the execution, and the attribution of its failure (attribution.py)."""
        if ex.started is None:
            return None, None
        fid = fidelity(ex.counters, self.params.fidelity)
        assert ex.ended is not None
        entities = set(ex.request.goal.args)
        seen = [c for c in self.belief.contradictions if c["entity"] in entities and ex.requested <= c["t"] <= ex.ended]
        blamed = attribute(ex.state, ex.category, ex.reason, fid, ex.counters, seen, ex.s2_lost, ex.s0_fault)
        return fid, blamed

    def _summary(self, ex: Execution) -> Json:
        res, b = ex.resolution, ex.resolution.binding if ex.resolution else None
        assert ex.ended is not None
        fid, blamed = self._attribution(ex)
        verdict = None
        if ex.verdict is not None:
            verdict = ex.verdict.to_json() | {"evidence_t": ex.evidence_t, "not_before": ex.not_before, "attempts": ex.attempts}
        return {
            "execution_id": ex.id,
            "request_id": ex.request.request_id,
            "task_id": ex.request.task_id,
            "retry_of": ex.request.retry_of,
            "goal": ex.request.goal.to_json(),
            "conditioning": dict(ex.request.conditioning),
            "capability": b.capability if b else None,
            "implementation": b.implementation if b else None,
            "implementation_version": b.version if b else None,
            "candidates": [c.to_json() for c in res.candidates] if res else [],
            "resolution": res.reason if res else "not resolved",
            "body": self.body.id,
            "action_space": self.body.action_space,
            "belief_initial": ex.belief_initial,
            "belief_final": ex.belief_final,
            "claim": ex.report.to_json() | {"t": ex.claimed} if ex.report else None,
            "verdict": verdict,
            "outcome": ex.state,
            "category": ex.category,
            "reason": ex.reason,
            "timing": {
                "requested": ex.requested,
                "started": ex.started,
                "first_chunk": ex.first_chunk,
                "time_to_first_chunk_ns": ex.first_chunk - ex.started if ex.first_chunk and ex.started is not None else None,
                "claimed": ex.claimed,
                "ended": ex.ended,
                "duration_ns": ex.ended - ex.requested,
                "fallback_ns": ex.counters.fallback_ns,
                "hold_ns": ex.counters.hold_ns,
            },
            "wall": {
                "resolution_ns": ex.wall["resolution_ns"],
                "verification_ns": ex.wall["verification_ns"],
                "trace_write_ns": self._trace.cost[ex.id],
            },
            "s0": ex.counters.to_json(),
            "fallbacks": ex.fallbacks,
            "fallback_coverage": self._coverage(ex),
            "fidelity": fid,
            "attribution": blamed,
            "config_hash": self._config_hash,
            "seed": self._seed,
        }
