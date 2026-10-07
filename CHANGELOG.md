# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0, see README).

## [Unreleased]


### Changed

- Contract change, fallbacks exist before they are needed: S0 holds two
  fallbacks and runs the first valid one, else HOLD. S2's (predicted with
  the goal, or refined) takes precedence over the base fallback, a
  capability's default computed by the runtime on the robot side. Both are
  installed when an execution starts, before S1's first chunk;
  `S0.install_fallback(spec, now, base=False)` replaces one in a single
  step, so the one replaced stays valid until then. When S2's runs out, S0
  goes on under the base in the same tick. A fallback engages when a chunk
  ran out with no successor, or when the runtime asks after a failure; while
  an execution awaits S1's first chunk, S0 holds. A chunk that takes over
  no longer spends the fallback: it engages again the next time. Before,
  S2 installed its fallback one decision after the execution started, and
  with a 5 s TTFT S1 gave up before any fallback existed.
- Contract change: `GoalRequest` carries S2's predicted fallback
  (`fallback`, its window relative to the execution's start); link protocol
  3 (protocol 2 peers are refused). `FallbackSpec` gains `relative` (per
  dimension: offsets from where S0 is when it engages) and `S0Status`
  `fallback_source`; `fallback_installed` now means a valid fallback is
  installed, S2's or the base.
- Contract change: S0 counts `fallback_cover_ns`, the time a valid fallback
  is installed, tick by tick. Execution summaries carry
  `fallback_coverage`, the fraction of their running time (from the start
  to S1's claim or the failure) covered; evaluation requires at least 99 %
  for every execution, and the placement experiment reports it per cell.
- Trace schema 5: S0's per-tick record gains `fallback` (none, base or S2's,
  the fallback S0 runs); `fallback.install` events carry the slot, the kind
  (capability default, s2 predicted, s2 refined) and the window; the header
  names each capability's default fallback generator.
- The full-matrix test requires again that a fallback engages in every
  recovery, and coverage of at least 99 % for every execution. The relaxed
  check of v0.0.2 part 4 (the retry comes, with or without a fallback) is gone: the
  strict one implies it.
- A recovery's `fallback_expired` now means S2's fallback ran out before the
  retry started and the capability's default took over (before: S0 went to
  HOLD).

### Added

- `FallbackGenerator` (contracts.py), with `hold` (every dimension holds
  where S0 is, the gripper included) and `lift` (the same; holding an
  object on a body with a `z` dimension, rise 2 cm first) in
  `DEFAULT_FALLBACKS`. `capabilities.toml` names one per capability
  (`default_fallback`; `place_in` uses `lift`). A default never moves the
  gripper (contract test `FallbackGeneratorContract`). The runtime renews
  it when the generator returns another motion, after a grasp or a release.
- The scripted S2 sends its predicted fallback with each goal
  (`[s2] fallback = true`) and refines it once the execution runs when
  `refine_fallback = true` (scenario `fallback_installed`).
- `omarchy-runtime placement-compare BEFORE AFTER [--docs FILE...]`: two
  placement runs compared for each profile and TTFT (fallback coverage,
  the fallback engaged in recoveries, S2's fallbacks expired, the HOLD time
  while S2 decides, the recovery times), coverage and HOLD time computed
  from both runs' traces the same way. The README and the manual show the
  v0.0.2 part 4 matrix against this release's.

## [0.0.2] - 2026-10-06

### Part 4 (2026-10-06)

Milestone R3, parts B and C: an experiment where S2's placement matters,
and the seam for asynchronous inference. Scenes that change during a task,
recovery under S2's fallback, the S2 placement matrix on them, and the
chunk corrector and inference trigger extension points with a simulated
baseline matrix. Tested in simulation and in VMs: not hardware-tested, and
nothing is real-time.

#### Changed

- Contract change, chunk timing: `ActionChunk` carries `observed_at` (when
  the observation it was computed from was taken) and `inference_ns` (how
  long S1's inference took); the trace's `chunk` events carry both. Every S0
  keeps a bounded history of its ticks (2 s in the simulated S0) and returns
  it with `S0.history(since)` (`S0History`: tick times, commanded and
  desired positions), for the robot side only.
- Contract change, fallback duration: `FallbackSpec` gains `lasts`, how
  long S0 holds the fallback's last row before it returns to HOLD (by
  default, the rows' duration; `fallback_lasts_s` in a scenario's `[s2]`).
  `S0.hold(fallback=True)` engages the installed fallback: when an execution
  fails, the runtime now engages it instead of holding. S0's counters gain
  `fallback_expiries`.
