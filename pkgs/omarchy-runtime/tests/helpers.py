"""Shared test fixtures: the configured scene and bodies, beliefs built from
hand-made observations, and scenario runs cached for the whole test run."""

from __future__ import annotations

import atexit
import functools
import tempfile
from pathlib import Path

from omarchy_runtime import config
from omarchy_runtime.belief import WorldState
from omarchy_runtime.contracts import BodySpec, Detection, Goal, Observation, Proprioception, ns
from omarchy_runtime.scenario import RunResult, run

TMP = tempfile.TemporaryDirectory()
atexit.register(TMP.cleanup)  # after every test module: runs are shared between them

ARM_CFG = config.effective("nominal_a")
GANTRY_CFG = config.effective("nominal_b")
SCENE = config.scene(ARM_CFG)
ARM: BodySpec = config.body(ARM_CFG)
GANTRY: BodySpec = config.body(GANTRY_CFG)
IN_RACK = Goal("in", ("cup_1", "drying_rack"))
RACK_SPOT = (0.30, 0.80)  # a cup resting in the drying rack
TABLE_SPOT = (0.78, 0.80)  # cup_1's place on the table


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(dir=TMP.name))


def observation(
    n: int, t_s: float, cups: dict[str, tuple[float, float]], *, gripper: float = 1.0, ee: tuple[float, float] = (0.6, 1.05)
) -> Observation:
    """An observation of the gantry at `ee` with its gripper at `gripper`, seeing `cups`."""
    detections = tuple(Detection(name, x, z, 0.95) for name, (x, z) in sorted(cups.items()))
    proprio = Proprioception(GANTRY.dims, (ee[0], ee[1], gripper), ee)
    return Observation(f"obs-{n}", ns(t_s), "test", detections, proprio)


def belief(*observations: Observation) -> WorldState:
    b = WorldState(SCENE, margin=0.01, min_confidence=0.5, grasp_radius=0.03)
    for o in observations:
        b.update(o)
    return b


@functools.cache
def scenario(name: str) -> RunResult:
    """Each scenario runs once per test process, with its S0 record written."""
    return run(name, tmpdir() / name, record_s0=True)
