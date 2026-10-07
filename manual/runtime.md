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
| S0 | Real-time execution: the only layer that commands actuators, and it enforces the body's limits. | 1 kHz (200 Hz on the wall clock) | a simulated controller |

![S2 and the runtime above; S1, S0 and the body below]({{ '/assets/runtime-layers.svg' | relative_url }})

**Figure 1.** The three layers and the runtime. Thick line: the high-rate path, where S1 sends action chunks to S0 directly, never through the runtime. S2 installs a fallback ahead of time through the runtime. S0 uses it when chunks stop.

The runtime is the **control plane**: it owns the application API, capability resolution, the execution lifecycle, supervision, the belief about the world, verification and traces. It is not in the high-rate path. Chunks go from S1 to S0 directly, and the runtime samples S0's status at its own rate.

**S0 never waits.** At every tick it executes a valid chunk, else a valid fallback (S2's, else the capability's default: see "Fallbacks"), else its built-in HOLD, which keeps the joints where they are and leaves the gripper as it is. A chunk is executed only until its `valid_until`; S0 leaves it at the first tick after that.

**S0's steps are bounded.** In one tick S0 never moves a dimension more than its velocity limit times S0's nominal tick period, whatever the real time since the last tick. A tick that comes later than a threshold (1.5 periods, `[s0]` in `config/world.toml`) is a late tick: S0 continues from where it is at that bound and never catches up with a jump; if the plan moved on meanwhile, S0 reaches it later and S1 plans again. S0 counts late ticks and reports the longest interval between ticks and its largest step as a fraction of the bound; the runtime records each new late tick (`s0.late`). This is part of the S0 contract, for a hardware S0 as well. When an execution stops running, the runtime tells S0 to hold: the rest of the chunk is dropped. After a claim or a cancellation the fallbacks go too; after a failure S0 keeps them and engages one while S2 decides.

### Fallbacks

A fallback must exist before it is needed. S0 holds two, in two slots, and runs the first valid one, else HOLD:

1. **S2's fallback.** S2 predicts it and sends it with the goal (`fallback` in the `request`), its window relative to the execution's start, so that the runtime installs it when the execution starts, before S1's first chunk, whatever S2's latency. S2 may refine it later (`install_fallback`): the refined one replaces the predicted one.
2. **The capability's default**, S0's base fallback. Each capability names a generator in `config/capabilities.toml` (`default_fallback`); the runtime computes it on the robot side from S0's status and installs it when the execution starts. `hold` keeps every dimension where S0 is when it engages, the gripper included (relative rows of zeros); `lift`, the default of `place_in`, does the same, and on a body with a `z` dimension (the gantry) with the gripper closed rises 2 cm first. A default never moves the gripper, so a closing gripper is never reopened on an object. The runtime renews the default whenever the generator returns another motion (after a grasp, after a release); the one replaced stays until then. It is valid for the execution and a wait for S2 after it (600 s from the start).

Precedence: S2's refined, then S2's predicted, then the capability's default, then HOLD. A fallback engages when a chunk ran out with no successor, or when the runtime asks after a failure; while an execution awaits S1's first chunk, S0 holds. A new chunk takes over at once, and the fallback stays installed for the next time. When S2's fallback runs out (`fallback_lasts_s`), S0 goes on under the default in the same tick. Rows can be targets or, per dimension (`relative`), offsets from where S0 is when it engages. Every installation is recorded (`fallback.install`: slot, kind, source, window, duration), and the execution summary lists them.

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

## Traces

Every run writes one directory:

| File | Contents |
|---|---|
| `run.json` | Header: `trace_schema` (5), the runtime's version, revision and dirty flag, the Omarchy Robots identity (the contents of `/etc/omarchy-robot/build.json`, or `null` with "no Omarchy Robots identity" on another machine), the hash and the contents of the effective configuration, scenario, seed, clock (`simulated` or `wall`), body, rates, the id and version of the observation filter, the verifiers, the recovery policy and the S2 latency model (with its median and spread), where S2 ran (`s2`), and the run's `tags`. |
| `events.jsonl` | Low-rate events only, one JSON object per line, flushed one by one, so a crashed run leaves a readable prefix. Each has `seq`, simulated time `t` (ns), `wall_ns`, `type`, `execution` and `payload`. A wall-clock run's `run.end` carries the loop statistics; with S2 in another process, also S2's liveness (`s2_link`). Types: `run.start`, `request`, `resolution`, `lifecycle`, `chunk` (metadata, never the actions, with `observed_at` and `inference_ns`), `s0.mode`, `s0.status` (at the supervisor rate), `fallback.install`, `fallback.rejected`, `s0.hold`, `verify.attempt`, `verify.evidence` (the observations a verdict rests on), `verdict`, `decision`, `cancel`, `error`, `s2.link` (S2's link up, lost, recovered with the outage's class, left, closed), `s0.late` (a late S0 tick), `run.end`. |
| `executions.jsonl` | One summary per execution: ids, goal, chosen capability and implementation with every candidate's reason, body and action space, belief before and after, claim, verdict with its evidence, outcome and category, simulated timings (time to first chunk, duration, time in fallback and in HOLD), wall-clock costs (resolution, verification, trace writing), S0's counters, configuration hash and seed. |
| `s0_<execution>.npz` | With `--record-s0`: S0's per-tick record for the execution (time, mode, position, desired position, chunk and its validity, when the chunk's observation was taken, and which fallback runs: none, base or S2's), kept in a preallocated buffer and written once at the end. |
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
nix build .#s2-placement                          # every scenario x profile x TTFT 0.1, 1, 5 s; wants KVM
nix run .#omarchy-runtime -- placement-docs result/placement/results.json README.md manual/runtime.md \
  --source "CI run <id>, ..."      # writes the table below