- Trace schema 4: S0's per-tick record gains `observed_at`; `chunk` events
  carry `observed_at` and `inference_ns`; S0's counters gain
  `fallback_expiries`. Validators of schema 3 refuse these traces.
- The simulated S1 plans its grasp again when its target moved by more than
  1.5 cm, and gives up ("blocked") when an object stands in its place spot.

#### Added

- Scenes that change during a task: `[[faults]]` kinds `move` (an entity
  moved by `x`) and `appear` (an object declared with `present = false`
  placed at `x`). A new fault trigger, `moving`: the body's first motion,
  after S2's first decision whatever its latency. Scenarios `cup_moved` (S1 reacts alone), `slot_blocked`
  (S1 gives up, S0 waits under the fallback, S2 chooses another spot) and
  `fallback_expiry` (the fallback expires before S2 answers). The recovery
  policy `retry_elsewhere` retries with the first free spot of the
  container (`{"place_x": x}`).
- Evaluation measures the dynamics of each run (`dynamics` in
  `results.json`): reaction time to a scene change, freeze time, recovery
  time with the time in fallback and whether it expired, the discontinuity
  at chunk switches, and the number of inferences.
- The S2 placement experiment runs the dynamic scenarios too: the full
  matrix is 4 scenarios x 7 profiles x 3 TTFTs. The table gains a summary
  with one row for each profile and TTFT: the two-cup task time,
  cup_moved's reaction, the recovery times, the fallback expiries and
  safety; every cell follows in a collapsed section.
- The asynchronous inference seam, on the robot side: `ChunkCorrector`
  (`none`, `blend`), `InferenceTrigger` (`periodic`, `mismatch`) and
  `InferenceContext`, chosen by id in `[extensions]` of `runtime.toml`, with
  their parameters in `[s1_host]`, and contract tests (`CorrectorContract`,
  `TriggerContract`). `omarchy-runtime
  inference-matrix` runs S1's inference delay (50, 150, 300 ms) x corrector
  x trigger on `nominal_a` and `cup_moved` in simulated time.


### Part 3 (2026-10-06)

Milestone R3, part A: hardening for hardware. Bounded S0 steps and late
ticks; deterministic timing faults with regressions for the two S1 bugs of
v0.0.2 part 2; reconnection to S2 after an outage, with idempotent requests; the S2
link over WireGuard (`omarchy.robot.s2Link`); CI as parallel jobs. Tested in
simulation and in VMs: not hardware-tested, and nothing is real-time.

#### Changed

- Contract change, bounded S0 steps: S0 never moves a dimension more in one
  tick than its velocity limit times its nominal tick period, whatever the
  real interval since the last tick. A tick later than a threshold (1.5
  periods, `[s0] late_after_periods` in `world.toml`) is a late tick and
  never produces a catch-up jump: S0 continues from its current state. Before,
  a late tick on the wall clock moved up to the velocity limit times the real
  interval in one step. `S0Counters` gains `late_ticks`, `max_interval_ns`
  and `max_step` (the largest step as a fraction of the bound); the runtime
  records each late tick (`s0.late`); evaluation checks the bound on every
  tick. Every S0 implementation, a hardware one included, keeps this.
- Trace schema 3: S0's counters gain `late_ticks`, `max_interval_ns` and
  `max_step`; a new event type `s0.late`.
- Contract change, link protocol 2: every message from S2 and every update
  from the robot carries a link number `n`; heartbeats and echoes carry
  `ack`; the hellos carry `ack` and the robot's a `session`; the robot says
  `bye` at the end of the run. Protocol 1 peers are refused.
- Contract change: a `GoalRequest` is idempotent by `request_id`. The same
  request again creates no second execution, and its latest update comes
  again; another request with a used id is still refused.
