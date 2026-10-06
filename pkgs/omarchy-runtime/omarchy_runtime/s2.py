"""The scripted S2: slow reasoning, a client of the runtime's public API only.

It works through a list of goals, one task each. Every decision it takes
(request a goal, install a fallback, retry or give up) is sent only after its
latency model's delay, on the clock its scheduler injects. It may pre-install
a fallback for each execution: the body's safe pose with the gripper closed.
After a failure it asks its recovery policy what to do.

It uses the API asynchronously, in the same way in the runtime's process and
in another one: it names each request, and learns the execution from the
Updates that carry that name. Times it sends (a fallback's window) are on the
runtime's clock, taken from those Updates, never on its own.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from omarchy_runtime.contracts import (
    BodySpec,
    Decision,
    FallbackSpec,
    GiveUp,
    Goal,
    GoalRequest,
    InstallFallback,
    LatencyModel,
    Message,
    Ns,
    RecoveryPolicy,
    Retry,
    RuntimeApi,
    State,
    Update,
    ns,
)

FALLBACK_DURATION = ns(1.5)  # time to reach the safe pose from wherever S0 is
FALLBACK_WINDOW = ns(60.0)


class ScriptedS2:
    def __init__(
        self,
        api: RuntimeApi,
        goals: Sequence[Goal],
        body: BodySpec,
        recovery: RecoveryPolicy,
        latency: LatencyModel,
        rng: np.random.Generator,
        *,
        fallback: bool,
        task_prefix: str,
    ) -> None:
        self.api = api
        self.body = body
        self.recovery = recovery
        self.latency = latency
        self.rng = rng
        self.fallback = fallback
        self.task_prefix = task_prefix
        self.goals = deque(goals)
        self.tasks = 0
        self.requests = 0
        self.waiting: str | None = None  # the request S2 is waiting for
        self.decided: list[tuple[Ns, Message]] = []  # decisions not sent yet, with the time they are ready
        self.goal: Goal | None = None
        self.task: str | None = None
        self.done = False

    def _decide(self, now: Ns, *messages: Message) -> Ns:
        """Queue the messages of one decision; they are sent when it is ready."""
        ready = now + self.latency.sample(self.rng)
        self.decided.extend((ready, m) for m in messages)
        return ready

    def step(self, now: Ns) -> None:
        for update in self.api.poll():
            self._see(update, now)
        ready = [m for t, m in self.decided if t <= now]
        self.decided = [(t, m) for t, m in self.decided if t > now]
        for message in ready:
            self.api.submit(message)
            if isinstance(message, GoalRequest):
                self.waiting = message.request_id
        if self.waiting is None and not self.decided:
            if self.goals:
                self.tasks += 1
                self.goal, self.task = self.goals.popleft(), f"{self.task_prefix}-t{self.tasks:02d}"
                self._decide(now, self._request(self.goal))
            else:
                self.done = True

    def _request(self, goal: Goal, retry_of: str | None = None, conditioning: Mapping[str, Any] | None = None) -> GoalRequest:
        assert self.task is not None
        self.requests += 1
        return GoalRequest(goal, self.task, f"{self.task}-r{self.requests:02d}", retry_of, conditioning or {})

    def _see(self, update: Update, now: Ns) -> None:
        if update.request_id != self.waiting:
            return
        if update.state is State.RUNNING and self.fallback:
            # The window opens when the execution starts running, on the runtime's clock.
            b = self.body
            spec = FallbackSpec.make(
                b.action_space, b.dims, FALLBACK_DURATION, [b.safe_pose], update.t, update.t + FALLBACK_WINDOW, "s2.scripted"
            )
            self._decide(now, InstallFallback(update.execution_id, spec))
        if update.summary is None:
            return
        self.waiting = None
        if update.state is State.SUCCEEDED:
            return
        assert self.goal is not None and self.task is not None
        summary = update.summary
        policy = f"{self.recovery.id}@{self.recovery.version}"
        try:
            choice = self.recovery.decide(self.goal, summary, summary["belief_final"])
            if not isinstance(choice, Retry | GiveUp):
                raise TypeError(f"returned {type(choice).__name__}")
        except Exception as e:  # noqa: BLE001 - a failing recovery policy means giving up, recorded as such
            choice = GiveUp(f"recovery policy {policy} failed: {type(e).__name__}: {e}")
        if isinstance(choice, GiveUp):
            self._decide(now, Decision(self.task, update.execution_id, "give_up", choice.reason, policy))
        else:
            self._decide(
                now,
                Decision(self.task, update.execution_id, "retry", choice.reason, policy),
                self._request(choice.goal, update.execution_id, choice.conditioning),
            )
