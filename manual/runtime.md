---
layout: default
title: Runtime
permalink: /runtime/
---

The Omarchy Robots runtime sits between the intelligence that decides what a robot should do and the body that does it. An application or a planner asks for a **goal**, such as "cup_1 is in the drying rack". The runtime finds a capability that can achieve it on this body, runs it, checks the result from fresh observations, and records everything that happened as a trace that names the exact system it ran on.

> **Everything here is simulated.** The bodies, the controller, the policy and the camera are simulated in Python, in simulated time. Nothing is real-time, and no part of the runtime has run on hardware. Every number below says whether it is simulated time or wall-clock time, and where it was measured.

## Three layers, one control plane

| Layer | Role | Rate (simulated) | In R1 |
|---|---|---|---|
| S2 | Slow reasoning and planning. Requests goals, may install a fallback, decides what to do after a failure. | about 10 Hz, decisions taking 0.1 s to 5 s | a scripted planner with a latency model |
| S1 | Action generation: turns a goal into short timestamped trajectories, **action chunks**. | 20 Hz | a scripted pick-and-place policy |
| S0 | Real-time execution: the only layer that commands actuators, and it enforces the body's limits. | 1 kHz | a simulated controller |

![S2 and the runtime above; S1, S0 and the body below]({{ '/assets/runtime-layers.svg' | relative_url }})

**Figure 1.** The three layers and the runtime. Thick line: the high-rate path, where S1 sends action chunks to S0 directly, never through the runtime. S2 installs a fallback ahead of time through the runtime. S0 uses it when chunks stop.

The runtime is the **control plane**: it owns the application API, capability resolution, the execution lifecycle, supervision, the belief about the world, verification and traces. It is not in the high-rate path. Chunks go from S1 to S0 directly, and the runtime samples S0's status at its own rate.

**S0 never waits.** At every tick it executes a valid chunk, else a fallback S2 installed ahead of time, else its built-in HOLD, which keeps the joints where they are and leaves the gripper as it is. A chunk is executed only until its `valid_until`; S0 leaves it at the first tick after that. When an execution stops running (claim, failure, cancellation), the runtime tells S0 to hold: the rest of the chunk and the fallback are dropped.

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
| `omarchy-runtime scenarios` | Lists the scenarios. |

`--runs DIR` chooses where run directories go (default: `$OMARCHY_RUNTIME_RUNS`, else `./runs`). `--record-s0` also writes S0's per-tick record for each execution.

`--clock wall` steps the same components on the wall clock instead of simulated time: soft real-time, in Python, on a general-purpose OS. Each component gets the real time elapsed since the run began. A step that starts a whole period or more late is an overrun, and the releases it missed are skipped, never caught up. The run ends with each component's loop statistics: intervals between steps and step durations (median, 99th percentile, maximum), overruns. They are labelled "soft real-time, Python" with where they were measured. `--s0-hz N` changes S0's rate. Simulated time stays the default, and it alone is deterministic.

## The scenarios

A simulated kitchen in a vertical plane: a table with two cups, a counter with a drying rack, and one of two bodies. Body A (`arm3`) is a planar arm with three joints and a gripper; body B (`gantry`) is a Cartesian gantry. They have different kinematics and different action spaces.

| Scenario | What happens | Outcome |
|---|---|---|
| `nominal_a`, `nominal_b` | `in(cup_1, drying_rack)` on body A, then the same file with only `body` changed | SUCCEEDED |
| `slip_retry` | The cup slips over the table while S1 carries it. S1 does not notice and claims DONE; the verifier sees the cup on the table; the recovery policy retries once | FAILED/VERIFICATION_FAILED, then SUCCEEDED |
| `occluded_long` | The rack is hidden from the camera for the whole run. The cup is in the rack, but nothing can show it | UNVERIFIED/VERIFICATION_INCONCLUSIVE |
| `occluded_short` | The rack is hidden for 2.5 s after the release, within the verifier's budget | SUCCEEDED |
| `s1_stall_short` | S1 stops producing chunks for 1 s while it carries the cup: S0 holds, S1 resumes | SUCCEEDED |
| `s1_stall_long` | S1 stops for good: after 3 s without a chunk the supervisor ends the execution, S0 still holding the cup | FAILED/S1_STALL |
| `fallback_installed` | S2 installs a fallback (the safe pose, gripper closed); S1 stalls for 2.5 s; S0 runs the fallback, then HOLDs, then S1 resumes | SUCCEEDED |
| `unsupported_clean` | `clean(cup_1)`: no capability achieves it, and the resolver says why | REJECTED/NO_CAPABILITY |
| `s2_latency_0p1`, `_1`, `_5` | Both cups, with S2 taking 0.1 s, 1 s or 5 s for every decision | SUCCEEDED twice; only the task time and the robot's idle time change |

