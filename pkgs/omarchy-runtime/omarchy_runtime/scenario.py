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

import heapq
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from omarchy_runtime import config, link
from omarchy_runtime import latency as latency_files
from omarchy_runtime.belief import Scene, WorldState
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
    S1Policy,
    ns,
)
from omarchy_runtime.controllers import CartesianImpedance
from omarchy_runtime.extensions import CORRECTORS, DEFAULT_FALLBACKS, FILTERS, INFERENCE_TRIGGERS, LATENCY, RECOVERY, VERIFIERS, describe
from omarchy_runtime.runtime import Runtime
from omarchy_runtime.s0 import S0Loop
from omarchy_runtime.s2 import ScriptedS2
from omarchy_runtime.sched import Latest, Mailbox, Scheduler, WallScheduler, distribution
from omarchy_runtime.sim.s0 import Adversary, SimS0
from omarchy_runtime.sim.s1 import ScriptedPickPlace, SimS1Host, Stall
from omarchy_runtime.sim.sensor import Ghost, Occlusion, Sensor
from omarchy_runtime.sim.wipe import ScriptedWiper
from omarchy_runtime.sim.world import Change, Trigger, World
from omarchy_runtime.trace import S0_MODES, TraceWriter, os_identity, runtime_identity, where_measured
from omarchy_runtime.wallclock import stamp

TRIGGERS = ("start", "moving", "grasp", "release", "contact")  # the run starts; the body first moves; the first grasp, release, contact
S2_TIMEOUT = ns(120.0)  # how long the robot waits for an S2 process to answer


@dataclass
class RunResult:
    name: str
    dir: Path
    cfg: Json
    summaries: list[Json]
    truths: list[bool | None]  # ground truth of each execution's goal when it ended
    world: Ground
    s0: S0Loop
    completed: bool  # S2 finished its goals before the time limit
    end: Ns
    clock: str  # "simulated" or "wall"
    loop: Json | None  # the wall-clock scheduler's loop statistics, with their label
    s0_timing_faults: bool  # the scenario makes S0 skip ticks (pauses, S0 stalls, jitter)
    s2: Json  # where S2 ran: in-process, or remote with its address and hello
    s2_link: Json | None  # S2's liveness as the runtime's supervisor saw it (remote S2 only)
    tags: Mapping[str, str]
    observations: list[tuple[Ns, Ns]]  # (taken, delivered to S1 and the runtime), for the harness
    inferences: int  # how many times S1 inferred (its trigger said due)

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
            "observed_at": self.s0.record_observed[:n][keep],
            "fallback": self.s0.record_fallback[:n][keep],
        }

    def violations(self, t0: Ns = 0, t1: Ns | None = None) -> list[tuple[Ns, str]]:
        end = self.end if t1 is None else t1
        return [(t, what) for t, what in self.world.violations if t0 <= t <= end]


@dataclass(frozen=True)
class Timing:
    """Deterministic timing faults, from a scenario's [timing] table and its
    pause and s0_stall faults: the conditions a wall-clock run meets on a
    loaded machine, reproduced in simulated time."""

    observation_age: Callable[[], Ns] | None  # how old each observation is when S1 and the runtime read it
    s0_skip: float  # probability that S0 skips a tick: S0 tick jitter, late ticks
    pauses: list[Trigger]  # the whole robot process stops: nothing is stepped
    s0_stalls: list[Trigger]  # S0 alone does not tick


def _delay(table: Json, key: str, where: str, rng: np.random.Generator, measured: str | None = None) -> Callable[[], Ns] | None:
    """A delay `key`_s, fixed, or lognormal with a nonzero `key`_spread; None
    when both are 0. With `measured` (a distribution kind), `key`_file names a
    measured distribution (latency.py) that replaces them."""
    if measured is not None and f"{key}_file" in table:
        model = latency_files.EmpiricalLatency(latency_files.load(str(table[f"{key}_file"]), measured))
        return lambda: model.sample(rng)
    seconds_, spread = float(table.get(f"{key}_s", 0.0)), float(table.get(f"{key}_spread", 0.0))
    if seconds_ < 0 or spread < 0:
        raise ConfigError(f"{where}: {key}_s and {key}_spread must not be negative")
    if seconds_ == 0 and spread == 0:
        return None
    modelled = LATENCY["fixed" if spread == 0 else "lognormal"](seconds_, spread)
    return lambda: modelled.sample(rng)


