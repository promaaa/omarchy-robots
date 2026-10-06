# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0, see README).

## [Unreleased]


## [0.0.2] - 2026-10-06

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
  overran in 6 to 20 % of S0's steps in a KVM VM; at 200 Hz, at most once
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

- The scripted S1 could push a held cup into the table. Found by
  `vm-s2-placement` in CI: the robot VM's loop stalled for 283 ms, S0 fell
  more than 3 cm behind S1's plan, and S1 replanned straight into its
  gripper-closing waypoint, moving down and closing together; the gripper
  caught the cup above the grasp point (ground truth: held cup_2 5 mm into
  the table). A replan now reaches a grasp or release spot before it
  changes the gripper. Simulated scenarios give the same results.


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