Each scenario is a TOML file in `pkgs/omarchy-runtime/omarchy_runtime/scenarios/`: seed, body, faults, S2's goals and latency, S1's inference latency, and the expected outcome of each execution.

## S2 in another process

S2 can run in its own process, on the robot or on another machine. The robot process keeps the runtime, S1, S0, the simulated world and the sensor; the S2 process runs the scripted S2, its recovery policy and its latency model. Both run on the wall clock.

```sh
omarchy-runtime s2 --listen 0.0.0.0:7447 --ttft 1                 # on the S2 machine
omarchy-runtime run s2_latency_1 --clock wall --s2 edge:7447      # on the robot
```

The link is newline-delimited JSON over TCP. S2 listens and the robot connects to it, so the robot's firewall still admits SSH only; the S2 machine admits the port. The robot speaks first: its hello carries the run's identity (run id, scenario, seed, configuration hash, the runtime and OS builds), the body spec S2 builds fallbacks from, and the heartbeat period. S2 answers with its own identity, latency model and recovery policy. Then only control-plane traffic crosses: requests, fallback installs, cancels, decisions and heartbeats from S2; execution updates with their summaries, heartbeat echoes and refusals from the robot. Action chunks never cross: S1 and S0 stay in the robot's process.

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
| `run.json` | Header: `trace_schema` (2), the runtime's version, revision and dirty flag, the Omarchy Robots identity (the contents of `/etc/omarchy-robot/build.json`, or `null` with "no Omarchy Robots identity" on another machine), the hash and the contents of the effective configuration, scenario, seed, clock (`simulated` or `wall`), body, rates, the id and version of the observation filter, the verifiers, the recovery policy and the S2 latency model (with its median and spread), where S2 ran (`s2`), and the run's `tags`. |
| `events.jsonl` | Low-rate events only, one JSON object per line, flushed one by one, so a crashed run leaves a readable prefix. Each has `seq`, simulated time `t` (ns), `wall_ns`, `type`, `execution` and `payload`. A wall-clock run's `run.end` carries the loop statistics; with S2 in another process, also S2's liveness (`s2_link`). Types: `run.start`, `request`, `resolution`, `lifecycle`, `chunk` (metadata, never the actions), `s0.mode`, `s0.status` (at the supervisor rate), `fallback.install`, `fallback.rejected`, `s0.hold`, `verify.attempt`, `verify.evidence` (the observations a verdict rests on), `verdict`, `decision`, `cancel`, `error`, `s2.link` (S2's link up, lost, recovered with the outage's class, left, closed), `run.end`. |
| `executions.jsonl` | One summary per execution: ids, goal, chosen capability and implementation with every candidate's reason, body and action space, belief before and after, claim, verdict with its evidence, outcome and category, simulated timings (time to first chunk, duration, time in fallback and in HOLD), wall-clock costs (resolution, verification, trace writing), S0's counters, configuration hash and seed. |
| `s0_<execution>.npz` | With `--record-s0`: S0's per-tick record for the execution (time, mode, position, desired position, chunk and its validity), kept in a preallocated buffer and written once at the end. |
| `results.json` | Written by the evaluation harness only: ground truth and agreement for each execution. |

Keys that start with `wall` are wall-clock values. Everything else follows from the scenario and its seed: the same scenario with the same seed gives the same trace.

## S2 placement experiment

