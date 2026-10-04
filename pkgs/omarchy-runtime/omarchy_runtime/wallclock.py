"""The only module that reads the wall clock.

It serves overhead measurements (resolution, verification and trace-writing
cost) and the wall-time stamps of trace events. Control logic never calls it:
components get `now` from the scheduler.
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
