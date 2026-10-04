"""S2 in R1: scripted slow reasoning, a client of the runtime's public API only.

It works through a list of goals, one task each. Every decision it takes
(request a goal, install a fallback, retry or give up) is sent only after its
latency model's delay, in simulated time. It may pre-install a fallback for
each execution: the body's safe pose with the gripper closed. After a
failure it asks its recovery policy what to do.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

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
        self.waiting: str | None = None  # the execution S2 is waiting for
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
            execution = self.api.submit(message)
            if isinstance(message, GoalRequest):
                self.waiting = execution
        if self.waiting is None and not self.decided:
            if self.goals:
                self.tasks += 1
                self.goal, self.task = self.goals.popleft(), f"{self.task_prefix}-t{self.tasks:02d}"
                self._decide(now, GoalRequest(self.goal, self.task))
            else:
                self.done = True

    def _see(self, update: Update, now: Ns) -> None:
        if update.execution_id != self.waiting:
            return
        if update.state is State.RUNNING and self.fallback:
            # The fallback is built when the decision is sent: its window starts then.
            ready = now + self.latency.sample(self.rng)
            b = self.body
            spec = FallbackSpec.make(
                b.action_space, b.dims, FALLBACK_DURATION, [b.safe_pose], ready, ready + FALLBACK_WINDOW, "s2.scripted"
            )
            self.decided.append((ready, InstallFallback(update.execution_id, spec)))
        if update.summary is None:
            return
        self.waiting = None
        if update.state is State.SUCCEEDED:
            return
        assert self.goal is not None and self.task is not None
        summary = update.summary
        choice = self.recovery.decide(self.goal, summary, summary["belief_final"])
        policy = f"{self.recovery.id}@{self.recovery.version}"
        if isinstance(choice, GiveUp):
            self._decide(now, Decision(self.task, update.execution_id, "give_up", choice.reason, policy))
        else:
            self._decide(
                now,
                Decision(self.task, update.execution_id, "retry", choice.reason, policy),
                GoalRequest(choice.goal, self.task, update.execution_id, choice.conditioning),
            )