> **Emulated network, VMs, soft real-time.** Two QEMU VMs, the network between them shaped with `tc netem`, Python on a general-purpose OS. Not a real network, nothing real-time, no hardware.

Where should S2 live: on the robot, on an edge server, or in the cloud (research question RQ5)? The VM test `tests/s2-placement.nix` starts a robot VM (Omarchy Robots, vm board, runtime enabled) and an edge VM. In each cell the robot runs the two-cup task (`s2_placement`) with S2 in its own process, on the robot VM (`local`) or on the edge VM, over one network profile, with one S2 time to first token (TTFT): 0.1 s, 1 s and 5 s stand in for a model on the robot, on an edge server and in the cloud. Delays are round trips, applied half on each VM; loss applies to every packet in each direction; with jitter, netem also reorders packets.

| Profile | Link |
|---|---|
| `local` | S2 on the robot VM, loopback |
| `lan` | about 1 ms |
| `wifi` | about 20 ms, jitter 10 ms, 1 % loss |
| `wan` | about 100 ms, jitter 20 ms |
| `degraded` | about 400 ms, jitter 100 ms, 5 % loss |
| `outage` | `lan`, then every packet dropped for 5 s during the first execution |

For every cell the table gives the outcomes, safety violations (ground truth), S0 starvation events, time in fallback and in HOLD over the whole task, the task time and the same task's time in one process on simulated time, S2's round trips (heartbeat echo), the outages the supervisor recorded with their class, and S0's loop intervals and overruns.

```sh
nix build .#checks.x86_64-linux.vm-s2-placement   # lan, degraded, outage x TTFT 0.1, 1 s (in nix flake check)
nix build .#s2-placement                          # every profile x TTFT 0.1, 1, 5 s; wants KVM
nix run .#omarchy-runtime -- placement-docs result/placement/results.json README.md manual/runtime.md
```

`result/placement` holds `results.json`, `table.txt` and `table.md`; `result/runs` the robot's traces; `result/s2-edge` and `result/s2-robot` the S2 processes' own reports. The last command writes the table below from a full run's results. CI runs the full matrix on demand (workflow dispatch with `placement`), alone on its runner, and keeps the results as an artifact.

<!-- s2-placement:begin -->
Not measured yet.
<!-- s2-placement:end -->

## Extension points

Each extension point is one small interface in `pkgs/omarchy-runtime/omarchy_runtime/contracts.py`, with a default implementation in `extensions.py`, chosen by id in `config/runtime.toml` (or a scenario's `[extensions]` table), and named with its version in every trace header.

| Interface | Default | For |
|---|---|---|
| `ObservationFilter.apply(observation)` returns the filtered observation and what it changed | `identity` | anonymization and privacy: it runs before the belief and the trace see an observation; it keeps entity ids stable within a run and says what it changed |
| `Verifier.verify(goal, belief, not_before)` returns a verdict with its evidence | `geometric`, per predicate | judging success independently of the executor |
| `RecoveryPolicy.decide(goal, summary, belief)` returns `Retry` or `GiveUp` | `retry_once`: once after VIOLATED; give up otherwise | retrying with modifications (`Retry` carries the conditioning passed to S1) |
| `LatencyModel.sample(rng)` returns a delay, S2's time to first token | `fixed`; also `lognormal` (sampled for each decision) | where S2 should run (R2) |

An extension that raises, or returns the wrong type, never stops the run: a failing filter loses that observation (unfiltered data never reaches the belief), a failing verifier gives INCONCLUSIVE, a failing recovery policy means giving up, and each leaves an `error` event in the trace.

To add one: write the class, add it to the registry in `extensions.py`, select it by id in the configuration, and pass the contract tests in `tests/contracts.py` by mixing the matching class into a test case. The runtime core does not change. The tests run with:

```sh
cd pkgs/omarchy-runtime
nix develop ../.. -c python3 -m unittest discover -s tests -t .
```

## What is not here

No ROS and no ROS types, no learned model, no images, no physics engine (a cup attaches when the gripper closes around it, and settles on the support below when released), no real-time guarantee, and no hardware. The S0 contract is plain data so that a native controller, a vendor controller or ros2_control behind an adapter can implement it later.
