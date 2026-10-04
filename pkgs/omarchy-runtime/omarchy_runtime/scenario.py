"""One run of one scenario: the composition root.

It builds the simulated world, S0, S1, the sensor, the runtime and S2 from
the effective configuration, steps them on simulated time, and writes the run
directory. The ground truth it returns is for the evaluation harness only;
the runtime never sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime import config
from omarchy_runtime.belief import WorldState
from omarchy_runtime.contracts import SECOND, ConfigError, Goal, Json, Ns, Observation, ns
from omarchy_runtime.extensions import FILTERS, LATENCY, RECOVERY, VERIFIERS, describe
from omarchy_runtime.runtime import Runtime
from omarchy_runtime.s2 import ScriptedS2
from omarchy_runtime.sched import Latest, Mailbox, Scheduler
from omarchy_runtime.sim.s0 import SimS0
from omarchy_runtime.sim.s1 import ScriptedPickPlace, SimS1Host, Stall
from omarchy_runtime.sim.sensor import Occlusion, Sensor
from omarchy_runtime.sim.world import Trigger, World
from omarchy_runtime.trace import S0_MODES, TraceWriter, os_identity, runtime_identity
from omarchy_runtime.wallclock import stamp

TRIGGERS = ("start", "grasp", "release")


@dataclass
class RunResult:
    name: str
    dir: Path
    cfg: Json
    summaries: list[Json]
    truths: list[bool | None]  # ground truth of each execution's goal when it ended
    world: World
    s0: SimS0
    completed: bool  # S2 finished its goals before the time limit
    end: Ns

    def record(self, t0: Ns = 0, t1: Ns | None = None) -> dict[str, Any]:
        """S0's per-tick record between t0 and t1, both included."""
        n = min(self.s0.c["ticks"], len(self.s0.record_t))
        t = self.s0.record_t[:n]
        keep = (t >= t0) & (t <= (self.end if t1 is None else t1))
        return {
            "t": t[keep],
            "mode": self.s0.record_mode[:n][keep],
            "position": self.s0.record_position[:n][keep],
            "desired": self.s0.record_desired[:n][keep],
            "seq": self.s0.record_seq[:n][keep],
            "valid_until": self.s0.record_valid_until[:n][keep],
        }

    def violations(self, t0: Ns = 0, t1: Ns | None = None) -> list[tuple[Ns, str]]:
        end = self.end if t1 is None else t1
        return [(t, what) for t, what in self.world.violations if t0 <= t <= end]


def _faults(cfg: Json) -> tuple[list[Trigger], list[Stall], list[Occlusion]]:
    scene = config.scene(cfg)
    slips, stalls, occlusions = [], [], []
    for n, f in enumerate(cfg["scenario"].get("faults", [])):
        where = f"scenario faults[{n}]"
        after = config.get(f, "after", str, where)
        if after not in TRIGGERS:
            raise ConfigError(f"{where}.after: {after!r} is not one of {', '.join(TRIGGERS)}")
        when = Trigger(after, ns(config.get(f, "delay_s", float, where)), ns(config.get(f, "duration_s", float, where)))
        kind = config.get(f, "kind", str, where)
        if kind == "slip":
            slips.append(when)
        elif kind == "s1_stall":
            stalls.append(Stall(when))
        elif kind == "occlusion":
            region = config.get(f, "region", str, where)
            if region not in scene.regions:
                raise ConfigError(f"{where}.region: no region {region!r}")
            occlusions.append(Occlusion(scene.regions[region], when))
        else:
            raise ConfigError(f"{where}.kind: {kind!r} is not slip, s1_stall or occlusion")
    return slips, stalls, occlusions