- The simulated S1's inference latency is drawn for each chunk (`[s1]
  latency_spread`, lognormal) and chunks reach S0 in the order they arrive.

#### Changed (CI)

- CI runs `nix flake check` as parallel jobs: a first job lists every check
  of the flake, and a matrix builds one VM test per job, every board's
  evaluation (with `nix flake check --no-build` and the aarch64 installer
  images) in one, and the rest in one. No check is removed; a new check is
  always included. `check=false` still dispatches only the chosen jobs.

#### Added

- `omarchy.robot.s2Link`: the link to S2 over a WireGuard tunnel declared in
  `robot.nix` (`enable`, `address`, `listenPort`, `privateKeyFile`, `port`,
  `peer.address`, `peer.publicKey` or `peer.publicKeyFile`,
  `peer.endpoint`; `role = "s2"` on S2's machine). The private key is a
  path read at run time, never in the Nix store (the build refuses one
  there). `/etc/omarchy-robot/s2-link.json` names the tunnel: `omarchy-runtime
  run --s2` then connects from and to tunnel addresses only, and
  `omarchy-runtime s2` listens on the tunnel only. The VM test `vm-s2-link`
  generates keys at test time, runs a task through the tunnel, captures the
  LAN to show no S2 traffic in the clear, and checks the refusals outside
  it. No TLS in the runtime: the OS owns transport security.
- Reconnection: after a broken connection, or 3 s of silence from S2
  (application heartbeats, not TCP timing), the robot connects again
  without blocking its loop, with a bounded backoff (0.25 s doubling to
  2 s; `[s2_link]` in `runtime.toml`). S2 keeps listening and resumes the
  session of the same run id; both ends send again what the other has not
  acknowledged and ignore repeats, so a resumed session neither repeats nor
  loses a goal. TCP_USER_TIMEOUT (3 s) and keepalive bound TCP's
  retransmission backoff. The trace records `s2.link` `closed` and
  `reconnected` (time down, attempts, updates sent again). A new placement
  profile, `outage_long`: the link down for 20 s during the first
  execution.
- Deterministic timing faults in simulated time, from a scenario's seed:
  observation age (`[timing] observation_age_s`, `_spread`), S0 tick jitter
  (`s0_skip_probability`), S0 stalls and whole-process pauses (`[[faults]]`
  kinds `s0_stall` and `pause`), S1 latency distributions. Scenarios
  `timing_observation_age` and `timing_pause_grasp` reproduce the two S1
  bugs CI found in v0.0.2 part 2 and fail with the S1 behaviour of each bug
  (`tests/test_timing.py`); `timing_s0_stall` and `timing_jitter` exercise
  late ticks and noisy timing.


### Part 2 (2026-10-06)

Milestone R2: S2 in its own process, on the robot or on another machine,
connected to the runtime over TCP; the runtime on the wall clock; and the S2
placement experiment (RQ5) on two VMs with an emulated network. Soft
real-time Python, an emulated network, VMs: not hardware-tested, and nothing
is real-time.

#### Changed

- Contract change, the runtime's API is asynchronous: `RuntimeApi.submit()`
  returns nothing; a `GoalRequest` carries a `request_id` chosen by its
  sender; every `Update` carries that `request_id` and the runtime's time `t`
  of the change. The runtime names an execution when it handles the request,
  not when it is submitted. Every client changes: the scripted S2 names its
  requests, learns the execution from the Updates, and opens a fallback's
  window at the Update's `t` (the runtime's clock) instead of its own time.
- Contract change for latency models: the `LATENCY` registry makes a model
  from a median time to first token and a spread, `(seconds, spread)`;
  `fixed` refuses a spread.
- Contract change: `Message` includes `Heartbeat`, and the runtime's
  `Params` gain the S2 link deadlines (`[s2_link]` in `runtime.toml`).
- Trace schema 2: execution summaries and `request` events carry
  `request_id`; the header names where S2 ran (`s2`) and the run's `tags`;
  a new event type `s2.link`; `run.end` carries S2's liveness when S2 ran in
  another process. Validators of schema 1 refuse these traces.

#### Added

- A wall-clock scheduler (`--clock wall`, `--s0-hz`): the same components,
  stepped on the real clock with the real elapsed time, soft real-time in
  Python. It records every component's loop intervals and step durations
  (median, 99th percentile, maximum) and its overruns, and writes them into
  `run.end`, labelled with where they were measured. On the wall clock S0
  runs at 200 Hz (`[wall_rates_hz]` in `world.toml`): at 1 kHz, Python
  overran in 6 to 20 % of S0's steps in a KVM VM; at 200 Hz, at most twice
  per run. Simulated time stays the default (S0 at 1 kHz) and stays
  deterministic. Only `wallclock.py` reads the clock or sleeps.
- S2 in another process, maybe on another machine: `omarchy-runtime s2`
  (the scripted S2, its recovery policy and latency model) and `run ...
  --clock wall --s2 HOST:PORT` (the runtime, S1, S0, the world and the
  sensor). One transport interface (`Transport`) and its first
  implementation, newline-delimited JSON over TCP, standard library only.
  S2 listens and the robot connects out. Only control-plane traffic
  crosses: hellos (run identity, body spec), requests, fallback installs,
  cancels, decisions, heartbeats, execution updates with summaries, echoes.
- S2 liveness: S2 sends a heartbeat every 0.1 s and measures the round trip
  from its echo; the runtime's supervisor records the link lost after 0.5 s
  without one, and each outage with its class against the deadlines (short,
  long from 2 s, unrecovered). Executions continue and S0 never waits.
- The S2 placement experiment (RQ5): scenario `s2_placement` (the two-cup
  task), the VM test `vm-s2-placement` (a robot VM and an edge VM, the link
  shaped with tc netem; profiles local, lan, wifi, wan, degraded, outage)
  and `omarchy-runtime placement` (one results.json and one table,
  labelled emulated network, VMs, soft real-time, each cell compared with
  the same task in one process on simulated time) and `placement-docs`
  (writes the table into the README and the manual). `nix flake check`
  runs lan, degraded and outage with S2 times to first token 0.1 and 1 s;
  `nix build .#s2-placement` and the manual CI job (`placement`) run every
  profile with 0.1, 1 and 5 s. The README and the manual hold the latest
  table, written by `placement-docs`.