def _timing(cfg: Json, seed: int, pauses: list[Trigger], s0_stalls: list[Trigger]) -> Timing:
    t = cfg["scenario"].get("timing", {})
    unknown = set(t) - {"observation_age_s", "observation_age_spread", "s0_skip_probability"}
    if unknown:
        raise ConfigError(f"scenario timing: unknown {', '.join(sorted(unknown))}")
    skip = float(t.get("s0_skip_probability", 0.0))
    if not 0 <= skip < 1:
        raise ConfigError(f"scenario timing.s0_skip_probability: {skip} is not in [0, 1)")
    return Timing(_delay(t, "observation_age", "scenario timing", np.random.default_rng([seed, 6])), skip, pauses, s0_stalls)


@dataclass
class Faults:
    slips: list[Trigger]
    stalls: list[Stall]
    occlusions: list[Occlusion]
    pauses: list[Trigger]
    s0_stalls: list[Trigger]
    changes: list[Change]
    ghosts: list[Ghost]
    stuck: list[Trigger]
    adversary: list[tuple[str, Trigger]]
    weak: list[tuple[float, Trigger]]


def _faults(cfg: Json) -> Faults:
    scene = config.scene(cfg)
    slips, stalls, occlusions, pauses, s0_stalls, changes, ghosts, stuck = [], [], [], [], [], [], [], []
    adversary: list[tuple[str, Trigger]] = []
    weak: list[tuple[float, Trigger]] = []
    for n, f in enumerate(cfg["scenario"].get("faults", [])):
        where = f"scenario faults[{n}]"
        after = config.get(f, "after", str, where)
        if after not in TRIGGERS:
            raise ConfigError(f"{where}.after: {after!r} is not one of {', '.join(TRIGGERS)}")
        kind = config.get(f, "kind", str, where)
        if kind in ("move", "appear"):
            entity = config.get(f, "entity", str, where)
            if entity not in scene.objects:
                raise ConfigError(f"{where}.entity: no object {entity!r} in world.toml")
            changes.append(Change(after, ns(config.get(f, "delay_s", float, where)), kind, entity, config.get(f, "x", float, where)))
            continue
        when = Trigger(after, ns(config.get(f, "delay_s", float, where)), ns(config.get(f, "duration_s", float, where)))
        if kind == "ghost":
            entity = config.get(f, "entity", str, where)
            if entity not in scene.objects:
                raise ConfigError(f"{where}.entity: no object {entity!r} in world.toml")
            ghosts.append(Ghost(entity, config.get(f, "x", float, where), when))
        elif kind == "gripper_stuck":
            stuck.append(when)
        elif kind == "weak_actuators":
            weak.append((config.get(f, "scale", float, where), when))
        elif kind == "controller":
            mode = config.get(f, "mode", str, where)
            if mode not in Adversary.MODES:
                raise ConfigError(f"{where}.mode: {mode!r} is not one of {', '.join(Adversary.MODES)}")
            adversary.append((mode, when))
        elif kind == "slip":
            slips.append(when)
        elif kind == "s1_stall":
            stalls.append(Stall(when))
        elif kind == "occlusion":
            region = config.get(f, "region", str, where)
            if region not in scene.regions:
                raise ConfigError(f"{where}.region: no region {region!r}")
            occlusions.append(Occlusion(scene.regions[region], when))
        elif kind == "pause":
            pauses.append(when)
        elif kind == "s0_stall":
            s0_stalls.append(when)
        else:
            kinds = "slip, s1_stall, occlusion, pause, s0_stall, move, appear, ghost, gripper_stuck, controller or weak_actuators"
            raise ConfigError(f"{where}.kind: {kind!r} is not {kinds}")
    return Faults(slips, stalls, occlusions, pauses, s0_stalls, changes, ghosts, stuck, adversary, weak)


