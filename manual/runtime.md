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
| `omarchy-runtime scenarios` | Lists the scenarios. |

`--runs DIR` chooses where run directories go (default: `$OMARCHY_RUNTIME_RUNS`, else `./runs`). `--record-s0` also writes S0's per-tick record for each execution.

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

S2 and applications use the runtime through two calls, `submit(message)` and `poll()`, with JSON messages: `request` (a goal), `install_fallback`, `cancel` and `decision`. The API is asynchronous. A request carries a `request_id` that its sender chooses, and every update about the execution it creates carries that id back, with the execution id and the runtime's time of the change. The same calls work in the runtime's process and from another process.

## Traces

Every run writes one directory:

| File | Contents |
|---|---|
| `run.json` | Header: `trace_schema` (2), the runtime's version, revision and dirty flag, the Omarchy Robots identity (the contents of `/etc/omarchy-robot/build.json`, or `null` with "no Omarchy Robots identity" on another machine), the hash and the contents of the effective configuration, scenario, seed, clock (`simulated`), body, rates, and the id and version of the observation filter, the verifiers, the recovery policy and the S2 latency model. |
| `events.jsonl` | Low-rate events only, one JSON object per line, flushed one by one, so a crashed run leaves a readable prefix. Each has `seq`, simulated time `t` (ns), `wall_ns`, `type`, `execution` and `payload`. Types: `run.start`, `request`, `resolution`, `lifecycle`, `chunk` (metadata, never the actions), `s0.mode`, `s0.status` (at the supervisor rate), `fallback.install`, `fallback.rejected`, `s0.hold`, `verify.attempt`, `verify.evidence` (the observations a verdict rests on), `verdict`, `decision`, `cancel`, `error`, `run.end`. |
| `executions.jsonl` | One summary per execution: ids, goal, chosen capability and implementation with every candidate's reason, body and action space, belief before and after, claim, verdict with its evidence, outcome and category, simulated timings (time to first chunk, duration, time in fallback and in HOLD), wall-clock costs (resolution, verification, trace writing), S0's counters, configuration hash and seed. |
| `s0_<execution>.npz` | With `--record-s0`: S0's per-tick record for the execution (time, mode, position, desired position, chunk and its validity), kept in a preallocated buffer and written once at the end. |
| `results.json` | Written by the evaluation harness only: ground truth and agreement for each execution. |

Keys that start with `wall` are wall-clock values. Everything else follows from the scenario and its seed: the same scenario with the same seed gives the same trace.

## Extension points

Each extension point is one small interface in `pkgs/omarchy-runtime/omarchy_runtime/contracts.py`, with a default implementation in `extensions.py`, chosen by id in `config/runtime.toml` (or a scenario's `[extensions]` table), and named with its version in every trace header.

| Interface | Default | For |
|---|---|---|
| `ObservationFilter.apply(observation)` returns the filtered observation and what it changed | `identity` | anonymization and privacy: it runs before the belief and the trace see an observation; it keeps entity ids stable within a run and says what it changed |
| `Verifier.verify(goal, belief, not_before)` returns a verdict with its evidence | `geometric`, per predicate | judging success independently of the executor |
| `RecoveryPolicy.decide(goal, summary, belief)` returns `Retry` or `GiveUp` | `retry_once`: once after VIOLATED; give up otherwise | retrying with modifications (`Retry` carries the conditioning passed to S1) |
| `LatencyModel.sample(rng)` returns a delay | `fixed` | where S2 should run (R2) |

An extension that raises, or returns the wrong type, never stops the run: a failing filter loses that observation (unfiltered data never reaches the belief), a failing verifier gives INCONCLUSIVE, a failing recovery policy means giving up, and each leaves an `error` event in the trace.

To add one: write the class, add it to the registry in `extensions.py`, select it by id in the configuration, and pass the contract tests in `tests/contracts.py` by mixing the matching class into a test case. The runtime core does not change. The tests run with:

```sh
cd pkgs/omarchy-runtime
nix develop ../.. -c python3 -m unittest discover -s tests -t .
```

## What is not here

No ROS and no ROS types, no learned model, no images, no physics engine (a cup attaches when the gripper closes around it, and settles on the support below when released), no real-time guarantee, and no hardware. The S0 contract is plain data so that a native controller, a vendor controller or ros2_control behind an adapter can implement it later.
