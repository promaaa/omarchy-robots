"""The only module that reads the wall clock or sleeps.

It serves overhead measurements (resolution, verification and trace-writing
cost), the wall-time stamps of trace events, and the wall-clock scheduler
(sched.WallScheduler). Control logic never calls it: components get `now`
from the scheduler.
"""

from __future__ import annotations

import time


def wall_ns() -> int:
    return time.time_ns()


def perf_ns() -> int:
    return time.perf_counter_ns()


def stamp() -> str:
    """UTC time for run directory names, like 20261004T031500Z."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def monotonic_ns() -> int:
    return time.monotonic_ns()


def sleep_until(deadline: int) -> None:
    """Sleep until monotonic_ns() reaches `deadline`; at once if it has. The
    OS decides when the process runs again: usually tens of microseconds late."""
    delay = deadline - time.monotonic_ns()
    if delay > 0:
        time.sleep(delay / 1e9)
