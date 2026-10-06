"""One run of one scenario: the composition root.

It builds the simulated world, S0, S1, the sensor, the runtime and S2 from
the effective configuration, steps them on simulated time (the default, and
deterministic) or on the wall clock (soft real-time), and writes the run
directory. The ground truth it returns is for the evaluation harness only;
the runtime never sees it.

S2 runs in the robot's process, or in its own (serve_s2), reached over the
link: the robot process then keeps the runtime, S1, S0, the world and the
sensor, and the S2 process the scripted S2, its recovery policy and its
latency model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from omarchy_runtime import config, link
from omarchy_runtime.belief import WorldState
from omarchy_runtime.contracts import (
    SECOND,
    BodySpec,
    ConfigError,
    Goal,
    Json,
    LatencyModel,
    Ns,
    Observation,
    OmarchyError,
    RecoveryPolicy,
    ns,
)
from omarchy_runtime.extensions import FILTERS, LATENCY, RECOVERY, VERIFIERS, describe
from omarchy_runtime.runtime import Runtime
from omarchy_runtime.s2 import ScriptedS2
from omarchy_runtime.sched import Latest, Mailbox, Scheduler, WallScheduler, distribution
from omarchy_runtime.sim.s0 import SimS0
from omarchy_runtime.sim.s1 import ScriptedPickPlace, SimS1Host, Stall
from omarchy_runtime.sim.sensor import Occlusion, Sensor
from omarchy_runtime.sim.world import Trigger, World
from omarchy_runtime.trace import S0_MODES, TraceWriter, os_identity, runtime_identity, where_measured
from omarchy_runtime.wallclock import stamp

TRIGGERS = ("start", "grasp", "release")
S2_TIMEOUT = ns(120.0)  # how long the robot waits for an S2 process to answer


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
    clock: str  # "simulated" or "wall"
    loop: Json | None  # the wall-clock scheduler's loop statistics, with their label
    s2: Json  # where S2 ran: in-process, or remote with its address and hello
    s2_link: Json | None  # S2's liveness as the runtime's supervisor saw it (remote S2 only)
    tags: Mapping[str, str]

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


def s2_parts(cfg: Json, ttft: float | None = None, spread: float = 0.0) -> tuple[RecoveryPolicy, LatencyModel, Json]:
    """S2's recovery policy and latency model, and how traces name them. The
    latency is the scenario's unless `ttft` gives another median time to
    first token: fixed, or lognormal with a nonzero `spread`."""
    s, ext = cfg["scenario"]["s2"], cfg["runtime"]["extensions"]
    if ttft is None:
        model, ttft, spread = ext["s2_latency"], config.get(s, "latency_s", float, "scenario s2"), float(s.get("latency_spread", 0.0))
    else:
        model = "fixed" if spread == 0 else "lognormal"
    try:
        recovery, latency = RECOVERY[ext["recovery"]](), LATENCY[model](ttft, spread)
    except KeyError as e:
        raise ConfigError(f"extensions: no implementation with id {e}") from e
    return recovery, latency, {"recovery": describe(recovery), "s2_latency": describe(latency) | {"seconds": ttft, "spread": spread}}


def _connect_s2(where: tuple[str, int], timeout: Ns, robot_hello: Json) -> tuple[link.TcpTransport, Json]:
    transport = link.connect(*where, timeout)
    try:
        transport.send(robot_hello)
        return transport, link.check_hello(transport.wait("hello", timeout), "s2")
    except OmarchyError:
        transport.close()
        raise


def run(
    name: str,
    run_dir: Path,
    *,
    record_s0: bool = False,
    clock: str = "simulated",
    s0_hz: int | None = None,
    s2: tuple[str, int] | None = None,
    s2_timeout: Ns = S2_TIMEOUT,
    tags: Mapping[str, str] | None = None,
) -> RunResult:
    """Run one scenario. `clock` is "simulated" or "wall"; `s0_hz` replaces
    S0's configured rate; `s2` is the address of an S2 process (omarchy-runtime
    s2) to connect to, waiting up to `s2_timeout`, instead of S2 in this
    process. `tags` go into the trace header as they are."""
    if clock not in ("simulated", "wall"):
        raise ConfigError(f"clock {clock!r} is not simulated or wall")
    if s2 is not None and clock != "wall":
        raise ConfigError("an S2 in another process runs on the wall clock: add --clock wall")
    cfg = config.effective(name)
    if s0_hz is not None:
        if s0_hz <= 0 or SECOND % s0_hz:
            raise ConfigError(f"S0 rate {s0_hz} Hz: the period must be a whole number of nanoseconds")
        cfg["world"]["rates_hz"]["s0"] = s0_hz
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
    except KeyError as e:
        raise ConfigError(f"extensions: no implementation with id {e}") from e
    recovery, latency, s2_named = s2_parts(cfg)
    goals = config.goals(s["s2"], "scenario s2")
    os_build, os_note = os_identity()
    placement: Json = {"placement": "in-process"}
    transport = None
    if s2 is not None:
        robot_hello = link.hello(
            "robot",
            run_id=run_id,
            scenario=s["name"],
            seed=seed,
            config_hash=digest,
            runtime=runtime_identity(),
            os=os_build,
            body=body.to_json(),
            heartbeat_s=config.get(cfg["runtime"]["s2_link"], "heartbeat_s", float, "s2_link"),
        )
        transport, peer = _connect_s2(s2, s2_timeout, robot_hello)
        placement = {"placement": "remote", "address": f"{s2[0]}:{s2[1]}", "hello": peer}
        s2_named = {k: peer[k] for k in ("recovery", "s2_latency")}  # S2's own, as it said
    sched = Scheduler(SECOND // rates["s0"]) if clock == "simulated" else WallScheduler()
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
        }
        | s2_named,
        "s2": placement,
        "tags": dict(tags or {}),
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
    s2_done: Callable[[], bool]
    robot_link = None
    if transport is None:
        scripted = ScriptedS2(
            runtime,
            goals,
            body,
            recovery,
            latency,
            np.random.default_rng([seed, 3]),
            fallback=bool(s["s2"].get("fallback", False)),
            task_prefix=run_id,
        )
        sched.add("s2", SECOND // rates["s2"], scripted.step)
        s2_done = lambda: scripted.done  # noqa: E731
    else:
        # S0's rate: a heartbeat's echo waits at most one S0 period.
        robot_link = link.RobotLink(transport, runtime, trace)
        sched.add("link", SECOND // rates["s0"], robot_link.step)
        s2_done = lambda: robot_link.s2_done  # noqa: E731
    sched.add("harness", supervisor, harness)
    trace.event(
        0, "run.start", None, {"scenario": s["name"], "seed": seed, "goals": [g.to_json() for g in goals], "s2": placement["placement"]}
    )
    completed = sched.run(lambda: s2_done() and runtime.idle(), until=limit, check_every=supervisor)
    loop = None
    if isinstance(sched, WallScheduler):
        loop = {"label": f"soft real-time, Python, {where_measured()}", "components": sched.stats()}
    s2_link = runtime.s2_link(sched.now)
    runtime.close(sched.now, "task complete" if completed else "time limit", **({"loop": loop} if loop else {}))
    if robot_link is not None:
        robot_link.close()
    result = RunResult(
        s["name"],
        run_dir,
        cfg,
        runtime.finished,
        truths,
        world,
        s0,
        completed,
        sched.now,
        sched.clock,
        loop,
        placement,
        s2_link,
        dict(tags or {}),
    )
    if record_s0:
        for summary in runtime.finished:
            timing = summary["timing"]
            rec = result.record(timing["requested"], timing["ended"])
            np.savez(run_dir / f"s0_{summary['execution_id']}.npz", **rec, dims=np.array(body.dims), modes=np.array(S0_MODES))
    return result


def serve_s2(where: tuple[str, int], *, ttft: float | None, spread: float, timeout: Ns, announce: bool = False) -> Json:
    """The S2 process: accept one robot at `where` (waiting up to `timeout`),
    run the scripted S2 for the robot's scenario on the wall clock, with the
    scenario's latency or a time to first token `ttft` (fixed, or lognormal
    with `spread`), then leave and wait for the robot to end the run.

    Returns S2's own view: whether it finished its goals, why the link
    closed, what the robot refused, its round trips (heartbeat echo, S2's
    clock) and its loop statistics."""
    transport = link.listen(*where, timeout, announce)
    try:
        robot = link.check_hello(transport.wait("hello", timeout), "robot")
        cfg = config.effective(config.get(robot, "scenario", str, "robot hello"))
        recovery, latency, named = s2_parts(cfg, ttft, spread)
        body = BodySpec.from_json(robot["body"])
        seed = config.get(robot, "seed", int, "robot hello")
        run_id = config.get(robot, "run_id", str, "robot hello")
        heartbeat = ns(config.get(robot, "heartbeat_s", float, "robot hello"))
        transport.send(link.hello("s2", runtime=runtime_identity(), **named))
    except OmarchyError as e:
        transport.send({"type": "error", "reason": str(e)})
        transport.close()
        raise
    s = cfg["scenario"]
    api = link.RemoteRuntime(transport, heartbeat)
    s2 = ScriptedS2(
        api,
        config.goals(s["s2"], "scenario s2"),
        body,
        recovery,
        latency,
        np.random.default_rng([seed, 3]),
        fallback=bool(s["s2"].get("fallback", False)),
        task_prefix=run_id,
    )
    sched = WallScheduler()
    sched.add("link", ns(0.001), api.step)
    sched.add("s2", SECOND // cfg["world"]["rates_hz"]["s2"], s2.step)
    limit = ns(float(s.get("limit_s", 60.0)))
    sched.run(lambda: s2.done or api.closed is not None, until=limit, check_every=ns(0.01))
    api.leave(sched.now)
    # The robot ends the run once its last execution is over, and closes the link.
    sched.run(lambda: api.closed is not None, until=sched.now + ns(30.0), check_every=ns(0.01))
    transport.close()
    return {
        "scenario": s["name"],
        "run_id": run_id,
        "done": s2.done,
        "closed": api.closed,
        "refused": api.errors,
        "rtt_ns": distribution(api.rtt),
        "rtt_label": "heartbeat echo on S2's monotonic clock: the network both ways plus up to one loop period at each end",
        "loop": {"label": f"soft real-time, Python, {where_measured()}", "components": sched.stats()},
    }