- CI: workflow_dispatch inputs `placement` (the full matrix, alone on its
  runner, results kept as an artifact) and `check` (on by default).
- `run --ttft S [--ttft-spread X]`: S2's time to first token in one
  process, instead of the scenario's latency.
- `lognormal`: a latency model whose time to first token is sampled for
  each decision (median and spread), registered in `extensions.py` and
  held to `LatencyContract`.
- `from_json()` for every message of the API (`message_from_json`), for
  `Update` and for `BodySpec`, with a round-trip test.

#### Fixed

- The scripted S1 on the wall clock, found by `vm-s2-placement` in CI on a
  starved runner. S1 compared the body it observed with its plan at its own
  step time instead of the observation's time: an observation 15 ms old
  made a gripper closing at 2/s look off the plan, and S1 replanned. A
  replan into the gripper-closing waypoint moved the arm down and closed
  together, so after a 283 ms stall the gripper caught a cup above the grasp
  point (ground truth: held cup_2 5 mm into the table). S1 now compares an
  observation with the plan at the time it was taken, and a replan into a
  grasp or release reaches the spot first unless the body is there. With
  every observation 20 ms old on the wall clock, the task now takes 18.67 s
  (simulated: 18.45 s). Simulated scenarios give the same results.


### Part 1 (2026-10-04)

Milestone R1: the first slice of the runtime, simulated, on Omarchy Robots;
and a generic `pc` board. Not hardware-tested, and nothing is real-time.

#### Added

- `omarchy-runtime` (`pkgs/omarchy-runtime`, Python with numpy): goals in,
  verified executions and traces out. A deterministic resolver that records
  every candidate and its reason; the execution lifecycle with outcomes
  SUCCEEDED, FAILED, UNVERIFIED, REJECTED, ABORTED and eight failure
  categories; a supervisor (deadline, S1 liveness, S0 fault); a belief built
  only from observations; verification as a stepped phase whose verdict is
  recorded apart from the executor's claim. Commands `demo`, `eval`, `run`,
  `show`, `validate-trace`.
- Contracts for action chunks, S0 status and fallbacks as plain data; a
  simulated 1 kHz S0 that never waits (chunk, fallback or HOLD) and enforces
  position, velocity and workspace limits; a scripted S1 per body; a planar
  arm and a gantry with different action spaces; a scripted S2 with a
  decision latency, using only the public API.
- Twelve scenarios (nominal on both bodies, slip and retry, short and long
  occlusion, short and long S1 stall, installed fallback, unsupported goal,
  S2 latency 0.1, 1 and 5 s) and an evaluation harness that compares each
  execution with the simulator's ground truth.
- Trace schema 1: `run.json` (with the contents of
  `/etc/omarchy-robot/build.json`, or null), `events.jsonl`,
  `executions.jsonl`, optional `s0_<id>.npz`, `results.json`. Versioned
  separately from releases and from the validation report.
- Extension points for collaborators, each with a default and reusable
  contract tests: ObservationFilter, Verifier (per predicate),
  RecoveryPolicy, S2 latency model.
- `omarchy.robot.runtime.enable`: the runtime on the robot, its runs in
  `/var/lib/omarchy-runtime/runs`; `build.json` gains `runtime` (version or
  null) and `omarchy-robot info` prints it. Validation report schema
  unchanged (1).
- Board `pc`: a generic x86_64 UEFI computer (NixOS generic hardware
  support, redistributable firmware, Intel and AMD microcode, `/dev/nvme0n1`
  by default) and its installer image `.#installer-pc`. `gpu.enable` and
  `power.mode` are refused on it.
- Checks: `omarchy-runtime` (ruff, mypy --strict, 97 tests with the
  scenario matrix, no VM), `vm-runtime` (the runtime in a vm-board VM),
  `vm-installer-pc`, `eval-pc`, `eval-runtime-orin-agx-devkit`,
  `eval-runtime-pc`, refusals for `pc`.