def run(name: str, run_dir: Path, *, record_s0: bool = False) -> RunResult:
    cfg = config.effective(name)
    digest = config.digest(cfg)
    s = cfg["scenario"]
    seed = config.get(s, "seed", int, "scenario")
    limit = ns(float(s.get("limit_s", 60.0)))
    rates = cfg["world"]["rates_hz"]
    scene, body, params = config.scene(cfg), config.body(cfg), config.params(cfg)
    verification = cfg["runtime"]["verification"]
    sensor_cfg = cfg["world"]["sensor"]
    ext = cfg["runtime"]["extensions"]
    slips, stalls, occlusions = _faults(cfg)
    run_id = f"{int(np.random.default_rng([seed, 1]).integers(1 << 32)):08x}"

    world = World(scene, {k: v["x"] for k, v in cfg["world"]["objects"].items()}, body, slips)
    s0 = SimS0(body, world, capacity=limit * rates["s0"] // SECOND + 1)
    sensor = Sensor(
        world,
        np.random.default_rng([seed, 2]),
        source=sensor_cfg["source"],
        noise=sensor_cfg["noise_m"],
        confidence=sensor_cfg["confidence"],
        occlusions=occlusions,
    )
    for_s1: Latest[Observation] = Latest()
    for_runtime: Mailbox[Observation] = Mailbox()
    host = SimS1Host(s0, for_s1, ns(config.get(s["s1"], "latency_s", float, "scenario s1")), stalls, world)
    try:
        observation_filter = FILTERS[ext["filter"]]()
        verifiers = {p: VERIFIERS[v]() for p, v in ext["verifiers"].items()}
        recovery = RECOVERY[ext["recovery"]]()
        latency = LATENCY[ext["s2_latency"]](config.get(s["s2"], "latency_s", float, "scenario s2"))
    except KeyError as e:
        raise ConfigError(f"extensions: no implementation with id {e}") from e
    os_build, os_note = os_identity()
    sched = Scheduler(SECOND // rates["s0"])
    header = {
        "runtime": runtime_identity(),
        "os": os_build,
        "os_note": os_note,
        "config_hash": digest,
        "scenario": s["name"],
        "seed": seed,
        "run_id": run_id,
        "clock": sched.clock,
        "body": body.id,
        "rates_hz": rates,
        "extensions": {
            "filter": describe(observation_filter),
            "verifiers": {p: describe(v) for p, v in verifiers.items()},
            "recovery": describe(recovery),
            "s2_latency": describe(latency),
        },
        "config": cfg,
        "wall_created": stamp(),
    }
    trace = TraceWriter(run_dir, header)
    belief = WorldState(
        scene,
        margin=verification["margin_m"],
        min_confidence=verification["min_confidence"],
        grasp_radius=verification["grasp_radius_m"],
    )
    implementations = {b.implementation: (lambda i=b.implementation: ScriptedPickPlace(i)) for b in config.bindings(cfg)}
    runtime = Runtime(
        body=body,
        capabilities=config.capabilities(cfg),
        bindings=config.bindings(cfg),
        implementations=implementations,
        params=params,
        belief=belief,
        s0=s0,
        s1_host=host,
        observations=for_runtime,
        observation_filter=observation_filter,
        verifiers=verifiers,
        trace=trace,
        run_id=run_id,
        config_hash=digest,
        seed=seed,
    )
    s2 = ScriptedS2(
        runtime,
        config.goals(s["s2"], "scenario s2"),
        body,
        recovery,
        latency,
        np.random.default_rng([seed, 3]),
        fallback=bool(s["s2"].get("fallback", False)),
        task_prefix=run_id,
    )
    truths: list[bool | None] = []

    def sense(now: Ns) -> None:
        observation = sensor.observe(now)
        for_s1.put(observation)
        for_runtime.put(observation)

    def control(now: Ns) -> None:
        host.deliver(now)
        s0.tick(now)

    def harness(now: Ns) -> None:
        while len(truths) < len(runtime.finished):
            truths.append(world.truth(Goal.from_json(runtime.finished[len(truths)]["goal"])))

    supervisor = SECOND // rates["supervisor"]
    sched.add("sense", SECOND // rates["observation"], sense)
    sched.add("s1", SECOND // rates["s1"], host.step)
    sched.add("s0", SECOND // rates["s0"], control)
    sched.add("runtime", supervisor, runtime.step)
    sched.add("s2", SECOND // rates["s2"], s2.step)
    sched.add("harness", supervisor, harness)
    trace.event(0, "run.start", None, {"scenario": s["name"], "seed": seed, "goals": [g.to_json() for g in s2.goals]})
    completed = sched.run(lambda: s2.done and runtime.idle(), until=limit, check_every=supervisor)
    runtime.close(sched.now, "task complete" if completed else "time limit")
    result = RunResult(s["name"], run_dir, cfg, runtime.finished, truths, world, s0, completed, sched.now)
    if record_s0:
        for summary in runtime.finished:
            timing = summary["timing"]
            rec = result.record(timing["requested"], timing["ended"])
            np.savez(run_dir / f"s0_{summary['execution_id']}.npz", **rec, dims=np.array(body.dims), modes=np.array(S0_MODES))
    return result