class Ground(Protocol):
    """The simulator's ground truth as the harness reads it: a planar World or the MuJoCo scene."""

    first: dict[str, Ns]
    events: list[tuple[Ns, str, str]]
    violations: list[tuple[Ns, str]]

    def truth(self, goal: Goal) -> bool | None: ...


class Observer(Protocol):
    count: int

    def observe(self, now: Ns) -> Observation: ...

    def ghost(self, now: Ns) -> Observation | None: ...


PLANAR_ONLY = ("slips", "changes", "stuck", "ghosts")


def _body_side(
    cfg: Json, body: BodySpec, scene: Scene, faults: Faults, seed: int, capacity: int, period: Ns, late: Ns
) -> tuple[Ground, S0Loop, Observer, Callable[[str], S1Policy]]:
    """The simulated world, S0 (guard and controller), the sensor and S1, for the body's kind."""
    controller_id, capability = config.controller(cfg)
    sensor_cfg = cfg["world"]["sensor"]
    if body.kind == "mujoco_panda":
        try:
            from omarchy_runtime.sim import panda  # MuJoCo: in omarchy-runtime-tools only
        except ImportError as e:
            raise ConfigError(f"body {body.id} is simulated in MuJoCo: run it with omarchy-runtime-tools ({e})") from e

        used = [k for k in PLANAR_ONLY if getattr(faults, k)]
        if used or faults.adversary or controller_id != "cartesian_impedance":
            raise ConfigError(f"body {body.id}: planar-only faults {used}, or controller {controller_id}")
        world = panda.PandaWorld(cfg["world"], body, np.random.default_rng([seed, 7]), capacity, faults.weak)
        stiffness = config.floats(body.kinematics.get("impedance", {}), "stiffness", f"bodies.{body.id}.kinematics.impedance", 6)
        controller = CartesianImpedance(capability, stiffness, body.home)
        s0 = S0Loop(body, controller, world, capacity, nominal=period, late_after=late)
        sensor = panda.PandaSensor(world, np.random.default_rng([seed, 2]), sensor_cfg, faults.occlusions)
        params = cfg["scenario"]["s1"]
        return world, s0, sensor, lambda i: ScriptedWiper(i, params)
    if controller_id != "motion_tracker" or faults.weak:
        raise ConfigError(f"body {body.id}: the planar simulator runs the motion tracker, not {controller_id}, and has no weak actuators")
    present = {k: config.get(v, "x", float, f"world objects.{k}") for k, v in cfg["world"]["objects"].items() if v.get("present", True)}
    planar = World(scene, present, body, faults.slips, faults.changes, faults.stuck)
    sim_s0 = SimS0(body, planar, capacity=capacity, nominal=period, late_after=late, capability=capability, adversary=faults.adversary)
    camera = Sensor(
        planar,
        np.random.default_rng([seed, 2]),
        source=sensor_cfg["source"],
        noise=sensor_cfg["noise_m"],
        confidence=sensor_cfg["confidence"],
        occlusions=faults.occlusions,
        ghosts=faults.ghosts,
    )
    return planar, sim_s0, camera, ScriptedPickPlace


def s2_parts(
    cfg: Json, ttft: float | None = None, spread: float = 0.0, ttft_file: str | None = None
) -> tuple[RecoveryPolicy, LatencyModel, Json]:
    """S2's recovery policy and latency model, and how traces name them. The
    latency is the scenario's unless `ttft` gives another median time to
    first token (fixed, or lognormal with a nonzero `spread`) or `ttft_file`
    a measured distribution (latency.py; its median is the TTFT named)."""
    s, ext = cfg["scenario"]["s2"], cfg["runtime"]["extensions"]
    if ttft_file is not None:
        if ttft is not None:
            raise ConfigError("give a time to first token or a measured distribution, not both")
        d = latency_files.load(ttft_file, "s2_ttft")
        recovery, measured = RECOVERY[ext["recovery"]](), latency_files.EmpiricalLatency(d)
        named = {"seconds": d["summary"]["median"], "spread": None, "distribution": d["name"], "sha256": d["sha256"], "label": d["label"]}
        return recovery, measured, {"recovery": describe(recovery), "s2_latency": describe(measured) | named}
    if ttft is None:
        model, ttft, spread = ext["s2_latency"], config.get(s, "latency_s", float, "scenario s2"), float(s.get("latency_spread", 0.0))
    else:
        model = "fixed" if spread == 0 else "lognormal"
    try:
        recovery, latency = RECOVERY[ext["recovery"]](), LATENCY[model](ttft, spread)
    except KeyError as e:
        raise ConfigError(f"extensions: no implementation with id {e}") from e
    return recovery, latency, {"recovery": describe(recovery), "s2_latency": describe(latency) | {"seconds": ttft, "spread": spread}}