- `AGENTS.md` for collaborators and coding agents, `.github/CODEOWNERS`, the
  manual's Runtime chapter and "Install on a spare PC".

#### Changed

- Versioning: v0.0.2 part 1 is milestone R1. The first board booted and validated
  on hardware takes v0.1.0.
- Jetson boards are the `boards.nix` entries with a `som`, instead of every
  board but `vm`.
- `nix develop` has python3 with numpy and mypy instead of python3 and mypy.

## [0.0.1] - 2026-10-03

### Part 5 (2026-10-03)

#### Changed

- The boot screen (Jetson UEFI firmware, installer menus) shows only the
  dotted wordmark, centred; the logo stays on the login banners.


### Part 4 (2026-10-03)

#### Added

- The Omarchy Robots manual, ten chapters published to GitHub Pages
  (`manual/`).

#### Changed

- New Omarchy Robots logo, Socket (tall slot eyes), and one wordmark, the
  dotted one: `branding/omarchy-robots-wordmark.svg` (the plain wordmark is
  removed). The Jetson UEFI boot screen, the installer menus, the login
  banners, the README and the manual use them.


### Part 3 (2026-10-03)

Milestone OS-1 complete: the system of 0.0.1, VM-tested, with every
Jetson target evaluating, the orin-agx-devkit system and every board's flash
script building. Not hardware-tested.

#### Changed

- README: support matrix and measurements for the final builds (Thor flash
  script, the branded orin-agx-devkit firmware, a local `nix flake check`),
  and a boot time comparison of rc.1 and rc.2 (no difference within noise).


### Part 2 (2026-10-02)

#### Added

- Omarchy Robots identity: os-release (`NAME="Omarchy Robots"`, version in
  `PRETTY_NAME`, `ID_LIKE=nixos`), boot menu entries, installer menus, console
  banner and SSH MOTD.
- The Omarchy Robots logo and wordmark (`branding/`) replace NixOS's: on the
  Jetson UEFI boot screen (jetpack-nixos's `firmware.uefi.logo`), the
  installer's GRUB and syslinux menus, and as a half-block logo on the login
  banners. Static files only.

#### Changed

- The boot menu waits 1 s instead of 5; users are managed by userborn instead
  of a Perl script at each activation; `nixos-generate-config` and the nixpkgs
  source are no longer in the system. The vm closure goes from 1.3 to 1.1 GiB.
- Test VMs wait up to 15 minutes for their disk (slow hosts without KVM).


### Part 1 (2026-10-02)

First release candidate of milestone OS-1: a declarative, Nix-based robot OS,
VM-validated and ready for Jetson bring-up. Not hardware-tested.

#### Added

- `robot.nix`, the one file that declares a robot, and the `omarchy.robot.*`
  options: name, board, storage device, administrator and SSH keys, wired
  network (DHCP or static), time zone, GPU stack, power mode, generations kept.
- Base system: headless, SSH keys only, firewall admitting SSH only, flakes,
  systemd-boot with a bounded number of entries, weekly garbage collection that
  keeps those generations, bounded journal, disko layout on `storage.device`.
- Boards: `vm` (QEMU, x86_64) and the Jetson developer kits `orin-agx-devkit`,
  `orin-nx-devkit`, `orin-nx-super-devkit`, `orin-nano-devkit`,
  `orin-nano-super-devkit` and `thor-agx-devkit`, mapped onto jetpack-nixos
  (JetPack 6.2.3 / L4T 36.5.2 on Orin, JetPack 7.2.1 / L4T 39.2.1 on Thor).
- Build identity in `/etc/omarchy-robot/build.json`: target, flake revision,
  robot.nix hash, input revisions, kernel, board and JetPack/L4T versions.
- `omarchy-robot` with `info`, `validate`, `update` and `rollback`.
  Validation report schema 1.
- Per-board flash scripts (jetpack-nixos, cross-built on x86_64) and
  installation images that carry the system they install (`omarchy-install`).
- Checks run by `nix flake check`: nixfmt, statix, deadnix, ruff, mypy, unit
  tests on fixtures, per-board evaluation assertions, refusal of invalid
  `robot.nix` combinations, and VM tests for the base system, update and
  rollback across a reboot, and installation onto a blank disk.
- GitHub Actions CI: `nix flake check` on x86_64; a manual arm64 job building
  one Jetson system.

[Unreleased]: https://github.com/promaaa/omarchy-robots/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/promaaa/omarchy-robots/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/promaaa/omarchy-robots/releases/tag/v0.0.1
