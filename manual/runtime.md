---
layout: default
title: Runtime
permalink: /runtime/
---

The Omarchy Robots runtime sits between the intelligence that decides what a robot should do and the body that does it. An application or a planner asks for a **goal**, such as "cup_1 is in the drying rack". The runtime finds a capability that can achieve it on this body, runs it, checks the result from fresh observations, and records everything that happened as a trace that names the exact system it ran on.

> **Everything here is simulated.** The bodies, the controller, the policy and the camera are simulated in Python, in simulated time or on the wall clock (soft real-time). Nothing is real-time, and no part of the runtime has run on hardware. Every number below says whether it is simulated time or wall-clock time, and where it was measured.

## Three layers, one control plane

| Layer | Role | Rate | In R2 |
|---|---|---|---|
| S2 | Slow reasoning and planning. Requests goals, may install a fallback, decides what to do after a failure. | about 10 Hz, decisions taking 0.1 s to 5 s | a scripted planner with a latency model, in the runtime's process or in its own |
| S1 | Action generation: turns a goal into short timestamped trajectories, **action chunks**. | 20 Hz | a scripted pick-and-place policy |
| S0 | Real-time execution: the only layer that commands actuators. Two halves: the **guard**, Omarchy's (limits, bounded steps, fallbacks, starvation watchdog), and a **controller** behind it. | 1 kHz (200 Hz on the wall clock) | a motion tracker behind the guard, simulated |

![S2 and the runtime above; S1, S0 and the body below]({{ '/assets/runtime-layers.svg' | relative_url }})

**Figure 1.** The three layers and the runtime. Thick line: the high-rate path, where S1 sends action chunks to S0 directly, never through the runtime. S2 installs a fallback ahead of time through the runtime. S0 uses it when chunks stop.

The runtime is the **control plane**: it owns the application API, capability resolution, the execution lifecycle, supervision, the belief about the world, verification and traces. It is not in the high-rate path. Chunks go from S1 to S0 directly, and the runtime samples S0's status at its own rate.

**S0 never waits.** At every tick it executes a valid chunk, else a valid fallback (S2's, else the capability's default: see "Fallbacks"), else its built-in HOLD, which keeps the joints where they are and leaves the gripper as it is. A chunk is executed only until its `valid_until`; S0 leaves it at the first tick after that.

**S0's steps are bounded.** In one tick S0 never moves a dimension more than its velocity limit times S0's nominal tick period, whatever the real time since the last tick. A tick that comes later than a threshold (1.5 periods, `[s0]` in `config/world.toml`) is a late tick: S0 continues from where it is at that bound and never catches up with a jump; if the plan moved on meanwhile, S0 reaches it later and S1 plans again. S0 counts late ticks and reports the longest interval between ticks and its largest step as a fraction of the bound; the runtime records each new late tick (`s0.late`). This is part of the S0 contract, for a hardware S0 as well. When an execution stops running, the runtime tells S0 to hold: the rest of the chunk is dropped. After a claim or a cancellation the fallbacks go too; after a failure S0 keeps them and engages one while S2 decides.

### Fallbacks

A fallback must exist before it is needed. S0 holds two, in two slots, and runs the first valid one, else HOLD:

1. **S2's fallback.** S2 predicts it and sends it with the goal (`fallback` in the `request`), its window relative to the execution's start, so that the runtime installs it when the execution starts, before S1's first chunk, whatever S2's latency. S2 may refine it later (`install_fallback`): the refined one replaces the predicted one.
2. **The capability's default**, S0's base fallback. Each capability names a generator in `config/capabilities.toml` (`default_fallback`); the runtime computes it on the robot side from S0's status and installs it when the execution starts. `hold` keeps every dimension where S0 is when it engages, the gripper included (relative rows of zeros); `lift`, the default of `place_in`, does the same, and on a body with a `z` dimension (the gantry) with the gripper closed rises 2 cm first. A default never moves the gripper, so a closing gripper is never reopened on an object. The runtime renews the default whenever the generator returns another motion (after a grasp, after a release); the one replaced stays until then. It is valid for the execution and a wait for S2 after it (600 s from the start).

Precedence: S2's refined, then S2's predicted, then the capability's default, then HOLD. A fallback engages when a chunk ran out with no successor, or when the runtime asks after a failure; while an execution awaits S1's first chunk, S0 holds. A new chunk takes over at once; S2's fallback, once engaged, is then spent (S2 may install another), so that brief gaps in S1's chunks do not send S0 towards S2's pose again and again; the default stays installed for the next time. When S2's fallback runs out (`fallback_lasts_s`), S0 goes on under the default in the same tick. Rows can be targets or, per dimension (`relative`), offsets from where S0 is when it engages. Every installation is recorded (`fallback.install`: slot, kind, source, window, duration), and the execution summary lists them.

**Fallback coverage** is the fraction of an execution's running time (from its start to S1's claim or its failure) during which S0 holds a valid fallback, as S0 counts it tick by tick (`fallback_cover_ns`). It is reported for each execution (`fallback_coverage` in its summary and in `results.json`), each scenario (the least) and each placement cell; the evaluation requires at least 99 % for every execution.

## Turning it on

In [`robot.nix`](../robot-nix/):

```nix
omarchy.robot.runtime.enable = true;
```

The system then carries the `omarchy-runtime` command, and run directories go to `/var/lib/omarchy-runtime/runs`, which the administrator can write. There is no service: the runtime is a command. `omarchy-robot info` and `/etc/omarchy-robot/build.json` name the runtime's version.

Without a robot, run it from a clone:

```sh
nix run .#omarchy-runtime -- demo     # nominal_a, then slip_retry, told step by step
nix run .#omarchy-runtime -- eval     # every scenario, and the results table
```

## Commands

| Command | What it does |
|---|---|
| `omarchy-runtime demo` | Runs `nominal_a` and `slip_retry`, says what happened in each execution, and prints the results table. |
| `omarchy-runtime eval [SCENARIO ...]` | Runs the given scenarios (all by default), checks every trace, compares each execution with the simulator's ground truth and with what the scenario expects, and writes `results.json`. Exits 1 if a scenario is not as expected. |
| `omarchy-runtime run SCENARIO` | One scenario, by name or as a path to a `.toml` file. |
| `omarchy-runtime show RUN_DIR` | A summary of one run directory. |
| `omarchy-runtime validate-trace RUN_DIR ...` | Checks run directories against the trace schema; exits 1 if one is incomplete. |
| `omarchy-runtime s2` | Is S2 for one robot run in another process: waits for the robot, runs the scripted S2 on the wall clock, prints its own report. See "S2 in another process". |
| `omarchy-runtime inference-matrix` | S1's inference delay x chunk corrector x inference trigger on `nominal_a` and `cup_moved`, in simulated time. See "Asynchronous inference". |
| `omarchy-runtime scenarios` | Lists the scenarios. |
| `omarchy-runtime scrub-experiment` | The scrubbing experiment on the simulated Panda (development tools). See "Contact on a simulated Franka Panda". |
| `omarchy-runtime scrub-render RUN_DIR --out FILE` | An mp4 of a recorded scrubbing run, with its provenance (development tools). |

`--runs DIR` chooses where run directories go (default: `$OMARCHY_RUNTIME_RUNS`, else `./runs`). `--record-s0` also writes S0's per-tick record for each execution.

`--clock wall` steps the same components on the wall clock instead of simulated time: soft real-time, in Python, on a general-purpose OS. Each component gets the real time elapsed since the run began. A step that starts a whole period or more late is an overrun, and the releases it missed are skipped, never caught up. The run ends with each component's loop statistics: intervals between steps and step durations (median, 99th percentile, maximum), overruns. They are labelled "soft real-time, Python" with where they were measured. On the wall clock S0 runs at 200 Hz, not 1 kHz, and the robot's end of an S2 link is polled at 1 kHz (`[wall_rates_hz]` in `config/world.toml`): Python did not keep S0 at 1 kHz in a KVM VM. `--s0-hz N` changes S0's rate. Simulated time stays the default, and it alone is deterministic.

## The scenarios

A simulated kitchen in a vertical plane: a table with two cups, a counter with a drying rack, and one of two bodies. Body A (`arm3`) is a planar arm with three joints and a gripper; body B (`gantry`) is a Cartesian gantry. They have different kinematics and different action spaces.

| Scenario | What happens | Outcome |
|---|---|---|
| `nominal_a`, `nominal_b` | `in(cup_1, drying_rack)` on body A, then the same file with only `body` changed | SUCCEEDED |
| `slip_retry` | The cup slips over the table while S1 carries it. S1 does not notice and claims DONE; the verifier sees the cup on the table; the recovery policy retries once | FAILED/VERIFICATION_FAILED, then SUCCEEDED |
| `occluded_long` | The rack is hidden from the camera for the whole run. The cup is in the rack, but nothing can show it | UNVERIFIED/VERIFICATION_INCONCLUSIVE |
| `occluded_short` | The rack is hidden for 2.5 s after the release, within the verifier's budget | SUCCEEDED |
| `s1_stall_short` | S1 stops producing chunks for 1 s while it carries the cup: S0 runs the capability's default (holds the cup where it is), S1 resumes | SUCCEEDED |
| `s1_stall_long` | S1 stops for good: after 3 s without a chunk the supervisor ends the execution, S0 still holding the cup under the capability's default | FAILED/S1_STALL |
| `fallback_installed` | S2 sends a fallback with the goal (the safe pose, gripper closed) and refines it once the execution runs; S1 stalls for 2.5 s; S0 runs S2's fallback, then the capability's default, never HOLD, then S1 resumes | SUCCEEDED |
| `unsupported_clean` | `clean(cup_1)`: no capability achieves it, and the resolver says why | REJECTED/NO_CAPABILITY |
| `s2_latency_0p1`, `_1`, `_5` | Both cups, with S2 taking 0.1 s, 1 s or 5 s for every decision | SUCCEEDED twice; only the task time and the robot's idle time change |
| `timing_observation_age` | Every observation is 20 ms old when S1 reads it. Regression: v0.0.2 part 2's first S1 fix timed out here | SUCCEEDED |
| `timing_pause_grasp` | The whole robot process stops for 0.3 s while the arm descends to the cup. Regression: v0.0.2 part 1's S1 pushed the held cup into the table here | SUCCEEDED |
| `timing_s0_stall` | S0 alone does not tick for 0.3 s while the arm carries the cup: one late tick, no catch-up jump | SUCCEEDED |
| `timing_jitter` | S0 skips 5 % of its ticks, S1's latency and the observations' age are lognormal | SUCCEEDED twice |
| `cup_moved` | A human moves the cup 6 cm while the arm approaches it. S1 plans the grasp again from fresh observations, without S2 | SUCCEEDED |
| `slot_blocked` | A dish appears in the rack spot the arm is carrying the cup to. S1 gives up (blocked), S0 runs S2's fallback (held 3 s), S2 chooses another spot (`retry_elsewhere`) | FAILED/IMPLEMENTATION_ERROR, then SUCCEEDED |
| `fallback_expiry` | `slot_blocked` with S2 taking 2 s and a fallback that lasts 1.5 s: the fallback expires, S0 returns to HOLD with the cup held, then S2's new goal runs | FAILED/IMPLEMENTATION_ERROR, then SUCCEEDED |
| `ghost_verify` | A second detector sees the cup 30 cm off while the verifier looks: the belief is contradictory, the verdict VIOLATED although the cup is in the rack; attributed to PERCEPTION. The retry finds the goal already satisfied | FAILED/VERIFICATION_FAILED, then SUCCEEDED |
| `gripper_stuck` | The gripper is stuck open (an actuator fault). S1 keeps planning the grasp again until the deadline; S0 never achieved the intent: attributed to EXECUTION | FAILED/TIMEOUT |
| `guard_controller_fault` | S0's controller emits NaN for 0.3 s while the arm carries the cup. The guard replaces each command by its own hold, the trace records it (`s0.guard`), S1 plans again | SUCCEEDED |

Each scenario is a TOML file in `pkgs/omarchy-runtime/omarchy_runtime/scenarios/`: seed, body, faults, S2's goals and latency, S1's inference latency, and the expected outcome of each execution.

### Timing faults

A loaded machine changes the timing a wall-clock run meets. A scenario reproduces those conditions in simulated time, deterministically from its seed, so a timing bug is caught by the fast check instead of by a lucky CI run:

| Where | Key | Effect |
|---|---|---|
| `[timing]` | `observation_age_s`, `observation_age_spread` | every observation reaches S1 and the runtime this much later than it was taken; fixed, or lognormal with a spread |
| `[timing]` | `s0_skip_probability` | S0 skips a tick with this probability: tick jitter, late ticks |
| `[s1]` | `latency_s`, `latency_spread` | S1's inference latency for each chunk; fixed, or lognormal |
| `[[faults]]` | `kind = "pause"` | the whole robot process stops for `duration_s`: nothing is stepped |
| `[[faults]]` | `kind = "s0_stall"` | S0 alone does not tick for `duration_s` |

`tests/test_timing.py` runs `timing_observation_age` and `timing_pause_grasp` with the S1 behaviour of the two bugs CI found in v0.0.2 part 2, and each fails there: a timeout, a held cup pushed into the table.

### Scenes that change