def _s2_fallbacks(s2: Json) -> dict[str, Any]:
    """The scripted S2's fallbacks, from a scenario's [s2]: `fallback` (send
    one predicted with each goal), `refine_fallback` (and refine it once the
    execution runs), `fallback_lasts_s` (how long S0 may stay in it once
    engaged; by default, its rows only)."""
    for key in ("fallback", "refine_fallback"):
        if not isinstance(s2.get(key, False), bool):
            raise ConfigError(f"scenario s2.{key}: expected true or false")
    lasts = ns(config.get(s2, "fallback_lasts_s", float, "scenario s2")) if "fallback_lasts_s" in s2 else None
    return {"fallback": s2.get("fallback", False), "refine": s2.get("refine_fallback", False), "fallback_lasts": lasts}


def _reconnect(cfg: Json) -> link.Reconnect:
    t = cfg["runtime"]["s2_link"]
    return link.Reconnect(*(ns(config.get(t, k, float, "s2_link")) for k in RECONNECT))


RECONNECT = ("reconnect_s", "reconnect_backoff_s", "reconnect_backoff_max_s", "connect_attempt_s")


def _connect_s2(where: tuple[str, int], timeout: Ns, robot_hello: Json, bind: str | None) -> tuple[link.TcpTransport, Json]:
    transport = link.connect(*where, timeout, bind)
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
    s2_bind: str | None = None,
    ttft: float | None = None,
    ttft_spread: float = 0.0,
    ttft_file: str | None = None,
    tags: Mapping[str, str] | None = None,
    overrides: Json | None = None,
) -> RunResult:
    """Run one scenario. `clock` is "simulated" or "wall"; `s0_hz` replaces
    S0's configured rate; `s2` is the address of an S2 process (omarchy-runtime
    s2) to connect to, waiting up to `s2_timeout`, instead of S2 in this
    process, from the local address `s2_bind` if given (the S2 link's
    tunnel); `ttft` and `ttft_spread`, or `ttft_file`, replace the latency of
    S2 in this process (see s2_parts). `tags` go into the trace header as they are.
    `overrides` is merged into the effective configuration (and its hash),
    as the inference matrix varies S1's latency, corrector and trigger."""
    if clock not in ("simulated", "wall"):
        raise ConfigError(f"clock {clock!r} is not simulated or wall")
    if s2 is not None and clock != "wall":
        raise ConfigError("an S2 in another process runs on the wall clock: add --clock wall")
    if s2 is not None and (ttft is not None or ttft_file is not None):
        raise ConfigError("an S2 in another process has its own time to first token: give it to omarchy-runtime s2")
    cfg = config.merge(config.effective(name), overrides or {})
    if clock == "wall":
        cfg["world"]["rates_hz"] |= config.get(cfg["world"], "wall_rates_hz", dict, "world.toml")
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
    ext = cfg["runtime"]["extensions"]
    faults = _faults(cfg)
    timing = _timing(cfg, seed, faults.pauses, faults.s0_stalls)
    run_id = f"{int(np.random.default_rng([seed, 1]).integers(1 << 32)):08x}"

    period = SECOND // rates["s0"]
    late = round(config.get(cfg["world"]["s0"], "late_after_periods", float, "world.toml s0") * period)
    world, s0, sensor, policy = _body_side(cfg, body, scene, faults, seed, limit * rates["s0"] // SECOND + 1, period, late)
    capability = s0.capability
    for_s1: Latest[Observation] = Latest()
    for_runtime: Mailbox[Observation] = Mailbox()
    config.get(s["s1"], "latency_s", float, "scenario s1")
    s1_latency = _delay(s["s1"], "latency", "scenario s1", np.random.default_rng([seed, 4]), measured="s1_inference")
    try:
        observation_filter = FILTERS[ext["filter"]]()
        verifiers = {p: VERIFIERS[v]() for p, v in ext["verifiers"].items()}
        corrector = CORRECTORS[ext["corrector"]](cfg["runtime"]["s1_host"])
        trigger = INFERENCE_TRIGGERS[ext["trigger"]](cfg["runtime"]["s1_host"])
        capabilities = config.capabilities(cfg)
        defaults = {c.default_fallback: DEFAULT_FALLBACKS[c.default_fallback]() for c in capabilities if c.default_fallback}
    except KeyError as e:
        raise ConfigError(f"extensions: no implementation with id {e}") from e
    host = SimS1Host(
        s0, for_s1, s1_latency or (lambda: 0), faults.stalls, world, period=SECOND // rates["s1"], corrector=corrector, trigger=trigger
    )
    recovery, latency, s2_named = s2_parts(cfg, ttft, ttft_spread, ttft_file)
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
        transport, peer = _connect_s2(s2, s2_timeout, robot_hello, s2_bind)
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
        "s0": capability.to_json(),
        "rates_hz": rates,
        "extensions": {
            "filter": describe(observation_filter),
            "verifiers": {p: describe(v) for p, v in verifiers.items()},
            "corrector": describe(corrector) | ({"blend_s": cfg["runtime"]["s1_host"]["blend_s"]} if corrector.id == "blend" else {}),
            "trigger": describe(trigger) | ({"mismatch": cfg["runtime"]["s1_host"]["mismatch"]} if trigger.id == "mismatch" else {}),
            "default_fallbacks": {c.id: describe(defaults[c.default_fallback]) for c in capabilities if c.default_fallback},
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
        contradiction_m=verification["contradiction_m"],
        contradiction_s=verification["contradiction_s"],
    )
    implementations = {b.implementation: (lambda i=b.implementation: policy(i)) for b in config.bindings(cfg)}
    runtime = Runtime(
        body=body,
        capabilities=capabilities,
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
        default_fallbacks=defaults,
    )
    truths: list[bool | None] = []

    aging: list[tuple[Ns, int, Observation]] = []  # observations on their way, a heap by arrival time
    observed: list[tuple[Ns, Ns]] = []
    skips = np.random.default_rng([seed, 5])

    def sense(now: Ns) -> None:
        observation = sensor.observe(now)
        age = timing.observation_age() if timing.observation_age else 0
        heapq.heappush(aging, (now + age, sensor.count, observation))
        if (ghost := sensor.ghost(now)) is not None:
            heapq.heappush(aging, (now + age, sensor.count, ghost))
        while aging and aging[0][0] <= now:
            _, _, ready = heapq.heappop(aging)
            observed.append((ready.t, now))
            for_s1.put(ready)
            for_runtime.put(ready)

    def control(now: Ns) -> None:
        if any(w.active(world, now) for w in timing.s0_stalls) or (timing.s0_skip and skips.random() < timing.s0_skip):
            return  # a late tick for S0: nothing commanded this period
        host.deliver(now)
        s0.tick(now)

    def unless_paused(step: Callable[[Ns], None]) -> Callable[[Ns], None]:
        if not timing.pauses:
            return step
        return lambda now: None if any(w.active(world, now) for w in timing.pauses) else step(now)

    def harness(now: Ns) -> None:
        while len(truths) < len(runtime.finished):
            truths.append(world.truth(Goal.from_json(runtime.finished[len(truths)]["goal"])))

    supervisor = SECOND // rates["supervisor"]
    sched.add("sense", SECOND // rates["observation"], unless_paused(sense))
    sched.add("s1", SECOND // rates["s1"], unless_paused(host.step))
    sched.add("s0", SECOND // rates["s0"], unless_paused(control))
    sched.add("runtime", supervisor, unless_paused(runtime.step))
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
            task_prefix=run_id,
            **_s2_fallbacks(s["s2"]),
        )
        sched.add("s2", SECOND // rates["s2"], unless_paused(scripted.step))
        s2_done = lambda: scripted.done  # noqa: E731
    else:
        assert s2 is not None
        robot_link = link.RobotLink(transport, s2, robot_hello, runtime, trace, _reconnect(cfg), bind=s2_bind)
        sched.add("link", SECOND // rates["link"], unless_paused(robot_link.step))
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
    if robot_link is not None:
        robot_link.close(sched.now)  # while the trace is open: its last events go in
    runtime.close(sched.now, "task complete" if completed else "time limit", **({"loop": loop} if loop else {}))
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
        bool(timing.pauses or timing.s0_stalls or timing.s0_skip),
        placement,
        s2_link,
        dict(tags or {}),
        observed,
        host.inferences,
    )
    if record_s0:
        for summary in runtime.finished:
            rec = result.record(summary["timing"]["requested"], summary["timing"]["ended"])
            np.savez(run_dir / f"s0_{summary['execution_id']}.npz", **rec, dims=np.array(body.dims), modes=np.array(S0_MODES))
        log = getattr(world, "log", None)
        if log is not None:  # the MuJoCo scene: its ground truth, tick by tick, for the harness and the video
            np.savez_compressed(run_dir / "world.npz", **log())
    return result


def serve_s2(
    where: tuple[str, int], *, ttft: float | None, spread: float, timeout: Ns, announce: bool = False, ttft_file: str | None = None
) -> Json:
    """The S2 process: accept one robot at `where` (waiting up to `timeout`),
    run the scripted S2 for the robot's scenario on the wall clock, with the
    scenario's latency, a time to first token `ttft` (fixed, or lognormal
    with `spread`) or a measured distribution `ttft_file`, then leave and wait for the robot to end the run.

    Returns S2's own view: whether it finished its goals, why the link
    closed, what the robot refused, its round trips (heartbeat echo, S2's
    clock) and its loop statistics."""
    listener = link.Listener(*where, announce)
    transport = listener.accept(timeout)
    try:
        robot = link.check_hello(transport.wait("hello", timeout), "robot")
        cfg = config.effective(config.get(robot, "scenario", str, "robot hello"))
        recovery, latency, named = s2_parts(cfg, ttft, spread, ttft_file)
        body = BodySpec.from_json(robot["body"])
        seed = config.get(robot, "seed", int, "robot hello")
        run_id = config.get(robot, "run_id", str, "robot hello")
        heartbeat = ns(config.get(robot, "heartbeat_s", float, "robot hello"))

        def reply(ack: int) -> Json:
            return link.hello("s2", runtime=runtime_identity(), ack=ack, **named)

        transport.send(reply(0))
    except OmarchyError as e:
        transport.send({"type": "error", "reason": str(e)})
        transport.close()
        listener.close()
        raise
    s = cfg["scenario"]
    api = link.RemoteRuntime(transport, listener, run_id, heartbeat, reply)
    s2 = ScriptedS2(
        api,
        config.goals(s["s2"], "scenario s2"),
        body,
        recovery,
        latency,
        np.random.default_rng([seed, 3]),
        task_prefix=run_id,
        **_s2_fallbacks(s["s2"]),
    )
    sched = WallScheduler()
    sched.add("link", ns(0.001), api.step)
    sched.add("s2", SECOND // cfg["world"]["rates_hz"]["s2"], s2.step)
    limit = ns(float(s.get("limit_s", 60.0)))
    sched.run(lambda: s2.done, until=limit, check_every=ns(0.01))
    api.leave(sched.now)
    # The robot ends the run once its last execution is over, and says bye.
    sched.run(lambda: api.finished, until=sched.now + ns(30.0), check_every=ns(0.01))
    if api.transport is not None:
        api.transport.close()
    listener.close()
    return {
        "scenario": s["name"],
        "run_id": run_id,
        "done": s2.done,
        "bye": api.finished,
        "sessions": api.sessions,
        "errors": api.errors,
        "rtt_ns": distribution(api.rtt),
        "rtt_label": "heartbeat echo on S2's monotonic clock: the network both ways plus up to one loop period at each end",
        "loop": {"label": f"soft real-time, Python, {where_measured()}", "components": sched.stats()},
    }