```

In `nix flake check`, the two-cup task runs on lan, degraded, outage and outage_long with a TTFT of 0.1 s and 1 s, and each scene that changes on lan and outage_long with 1 s: 14 cells. `result/placement` holds `results.json`, `table.txt` and `table.md`; `result/runs` the robot's traces; `result/s2-edge` and `result/s2-robot` the S2 processes' own reports. The last command writes the table below from a full run's results. CI runs the full matrix on demand (workflow dispatch with `placement`), alone on its runner, and keeps the results as an artifact.

<!-- s2-placement:begin -->
S2 placement, the two-cup task and the dynamic scenarios (emulated network (tc netem), VMs, soft real-time). Measured 2026-10-06 with omarchy-runtime 0.0.2 (61dba14b4fc9), Omarchy Robots 0.0.2 (board vm), S0 at 200 Hz; soft real-time, Python, wall-clock in an Omarchy Robots VM (0.0.2, Python 3.13.15, 2 CPUs, virtualization kvm). Times are wall-clock; in parentheses and in the simulated column, the same task with S2 in one process on simulated time. Reaction, cup_moved: from the move to S0 executing the first chunk computed from an observation taken after it. Recovery, slot_blocked, fallback_expiry: from S1 giving up to the retry running; expired: S0 went from the fallback to HOLD before the retry's first chunk (the fallback lasts 3 s in slot_blocked, 1.5 s in fallback_expiry). S2 RTT: heartbeat echo measured by S2: the network both ways, plus up to one loop period at each end. Source: CI run 37465247670 (https://github.com/promaaa/omarchy-robots/actions/runs/37465247670), the full matrix on 61dba14 alone on a GitHub ubuntu-24.04 runner, 2 CPUs, KVM; each VM 2 vCPUs.

| profile | TTFT s | two-cup task s | cup_moved reaction s | slot_blocked recovery s | fallback_expiry recovery s | fallbacks expired | violations | S0 starvations | cells within tolerance |
|---|---|---|---|---|---|---|---|---|---|
| local | 0.1 | 18.60 (18.45) | 0.10 (0.10) | 0.15 (0.15) | 0.25 (0.15) | 0 of 2 | 0 | 0 | 4 of 4 |
| local | 1.0 | 20.55 (20.35) | 0.10 (0.10) | 1.05 (1.05) | 1.05 (1.05) | 0 of 2 | 0 | 0 | 4 of 4 |
| local | 5.0 | 28.50 (28.30) | 0.10 (0.10) | 5.15 (5.05) | 5.05 (5.10) | 0 of 2 | 0 | 0 | 4 of 4 |
| lan | 0.1 | 18.70 (18.45) | 0.10 (0.10) | 0.15 (0.15) | 0.15 (0.15) | 0 of 2 | 0 | 0 | 4 of 4 |
| lan | 1.0 | 20.60 (20.35) | 0.10 (0.10) | 1.05 (1.05) | 1.25 (1.05) | 0 of 2 | 0 | 0 | 4 of 4 |
| lan | 5.0 | 28.70 (28.30) | 0.10 (0.10) | 5.20 (5.05) | 5.20 (5.10) | 0 of 2 | 0 | 0 | 4 of 4 |
| wifi | 0.1 | 18.45 (18.45) | 0.10 (0.10) | 0.20 (0.15) | 0.35 (0.15) | 0 of 2 | 0 | 0 | 4 of 4 |
| wifi | 1.0 | 20.60 (20.35) | 0.10 (0.10) | 1.20 (1.05) | 1.15 (1.05) | 0 of 2 | 0 | 0 | 4 of 4 |
| wifi | 5.0 | 28.50 (28.30) | 0.10 (0.10) | 5.15 (5.05) | 5.15 (5.10) | 0 of 2 | 0 | 0 | 4 of 4 |
| wan | 0.1 | 18.60 (18.45) | 0.10 (0.10) | 0.25 (0.15) | 0.35 (0.15) | 0 of 2 | 0 | 0 | 4 of 4 |
| wan | 1.0 | 20.60 (20.35) | 0.10 (0.10) | 1.25 (1.05) | 1.15 (1.05) | 0 of 2 | 0 | 0 | 4 of 4 |
| wan | 5.0 | 28.65 (28.30) | 0.10 (0.10) | 5.30 (5.05) | 5.30 (5.10) | 0 of 2 | 0 | 0 | 4 of 4 |
| degraded | 0.1 | 19.75 (18.45) | 0.10 (0.10) | 0.65 (0.15) | 0.75 (0.15) | 0 of 2 | 0 | 0 | 4 of 4 |
| degraded | 1.0 | 21.35 (20.35) | 0.10 (0.10) | 1.75 (1.05) | 2.30 (1.05), expired | 1 of 2 | 0 | 0 | 4 of 4 |
| degraded | 5.0 | 29.70 (28.30) | 0.10 (0.10) | 5.60 (5.05) | 6.10 (5.10) | 0 of 2 | 0 | 0 | 4 of 4 |
| outage | 0.1 | 18.60 (18.45) | 0.10 (0.10) | 3.65 (0.15), expired | 3.55 (0.15), expired | 2 of 2 | 0 | 0 | 4 of 4 |
| outage | 1.0 | 20.55 (20.35) | 0.10 (0.10) | 4.65 (1.05), expired | 4.85 (1.05), expired | 2 of 2 | 0 | 0 | 4 of 4 |
| outage | 5.0 | 29.45 (28.30) | 0.09 (0.10) | 9.30 (5.05) | 8.95 (5.10) | 0 of 2 | 0 | 0 | 4 of 4 |
| outage_long | 0.1 | 36.45 (18.45) | 0.10 (0.10) | 21.35 (0.15), expired | 21.60 (0.15), expired | 2 of 2 | 0 | 0 | 4 of 4 |
| outage_long | 1.0 | 37.70 (20.35) | 0.10 (0.10) | 21.45 (1.05), expired | 21.55 (1.05), expired | 2 of 2 | 0 | 0 | 4 of 4 |
| outage_long | 5.0 | 46.45 (28.30) | 0.10 (0.10) | 26.25 (5.05) | 26.45 (5.10) | 0 of 2 | 0 | 0 | 4 of 4 |

<details markdown="1"><summary>Every cell (84)</summary>

| scenario | profile | TTFT s | outcomes | violations | S0 starvations | fallback s | HOLD s | task s | simulated s | S2 RTT p50/p99/max ms | outages | S0 interval p99/max ms | S0 overruns | reaction s | recovery s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| s2_placement | local | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.29 | 18.60 | 18.45 | 2.0/3.4/4.0 | none | 6.08/12.72 | 3 of 3730 | - | - |
| s2_placement | local | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.09 | 20.55 | 20.35 | 2.0/3.0/4.7 | none | 5.73/9.94 | 1 of 4136 | - | - |
| s2_placement | local | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.14 | 28.50 | 28.30 | 1.0/3.8/11.3 | none | 5.69/20.79 | 4 of 5712 | - | - |
| cup_moved | local | 0.1 | 1x SUCCEEDED | 0 | 0 | 0.00 | 0.60 | 9.20 | 9.15 | 1.0/2.8/2.9 | none | 5.70/7.73 | 0 of 1852 | 0.10 (0.10) | - |
| cup_moved | local | 1.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 1.60 | 10.25 | 10.00 | 2.0/3.0/3.0 | none | 5.68/7.68 | 0 of 2074 | 0.10 (0.10) | - |
| cup_moved | local | 5.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 5.60 | 14.20 | 14.05 | 2.0/2.6/3.9 | none | 5.75/7.57 | 0 of 2855 | 0.10 (0.10) | - |
| slot_blocked | local | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.25 | 0.70 | 8.25 | 8.10 | 1.0/3.6/6.3 | none | 5.80/8.46 | 0 of 1671 | - | 0.15 (0.15) |
| slot_blocked | local | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.15 | 1.50 | 10.20 | 10.15 | 2.0/3.0/3.5 | none | 5.76/7.14 | 0 of 2055 | - | 1.05 (1.05) |
| slot_blocked | local | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 10.74 | 18.00 | 17.85 | 2.0/2.7/4.2 | none | 5.68/7.13 | 0 of 3995 | - | 5.15 (5.05) |
| fallback_expiry | local | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.35 | 0.60 | 8.25 | 8.05 | 1.0/2.1/2.4 | none | 5.51/6.79 | 0 of 1680 | - | 0.25 (0.15) |
| fallback_expiry | local | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.15 | 1.50 | 10.20 | 10.15 | 1.0/2.6/3.0 | none | 5.69/7.09 | 0 of 2056 | - | 1.05 (1.05) |
| fallback_expiry | local | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 10.74 | 18.00 | 17.85 | 2.0/2.5/4.7 | none | 5.61/7.42 | 0 of 3993 | - | 5.05 (5.10) |
| s2_placement | lan | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.34 | 18.70 | 18.45 | 3.9/6.5/7.4 | none | 5.94/8.31 | 0 of 3765 | - | - |
| s2_placement | lan | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.25 | 20.60 | 20.35 | 3.8/6.8/8.2 | none | 6.04/10.20 | 3 of 4134 | - | - |
| s2_placement | lan | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.29 | 28.70 | 28.30 | 3.0/6.5/7.7 | none | 5.61/7.90 | 0 of 5766 | - | - |
| cup_moved | lan | 0.1 | 1x SUCCEEDED | 0 | 0 | 0.00 | 0.75 | 9.35 | 9.15 | 3.0/5.9/6.2 | none | 5.61/7.19 | 0 of 1882 | 0.10 (0.10) | - |
| cup_moved | lan | 1.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 1.45 | 10.00 | 10.00 | 3.9/6.1/7.0 | none | 5.94/7.51 | 0 of 2024 | 0.10 (0.10) | - |
| cup_moved | lan | 5.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 5.60 | 14.20 | 14.05 | 3.9/5.3/5.4 | none | 5.76/7.52 | 0 of 2853 | 0.10 (0.10) | - |
| slot_blocked | lan | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.25 | 0.70 | 8.25 | 8.10 | 3.2/5.7/6.1 | none | 5.85/8.99 | 0 of 1680 | - | 0.15 (0.15) |
| slot_blocked | lan | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.15 | 1.50 | 10.20 | 10.15 | 3.9/5.7/5.8 | none | 6.00/8.12 | 0 of 2057 | - | 1.05 (1.05) |
| slot_blocked | lan | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 10.89 | 18.10 | 17.85 | 3.0/6.2/6.6 | none | 5.65/9.38 | 1 of 4017 | - | 5.20 (5.05) |
| fallback_expiry | lan | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.25 | 0.60 | 8.10 | 8.05 | 4.0/6.9/9.0 | none | 5.89/13.91 | 2 of 1629 | - | 0.15 (0.15) |
| fallback_expiry | lan | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.35 | 1.55 | 10.50 | 10.15 | 3.2/5.5/7.0 | none | 5.95/10.68 | 1 of 2128 | - | 1.25 (1.05) |
| fallback_expiry | lan | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 10.79 | 18.00 | 17.85 | 4.0/6.1/7.1 | none | 5.89/8.36 | 0 of 3975 | - | 5.20 (5.10) |
| s2_placement | wifi | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.19 | 18.45 | 18.45 | 23.2/138.3/150.6 | none | 5.89/9.01 | 0 of 3719 | - | - |
| s2_placement | wifi | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.24 | 20.60 | 20.35 | 23.1/138.2/190.9 | none | 5.66/7.19 | 0 of 4136 | - | - |
| s2_placement | wifi | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.14 | 28.50 | 28.30 | 22.9/132.8/153.0 | none | 5.81/8.21 | 0 of 5732 | - | - |
| cup_moved | wifi | 0.1 | 1x SUCCEEDED | 0 | 0 | 0.00 | 0.59 | 9.20 | 9.15 | 23.0/53.0/144.0 | none | 6.00/10.39 | 2 of 1862 | 0.10 (0.10) | - |
| cup_moved | wifi | 1.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 1.60 | 10.25 | 10.00 | 22.4/146.1/150.3 | none | 5.79/9.36 | 1 of 2074 | 0.10 (0.10) | - |
| cup_moved | wifi | 5.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 5.65 | 14.25 | 14.05 | 24.0/148.1/152.9 | none | 5.87/7.28 | 0 of 2883 | 0.10 (0.10) | - |
| slot_blocked | wifi | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.30 | 0.65 | 8.15 | 8.10 | 23.0/116.1/146.2 | none | 5.98/14.66 | 2 of 1667 | - | 0.20 (0.15) |
| slot_blocked | wifi | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.30 | 1.60 | 10.50 | 10.15 | 22.4/135.3/144.1 | none | 5.77/6.86 | 0 of 2126 | - | 1.20 (1.05) |
| slot_blocked | wifi | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 10.84 | 18.05 | 17.85 | 23.1/134.4/156.0 | none | 5.94/7.77 | 0 of 4020 | - | 5.15 (5.05) |
| fallback_expiry | wifi | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.45 | 0.64 | 8.40 | 8.05 | 23.4/140.1/150.9 | none | 5.98/8.98 | 1 of 1699 | - | 0.35 (0.15) |
| fallback_expiry | wifi | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.25 | 1.50 | 10.30 | 10.15 | 22.9/147.9/151.1 | none | 6.10/7.44 | 0 of 2077 | - | 1.15 (1.05) |
| fallback_expiry | wifi | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 10.84 | 18.10 | 17.85 | 23.9/139.5/158.9 | none | 5.84/8.05 | 0 of 3995 | - | 5.15 (5.10) |
| s2_placement | wan | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.30 | 18.60 | 18.45 | 103.1/120.2/123.3 | none | 6.06/7.68 | 0 of 3756 | - | - |
| s2_placement | wan | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.19 | 20.60 | 20.35 | 102.3/119.9/122.3 | none | 5.82/10.13 | 1 of 4150 | - | - |
| s2_placement | wan | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.24 | 28.65 | 28.30 | 102.2/119.9/121.0 | none | 5.77/9.46 | 0 of 5771 | - | - |
| cup_moved | wan | 0.1 | 1x SUCCEEDED | 0 | 0 | 0.00 | 0.60 | 9.20 | 9.15 | 103.2/117.4/119.7 | none | 5.86/8.60 | 0 of 1871 | 0.10 (0.10) | - |
| cup_moved | wan | 1.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 1.60 | 10.25 | 10.00 | 103.0/119.3/120.2 | none | 5.91/8.35 | 0 of 2097 | 0.10 (0.10) | - |
| cup_moved | wan | 5.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 5.60 | 14.20 | 14.05 | 102.5/117.0/118.0 | none | 5.84/7.93 | 0 of 2875 | 0.10 (0.10) | - |
| slot_blocked | wan | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.35 | 0.60 | 8.25 | 8.10 | 103.1/118.2/119.2 | none | 5.90/8.48 | 0 of 1692 | - | 0.25 (0.15) |
| slot_blocked | wan | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.35 | 1.50 | 10.45 | 10.15 | 104.0/120.1/122.0 | none | 5.98/7.55 | 0 of 2136 | - | 1.25 (1.05) |
| slot_blocked | wan | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 10.99 | 18.20 | 17.85 | 103.0/119.6/123.0 | none | 5.76/8.64 | 0 of 4037 | - | 5.30 (5.05) |
| fallback_expiry | wan | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.45 | 0.60 | 8.40 | 8.05 | 104.0/120.3/122.0 | none | 5.87/7.44 | 0 of 1719 | - | 0.35 (0.15) |
| fallback_expiry | wan | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.25 | 1.60 | 10.40 | 10.15 | 103.3/120.0/120.9 | none | 5.64/8.55 | 0 of 2116 | - | 1.15 (1.05) |
| fallback_expiry | wan | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 10.89 | 18.10 | 17.85 | 102.1/120.1/121.0 | none | 5.88/8.71 | 0 of 4019 | - | 5.30 (5.10) |
| s2_placement | degraded | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 2.29 | 19.75 | 18.45 | 717.1/1392.6/1562.9 | short 0.6 s, short 0.7 s, short 0.7 s, short 0.6 s, short 0.7 s, short 0.7 s, short 0.8 s, short 0.7 s, short 0.7 s | 6.13/8.98 | 0 of 4069 | - | - |
| s2_placement | degraded | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 4.04 | 21.35 | 20.35 | 714.2/1613.9/1766.0 | short 0.6 s, short 0.6 s, short 0.6 s, short 0.7 s, short 0.7 s, short 0.8 s, short 0.5 s, short 0.8 s | 5.67/9.09 | 0 of 4613 | - | - |
| s2_placement | degraded | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 12.39 | 29.70 | 28.30 | 592.2/1923.1/2217.7 | short 0.6 s, short 0.6 s, short 0.7 s, short 0.7 s, short 0.7 s, short 0.7 s, short 0.6 s, short 0.7 s, short 0.6 s, short 0.6 s, short 0.7 s, short 1.1 s, short 1.7 s | 5.61/6.92 | 0 of 6486 | - | - |
| cup_moved | degraded | 0.1 | 1x SUCCEEDED | 0 | 0 | 0.00 | 0.60 | 9.20 | 9.15 | 635.2/1473.4/1480.2 | short 0.6 s, short 1.2 s, short 0.6 s, short 0.6 s | 5.79/9.10 | 0 of 2055 | 0.10 (0.10) | - |
| cup_moved | degraded | 1.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 1.55 | 10.15 | 10.00 | 672.4/1260.1/1359.1 | short 0.6 s, short 0.6 s, short 0.7 s, short 0.6 s | 5.83/7.47 | 0 of 2197 | 0.10 (0.10) | - |
| cup_moved | degraded | 5.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 5.65 | 14.25 | 14.05 | 439.1/985.8/1030.9 | short 0.7 s, short 0.6 s, short 0.7 s, short 0.6 s | 5.71/6.97 | 0 of 2964 | 0.10 (0.10) | - |
| slot_blocked | degraded | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.75 | 0.65 | 8.75 | 8.10 | 557.0/1374.2/1462.0 | short 0.6 s, short 0.7 s | 5.71/8.08 | 0 of 1884 | - | 0.65 (0.15) |
| slot_blocked | degraded | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.85 | 1.55 | 11.05 | 10.15 | 509.0/1041.4/1146.0 | short 0.6 s, short 0.6 s | 5.85/7.18 | 0 of 2481 | - | 1.75 (1.05) |
| slot_blocked | degraded | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 11.34 | 18.55 | 17.85 | 680.9/1488.5/1689.5 | short 0.6 s, short 0.7 s, short 0.6 s, short 0.6 s, short 0.6 s, short 0.8 s, short 0.8 s, short 0.7 s | 5.69/7.27 | 0 of 4171 | - | 5.60 (5.05) |
| fallback_expiry | degraded | 0.1 | FAILED SUCCEEDED | 0 | 0 | 0.85 | 0.70 | 9.00 | 8.05 | 561.2/1080.4/1142.1 | short 0.7 s, short 0.5 s, short 0.7 s | 5.63/6.61 | 0 of 1943 | - | 0.75 (0.15) |
| fallback_expiry | degraded | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.50 | 2.49 | 11.65 | 10.15 | 505.1/1200.7/1282.8 | short 0.6 s, short 0.7 s, short 0.8 s, short 0.6 s, short 0.7 s | 5.72/8.98 | 0 of 2479 | - | 2.30 (1.05), expired |
| fallback_expiry | degraded | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 11.79 | 19.05 | 17.85 | 599.6/1411.2/1532.6 | short 1.2 s, short 0.6 s, short 0.7 s, short 0.8 s, short 0.7 s, short 0.6 s, short 0.7 s, short 0.7 s | 5.70/8.33 | 0 of 4267 | - | 6.10 (5.10) |
| s2_placement | outage | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.29 | 18.60 | 18.45 | 3.9/5.8/7.3 | long 5.7 s | 5.93/9.09 | 0 of 3734 | - | - |
| s2_placement | outage | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.19 | 20.55 | 20.35 | 3.2/6.0/7.2 | long 5.8 s | 5.82/8.41 | 0 of 4127 | - | - |
| s2_placement | outage | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 12.04 | 29.45 | 28.30 | 4.0/7.5/11.7 | long 5.7 s | 5.80/8.44 | 0 of 5918 | - | - |
| cup_moved | outage | 0.1 | 1x SUCCEEDED | 0 | 0 | 0.00 | 0.70 | 9.30 | 9.15 | 3.1/5.6/5.9 | long 5.7 s | 5.75/7.18 | 0 of 1871 | 0.10 (0.10) | - |
| cup_moved | outage | 1.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 1.60 | 10.25 | 10.00 | 3.1/5.9/6.0 | long 5.7 s | 6.00/8.37 | 0 of 2075 | 0.10 (0.10) | - |
| cup_moved | outage | 5.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 5.50 | 14.10 | 14.05 | 4.0/6.5/6.5 | long 5.7 s | 5.76/13.16 | 1 of 2834 | 0.09 (0.10) | - |
| slot_blocked | outage | 0.1 | FAILED SUCCEEDED | 0 | 0 | 3.00 | 1.34 | 12.00 | 8.10 | 3.1/5.0/5.0 | long 5.8 s | 5.57/7.96 | 0 of 2418 | - | 3.65 (0.15), expired |
| slot_blocked | outage | 1.0 | FAILED SUCCEEDED | 0 | 0 | 3.00 | 3.34 | 14.00 | 10.15 | 3.3/5.1/5.5 | long 5.7 s | 5.67/10.56 | 1 of 2822 | - | 4.65 (1.05), expired |
| slot_blocked | outage | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 14.99 | 22.20 | 17.85 | 3.0/6.1/7.8 | long 5.8 s | 5.56/7.50 | 0 of 4813 | - | 9.30 (5.05) |
| fallback_expiry | outage | 0.1 | FAILED SUCCEEDED | 0 | 0 | 1.50 | 2.84 | 12.00 | 8.05 | 4.0/7.5/9.7 | long 5.7 s | 5.79/7.46 | 0 of 2417 | - | 3.55 (0.15), expired |
| fallback_expiry | outage | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.50 | 4.94 | 14.10 | 10.15 | 3.9/10.1/10.2 | long 5.7 s | 5.81/32.13 | 1 of 2834 | - | 4.85 (1.05), expired |
| fallback_expiry | outage | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 14.64 | 21.90 | 17.85 | 3.9/5.8/7.3 | long 5.8 s | 5.72/9.07 | 1 of 4757 | - | 8.95 (5.10) |
| s2_placement | outage_long | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 19.04 | 36.45 | 18.45 | 3.4/6.0/6.5 | long 22.7 s | 5.64/14.88 | 2 of 7301 | - | - |
| s2_placement | outage_long | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 20.34 | 37.70 | 20.35 | 3.1/7.7/14.2 | long 22.7 s | 5.61/9.26 | 0 of 7565 | - | - |
| s2_placement | outage_long | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 29.04 | 46.45 | 28.30 | 3.2/7.4/7.9 | long 22.8 s | 5.77/32.00 | 1 of 9311 | - | - |
| cup_moved | outage_long | 0.1 | 1x SUCCEEDED | 0 | 0 | 0.00 | 0.60 | 9.20 | 9.15 | 3.2/5.1/5.1 | long 22.7 s | 5.56/9.47 | 2 of 5098 | 0.10 (0.10) | - |
| cup_moved | outage_long | 1.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 1.55 | 10.15 | 10.00 | 3.1/6.6/7.2 | long 22.8 s | 5.69/17.34 | 1 of 5268 | 0.10 (0.10) | - |
| cup_moved | outage_long | 5.0 | 1x SUCCEEDED | 0 | 0 | 0.00 | 5.60 | 14.20 | 14.05 | 3.0/6.4/7.0 | long 22.8 s | 5.51/7.30 | 0 of 6212 | 0.10 (0.10) | - |
| slot_blocked | outage_long | 0.1 | FAILED SUCCEEDED | 0 | 0 | 3.00 | 19.14 | 29.80 | 8.10 | 3.1/8.2/15.3 | long 22.7 s | 5.45/12.04 | 3 of 5985 | - | 21.35 (0.15), expired |
| slot_blocked | outage_long | 1.0 | FAILED SUCCEEDED | 0 | 0 | 3.00 | 20.04 | 30.70 | 10.15 | 3.0/6.0/6.0 | long 22.7 s | 5.49/23.52 | 1 of 6153 | - | 21.45 (1.05), expired |
| slot_blocked | outage_long | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 31.94 | 39.15 | 17.85 | 3.1/5.8/6.3 | long 22.8 s | 5.61/7.92 | 0 of 8212 | - | 26.25 (5.05) |
| fallback_expiry | outage_long | 0.1 | FAILED SUCCEEDED | 0 | 0 | 1.50 | 20.94 | 30.05 | 8.05 | 4.0/5.9/5.9 | long 22.8 s | 5.66/6.94 | 0 of 6021 | - | 21.60 (0.15), expired |
| fallback_expiry | outage_long | 1.0 | FAILED SUCCEEDED | 0 | 0 | 1.50 | 21.74 | 30.90 | 10.15 | 3.9/7.4/9.1 | long 22.7 s | 5.58/10.03 | 1 of 6199 | - | 21.55 (1.05), expired |
| fallback_expiry | outage_long | 5.0 | FAILED SUCCEEDED | 0 | 0 | 0.00 | 32.14 | 39.40 | 17.85 | 3.3/6.0/6.2 | long 22.8 s | 5.56/16.72 | 2 of 8279 | - | 26.45 (5.10) |

</details>

Tolerance: Wall-clock runs are not bit-identical. Every cell must give the simulated run's outcomes, claims, verdicts and ground truth exactly, no safety violation, and no S0 tick without a chunk, a fallback or HOLD; outages must be classified by the deadlines. Task time: local and lan within 15 % of the simulated run with the same time to first token when S0 kept its rate (overruns in under 1 % of its steps), within 30 % when it did not (a late loop delays every step, as on an emulated CPU); every profile at least 85 % of it.
<!-- s2-placement:end -->

Before and after the fallback design of v0.0.2 part 5 (the full matrix of v0.0.2 part 4 against the one of this release), written by `omarchy-runtime placement-compare BEFORE AFTER --docs ...` from both runs' results and traces:

<!-- s2-placement-compare:begin -->
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
| `FallbackGenerator.fallback(body, status, valid_from, valid_until)` returns a capability's default fallback | `lift` for `place_in`; also `hold` | what S0 does when S1's chunks stop and S2 has no fallback, or after S2's ran out (see "Fallbacks"); named per capability in `capabilities.toml` (`default_fallback`); contract tests `FallbackGeneratorContract` |

An extension that raises, or returns the wrong type, never stops the run: a failing filter loses that observation (unfiltered data never reaches the belief), a failing verifier gives INCONCLUSIVE, a failing recovery policy means giving up, and each leaves an `error` event in the trace.

To add one: write the class, add it to the registry in `extensions.py`, select it by id in the configuration, and pass the contract tests in `tests/contracts.py` by mixing the matching class into a test case. The runtime core does not change. The tests run with:

```sh
cd pkgs/omarchy-runtime
nix develop ../.. -c python3 -m unittest discover -s tests -t .
```

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
| cup_moved | 0.30 | none | periodic | SUCCEEDED | 0 | 10.30 | 0 | 197 | 0.30 | 1.94 | 0.35 | 0.30 |
| cup_moved | 0.30 | none | mismatch | SUCCEEDED | 0 | 11.35 | 30 | 32 | - | - | 0.60 | 0.55 |
| cup_moved | 0.30 | blend | periodic | none (time limit) | 0 | 60.00 | 0 | 1197 | 0.04 | 2.15 | 0.35 | 0.30 |
| cup_moved | 0.30 | blend | mismatch | SUCCEEDED | 0 | 12.75 | 34 | 36 | - | - | 0.60 | 0.55 |

jump: the largest jump of S0's desired position at a chunk switch (rad for the arm's joints); velocity jump: of its desired velocity. reaction: from the cup's move to S0 executing a chunk computed after it; freeze: from the first observation after the move reaching S1 to that tick.
<!-- inference-matrix:end -->

## What is not here

No ROS and no ROS types, no learned model, no images, no physics engine (a cup attaches when the gripper closes around it, and settles on the support below when released), no real-time guarantee, and no hardware. The S0 contract is plain data so that a native controller, a vendor controller or ros2_control behind an adapter can implement it later.