The scene can change during a task. A change is a `[[faults]]` entry, at a delay (`delay_s`) after a point of the run (`after`), like the other faults: `start` (the run starts), `moving` (the body first moves, S0 executing the first chunk: after S2's first decision, whatever its latency), `grasp` or `release` (the first of each):

| Kind | Keys | Effect |
|---|---|---|
| `move` | `entity`, `x` | the entity moves by `x` along the table (refused while it is held) and settles on the support below |
| `appear` | `entity`, `x` | an object declared absent (`present = false` in `world.toml`, as `dish_1`) appears at `x` |

Three more fault kinds exercise the guard and the attribution (see "S0: the guard and its controller" and "Fidelity and failure attribution"):

| Kind | Keys | Effect |
|---|---|---|
| `ghost` | `entity`, `x` | while it lasts, a second detector (source `sim.camera.b`) reports the entity `x` metres from where it is |
| `gripper_stuck` | | while it lasts, the gripper does not move, whatever S0 commands |
| `controller` | `mode` | while it lasts, S0's controller returns NaN (`nan`), huge values (`huge`), a one-unit jump (`jump`) or a command computed for an earlier tick (`stale`) |

S2's fallback has a duration: `fallback_lasts_s` in a scenario's `[s2]` (by default, the duration of its rows). S0 holds the fallback's last row until then, and then goes on under the capability's default; S0's counters count each expiry (`fallback_expiries`). When an execution fails, the runtime engages the installed fallbacks. `fallback = true` in `[s2]` makes the scripted S2 send its prediction with each goal; `refine_fallback = true` also refines it once the execution runs.

The evaluation measures how the robot met each change (`dynamics` in `results.json`, seconds on the run's clock):

- **reaction**: from a move to S0 executing the first chunk computed from an observation taken after it;
- **freeze**: from the first observation after the move reaching S1 to that tick, while S0 executes a plan the belief already contradicts;
- **recovery**: for each failed execution that a retry follows, from the failure to the retry running, the time S0 spent in a fallback meanwhile, whether one engaged and which first (S2's or the capability's default), and whether S2's expired (S0 went from it to the default or HOLD) before the retry started;
- **discontinuity**: the largest jump of S0's desired position, and of its desired velocity, where one chunk takes over from another;
- **inferences**: how many times S1 inferred.

## S2 in another process

S2 can run in its own process, on the robot or on another machine. The robot process keeps the runtime, S1, S0, the simulated world and the sensor; the S2 process runs the scripted S2, its recovery policy and its latency model. Both run on the wall clock.

```sh
omarchy-runtime s2 --listen 0.0.0.0:7447 --ttft 1                 # on the S2 machine
omarchy-runtime run s2_latency_1 --clock wall --s2 edge:7447      # on the robot
```

The link is newline-delimited JSON over TCP. S2 listens and the robot connects to it, so the robot's firewall still admits SSH only; the S2 machine admits the port. The robot speaks first: its hello carries the run's identity (run id, scenario, seed, configuration hash, the runtime and OS builds), the body spec S2 builds fallbacks from, and the heartbeat period. S2 answers with its own identity, latency model and recovery policy. Then only control-plane traffic crosses: requests (each may carry S2's predicted fallback), fallback installs, cancels, decisions and heartbeats from S2; execution updates with their summaries, heartbeat echoes and refusals from the robot. Action chunks never cross: S1 and S0 stay in the robot's process.

A run survives a broken connection. Every message from S2 and every update from the robot carries a link number; heartbeats and echoes acknowledge the highest number applied. When S2 has been silent for 3 s, or the connection breaks, the robot connects again without blocking its loop, first at once, then after 0.25 s, doubling up to 2 s between attempts (`[s2_link]` in `config/runtime.toml`). S2 keeps listening for the whole run and resumes the session of the same run id. Both ends then send again, in order, what the other has not acknowledged, and ignore a number already applied, so a resumed session neither repeats nor loses a goal. The runtime itself treats a GoalRequest as idempotent by its `request_id`: the same request again creates no second execution, and its latest update comes again. TCP_USER_TIMEOUT (3 s) and keepalive bound how long the kernel holds unacknowledged data, so TCP's retransmission backoff no longer stretches an outage. The trace records each closed connection and each reconnection (`s2.link`: `closed`, `reconnected` with the time down, the attempts and the updates sent again).

S2 sends a heartbeat every 0.1 s and measures each round trip from the robot's echo: the network both ways, plus up to one loop period at each end. The runtime's supervisor watches the heartbeats from the first to the final one. After 0.5 s without one it records the link as lost (an `s2.link` event). When heartbeats return it records the outage with its duration and class: short (under 2 s, what TCP retransmission on a lossy link gives), long (2 s or more), or unrecovered if the run ends first. Executions continue during an outage, and S0 never waits. The deadlines are in `config/runtime.toml` `[s2_link]`. The trace header names where S2 ran and S2's hello; `run.end` carries every outage, the round trips S2 reported (median, 99th percentile, maximum, and each sample), and the loop statistics.

`--ttft SECONDS` sets S2's time to first token, the delay of every decision. With `--ttft-spread X` it is sampled for each decision from a lognormal distribution with that median (the `lognormal` latency model) instead of fixed. `--tag KEY=VALUE` on the robot records a label in the trace header.

## Goals, capabilities and executions

A **goal** is a predicate over entities, in JSON: `{"predicate": "in", "args": ["cup_1", "drying_rack"]}`. The runtime knows `in`, `on` and `held`; only `in` has a capability today. Goals are what an application or an LLM agent would send; robot verbs are capabilities, never goals.

A **capability** says which predicate it achieves, what the body must have, and what must hold first (`config/capabilities.toml`). A **binding** says which implementation runs it on which bodies, with a priority. The **resolver** picks the highest-priority eligible binding and records every candidate with its reason. If fresh observations already show the goal true, it skips execution and only verifies.

An **execution** is one attempt at a goal: `PENDING`, `RUNNING`, `VERIFYING`, then one outcome. An illegal transition raises an error.

| Outcome | Failure categories |
|---|---|
| SUCCEEDED | none: needs a SATISFIED verdict from observations taken after the executor finished |
| FAILED | IMPLEMENTATION_ERROR, TIMEOUT, S1_STALL, VERIFICATION_FAILED |
| UNVERIFIED | VERIFICATION_INCONCLUSIVE: never counted as success |
| REJECTED | NO_CAPABILITY, PRECONDITION_UNMET |
| ABORTED | CANCELLED |

The executor's **claim** (DONE or GAVE_UP) and the verifier's **verdict** (SATISFIED, VIOLATED or INCONCLUSIVE) are recorded separately. Verification is a phase of the execution: it waits a settling time after the claim, then judges each fresh observation until its budget runs out. The runtime itself refuses a verdict whose evidence is older than the claim.

The runtime's **belief** is built only from observations, each estimate with its provenance (observation id, time, source, confidence). It never reads the simulator's ground truth; only the evaluation harness does, to show where the verifier and the truth agree.

Retries are new executions of the same task, linked by `task_id` and `retry_of`.

S2 and applications use the runtime through two calls, `submit(message)` and `poll()`, with JSON messages: `request` (a goal), `install_fallback`, `cancel`, `decision` and `heartbeat`. The API is asynchronous. A request carries a `request_id` that its sender chooses, and every update about the execution it creates carries that id back, with the execution id and the runtime's time of the change. The same calls work in the runtime's process and from another process.

## Intents: what S1 asks of S0 (contract v2)

An action chunk (v1) says only where the joints should go. That is the motion-only interface that cannot say "press here without moving the board". Since v0.0.3 part 1, S1 sends S0 an **intent chunk** (`contracts.IntentChunk`) with optional channels, each validated on its own:

| Channel | Contents |
|---|---|
| motion | targets over the body's own action space (joint targets, as in v1), or Cartesian end-effector poses (`x y z qw qx qy qz`, unit quaternion) in a declared frame |
| object | the intended motion of one object in a frame: target poses, or sparse 3D points and their future positions (flow) |
| contact, per end-effector | per row a mode (`FREE`, `MAKE`, `MAINTAIN`, `BREAK`); the surface normal in a frame; a target normal force or 6D wrench with lower and upper bounds; the stiffness along each Cartesian axis |

Each channel is a timed trajectory with an action chunk's semantics: an absolute start, `dt` between rows, a horizon, `valid_until`, `observed_at`, the inference latency and its source. Rows are interpolated, contact modes step. A chunk is executed only while every channel it carries is valid. A chunk without motion keeps the pose S0 had when it took over. A malformed channel is refused with `SHAPE`, `NOT_FINITE` or `BOUNDS` (a `MAKE` or `MAINTAIN` target outside its own bounds), and the refusal names the channel.

**What a controller honours.** Each S0 controller declares it (`ControllerCapability`, from the body's `[<body>.s0]` table in `config/bodies.toml`): the command it returns (position, velocity or torque), its rate, the spacing of rows it accepts, the motion action spaces and Cartesian frames, the object intent forms, contact (end-effectors, frames, modes, the largest force, a stiffness range per axis, wrench or not), and the sensor streams it consumes. An intent outside the declaration is refused with `UNSUPPORTED_INTENT`. The only exception is a degraded mode that the configuration names (`degraded`):

| Degraded mode | What S0 executes instead |
|---|---|
| `drop_object` | the intent without its object channel |
| `drop_contact` | the intent without its contact channels: the motion only |
| `clamp_force` | force targets and bounds above the controller's maximum, clamped to it |
| `clamp_stiffness` | stiffness outside the controller's ranges, clamped into them |

A degradation is never silent: the chunk's metadata names it (`degraded` in its `chunk` event), and S0 counts degraded chunks. A refusal is never silent either: S0 keeps the last one (`rejection` in its status: the chunk, when, the code, the channel, the detail) and the runtime records it (`chunk.rejected`). The planar bodies declare the motion tracker, motion only, no degraded mode.

**Compatibility.** A v1 `ActionChunk` is a motion-only intent (`IntentChunk.from_v1`), timing and rows unchanged: S1, the chunk correctors and the inference triggers keep v1, and every scenario and matrix runs as before (a test runs a chunk both ways and compares S0's ticks).

## S0: the guard and its controller

S0 is two halves (`s0.S0Loop`). The **guard** (`guard.py`) is Omarchy's, minimal and plain data: it does not depend on the controller's type, only on the command kind it returns, so it can become a native process later. It owns:

- what S0 executes: S1's chunk while it is valid; when it ran out with no successor, the starvation watchdog arms the fallbacks, S2's before the base; else HOLD;
- position, velocity and workspace limits, joint torque limits and the end-effector force limit;
- the bounded step: a position never moves more than its velocity limit times the nominal tick period, a torque never changes more than its rate limit times the time since the last tick (at most that period), and starts from zero;
- its own safe command: hold the position (position control), stop (velocity), or a joint spring and damper to where it took over (torque control, gravity being the robot's own, as a Franka compensates it).

The **controller** (`contracts.S0Controller`, implementations in `controllers.py`) gets, at each tick, the active intent (S1's chunk or the fallback the guard engaged), the robot state (`RobotState`: positions, velocities, torques, the end-effector pose and twist, the external wrench, the Jacobian and Coriolis terms, each as the robot reports it), and the scene inputs it declares. It returns a command. The guard checks every one: a command that is not finite, computed for another tick, of another kind or shape, or from a controller that raised, is replaced by the guard's safe command; in torque control a breach no clamp fixes (a joint past its limit, too much force) is replaced too. A command out of its limits or steps is clamped. Both are counted (`limit_clamps`, `guard_replacements`) and recorded (`s0.guard` events, at most one per supervisor step). The guard is additional to a robot's own safety, never a replacement for it.

**Scene inputs.** A controller declares the sensor streams it consumes (`scene` in `[<body>.s0]`, such as `pose:plate`). They reach S0 from the robot side on the data plane, never through runtime events; S0 passes the controller the declared ones only. In simulation they come straight from the simulator.

**Fallbacks are intents.** A fallback (`FallbackSpec`) is a motion and, optionally, a contact channel: "hold where you are" is a relative row of zeros; with a Cartesian frame, relative positions add and a relative orientation composes with the one S0 has when it engages; a contact channel can say "leave contact upwards, compliant along z". When the guard engages one it becomes the intent the controller executes (`FallbackSpec.engage`), by the precedence above. S0 refuses a fallback its controller does not honour, and never degrades one.

**Adversarial tests.** `tests/test_guard.py` drives the guard with controllers that emit NaN, infinities, huge values, jumps, stale or malformed commands, or raise, over seeded random runs in position and torque control: nothing but a finite, limited, step-bounded command reaches the actuators, and every replacement is counted. `guard_controller_fault` shows the same in a whole run, with the trace.

Implementations: the **motion tracker** (`motion_tracker`), S0's behaviour before v0.0.3 part 1, behind the guard. A learned, interaction-competent controller can implement the same interface; none is here.

## Fidelity and failure attribution

Verification has two levels. The outcome verifier is unchanged: SUCCEEDED still needs a SATISFIED verdict from fresh observations. Beside it, the **fidelity check** compares what S0 achieved with the intent it executed (`attribution.py`). S0 accumulates, tick by tick on the data plane, only for S1's chunks: the tracking error of the motion channel (in its units, rad or m; along a contact normal outside `FREE` force rules, not position), the share of steady contact ticks (`FREE`, `MAINTAIN`) in the mode requested (contact is a measured normal force of at least 1 N), the share of `MAINTAIN` ticks with the force within bounds, and the object's error against its intent. The runtime reads them once per execution and checks them against `[fidelity]` in `config/runtime.toml`:

| Key | Default | Unfaithful when |
|---|---|---|
| `tracking` | 0.05 | the mean tracking error is above it |
| `contact_mode` | 0.9 | the share in the requested mode is below it |
| `force` | 0.8 | the share within bounds is below it |
| `object_m` | 0.01 | the mean object error is above it (m) |

A command the guard replaced, or a chunk executed degraded, also makes the execution unfaithful. Every execution that ran has its `fidelity` in its summary.

**Attribution.** An execution that ran and did not succeed (FAILED, UNVERIFIED) gets one category, with its reason, the first rule that holds:

1. **INFRASTRUCTURE**: S1 stalled, S0 starved (a chunk ran out with no successor) or ticked late, or S2's link was lost during the execution.
2. **PERCEPTION**: the verdict is inconclusive, or the belief was contradictory on one of the goal's entities during the execution: two sources saw it, confidently, at most 0.1 s apart, more than 5 cm apart (`contradiction_s`, `contradiction_m` in `[verification]`).
3. **EXECUTION**: S0 did not achieve the intent: the fidelity check failed, or S0 reported a fault.
4. **INTENT**: S0 executed the intent faithfully, and the goal was not reached.

SUCCEEDED, REJECTED and ABORTED executions have none. The scenarios that produce each one, checked by `tests/test_attribution.py`:

| Category | Scenario | Why |
|---|---|---|
| INTENT | `slip_retry`, `slot_blocked` (first execution) | S0 tracked every chunk; the cup slipped, or the spot was taken |
| EXECUTION | `gripper_stuck` | the gripper never reached its target: mean tracking error above the threshold |
| PERCEPTION | `occluded_long`; `ghost_verify` (first execution) | an inconclusive verdict; two sources a contradictory 30 cm apart |
| INFRASTRUCTURE | `s1_stall_long` | S1 stalled, S0 starved |

The attribution is only as precise as the intent: a motion-only intent cannot say "keep the grasp", so a slip is an INTENT failure. A contact channel that says it can be checked for it.

## Contact on a simulated Franka Panda: scrubbing a plate

> **Tested in simulation, and nothing more.** MuJoCo on the CPU, in simulated time. No real robot has run any of this, and no number here is a measurement on a robot.

Wiping a plate means pressing on it without moving it: a motion-only interface cannot say that. This scene puts the intent contract, the guard and the attribution on a body with real contact dynamics.

**The scene** (`sim/panda.py`, `config/world_scrub.toml`). The arm is the Franka Emika Panda of the official [MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie/tree/main/franka_emika_panda) (`panda_nohand.xml`, Apache-2.0), fetched by Nix at a pinned commit (`pkgs/mujoco-menagerie`, 62ab00f, 2026-10-15; a fixed-output fetch of the Panda's directory only). The scene is built around the file without editing it: the position actuators become torque motors within the Panda's torque limits (87 and 12 Nm), the arm is gravity-compensated (a real Panda compensates gravity itself under torque control), a sponge (8 x 6 cm, a soft contact) is fixed to the flange, and a table and a 24 cm square plate, free to slide, are added. Physics and S0 run at 1 kHz, S1 at 20 Hz, all in simulated time; MuJoCo 3.8.1 from nixpkgs, on the CPU. The same seed gives the same trace.

**The dirt and its verifier.** The plate carries a 10 x 10 grid of dirt cells. A cell is clean once the sponge has pressed on it with at least 4 N for at least 0.3 s in all (ground truth, read only by the harness). A simulated camera sees the plate's position (2 mm noise, plus a bias in height: the perception error of the experiment) and its dirt as a coverage (`contracts.Coverage`: cells seen, cells that look clean): each cell is seen flipped with probability 0.03, the cells under the sponge are hidden while it is close to the plate, and an occlusion hides everything. The verifier of `clean(plate)` (`coverage`) says SATISFIED when at least 90 % of the cells seen look clean, VIOLATED below, INCONCLUSIVE when fewer than 80 % of the cells are seen. **A real cleanliness verifier is future work:** this one reads a simulated sensor.

**S1 and S0.** A scripted S1 (`sim/wipe.py`, capability `wipe`, `clean(surface)`, bound to `panda`) plans one path from the belief: above the plate's first corner, down, a raster of lines 3.5 cm apart at 5 cm/s, 10 mm below the believed top, then up. The path is the same in every condition. With `contact = true` in the scenario's `[s1]`, its chunks also carry a contact channel: MAKE on the way down, MAINTAIN while wiping (10 N, bounds 5 to 20 N, stiffness 1500, 1500 and 800 N/m), BREAK on the way up. S0's controller (`cartesian_impedance`, `controllers.py`) is a Cartesian impedance controller with normal-force regulation, behind the guard: the pad tracks the motion channel with the contact channel's stiffness (else its default, 1000 N/m); along the normal in MAKE or MAINTAIN, its set-point moves (at most 3 cm/s, at most 4 cm from the path) until the measured force reaches the target. It returns joint torques; the guard limits them, their rate (1000 Nm/s) and the end-effector force (40 N). The Panda's declaration (`[panda.s0]`) honours Cartesian motion in `panda.base`, contact on the pad up to 30 N, and object poses; joint targets and anything above 30 N are refused. The force S0 reads is an estimate (0.5 N noise, filtered), as a Panda estimates it from its joint torques. The capability's default fallback, `retreat`, rises 3 cm, compliant along z, leaving contact.

| Scenario | What happens | Outcome |
|---|---|---|
| `scrub_contact` | the plate where the belief says, with the contact channel | SUCCEEDED |
| `scrub_motion` | the same, motion only | SUCCEEDED |
| `scrub_hover` | motion only, the plate believed 2 cm higher than it is: the sponge never touches it | FAILED/VERIFICATION_FAILED, attributed to INTENT |
| `scrub_occluded` | with contact; the camera cannot see the plate after the wiping | UNVERIFIED, attributed to PERCEPTION |
| `scrub_weak` | with contact; the actuators deliver a tenth of the torque from the first contact on | FAILED/VERIFICATION_FAILED, attributed to EXECUTION |

They run with the development tools: `nix run .#omarchy-runtime-tools -- run scrub_contact`. The robot's package has no MuJoCo; there, `eval` lists them as not run.

### The scrubbing experiment

`omarchy-runtime scrub-experiment --seeds 5` runs both interface conditions (motion only; motion and the contact channel) x five errors in the plate's height in the belief (-2, -1, 0, +1, +2 cm) x seeds 1 to 5, each a whole run of `scrub_contact` with the condition and the error changed, one attempt each (S2 never retries). Every number comes from ground truth or from the execution's summary, the same way in both conditions; `--docs` writes the table below.

<!-- scrub:begin -->

Scrubbing experiment: tested in simulation: MuJoCo on the CPU, simulated time, physics and S0 at 1 kHz, S1 at 20 Hz; the Panda is the MuJoCo Menagerie model, the dirt and its sensor are simulated; no real robot. 5 seeds per row; runtime 9f3b6b8bff29, runs of 2026-10-19, `omarchy-runtime scrub-experiment --seeds 5`.

| condition | belief error | outcomes | coverage % | force in bounds % | mean force N | peak force N | guard | verdict = truth | attribution |
|---|---|---|---|---|---|---|---|---|---|
| motion only | -2 cm | SUCCEEDED 5 | 100 (99 to 100) | 5 | 24.2 (22.9 to 24.9) | 27.9 (26.3 to 28.7) | 3 | 5 of 5 | - 5 |
| motion only | -1 cm | SUCCEEDED 5 | 100 | 91 | 16.0 (14.6 to 16.8) | 18.3 (16.7 to 19.2) | 2 | 5 of 5 | - 5 |
| motion only | +0 cm | SUCCEEDED 5 | 100 | 91 | 7.7 (6.3 to 8.5) | 9.1 (7.7 to 9.9) | 2 | 5 of 5 | - 5 |
| motion only | +1 cm | FAILED 5 | 0 | 0 | 0.0 (0.0 to 0.1) | 0.6 (0.0 to 1.8) | 2 | 5 of 5 | INTENT 5 |
| motion only | +2 cm | FAILED 5 | 0 | 0 | 0.0 | 0.0 | 2 | 5 of 5 | INTENT 5 |
| motion + contact | -2 cm | SUCCEEDED 5 | 100 | 93 | 9.7 (9.7 to 9.8) | 17.3 (17.2 to 17.3) | 2 | 5 of 5 | - 5 |
| motion + contact | -1 cm | SUCCEEDED 5 | 100 | 93 | 9.7 | 17.1 (17.1 to 17.2) | 2 | 5 of 5 | - 5 |
| motion + contact | +0 cm | SUCCEEDED 5 | 100 | 93 (93 to 94) | 9.7 | 16.9 (16.8 to 16.9) | 2 | 5 of 5 | - 5 |
| motion + contact | +1 cm | SUCCEEDED 5 | 100 | 95 | 9.8 | 16.5 (16.4 to 16.6) | 2 (2 to 3) | 5 of 5 | - 5 |
| motion + contact | +2 cm | SUCCEEDED 5 | 100 | 95 | 9.7 | 15.8 (15.4 to 15.9) | 3 (2 to 3) | 5 of 5 | - 5 |

belief error: added to the plate's height in the belief (+ : believed higher than it is). Each row: the runs' mean, then their range. coverage: dirt cells clean at the end (ground truth). force in bounds: the share of the raster window (the pad over the dirty area, at most 2 cm above the plate) with the true normal force within 5 to 20 N. guard: commands the guard clamped or replaced. verdict = truth: the verifier's verdict agrees with ground truth. attribution: the runtime's, for each failed run (- for none).

<!-- scrub:end -->

What it shows (the 50 runs above, 2026-10-19, 8 min 48 s on the development host, an Intel i7-9700 without KVM; traces and `results.json` in `~/src/omarchy-robots-evidence/R6-v0.0.11/scrub-experiment/` there): with the same path, the contact channel kept the force at its target (9.7 N on average in every row) whatever the plate's height within 2 cm either way, and all 25 runs succeeded. Motion only did as well where the belief was right, but never touched the plate when it was 1 or 2 cm lower than believed (10 runs, all FAILED, the dirt untouched), and pressed harder when it was higher: 16 N on average at 1 cm, 24 N at 2 cm, outside the 20 N bound 95 % of the time, peaking at 28.7 N. Those failures are attributed to INTENT: S0 tracked the path it was given, and nothing below S1 could know the path was wrong. The verifier agreed with ground truth in all 50 runs. The guard clamped 2 or 3 commands per run, in every row: the torque rate, during the first ticks after the controller took over from the guard's own hold; no run reached its 40 N force limit. What it does not show: anything about a real robot, a real sponge, real dirt or a real cleanliness sensor; a learned controller; other surfaces, frictions or speeds; or that these numbers transfer. The simulated contact is MuJoCo's soft contact, tuned once (a soft sponge, an elliptic friction cone), and the controller's gains were chosen on this scene.

### Videos

`omarchy-runtime scrub-render RUN_DIR --out FILE.mp4` renders a run recorded with `--record-s0` (its `world.npz`): the scene, the dirt as it was at each frame, and two bars along the bottom, the true normal force (grey below 5 N, green within 5 to 20 N, red above; white marks at 5 and 20 N; full width 30 N) and the plate's coverage (blue). Software rendering through EGL (Mesa's llvmpipe), no display; the provenance (run, seed, revision, configuration hash) goes next to the video.

## Traces

Every run writes one directory:

| File | Contents |
|---|---|
| `run.json` | Header: `trace_schema` (7), the runtime's version, revision and dirty flag, the Omarchy Robots identity (the contents of `/etc/omarchy-robot/build.json`, or `null` with "no Omarchy Robots identity" on another machine), the hash and the contents of the effective configuration, scenario, seed, clock (`simulated` or `wall`), body, S0's controller and what it honours (`s0`, its ControllerCapability), rates, the id and version of the observation filter, the verifiers, the recovery policy and the S2 latency model (with its median and spread), where S2 ran (`s2`), and the run's `tags`. |
| `events.jsonl` | Low-rate events only, one JSON object per line, flushed one by one, so a crashed run leaves a readable prefix. Each has `seq`, simulated time `t` (ns), `wall_ns`, `type`, `execution` and `payload`. A wall-clock run's `run.end` carries the loop statistics; with S2 in another process, also S2's liveness (`s2_link`). Types: `run.start`, `request`, `resolution`, `lifecycle`, `chunk` (metadata, never the rows, with `observed_at`, `inference_ns`, the `channels` and any degradation), `chunk.rejected` (the structured error of S0's last refusal), `s0.mode`, `s0.status` (at the supervisor rate), `fallback.install`, `fallback.rejected`, `s0.hold`, `verify.attempt`, `verify.evidence` (the observations a verdict rests on), `verdict`, `decision`, `cancel`, `error`, `s2.link` (S2's link up, lost, recovered with the outage's class, left, closed), `s0.late` (a late S0 tick), `s0.guard` (the guard clamped or replaced commands since the last step), `run.end`. |
| `executions.jsonl` | One summary per execution: ids, goal, chosen capability and implementation with every candidate's reason, body and action space, belief before and after, claim, verdict with its evidence, outcome and category, simulated timings (time to first chunk, duration, time in fallback and in HOLD), wall-clock costs (resolution, verification, trace writing), S0's counters, S0's `fidelity` to the intents and the failure's `attribution` (see "Fidelity and failure attribution"), configuration hash and seed. |
| `s0_<execution>.npz` | With `--record-s0`: S0's per-tick record for the execution (time, mode, position, the controller's command before the guard, chunk and its validity, when the chunk's observation was taken, and which fallback runs: none, base or S2's), kept in a preallocated buffer and written once at the end. |
| `results.json` | Written by the evaluation harness only: ground truth and agreement for each execution. |

Keys that start with `wall` are wall-clock values. Everything else follows from the scenario and its seed: the same scenario with the same seed gives the same trace.

## S2 placement experiment

> **Emulated network, VMs, soft real-time.** Two QEMU VMs, the network between them shaped with `tc netem`, Python on a general-purpose OS. Not a real network, nothing real-time, no hardware.

Where should S2 live: on the robot, on an edge server, or in the cloud (research question RQ5)? The VM test `tests/s2-placement.nix` starts a robot VM (Omarchy Robots, vm board, runtime enabled) and an edge VM. In each cell the robot runs one scenario with S2 in its own process, on the robot VM (`local`) or on the edge VM, over one network profile, with one S2 time to first token (TTFT): 0.1 s, 1 s and 5 s stand in for a model on the robot, on an edge server and in the cloud. The scenarios are the two-cup task (`s2_placement`), and the three scenes that change (see "Scenes that change"): in `cup_moved` S1 reacts alone, in `slot_blocked` and `fallback_expiry` S0 waits under the fallback until S2 chooses another spot. The S2 process's TTFT replaces the scenario's S2 latency, `fallback_expiry`'s 2 s included: the matrix shows for which placements the fallback (3 s in `slot_blocked`, 1.5 s in `fallback_expiry`) outlasts S2's answer. Delays are round trips, applied half on each VM; loss applies to every packet in each direction; with jitter, netem also reorders packets.

| Profile | Link |
|---|---|
| `local` | S2 on the robot VM, loopback |
| `lan` | about 1 ms |
| `wifi` | about 20 ms, jitter 10 ms, 1 % loss |
| `wan` | about 100 ms, jitter 20 ms |
| `degraded` | about 400 ms, jitter 100 ms, 5 % loss |
| `outage` | `lan`, then every packet dropped for 5 s during the first execution |
| `outage_long` | `lan`, then every packet dropped for 20 s during the first execution: the robot connects again |

The first table has one row for each profile and TTFT: the two-cup task time, `cup_moved`'s reaction time, the recovery time of `slot_blocked` and `fallback_expiry` (from S1 giving up to the retry running; "expired" when S2's fallback ran out before it and the capability's default took over), how many of S2's fallbacks expired, the least fallback coverage over the row's executions, and the violations, starvations and cells within tolerance over the row. Times are wall-clock, and in parentheses the same task in one process on simulated time. Every cell then follows: the outcomes, safety violations (ground truth), S0 starvation events, time in fallback and in HOLD over the whole task, the task time and its simulated time, S2's round trips (heartbeat echo), the outages the supervisor recorded with their class, S0's loop intervals and overruns, and the reaction and recovery times.

```sh
nix build .#checks.x86_64-linux.vm-s2-placement   # in nix flake check: 14 cells (below)
nix build .#s2-placement                          # every scenario x profile x TTFT 0.1, 1, 5 s; hours without KVM
nix run .#omarchy-runtime -- placement-docs result/placement/results.json README.md manual/runtime.md \
  --source "local run <start>, <commit>, <host>"   # writes the table below
```

In `nix flake check`, the two-cup task runs on lan, degraded, outage and outage_long with a TTFT of 0.1 s and 1 s, and each scene that changes on lan and outage_long with 1 s: 14 cells. `result/placement` holds `results.json`, `table.txt` and `table.md`; `result/runs` the robot's traces; `result/s2-edge` and `result/s2-robot` the S2 processes' own reports. The last command writes the table below from a full run's results. The experiments run locally (AGENTS.md, "Checks and releases"): on the development host, without KVM, their numbers are labelled "measured in VMs on the host, without KVM (QEMU TCG)", and a wall-clock tolerance that fails only because of the emulation is reported, not changed. The tables below come from earlier runs on GitHub Actions with KVM, as their captions say.

<!-- s2-placement:begin -->

S2 placement, the two-cup task and the dynamic scenarios (emulated network (tc netem), VMs, soft real-time). Measured 2026-10-07 with omarchy-runtime 0.0.2 (5c34345f5049), Omarchy Robots 0.0.2 (board vm), S0 at 200 Hz; soft real-time, Python, wall-clock in an Omarchy Robots VM (0.0.2, Python 3.13.15, 2 CPUs, virtualization kvm). Times are wall-clock; in parentheses and in the simulated column, the same task with S2 in one process on simulated time. Values: median of a cell's runs, and in brackets a distribution-free confidence interval for that median from order statistics: the narrowest pair (k-th smallest, k-th largest) whose binomial coverage is at least 95 % (with 6 runs, the smallest and the largest, 96.9 %); no interval below 6 runs. Reaction, cup_moved: from the move to S0 executing the first chunk computed from an observation taken after it. Recovery, slot_blocked, fallback_expiry: from S1 giving up to the retry running; expired: S2's fallback (3 s in slot_blocked, 1.5 s in fallback_expiry) ran out before the retry started, and S0 went on under the capability's default. Fallback coverage, the least fraction of an execution's running time during which S0 held a valid fallback, as S0 counted it. S2 RTT: heartbeat echo measured by S2: the network both ways, plus up to one loop period at each end. Source: CI run 37603846510 (https://github.com/promaaa/omarchy-robots/actions/runs/37603846510), the repeated matrix on 5c34345, one scenario per job, each alone on a GitHub ubuntu-24.04 runner, 2 CPUs, KVM, each VM 2 vCPUs; the four parts merged with placement-merge. The 6 runs of a cell use the scenario's seed, so S2 draws the same TTFTs from a measured distribution in each: the interval is the run-to-run variation of the wall clock and the emulated network, not the spread of the TTFT distribution (its p05 to p95 is in the TTFT table).

| profile | TTFT s | two-cup task s | cup_moved reaction s | slot_blocked recovery s | fallback_expiry recovery s | S2 fallbacks expired | fallback coverage | violations | S0 starvations | runs within tolerance |
|---|---|---|---|---|---|---|---|---|---|---|
| local | 0.1 | 18.60 [18.45, 18.80] (18.45) | 0.10 [0.10, 0.10] (0.10) | 0.25 [0.15, 0.25] (0.15) | 0.25 [0.15, 0.25] (0.15) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| local | 1.0 | 20.58 [20.40, 20.60] (20.35) | 0.10 [0.10, 0.10] (0.10) | 1.05 [1.05, 1.15] (1.05) | 1.10 [1.05, 1.15] (1.05) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| local | 5.0 | 28.50 [28.40, 28.70] (28.30) | 0.10 [0.10, 0.10] (0.10) | 5.15 [5.05, 5.20] (5.05) | 5.12 [5.05, 5.15] (5.10) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| local | qwen2.5-1.5b-q4km-host (median 8.50 s) | 35.95 [35.95, 35.95] (35.75) | 0.10 [0.10, 0.10] (0.10) | 8.60 [8.60, 8.60] (8.55) | 8.55 [8.55, 8.55] (8.55) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| local | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 25.45 [25.45, 25.45] (25.30) | 0.10 [0.10, 0.10] (0.10) | 3.85 [3.85, 3.90] (3.90) | 3.85 [3.85, 3.85] (3.85) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| lan | 0.1 | 18.68 [18.45, 18.80] (18.45) | 0.10 [0.10, 0.10] (0.10) | 0.15 [0.15, 0.30] (0.15) | 0.25 [0.15, 0.25] (0.15) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| lan | 1.0 | 20.58 [20.40, 20.60] (20.35) | 0.10 [0.09, 0.10] (0.10) | 1.05 [1.05, 1.15] (1.05) | 1.15 [1.05, 1.15] (1.05) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| lan | 5.0 | 28.50 [28.35, 28.50] (28.30) | 0.10 [0.10, 0.10] (0.10) | 5.05 [5.05, 5.25] (5.05) | 5.15 [5.05, 5.20] (5.10) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| lan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 35.90 [35.85, 35.95] (35.75) | 0.10 [0.09, 0.10] (0.10) | 8.60 [8.55, 8.60] (8.55) | 8.55 [8.55, 8.55] (8.55) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| lan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 25.45 [25.30, 25.45] (25.30) | 0.10 [0.10, 0.10] (0.10) | 3.85 [3.85, 3.90] (3.90) | 3.85 [3.85, 3.85] (3.85) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wifi | 0.1 | 18.70 [18.45, 18.90] (18.45) | 0.10 [0.10, 0.10] (0.10) | 0.23 [0.15, 0.30] (0.15) | 0.35 [0.25, 0.35] (0.15) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wifi | 1.0 | 20.55 [20.40, 20.65] (20.35) | 0.10 [0.10, 0.10] (0.10) | 1.15 [1.05, 1.20] (1.05) | 1.15 [1.15, 1.25] (1.05) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wifi | 5.0 | 28.50 [28.40, 28.70] (28.30) | 0.10 [0.10, 0.10] (0.10) | 5.05 [5.05, 5.20] (5.05) | 5.17 [5.05, 5.25] (5.10) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wifi | qwen2.5-1.5b-q4km-host (median 8.50 s) | 35.95 [35.95, 36.10] (35.75) | 0.10 [0.10, 0.10] (0.10) | 8.65 [8.60, 8.70] (8.55) | 8.65 [8.55, 8.65] (8.55) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wifi | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 25.45 [25.45, 25.45] (25.30) | 0.10 [0.10, 0.10] (0.10) | 3.90 [3.85, 3.90] (3.90) | 3.95 [3.85, 3.95] (3.85) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wan | 0.1 | 18.63 [18.60, 18.80] (18.45) | 0.10 [0.10, 0.10] (0.10) | 0.32 [0.25, 0.35] (0.15) | 0.25 [0.25, 0.35] (0.15) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wan | 1.0 | 20.60 [20.55, 20.70] (20.35) | 0.10 [0.10, 0.10] (0.10) | 1.15 [1.15, 1.25] (1.05) | 1.25 [1.15, 1.35] (1.05) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wan | 5.0 | 28.58 [28.50, 28.65] (28.30) | 0.10 [0.10, 0.10] (0.10) | 5.25 [5.15, 5.30] (5.05) | 5.20 [5.15, 5.25] (5.10) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 36.00 [35.95, 36.00] (35.75) | 0.10 [0.10, 0.10] (0.10) | 8.70 [8.70, 8.70] (8.55) | 8.65 [8.65, 8.75] (8.55) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| wan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 25.55 [25.55, 25.55] (25.30) | 0.10 [0.10, 0.10] (0.10) | 3.97 [3.95, 4.00] (3.90) | 3.95 [3.95, 3.95] (3.85) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| degraded | 0.1 | 19.70 [19.30, 20.35] (18.45) | 0.10 [0.10, 0.10] (0.10) | 0.95 [0.70, 1.35] (0.15) | 1.27 [0.90, 1.50] (0.15) | 0 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| degraded | 1.0 | 21.35 [20.95, 21.80] (20.35) | 0.10 [0.10, 0.10] (0.10) | 2.10 [1.60, 3.30] (1.05) | 1.75 [1.50, 1.95] (1.05) | 6 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| degraded | 5.0 | 29.23 [28.90, 29.70] (28.30) | 0.10 [0.10, 0.10] (0.10) | 5.57 [5.55, 7.00] (5.05) | 5.80 [5.50, 6.30] (5.10) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| degraded | qwen2.5-1.5b-q4km-host (median 8.50 s) | 36.60 [36.35, 37.05] (35.75) | 0.10 [0.10, 0.10] (0.10) | 9.55 [9.10, 9.80] (8.55) | 9.40 [9.20, 10.95] (8.55) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| degraded | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 26.23 [26.05, 26.75] (25.30) | 0.10 [0.10, 0.10] (0.10) | 4.62 [4.30, 5.15] (3.90) | 4.52 [4.25, 5.10] (3.85) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage | 0.1 | 18.53 [18.45, 18.60] (18.45) | 0.10 [0.10, 0.10] (0.10) | 3.75 [3.65, 3.85] (0.15) | 4.35 [3.95, 4.45] (0.15) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage | 1.0 | 20.70 [20.55, 21.50] (20.35) | 0.10 [0.10, 0.10] (0.10) | 4.85 [4.75, 4.95] (1.05) | 5.25 [5.15, 5.45] (1.05) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage | 5.0 | 29.05 [29.00, 29.20] (28.30) | 0.10 [0.10, 0.10] (0.10) | 9.40 [9.30, 9.65] (5.05) | 8.60 [8.45, 8.70] (5.10) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage | qwen2.5-1.5b-q4km-host (median 8.50 s) | 36.25 [36.20, 36.60] (35.75) | 0.10 [0.10, 0.10] (0.10) | 12.75 [12.40, 12.80] (8.55) | 12.80 [12.75, 12.95] (8.55) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 25.75 [25.55, 26.30] (25.30) | 0.10 [0.10, 0.10] (0.10) | 8.27 [8.05, 8.40] (3.90) | 8.15 [7.35, 8.25] (3.85) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage_long | 0.1 | 36.35 [35.45, 36.70] (18.45) | 0.10 [0.09, 0.10] (0.10) | 21.10 [20.65, 21.70] (0.15) | 21.10 [20.65, 21.35] (0.15) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage_long | 1.0 | 37.93 [37.30, 38.35] (20.35) | 0.10 [0.10, 0.10] (0.10) | 21.60 [21.45, 22.65] (1.05) | 22.05 [21.85, 22.35] (1.05) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage_long | 5.0 | 45.75 [45.65, 46.10] (28.30) | 0.10 [0.10, 0.10] (0.10) | 26.15 [25.95, 26.40] (5.05) | 25.77 [25.65, 26.55] (5.10) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage_long | qwen2.5-1.5b-q4km-host (median 8.50 s) | 53.00 [52.90, 53.25] (35.75) | 0.10 [0.10, 0.10] (0.10) | 29.35 [29.10, 29.80] (8.55) | 29.75 [29.45, 29.85] (8.55) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |
| outage_long | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 42.75 [42.30, 43.30] (25.30) | 0.10 [0.10, 0.10] (0.10) | 24.87 [24.75, 24.95] (3.90) | 24.80 [24.15, 25.25] (3.85) | 12 of 12 | 100.0 % | 0 | 0 | 24 of 24 |

<details markdown="1"><summary>Every cell (840)</summary>

| scenario | profile | TTFT s | runs | outcomes | violations | S0 starvations | task s (simulated) | reaction s | recovery s | fallback engaged | S2 fallbacks expired | fallback coverage | S2 RTT p50 ms | long outage s | S0 overruns, most | runs within tolerance |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cup_moved | local | 0.1 | 6 | SUCCEEDED | 0 | 0 | 9.20 [9.15, 9.20] (9.15) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 1.01 [0.98, 1.57] | - | 2 | 6 of 6 |
| fallback_expiry | local | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 8.25 [8.10, 8.25] (8.05) | - | 0.25 [0.15, 0.25] | 6 of 6 | 0 of 6 | 100.0 % | 1.01 [0.98, 2.00] | - | 1 | 6 of 6 |
| s2_placement | local | 0.1 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 18.60 [18.45, 18.80] (18.45) | - | - | - | - | 100.0 % | 1.05 [1.01, 2.00] | - | 2 | 6 of 6 |
| slot_blocked | local | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 8.25 [8.25, 8.26] (8.10) | - | 0.25 [0.15, 0.25] | 6 of 6 | 0 of 6 | 100.0 % | 1.10 [1.01, 2.00] | - | 10 | 6 of 6 |
| cup_moved | local | 1.0 | 6 | SUCCEEDED | 0 | 0 | 10.25 [10.05, 10.25] (10.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 1.07 [0.99, 2.00] | - | 0 | 6 of 6 |
| fallback_expiry | local | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 10.30 [10.30, 10.40] (10.15) | - | 1.10 [1.05, 1.15] | 6 of 6 | 0 of 6 | 100.0 % | 1.19 [1.01, 2.01] | - | 0 | 6 of 6 |
| s2_placement | local | 1.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 20.58 [20.40, 20.60] (20.35) | - | - | - | - | 100.0 % | 1.05 [1.01, 1.87] | - | 0 | 6 of 6 |
| slot_blocked | local | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 10.30 [10.30, 10.40] (10.15) | - | 1.05 [1.05, 1.15] | 6 of 6 | 0 of 6 | 100.0 % | 1.51 [1.01, 2.01] | - | 0 | 6 of 6 |
| cup_moved | local | 5.0 | 6 | SUCCEEDED | 0 | 0 | 14.20 [14.20, 14.20] (14.05) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 1.48 [1.01, 2.00] | - | 0 | 6 of 6 |
| fallback_expiry | local | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.45 [18.30, 18.50] (18.25) | - | 5.12 [5.05, 5.15] | 6 of 6 | 6 of 6 | 100.0 % | 1.52 [0.98, 2.00] | - | 0 | 6 of 6 |
| s2_placement | local | 5.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 28.50 [28.40, 28.70] (28.30) | - | - | - | - | 100.0 % | 1.03 [0.98, 2.00] | - | 0 | 6 of 6 |
| slot_blocked | local | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.40 [18.30, 18.50] (18.25) | - | 5.15 [5.05, 5.20] | 6 of 6 | 6 of 6 | 100.0 % | 1.52 [0.97, 2.00] | - | 0 | 6 of 6 |
| cup_moved | local | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED | 0 | 0 | 17.65 [17.60, 17.65] (17.60) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 1.48 [0.99, 2.00] | - | 0 | 6 of 6 |
| fallback_expiry | local | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 25.30 [25.30, 25.30] (25.25) | - | 8.55 [8.55, 8.55] | 6 of 6 | 6 of 6 | 100.0 % | 1.01 [0.99, 2.00] | - | 1 | 6 of 6 |
| s2_placement | local | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 35.95 [35.95, 35.95] (35.75) | - | - | - | - | 100.0 % | 1.02 [1.01, 1.82] | - | 0 | 6 of 6 |
| slot_blocked | local | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 25.40 [25.40, 25.40] (25.25) | - | 8.60 [8.60, 8.60] | 6 of 6 | 6 of 6 | 100.0 % | 1.02 [1.00, 2.01] | - | 0 | 6 of 6 |
| cup_moved | local | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED | 0 | 0 | 13.05 [13.05, 13.05] (13.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 1.52 [1.03, 2.00] | - | 4 | 6 of 6 |
| fallback_expiry | local | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.00 [16.00, 16.00] (15.95) | - | 3.85 [3.85, 3.85] | 6 of 6 | 6 of 6 | 100.0 % | 1.01 [1.01, 2.00] | - | 0 | 6 of 6 |
| s2_placement | local | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 25.45 [25.45, 25.45] (25.30) | - | - | - | - | 100.0 % | 1.04 [1.02, 2.01] | - | 1 | 6 of 6 |
| slot_blocked | local | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.00 [16.00, 16.00] (15.95) | - | 3.85 [3.85, 3.90] | 6 of 6 | 6 of 6 | 100.0 % | 1.52 [1.02, 2.01] | - | 0 | 6 of 6 |
| cup_moved | lan | 0.1 | 6 | SUCCEEDED | 0 | 0 | 9.35 [9.30, 9.45] (9.15) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.18 [3.14, 3.99] | - | 1 | 6 of 6 |
| fallback_expiry | lan | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 8.25 [8.10, 8.35] (8.05) | - | 0.25 [0.15, 0.25] | 6 of 6 | 0 of 6 | 100.0 % | 3.62 [3.01, 4.01] | - | 0 | 6 of 6 |
| s2_placement | lan | 0.1 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 18.68 [18.45, 18.80] (18.45) | - | - | - | - | 100.0 % | 3.47 [3.05, 4.00] | - | 0 | 6 of 6 |
| slot_blocked | lan | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 8.15 [8.10, 8.45] (8.10) | - | 0.15 [0.15, 0.30] | 6 of 6 | 0 of 6 | 100.0 % | 3.57 [3.05, 3.99] | - | 0 | 6 of 6 |
| cup_moved | lan | 1.0 | 6 | SUCCEEDED | 0 | 0 | 10.25 [10.05, 10.30] (10.00) | 0.10 [0.09, 0.10] | - | - | - | 100.0 % | 3.46 [3.04, 3.98] | - | 1 | 6 of 6 |
| fallback_expiry | lan | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 10.40 [10.30, 10.40] (10.15) | - | 1.15 [1.05, 1.15] | 6 of 6 | 0 of 6 | 100.0 % | 3.02 [3.00, 4.00] | - | 0 | 6 of 6 |
| s2_placement | lan | 1.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 20.58 [20.40, 20.60] (20.35) | - | - | - | - | 100.0 % | 3.69 [3.13, 4.00] | - | 0 | 6 of 6 |
| slot_blocked | lan | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 10.30 [10.20, 10.45] (10.15) | - | 1.05 [1.05, 1.15] | 6 of 6 | 0 of 6 | 100.0 % | 3.95 [3.17, 4.01] | - | 1 | 6 of 6 |
| cup_moved | lan | 5.0 | 6 | SUCCEEDED | 0 | 0 | 14.20 [14.10, 14.25] (14.05) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.57 [3.13, 3.98] | - | 0 | 6 of 6 |
| fallback_expiry | lan | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.45 [18.30, 18.50] (18.25) | - | 5.15 [5.05, 5.20] | 6 of 6 | 6 of 6 | 100.0 % | 3.01 [3.01, 3.03] | - | 0 | 6 of 6 |
| s2_placement | lan | 5.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 28.50 [28.35, 28.50] (28.30) | - | - | - | - | 100.0 % | 3.84 [3.11, 4.00] | - | 1 | 6 of 6 |
| slot_blocked | lan | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.43 [18.40, 18.55] (18.25) | - | 5.05 [5.05, 5.25] | 6 of 6 | 6 of 6 | 100.0 % | 3.23 [3.12, 4.00] | - | 1 | 6 of 6 |
| cup_moved | lan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED | 0 | 0 | 17.60 [17.60, 17.65] (17.60) | 0.10 [0.09, 0.10] | - | - | - | 100.0 % | 3.50 [3.10, 3.98] | - | 1 | 6 of 6 |
| fallback_expiry | lan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 25.30 [25.30, 25.30] (25.25) | - | 8.55 [8.55, 8.55] | 6 of 6 | 6 of 6 | 100.0 % | 3.01 [2.99, 3.03] | - | 0 | 6 of 6 |
| s2_placement | lan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 35.90 [35.85, 35.95] (35.75) | - | - | - | - | 100.0 % | 4.00 [3.04, 4.00] | - | 1 | 6 of 6 |
| slot_blocked | lan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 25.40 [25.35, 25.40] (25.25) | - | 8.60 [8.55, 8.60] | 6 of 6 | 6 of 6 | 100.0 % | 3.25 [3.06, 4.00] | - | 1 | 6 of 6 |
| cup_moved | lan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED | 0 | 0 | 13.05 [13.00, 13.05] (13.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.95 [3.08, 3.98] | - | 1 | 6 of 6 |
| fallback_expiry | lan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.00 [16.00, 16.00] (15.95) | - | 3.85 [3.85, 3.85] | 6 of 6 | 6 of 6 | 100.0 % | 3.02 [3.00, 3.97] | - | 0 | 6 of 6 |
| s2_placement | lan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 25.45 [25.30, 25.45] (25.30) | - | - | - | - | 100.0 % | 3.58 [3.07, 4.00] | - | 2 | 6 of 6 |
| slot_blocked | lan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.00 [16.00, 16.00] (15.95) | - | 3.85 [3.85, 3.90] | 6 of 6 | 6 of 6 | 100.0 % | 3.94 [3.15, 4.01] | - | 0 | 6 of 6 |
| cup_moved | wifi | 0.1 | 6 | SUCCEEDED | 0 | 0 | 9.20 [9.20, 9.30] (9.15) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 23.18 [21.81, 24.01] | - | 1 | 6 of 6 |
| fallback_expiry | wifi | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 8.35 [8.25, 8.35] (8.05) | - | 0.35 [0.25, 0.35] | 6 of 6 | 0 of 6 | 100.0 % | 23.01 [21.95, 24.00] | - | 0 | 6 of 6 |
| s2_placement | wifi | 0.1 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 18.70 [18.45, 18.90] (18.45) | - | - | - | - | 100.0 % | 22.98 [22.02, 23.28] | - | 2 | 6 of 6 |
| slot_blocked | wifi | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 8.20 [8.05, 8.30] (8.10) | - | 0.23 [0.15, 0.30] | 6 of 6 | 0 of 6 | 100.0 % | 23.03 [22.15, 23.21] | - | 0 | 6 of 6 |
| cup_moved | wifi | 1.0 | 6 | SUCCEEDED | 0 | 0 | 10.25 [10.05, 10.30] (10.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 23.03 [22.12, 23.97] | - | 0 | 6 of 6 |
| fallback_expiry | wifi | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 10.40 [10.40, 10.55] (10.15) | - | 1.15 [1.15, 1.25] | 6 of 6 | 0 of 6 | 100.0 % | 22.50 [21.01, 23.04] | - | 0 | 6 of 6 |
| s2_placement | wifi | 1.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 20.55 [20.40, 20.65] (20.35) | - | - | - | - | 100.0 % | 22.97 [22.22, 24.00] | - | 0 | 6 of 6 |
| slot_blocked | wifi | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 10.33 [10.30, 10.40] (10.15) | - | 1.15 [1.05, 1.20] | 6 of 6 | 0 of 6 | 100.0 % | 22.98 [22.73, 23.36] | - | 0 | 6 of 6 |
| cup_moved | wifi | 5.0 | 6 | SUCCEEDED | 0 | 0 | 14.20 [14.10, 14.20] (14.05) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 22.97 [22.06, 23.12] | - | 1 | 6 of 6 |
| fallback_expiry | wifi | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.50 [18.40, 18.60] (18.25) | - | 5.17 [5.05, 5.25] | 6 of 6 | 6 of 6 | 100.0 % | 22.98 [22.02, 23.03] | - | 0 | 6 of 6 |
| s2_placement | wifi | 5.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 28.50 [28.40, 28.70] (28.30) | - | - | - | - | 100.0 % | 22.69 [22.09, 23.03] | - | 3 | 6 of 6 |
| slot_blocked | wifi | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.40 [18.40, 18.50] (18.25) | - | 5.05 [5.05, 5.20] | 6 of 6 | 6 of 6 | 100.0 % | 23.03 [22.08, 23.97] | - | 0 | 6 of 6 |
| cup_moved | wifi | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED | 0 | 0 | 17.65 [17.60, 17.65] (17.60) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 23.01 [22.15, 23.25] | - | 1 | 6 of 6 |
| fallback_expiry | wifi | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 25.40 [25.30, 25.40] (25.25) | - | 8.65 [8.55, 8.65] | 6 of 6 | 6 of 6 | 100.0 % | 22.96 [22.01, 22.99] | - | 0 | 6 of 6 |
| s2_placement | wifi | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 35.95 [35.95, 36.10] (35.75) | - | - | - | - | 100.0 % | 22.95 [22.68, 23.01] | - | 3 | 6 of 6 |
| slot_blocked | wifi | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 25.45 [25.40, 25.50] (25.25) | - | 8.65 [8.60, 8.70] | 6 of 6 | 6 of 6 | 100.0 % | 23.01 [22.18, 23.16] | - | 1 | 6 of 6 |
| cup_moved | wifi | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED | 0 | 0 | 13.05 [12.95, 13.05] (13.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 23.01 [22.09, 24.24] | - | 1 | 6 of 6 |
| fallback_expiry | wifi | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.10 [16.00, 16.10] (15.95) | - | 3.95 [3.85, 3.95] | 6 of 6 | 6 of 6 | 100.0 % | 22.94 [22.04, 23.00] | - | 1 | 6 of 6 |
| s2_placement | wifi | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 25.45 [25.45, 25.45] (25.30) | - | - | - | - | 100.0 % | 23.00 [22.45, 23.12] | - | 1 | 6 of 6 |
| slot_blocked | wifi | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.00 [16.00, 16.00] (15.95) | - | 3.90 [3.85, 3.90] | 6 of 6 | 6 of 6 | 100.0 % | 23.01 [22.03, 24.64] | - | 0 | 6 of 6 |
| cup_moved | wan | 0.1 | 6 | SUCCEEDED | 0 | 0 | 9.20 [9.20, 9.20] (9.15) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 102.54 [101.25, 104.01] | - | 2 | 6 of 6 |
| fallback_expiry | wan | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 8.25 [8.25, 8.45] (8.05) | - | 0.25 [0.25, 0.35] | 6 of 6 | 0 of 6 | 100.0 % | 103.05 [103.00, 106.04] | - | 0 | 6 of 6 |
| s2_placement | wan | 0.1 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 18.63 [18.60, 18.80] (18.45) | - | - | - | - | 100.0 % | 102.67 [101.99, 103.00] | - | 0 | 6 of 6 |
| slot_blocked | wan | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 8.40 [8.25, 8.40] (8.10) | - | 0.32 [0.25, 0.35] | 6 of 6 | 0 of 6 | 100.0 % | 103.08 [101.10, 104.08] | - | 0 | 6 of 6 |
| cup_moved | wan | 1.0 | 6 | SUCCEEDED | 0 | 0 | 10.05 [10.05, 10.05] (10.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 103.04 [101.96, 104.15] | - | 1 | 6 of 6 |
| fallback_expiry | wan | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 10.45 [10.40, 10.55] (10.15) | - | 1.25 [1.15, 1.35] | 6 of 6 | 0 of 6 | 100.0 % | 101.97 [100.93, 104.02] | - | 2 | 6 of 6 |
| s2_placement | wan | 1.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 20.60 [20.55, 20.70] (20.35) | - | - | - | - | 100.0 % | 102.10 [101.00, 104.00] | - | 0 | 6 of 6 |
| slot_blocked | wan | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 10.30 [10.30, 10.45] (10.15) | - | 1.15 [1.15, 1.25] | 6 of 6 | 0 of 6 | 100.0 % | 102.23 [102.02, 104.03] | - | 1 | 6 of 6 |
| cup_moved | wan | 5.0 | 6 | SUCCEEDED | 0 | 0 | 14.10 [14.10, 14.20] (14.05) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 103.08 [101.17, 104.66] | - | 2 | 6 of 6 |
| fallback_expiry | wan | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.55 [18.50, 18.60] (18.25) | - | 5.20 [5.15, 5.25] | 6 of 6 | 6 of 6 | 100.0 % | 102.02 [101.93, 103.99] | - | 0 | 6 of 6 |
| s2_placement | wan | 5.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 28.58 [28.50, 28.65] (28.30) | - | - | - | - | 100.0 % | 102.22 [102.03, 103.01] | - | 1 | 6 of 6 |
| slot_blocked | wan | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.50 [18.40, 18.60] (18.25) | - | 5.25 [5.15, 5.30] | 6 of 6 | 6 of 6 | 100.0 % | 103.07 [102.03, 103.91] | - | 2 | 6 of 6 |
| cup_moved | wan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED | 0 | 0 | 17.60 [17.60, 17.65] (17.60) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 103.06 [101.59, 104.77] | - | 2 | 6 of 6 |
| fallback_expiry | wan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 25.40 [25.40, 25.50] (25.25) | - | 8.65 [8.65, 8.75] | 6 of 6 | 6 of 6 | 100.0 % | 102.02 [101.22, 103.06] | - | 2 | 6 of 6 |
| s2_placement | wan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 36.00 [35.95, 36.00] (35.75) | - | - | - | - | 100.0 % | 102.26 [102.02, 102.79] | - | 1 | 6 of 6 |
| slot_blocked | wan | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 25.50 [25.50, 25.50] (25.25) | - | 8.70 [8.70, 8.70] | 6 of 6 | 6 of 6 | 100.0 % | 103.00 [102.01, 104.01] | - | 1 | 6 of 6 |
| cup_moved | wan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED | 0 | 0 | 13.05 [13.05, 13.05] (13.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 103.01 [102.20, 104.07] | - | 1 | 6 of 6 |
| fallback_expiry | wan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.10 [16.10, 16.10] (15.95) | - | 3.95 [3.95, 3.95] | 6 of 6 | 6 of 6 | 100.0 % | 103.03 [102.04, 104.00] | - | 1 | 6 of 6 |
| s2_placement | wan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 25.55 [25.55, 25.55] (25.30) | - | - | - | - | 100.0 % | 102.62 [101.96, 102.99] | - | 1 | 6 of 6 |
| slot_blocked | wan | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.10 [16.10, 16.10] (15.95) | - | 3.97 [3.95, 4.00] | 6 of 6 | 6 of 6 | 100.0 % | 102.59 [102.01, 103.00] | - | 1 | 6 of 6 |
| cup_moved | degraded | 0.1 | 6 | SUCCEEDED | 0 | 0 | 9.25 [9.20, 9.30] (9.15) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 531.55 [417.89, 754.09] | - | 1 | 6 of 6 |
| fallback_expiry | degraded | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 9.58 [9.05, 9.85] (8.05) | - | 1.27 [0.90, 1.50] | 6 of 6 | 0 of 6 | 100.0 % | 526.28 [433.97, 732.51] | - | 0 | 6 of 6 |
| s2_placement | degraded | 0.1 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 19.70 [19.30, 20.35] (18.45) | - | - | - | - | 100.0 % | 601.02 [571.06, 809.75] | - | 0 | 6 of 6 |
| slot_blocked | degraded | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 9.28 [8.85, 10.05] (8.10) | - | 0.95 [0.70, 1.35] | 6 of 6 | 0 of 6 | 100.0 % | 502.16 [425.58, 621.97] | - | 1 | 6 of 6 |
| cup_moved | degraded | 1.0 | 6 | SUCCEEDED | 0 | 0 | 10.20 [10.05, 10.45] (10.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 483.46 [455.16, 583.05] | - | 1 | 6 of 6 |
| fallback_expiry | degraded | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 11.05 [10.80, 11.40] (10.15) | - | 1.75 [1.50, 1.95] | 6 of 6 | 5 of 6 | 100.0 % | 500.36 [429.02, 589.54] | - | 0 | 6 of 6 |
| s2_placement | degraded | 1.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 21.35 [20.95, 21.80] (20.35) | - | - | - | - | 100.0 % | 548.21 [485.04, 718.31] | - | 1 | 6 of 6 |
| slot_blocked | degraded | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 11.55 [10.95, 12.55] (10.15) | - | 2.10 [1.60, 3.30] | 6 of 6 | 1 of 6 | 100.0 % | 695.99 [507.03, 742.02] | - | 0 | 6 of 6 |
| cup_moved | degraded | 5.0 | 6 | SUCCEEDED | 0 | 0 | 14.23 [14.10, 14.75] (14.05) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 612.20 [428.05, 808.03] | - | 0 | 6 of 6 |
| fallback_expiry | degraded | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 19.15 [18.80, 19.60] (18.25) | - | 5.80 [5.50, 6.30] | 6 of 6 | 6 of 6 | 100.0 % | 516.24 [474.04, 686.99] | - | 0 | 6 of 6 |
| s2_placement | degraded | 5.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 29.23 [28.90, 29.70] (28.30) | - | - | - | - | 100.0 % | 586.03 [545.02, 676.01] | - | 1 | 6 of 6 |
| slot_blocked | degraded | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 18.88 [18.35, 20.35] (18.25) | - | 5.57 [5.55, 7.00] | 6 of 6 | 6 of 6 | 100.0 % | 460.16 [441.99, 617.72] | - | 1 | 6 of 6 |
| cup_moved | degraded | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED | 0 | 0 | 17.65 [17.60, 17.90] (17.60) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 596.85 [461.35, 694.84] | 2.05 | 2 | 6 of 6 |
| fallback_expiry | degraded | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 26.25 [26.10, 27.80] (25.25) | - | 9.40 [9.20, 10.95] | 6 of 6 | 6 of 6 | 100.0 % | 594.74 [465.00, 707.08] | - | 0 | 6 of 6 |
| s2_placement | degraded | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 36.60 [36.35, 37.05] (35.75) | - | - | - | - | 100.0 % | 551.02 [489.09, 711.84] | - | 0 | 6 of 6 |
| slot_blocked | degraded | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 26.63 [25.35, 27.10] (25.25) | - | 9.55 [9.10, 9.80] | 6 of 6 | 6 of 6 | 100.0 % | 547.84 [487.92, 731.13] | - | 0 | 6 of 6 |
| cup_moved | degraded | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED | 0 | 0 | 13.05 [12.95, 13.90] (13.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 509.12 [421.41, 628.10] | - | 1 | 6 of 6 |
| fallback_expiry | degraded | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.68 [16.40, 17.40] (15.95) | - | 4.52 [4.25, 5.10] | 6 of 6 | 6 of 6 | 100.0 % | 512.51 [472.52, 598.00] | - | 0 | 6 of 6 |
| s2_placement | degraded | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 26.23 [26.05, 26.75] (25.30) | - | - | - | - | 100.0 % | 528.02 [440.67, 643.60] | - | 0 | 6 of 6 |
| slot_blocked | degraded | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 16.80 [16.00, 17.75] (15.95) | - | 4.62 [4.30, 5.15] | 6 of 6 | 6 of 6 | 100.0 % | 545.10 [444.93, 629.10] | - | 0 | 6 of 6 |
| cup_moved | outage | 0.1 | 6 | SUCCEEDED | 0 | 0 | 9.30 [9.20, 9.35] (9.15) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.37 [3.05, 3.99] | 5.75 [5.75, 5.80] | 1 | 6 of 6 |
| fallback_expiry | outage | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 12.70 [12.40, 12.80] (8.05) | - | 4.35 [3.95, 4.45] | 6 of 6 | 6 of 6 | 100.0 % | 3.01 [3.01, 3.12] | 5.75 [5.75, 5.80] | 0 | 6 of 6 |
| s2_placement | outage | 0.1 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 18.53 [18.45, 18.60] (18.45) | - | - | - | - | 100.0 % | 3.27 [3.01, 4.00] | 5.75 [5.75, 5.80] | 1 | 6 of 6 |
| slot_blocked | outage | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 12.10 [12.10, 12.20] (8.10) | - | 3.75 [3.65, 3.85] | 6 of 6 | 6 of 6 | 100.0 % | 3.29 [3.04, 3.97] | 5.75 [5.75, 5.80] | 0 | 6 of 6 |
| cup_moved | outage | 1.0 | 6 | SUCCEEDED | 0 | 0 | 10.25 [10.05, 10.30] (10.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.33 [3.04, 3.99] | 5.75 [5.75, 5.80] | 0 | 6 of 6 |
| fallback_expiry | outage | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 14.60 [14.50, 14.70] (10.15) | - | 5.25 [5.15, 5.45] | 6 of 6 | 6 of 6 | 100.0 % | 3.01 [3.00, 3.04] | 5.75 [5.75, 5.80] | 1 | 6 of 6 |
| s2_placement | outage | 1.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 20.70 [20.55, 21.50] (20.35) | - | - | - | - | 100.0 % | 3.87 [3.06, 4.00] | 5.75 [5.75, 5.75] | 1 | 6 of 6 |
| slot_blocked | outage | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 14.20 [14.10, 14.25] (10.15) | - | 4.85 [4.75, 4.95] | 6 of 6 | 6 of 6 | 100.0 % | 3.61 [3.08, 4.00] | 5.75 [5.75, 5.75] | 0 | 6 of 6 |
| cup_moved | outage | 5.0 | 6 | SUCCEEDED | 0 | 0 | 14.20 [14.10, 14.25] (14.05) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.53 [3.07, 3.99] | 5.77 [5.75, 5.80] | 1 | 6 of 6 |
| fallback_expiry | outage | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 21.90 [21.80, 22.00] (18.25) | - | 8.60 [8.45, 8.70] | 6 of 6 | 6 of 6 | 100.0 % | 3.01 [3.01, 3.92] | 5.75 [5.75, 5.75] | 3 | 6 of 6 |
| s2_placement | outage | 5.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 29.05 [29.00, 29.20] (28.30) | - | - | - | - | 100.0 % | 3.44 [3.11, 4.00] | 5.75 [5.75, 5.80] | 1 | 6 of 6 |
| slot_blocked | outage | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 22.65 [22.60, 22.90] (18.25) | - | 9.40 [9.30, 9.65] | 6 of 6 | 6 of 6 | 100.0 % | 3.95 [3.05, 4.00] | 5.75 [5.75, 5.80] | 0 | 6 of 6 |
| cup_moved | outage | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED | 0 | 0 | 17.65 [17.60, 17.65] (17.60) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.80 [3.32, 3.99] | 5.75 [5.75, 5.80] | 1 | 6 of 6 |
| fallback_expiry | outage | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 29.55 [29.50, 29.70] (25.25) | - | 12.80 [12.75, 12.95] | 6 of 6 | 6 of 6 | 100.0 % | 3.88 [3.01, 4.01] | 5.75 [5.75, 5.80] | 7 | 6 of 6 |
| s2_placement | outage | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 36.25 [36.20, 36.60] (35.75) | - | - | - | - | 100.0 % | 3.25 [3.09, 3.96] | 5.75 [5.75, 5.80] | 0 | 6 of 6 |
| slot_blocked | outage | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 29.55 [29.20, 29.60] (25.25) | - | 12.75 [12.40, 12.80] | 6 of 6 | 6 of 6 | 100.0 % | 3.66 [3.07, 4.00] | 5.75 [5.75, 5.80] | 1 | 6 of 6 |
| cup_moved | outage | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED | 0 | 0 | 13.03 [13.00, 13.05] (13.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.53 [3.03, 3.96] | 5.75 [5.75, 5.80] | 2 | 6 of 6 |
| fallback_expiry | outage | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 20.30 [19.50, 20.40] (15.95) | - | 8.15 [7.35, 8.25] | 6 of 6 | 6 of 6 | 100.0 % | 3.62 [3.07, 4.00] | 5.75 [5.75, 5.75] | 3 | 6 of 6 |
| s2_placement | outage | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 25.75 [25.55, 26.30] (25.30) | - | - | - | - | 100.0 % | 3.14 [3.07, 3.99] | 5.75 [5.75, 5.75] | 1 | 6 of 6 |
| slot_blocked | outage | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 20.40 [20.20, 20.50] (15.95) | - | 8.27 [8.05, 8.40] | 6 of 6 | 6 of 6 | 100.0 % | 3.99 [3.95, 4.00] | 5.75 [5.75, 5.75] | 0 | 6 of 6 |
| cup_moved | outage_long | 0.1 | 6 | SUCCEEDED | 0 | 0 | 9.20 [9.20, 9.30] (9.15) | 0.10 [0.09, 0.10] | - | - | - | 100.0 % | 3.98 [3.90, 4.02] | 22.75 [22.75, 22.75] | 1 | 6 of 6 |
| fallback_expiry | outage_long | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 29.45 [29.10, 29.70] (8.05) | - | 21.10 [20.65, 21.35] | 6 of 6 | 6 of 6 | 100.0 % | 3.99 [3.13, 4.01] | 22.77 [22.75, 22.80] | 19 | 6 of 6 |
| s2_placement | outage_long | 0.1 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 36.35 [35.45, 36.70] (18.45) | - | - | - | - | 100.0 % | 3.93 [3.25, 4.01] | 22.75 [22.75, 22.80] | 2 | 6 of 6 |
| slot_blocked | outage_long | 0.1 | 6 | FAILED SUCCEEDED | 0 | 0 | 29.45 [29.00, 30.05] (8.10) | - | 21.10 [20.65, 21.70] | 6 of 6 | 6 of 6 | 100.0 % | 3.30 [3.06, 4.00] | 22.75 [22.75, 22.80] | 0 | 6 of 6 |
| cup_moved | outage_long | 1.0 | 6 | SUCCEEDED | 0 | 0 | 10.25 [10.05, 10.25] (10.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.82 [3.11, 4.00] | 22.75 [22.75, 22.80] | 2 | 6 of 6 |
| fallback_expiry | outage_long | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 31.40 [31.20, 31.70] (10.15) | - | 22.05 [21.85, 22.35] | 6 of 6 | 6 of 6 | 100.0 % | 3.96 [3.01, 4.01] | 22.75 [22.75, 22.75] | 0 | 6 of 6 |
| s2_placement | outage_long | 1.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 37.93 [37.30, 38.35] (20.35) | - | - | - | - | 100.0 % | 3.53 [3.01, 3.99] | 22.75 [22.75, 22.75] | 1 | 6 of 6 |
| slot_blocked | outage_long | 1.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 30.93 [30.70, 32.05] (10.15) | - | 21.60 [21.45, 22.65] | 6 of 6 | 6 of 6 | 100.0 % | 3.14 [3.03, 3.25] | 22.75 [22.75, 22.80] | 0 | 6 of 6 |
| cup_moved | outage_long | 5.0 | 6 | SUCCEEDED | 0 | 0 | 14.23 [14.10, 14.25] (14.05) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.45 [3.09, 3.97] | 22.77 [22.75, 22.80] | 1 | 6 of 6 |
| fallback_expiry | outage_long | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 39.05 [39.00, 39.90] (18.25) | - | 25.77 [25.65, 26.55] | 6 of 6 | 6 of 6 | 100.0 % | 3.01 [2.99, 3.05] | 22.75 [22.75, 22.80] | 1 | 6 of 6 |
| s2_placement | outage_long | 5.0 | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 45.75 [45.65, 46.10] (28.30) | - | - | - | - | 100.0 % | 3.10 [3.01, 4.00] | 22.77 [22.75, 22.80] | 2 | 6 of 6 |
| slot_blocked | outage_long | 5.0 | 6 | FAILED SUCCEEDED | 0 | 0 | 39.40 [39.30, 39.70] (18.25) | - | 26.15 [25.95, 26.40] | 6 of 6 | 6 of 6 | 100.0 % | 3.94 [3.18, 4.00] | 22.80 [22.75, 22.80] | 0 | 6 of 6 |
| cup_moved | outage_long | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED | 0 | 0 | 17.65 [17.60, 17.65] (17.60) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.65 [3.19, 3.98] | 22.75 [22.75, 22.75] | 1 | 6 of 6 |
| fallback_expiry | outage_long | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 46.50 [46.20, 46.60] (25.25) | - | 29.75 [29.45, 29.85] | 6 of 6 | 6 of 6 | 100.0 % | 3.01 [3.00, 3.01] | 22.77 [22.75, 22.80] | 1 | 6 of 6 |
| s2_placement | outage_long | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 53.00 [52.90, 53.25] (35.75) | - | - | - | - | 100.0 % | 3.34 [3.05, 4.01] | 22.77 [22.75, 22.80] | 1 | 6 of 6 |
| slot_blocked | outage_long | qwen2.5-1.5b-q4km-host (median 8.50 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 46.15 [45.90, 46.60] (25.25) | - | 29.35 [29.10, 29.80] | 6 of 6 | 6 of 6 | 100.0 % | 3.92 [3.06, 4.00] | 22.75 [22.75, 22.80] | 0 | 6 of 6 |
| cup_moved | outage_long | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED | 0 | 0 | 13.05 [13.00, 13.10] (13.00) | 0.10 [0.10, 0.10] | - | - | - | 100.0 % | 3.15 [3.06, 3.95] | 22.75 [22.75, 22.80] | 1 | 6 of 6 |
| fallback_expiry | outage_long | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 36.95 [36.30, 37.40] (15.95) | - | 24.80 [24.15, 25.25] | 6 of 6 | 6 of 6 | 100.0 % | 2.61 [2.04, 3.01] | 22.75 [22.75, 22.80] | 0 | 6 of 6 |
| s2_placement | outage_long | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | SUCCEEDED SUCCEEDED | 0 | 0 | 42.75 [42.30, 43.30] (25.30) | - | - | - | - | 100.0 % | 3.16 [3.06, 3.99] | 22.75 [22.75, 22.80] | 0 | 6 of 6 |
| slot_blocked | outage_long | qwen2.5-1.5b-q4km-host-avx2 (median 3.80 s) | 6 | FAILED SUCCEEDED | 0 | 0 | 37.00 [36.90, 37.15] (15.95) | - | 24.87 [24.75, 24.95] | 6 of 6 | 6 of 6 | 100.0 % | 3.50 [3.04, 4.01] | 22.75 [22.75, 22.80] | 0 | 6 of 6 |

</details>

Tolerance: Wall-clock runs are not bit-identical. Every cell must give the simulated run's outcomes, claims, verdicts and ground truth exactly, no safety violation, and no S0 tick without a chunk, a fallback or HOLD; outages must be classified by the deadlines; every execution's fallback coverage at least 99 %. Task time: local and lan within 15 % of the simulated run with the same time to first token when S0 kept its rate (overruns in under 1 % of its steps), within 30 % when it did not (a late loop delays every step, as on an emulated CPU); every profile at least 85 % of it.

<!-- s2-placement:end -->

Before and after the fallback design of v0.0.2 part 5 (the full matrix of v0.0.2 part 4 against the one of this release), written by `omarchy-runtime placement-compare BEFORE AFTER --docs ...` from both runs' results and traces:

<!-- s2-placement-compare:begin -->

Before and after, each cell "before → after". Before: v0.0.2 part 4, CI run 37465247670 (https://github.com/promaaa/omarchy-robots/actions/runs/37465247670), the full matrix on 61dba14. After: v0.0.2 part 5, CI run 37589536784 (https://github.com/promaaa/omarchy-robots/actions/runs/37589536784), the full matrix on b40ec10; both alone on a GitHub ubuntu-24.04 runner, 2 CPUs, KVM, each VM 2 vCPUs. Coverage: the least, over the row's executions, of the fraction of S0's status samples (the supervisor's, 20 Hz) taken while an execution ran that report a fallback installed; HOLD while S2 decides: the longest time S0 spent in HOLD from a failure to the next execution's start, from S0's mode changes as the supervisor saw them; both from the traces, the same way for both runs. Engaged, expired and recovery: from each run's results (expired: before, S0 went from S2's fallback to HOLD; after, S2's ran out and the capability's default took over).

| profile | TTFT s | fallback coverage | fallback engaged in recoveries | S2 fallbacks expired | HOLD while S2 decides s | slot_blocked recovery s | fallback_expiry recovery s |
|---|---|---|---|---|---|---|---|
| local | 0.1 | 93.4 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 0.15 → 0.25 | 0.25 → 0.15 |
| local | 1.0 | 64.1 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 1.05 → 1.05 | 1.05 → 1.15 |
| local | 5.0 | 0.0 % → 100.0 % | 0 of 2 → 2 of 2 | 0 of 2 → 2 of 2 | 5.10 → 0.00 | 5.15 → 5.05 | 5.05 → 5.20 |
| lan | 0.1 | 91.4 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 0.15 → 0.15 | 0.15 → 0.25 |
| lan | 1.0 | 67.2 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 1.05 → 1.15 | 1.25 → 1.05 |
| lan | 5.0 | 0.0 % → 100.0 % | 0 of 2 → 2 of 2 | 0 of 2 → 2 of 2 | 5.15 → 0.00 | 5.20 → 5.15 | 5.20 → 5.20 |
| wifi | 0.1 | 91.4 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 0.20 → 0.25 | 0.35 → 0.35 |
| wifi | 1.0 | 67.2 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 1.20 → 1.15 | 1.15 → 1.05 |
| wifi | 5.0 | 0.0 % → 100.0 % | 0 of 2 → 2 of 2 | 0 of 2 → 2 of 2 | 5.10 → 0.00 | 5.15 → 5.15 | 5.15 → 5.15 |
| wan | 0.1 | 88.1 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 0.25 → 0.35 | 0.35 → 0.25 |
| wan | 1.0 | 61.5 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 1.25 → 1.25 | 1.15 → 1.15 |
| wan | 5.0 | 0.0 % → 100.0 % | 0 of 2 → 2 of 2 | 0 of 2 → 2 of 2 | 5.25 → 0.00 | 5.30 → 5.25 | 5.30 → 5.20 |
| degraded | 0.1 | 82.0 % → 100.0 % | 2 of 2 → 2 of 2 | 0 of 2 → 0 of 2 | 0.00 → 0.00 | 0.65 → 1.00 | 0.75 → 0.65 |
| degraded | 1.0 | 45.5 % → 100.0 % | 2 of 2 → 2 of 2 | 1 of 2 → 1 of 2 | 0.75 → 0.00 | 1.75 → 1.50 | 2.30 → 1.85 |
| degraded | 5.0 | 0.0 % → 100.0 % | 0 of 2 → 2 of 2 | 0 of 2 → 2 of 2 | 6.05 → 0.00 | 5.60 → 5.85 | 6.10 → 5.75 |
| outage | 0.1 | 90.9 % → 100.0 % | 2 of 2 → 2 of 2 | 2 of 2 → 2 of 2 | 2.00 → 0.00 | 3.65 → 3.85 | 3.55 → 4.55 |
| outage | 1.0 | 66.7 % → 100.0 % | 2 of 2 → 2 of 2 | 2 of 2 → 2 of 2 | 3.30 → 0.00 | 4.65 → 4.75 | 4.85 → 4.85 |
| outage | 5.0 | 0.0 % → 100.0 % | 0 of 2 → 2 of 2 | 0 of 2 → 2 of 2 | 9.25 → 0.00 | 9.30 → 9.25 | 8.95 → 9.50 |
| outage_long | 0.1 | 90.9 % → 100.0 % | 2 of 2 → 2 of 2 | 2 of 2 → 2 of 2 | 20.05 → 0.00 | 21.35 → 21.70 | 21.60 → 20.85 |
| outage_long | 1.0 | 63.6 % → 100.0 % | 2 of 2 → 2 of 2 | 2 of 2 → 2 of 2 | 20.00 → 0.00 | 21.45 → 22.45 | 21.55 → 21.55 |
| outage_long | 5.0 | 0.0 % → 100.0 % | 0 of 2 → 2 of 2 | 0 of 2 → 2 of 2 | 26.40 → 0.00 | 26.25 → 26.25 | 26.45 → 26.30 |

<!-- s2-placement-compare:end -->

## Extension points

Each extension point is one small interface in `pkgs/omarchy-runtime/omarchy_runtime/contracts.py`, with a default implementation in `extensions.py`, chosen by id in `config/runtime.toml` (or a scenario's `[extensions]` table), and named with its version in every trace header.

| Interface | Default | For |
|---|---|---|
| `ObservationFilter.apply(observation)` returns the filtered observation and what it changed | `identity` | anonymization and privacy: it runs before the belief and the trace see an observation; it keeps entity ids stable within a run and says what it changed |
| `Verifier.verify(goal, belief, not_before)` returns a verdict with its evidence | `geometric`, per predicate | judging success independently of the executor |
| `RecoveryPolicy.decide(goal, summary, belief)` returns `Retry` or `GiveUp` | `retry_once`: once after VIOLATED; give up otherwise. Also `retry_elsewhere`: after a blocked place spot, the first free spot of the container | retrying with modifications (`Retry` carries the conditioning passed to S1) |
| `LatencyModel.sample(rng)` returns a delay, S2's time to first token | `fixed`; also `lognormal` (sampled for each decision) | where S2 should run (R2) |
| `ChunkCorrector.correct(chunk, history, state, now)` returns the chunk S0 executes | `none`; also `blend` | asynchronous inference: correcting a chunk computed from an older observation (see below) |
| `InferenceTrigger.due(context)` says whether S1 infers now | `periodic`; also `mismatch` | asynchronous inference: when to infer again (see below) |
| `FallbackGenerator.fallback(body, status, valid_from, valid_until)` returns a capability's default fallback; `motion_key(body, status)`, cheap, changes exactly when that motion would | `lift` for `place_in`; also `hold` | what S0 does when S1's chunks stop and S2 has no fallback, or after S2's ran out (see "Fallbacks"); named per capability in `capabilities.toml` (`default_fallback`); contract tests `FallbackGeneratorContract` |

An extension that raises, or returns the wrong type, never stops the run: a failing filter loses that observation (unfiltered data never reaches the belief), a failing verifier gives INCONCLUSIVE, a failing recovery policy means giving up, and each leaves an `error` event in the trace.

To add one: write the class, add it to the registry in `extensions.py`, select it by id in the configuration, and pass the contract tests in `tests/contracts.py` by mixing the matching class into a test case. The runtime core does not change. The tests run with:

```sh
cd pkgs/omarchy-runtime
nix develop ../.. -c python3 -m unittest discover -s tests -t .
```

## Measured latencies

> **Measured on the development host.** A desktop CPU (Intel i7-9700, 8 threads), no GPU, no robot.

The S2 placement experiment's 0.1 s, 1 s and 5 s stand in for a model's time to first token (TTFT). `omarchy-runtime ttft-measure` measures a real one: it sends a representative S2 prompt (choose the next rack spot for the cup from the belief S2 has when S1 gave up in `slot_blocked`, answer in JSON) N times, streaming, to an OpenAI-compatible endpoint, and records for each request the TTFT (from sending it to the first token of the answer), the total latency and any failure. The result is a latency distribution file (`omarchy_runtime/data/latency/`, the format in AGENTS.md) with its provenance: date, host, backend, model and its file's SHA-256, prompt hash, method, network path. Its label is "measured on host".

```sh
nix build --inputs-from . nixpkgs#llama-cpp -o llama-cpp     # llama.cpp as nixpkgs packages it (no AVX2)
nix build .#llama-cpp-avx2 -o llama-cpp-avx2                 # the same, built for AVX2 hosts
llama-cpp-avx2/bin/llama-server -m qwen2.5-1.5b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 8090 --threads 8 --parallel 1
nix run .#omarchy-runtime -- ttft-measure --url http://127.0.0.1:8090/v1 --model qwen2.5-1.5b-instruct --n 50 --no-prompt-cache \
  --backend "..." --model-file qwen2.5-1.5b-instruct-q4_k_m.gguf --model-source "<official URL>" --quantization Q4_K_M \
  --out omarchy_runtime/data/latency/<name>.json
nix run .#omarchy-runtime -- ttft-docs --docs README.md manual/runtime.md   # writes the table below
```

The model is Qwen2.5-1.5B-Instruct, quantized Q4_K_M, from its publisher's repository on Hugging Face (`Qwen/Qwen2.5-1.5B-Instruct-GGUF`, revision 91cad511, Apache-2.0); the file's SHA-256 is in each distribution's provenance. The weights are not in this repository. `--no-prompt-cache` makes llama-server process the whole prompt every time, as a fresh S2 decision would. A cloud backend takes the same command with its URL and a key from an environment variable (`OMARCHY_S2_API_KEY` by default) or a file outside the repository (`--api-key-file`); the key is sent only to the backend and never printed or written. None has been measured: no key was provided.

<!-- s2-ttft:begin -->

S2's time to first token, measured with `omarchy-runtime ttft-measure` (from sending a request to the first token of the answer; the prompt and the method are in each file's provenance, with the model file's SHA-256). Measured on the development host, never on a robot.

| distribution | label | backend | model | TTFT median s | p05 s | p95 s | n | failures | network path | date |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen2.5-1.5b-q4km-host | measured on host | llama.cpp llama-server 9190 (b64739e) as packaged by nixpkgs (flake.lock; GGML_NATIVE off, no AVX2), CPU only, 8 threads, 1 slot, context 4096 | Qwen2.5-1.5B-Instruct Q4_K_M | 8.505 | 8.354 | 8.889 | 50 | 0 | loopback on the measuring host | 2026-10-07 |
| qwen2.5-1.5b-q4km-host-avx2 | measured on host | llama.cpp llama-server 9190 (b64739e) from nixpkgs (flake.lock), built with AVX2, FMA, F16C, BMI2 (flake output llama-cpp-avx2), CPU only, 8 threads, 1 slot, context 4096 | Qwen2.5-1.5B-Instruct Q4_K_M | 3.804 | 3.539 | 3.945 | 50 | 0 | loopback on the measuring host | 2026-10-07 |

<!-- s2-ttft:end -->

The placement experiment draws S2's TTFT from these files as well (`omarchy-runtime s2 --ttft-file NAME`: each decision takes one of the measured TTFTs, drawn at random with replacement from the run's seed). The repeated matrix runs every cell 6 times, with both measured distributions (llama.cpp as packaged, and built for AVX2) besides 0.1, 1 and 5 s, one scenario per CI job (`nix build .#s2-placement-repeated-<scenario>`; workflow dispatch with `repeated`), and `omarchy-runtime placement-merge` joins the four parts into one table. Each value is the median of a cell's runs, with a distribution-free confidence interval for that median from order statistics: the narrowest pair of the k-th smallest and k-th largest runs whose binomial coverage is at least 95 %; with 6 runs, the smallest and the largest, 96.9 %.

The inference matrix takes measured S1 inference delays the same way: `omarchy-runtime inference-matrix --delay-file FILE` adds one row of delays drawn from an `s1_inference` file (`[s1] latency_file` in a scenario does it for one run). No such file is in this repository: the S1 delays above are the fixed 50, 150 and 300 ms.

## Asynchronous inference

A VLA model infers a chunk from an observation, and the robot moves while it infers: when the chunk reaches S0, it was computed for a state that is already gone. Two extension points, both on the robot side in S1's host, never in the runtime, let a method act on this:

- A **chunk corrector** (`ChunkCorrector.correct(chunk, history, state, now)`) is applied when a new chunk is handed to S0, and returns the chunk S0 executes. It receives the chunk, with `observed_at` (when its observation was taken) and `inference_ns` (how long the inference took), S0's ticks since `observed_at` (`S0History`: tick times, commanded and desired positions) and S0's current status. A corrector keeps the chunk's seq, source, action space and observation time.
- An **inference trigger** (`InferenceTrigger.due(context)`) is asked at S1's rate whether S1 infers now. `context` (`InferenceContext`) holds S0's status, its last 0.5 s of history, the inferences on their way, the last measured inference latency and S1's period. A trigger must ask for an inference when S0 has no chunk and none is on its way.

Each call returns within about 0.5 ms (the contract tests measure it): a corrector runs in S0's loop. Neither blocks, reads ground truth or calls S0: they receive data and return data. One that raises fails the execution, never S0. `[extensions]` in `runtime.toml` chooses them by id (`corrector`, `trigger`); their parameters are in `[s1_host]`. The baselines are naive on purpose: `blend` blends from S0's position into the new chunk over 0.1 s, a stand-in for smoothing methods such as Real-Time Chunking; `mismatch` infers when S0's state is 0.02 from what it tracks. To add one, register it in `CORRECTORS` or `INFERENCE_TRIGGERS` in `extensions.py` and mix `CorrectorContract` or `TriggerContract` from `tests/contracts.py` into its test case.

`omarchy-runtime inference-matrix --docs manual/runtime.md` runs S1's inference delay (50, 150, 300 ms) x every registered corrector x every registered trigger on `nominal_a` and `cup_moved`, in simulated time, and writes the table below. A new corrector or trigger joins the matrix once it is registered. A baseline, not a claim about which method is best.

<!-- inference-matrix:begin -->

Inference matrix (simulated time; a baseline for asynchronous inference, not a claim about which method is best).

| scenario | inference s | corrector | trigger | outcomes | violations | task s | starvations | inferences | jump | velocity jump /s | reaction s | freeze s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nominal_a | 0.05 | none | periodic | SUCCEEDED | 0 | 8.20 | 0 | 155 | 0.00 | 1.37 | - | - |
| nominal_a | 0.05 | none | mismatch | SUCCEEDED | 0 | 8.20 | 0 | 23 | 0.00 | 0.63 | - | - |
| nominal_a | 0.05 | blend | periodic | SUCCEEDED | 0 | 9.05 | 0 | 172 | 0.00 | 2.68 | - | - |
| nominal_a | 0.05 | blend | mismatch | SUCCEEDED | 0 | 8.25 | 0 | 26 | 0.00 | 1.36 | - | - |
| nominal_a | 0.15 | none | periodic | SUCCEEDED | 0 | 8.35 | 0 | 158 | 0.08 | 1.45 | - | - |
| nominal_a | 0.15 | none | mismatch | SUCCEEDED | 0 | 8.20 | 0 | 32 | 0.00 | 0.03 | - | - |
| nominal_a | 0.15 | blend | periodic | SUCCEEDED | 0 | 8.75 | 0 | 166 | 0.00 | 2.60 | - | - |
| nominal_a | 0.15 | blend | mismatch | SUCCEEDED | 0 | 8.25 | 0 | 32 | 0.00 | 0.98 | - | - |
| nominal_a | 0.30 | none | periodic | SUCCEEDED | 0 | 8.50 | 0 | 161 | 0.08 | 1.42 | - | - |
| nominal_a | 0.30 | none | mismatch | SUCCEEDED | 0 | 10.30 | 27 | 29 | - | - | - | - |
| nominal_a | 0.30 | blend | periodic | SUCCEEDED | 0 | 8.85 | 0 | 168 | 0.03 | 2.50 | - | - |
| nominal_a | 0.30 | blend | mismatch | SUCCEEDED | 0 | 11.00 | 29 | 31 | - | - | - | - |
| cup_moved | 0.05 | none | periodic | SUCCEEDED | 0 | 9.15 | 0 | 174 | 0.04 | 1.55 | 0.10 | 0.05 |
| cup_moved | 0.05 | none | mismatch | SUCCEEDED | 0 | 9.25 | 0 | 26 | 0.04 | 0.84 | 0.10 | 0.05 |
| cup_moved | 0.05 | blend | periodic | SUCCEEDED | 0 | 10.15 | 0 | 194 | 0.00 | 1.65 | 0.10 | 0.05 |
| cup_moved | 0.05 | blend | mismatch | SUCCEEDED | 0 | 9.15 | 0 | 28 | 0.00 | 1.74 | 0.10 | 0.05 |
| cup_moved | 0.15 | none | periodic | SUCCEEDED | 0 | 9.40 | 0 | 179 | 0.13 | 1.77 | 0.20 | 0.15 |
| cup_moved | 0.15 | none | mismatch | SUCCEEDED | 0 | 9.65 | 0 | 38 | 0.12 | 0.82 | 0.25 | 0.20 |
| cup_moved | 0.15 | blend | periodic | none (time limit) | 0 | 60.00 | 0 | 1197 | 0.01 | 2.34 | 0.20 | 0.15 |
| cup_moved | 0.15 | blend | mismatch | SUCCEEDED | 0 | 9.75 | 0 | 38 | 0.00 | 1.83 | 0.30 | 0.25 |
| cup_moved | 0.30 | none | periodic | SUCCEEDED | 0 | 10.65 | 0 | 204 | 0.33 | 1.79 | 0.35 | 0.30 |
| cup_moved | 0.30 | none | mismatch | SUCCEEDED | 0 | 11.35 | 30 | 32 | - | - | 0.45 | 0.40 |
| cup_moved | 0.30 | blend | periodic | none (time limit) | 0 | 60.00 | 0 | 1197 | 0.06 | 2.81 | 0.35 | 0.30 |
| cup_moved | 0.30 | blend | mismatch | SUCCEEDED | 0 | 12.05 | 32 | 34 | - | - | 0.45 | 0.40 |

jump: the largest jump of S0's desired position at a chunk switch (rad for the arm's joints); velocity jump: of its desired velocity. reaction: from the cup's move to S0 executing a chunk computed after it; freeze: from the first observation after the move reaching S1 to that tick.

<!-- inference-matrix:end -->

## What is not here

No ROS and no ROS types, no learned model, no images, no physics engine in the kitchen (a cup attaches when the gripper closes around it, and settles on the support below when released; only the Panda's scene is MuJoCo), no real-time guarantee, and no hardware. The S0 contract is plain data so that a native controller, a vendor controller or ros2_control behind an adapter can implement it later: the guard and the controller interface are its two halves.
