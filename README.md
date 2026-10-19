<h1 align="center"><img src="branding/omarchy-robots-wordmark.svg" alt="Omarchy Robots" width="640"></h1>

A small, opinionated operating system for robot computers, built on NixOS and
[jetpack-nixos](https://github.com/anduril/jetpack-nixos). One file,
`robot.nix`, declares the whole machine: which board it runs on, who may log
in, its network, its disk, its GPU stack and power mode. Everything else is a
default you do not have to think about, and every build carries its own
identity so an experiment can name the exact system it ran on.

It is headless and deliberately plain: SSH keys only, a firewall that admits
SSH and nothing else, a bounded history of system generations to roll back to,
and one command, `omarchy-robot`, to inspect, validate, update and roll back.
It boots as Omarchy Robots: its dotted wordmark on the Jetson's UEFI boot
screen and the installer's menus, its logo on the login banners.

On top of it runs the first slice of the [runtime](#runtime), the layer
between intelligence and robot bodies: an application asks for a goal, the
runtime executes it, verifies it from fresh observations and records a trace
that names the exact system it ran on. **The robot is simulated** and nothing
is real-time: the runtime runs in simulated time, or on the wall clock as soft
real-time Python, with S2 in its process or in its own, on another machine.

**Status:** v0.0.3 part 1 (milestone R6, part 1: intents with contact and object
channels, S0 split into a guard and a controller, failure attribution) runs
and is tested in virtual machines: the base
system, the runtime's simulated scenarios (on a laptop and inside an Omarchy
Robots VM), the runtime on the wall clock with S2 in its own process on a
second VM over an emulated network (the S2 placement experiment), and the
installation of a generic PC onto an emulated disk. The Jetson and pc
targets build (system, installer image and, on a Jetson, flash script); none
has run on hardware yet (see the [support matrix](#support-matrix)).

## One file

```nix
# robot.nix
{
  omarchy.robot = {
    name = "robot";
    board = "orin-agx-devkit";   # or "vm", "orin-nano-super-devkit", "thor-agx-devkit", ...
    admin.sshKeys = [ "ssh-ed25519 AAAA... you@laptop" ];
    gpu.enable = true;           # CUDA and TensorRT samples for validation
    power.mode = 0;              # nvpmodel mode ID
    generations = 10;            # kept for rollback
  };
}
```

The options are documented in [`robot.nix`](robot.nix) and, with types and
defaults, in [`modules/options.nix`](modules/options.nix). `robot.nix` is a
NixOS module, so any NixOS option can be set there too when nothing under
`omarchy.robot` fits. The boards and their mapping onto jetpack-nixos are in
[`modules/boards.nix`](modules/boards.nix).

## Quickstart: the robot as a virtual machine

You need an x86_64 Linux machine with [Nix](https://nixos.org/download)
(flakes enabled). KVM makes the VM fast; without it QEMU emulates, slowly.

```sh
git clone https://github.com/promaaa/omarchy-robots
cd omarchy-robots
nix run .#vm
```

The VM boots through UEFI and systemd-boot, as the hardware does, and its
serial console (this terminal) logs you in as the administrator. Then:

```sh
sudo omarchy-robot validate
sudo poweroff        # leaves QEMU
```

To log in over SSH instead, put your public key in `robot.nix`
(`admin.sshKeys`) before `nix run .#vm`, then `ssh -p 2222 admin@localhost`.
The VM keeps its disk in `robot.qcow2` in the current directory; delete it to
start over. Without a clone, `nix run github:promaaa/omarchy-robots#vm` works
the same (the repository is private: Nix needs a GitHub token, for example
`nix --option access-tokens github.com=$(gh auth token) run ...`).

## omarchy-robot

| Command | What it does |
|---|---|
| `omarchy-robot info [--json]` | This system's identity: target, flake revision, robot.nix hash, input revisions, NixOS and kernel, JetPack/L4T, current and booted generation. |
| `omarchy-robot validate [--json]` | The bring-up checklist below. Exits 1 if any check fails. Run it with `sudo`: `/boot` and some Jetson tools are root-only. |
| `sudo omarchy-robot update <flake-or-system>` | Switch to a new system: a flake reference (the build's target is appended unless given, e.g. `github:me/robot`) or an already built `/nix/store/...-nixos-system-...` path. |
| `sudo omarchy-robot rollback` | Switch back to the previous generation; it also becomes the boot default. |

`validate` reports each check as `PASS`, `FAIL`, `SKIPPED` (with the reason)
or `NOT_APPLICABLE`; a check that did not run is never a `PASS`. The JSON
report has a `schema` version (currently 1), the build identity, the
timestamp, the kernel and every check with its data.

| Check | Measures | Applies to |
|---|---|---|
| `identity` | `/etc/omarchy-robot/build.json` is present and matches the running host; warns on a dirty revision or a pending reboot | all |
| `boot.root` | the root filesystem is on `storage.device` | all |
| `boot.entries` | systemd-boot entries exist; kept generations, current and default | all |
| `services.ssh` | sshd active; password and keyboard-interactive login off | all |
| `services.firewall` | the firewall is active | all |
| `storage` | root filesystem type and free space | all |
| `jetson.model` | `/proc/device-tree/compatible` has the board's carrier and module part numbers | Jetson |
| `jetson.l4t` | `/etc/nv_tegra_release` is the pinned L4T, and `ota-check-firmware` reports the firmware this L4T expects | Jetson |
| `gpu.device` | the GPU device node of the pinned L4T (`/dev/nvgpu/igpu0/ctrl` on Orin, `/dev/nvidiactl` on Thor) | Jetson, `gpu.enable` |
| `cuda.runtime` | CUDA `deviceQuery` passes and reports the expected compute capability | Jetson, `gpu.enable` |
| `tensorrt.runtime` | TensorRT `sample_onnx_mnist` passes | Jetson, `gpu.enable` |
| `camera` | V4L2 devices; `SKIPPED` when none is connected | Jetson |
| `power.mode` | `nvpmodel -q` against `power.mode` | Jetson |
| `thermal` | thermal zones readable; temperatures recorded | Jetson |

## Runtime

> **Simulated.** Bodies, controller, policy and camera are Python simulations
> running in simulated time, or on the wall clock as soft real-time Python.
> Nothing is real-time and nothing has run on hardware. Every number says
> whether it is simulated or wall-clock time, and where it was measured.

`omarchy-runtime` is the control plane between a slow planner (S2), a policy
that produces short trajectories, action chunks (S1), and the controller that
alone commands the actuators (S0). S2 asks for goals such as
`in(cup_1, drying_rack)`, in JSON; the runtime resolves a capability for this
body, starts S1, supervises the execution, judges the outcome from fresh
observations independently of what S1 claims, and writes a trace whose header
embeds `/etc/omarchy-robot/build.json`.

![S2 and the runtime above; S1, S0 and the body below](manual/assets/runtime-layers.svg)

**Figure 1.** The three layers and the runtime. Thick line: the high-rate
path, where S1 sends action chunks to S0 directly, never through the runtime.
S2 installs a fallback ahead of time through the runtime. S0 uses it when
chunks stop.

S1 sends S0 intents (contract v2): a motion and, optionally, an object's
intended motion and contact (make, maintain or break it; a target force or
wrench with bounds; a stiffness per axis), each channel validated on its own.
A v1 action chunk is a motion-only intent. Each S0 controller declares what it
honours; anything else is refused (`UNSUPPORTED_INTENT`), or executed in a
degraded mode its configuration allows, and recorded. S0 is two halves: the
guard, Omarchy's, and a controller behind it. The guard never waits: at every
tick (1 kHz, simulated) it gives the controller a valid chunk, else a
fallback, else it holds itself; it checks every command (limits, NaN, stale,
force) and clamps or replaces it. Its steps are bounded: never more than the
velocity limit times its nominal tick period, so a late tick never catches up
with a jump. A fidelity check compares what S0 achieved with the intent, and
each failed execution is attributed to INTENT, EXECUTION, PERCEPTION or
INFRASTRUCTURE (manual, Runtime). Every execution holds a
fallback from its start, before S1's first chunk: the capability's default
(hold where S0 is and keep the grasp; on a gantry, holding an object, rise
2 cm first), computed by the runtime on the robot, and S2's prediction,
which travels with the goal and takes precedence. When S2's runs out, the
default takes over in the same tick.

```sh
nix run .#omarchy-runtime -- demo    # nominal_a, then slip_retry, told step by step
nix run .#omarchy-runtime -- eval    # every scenario, against ground truth
```

On the robot itself: set `runtime.enable = true;` in `robot.nix`, run
`nix run .#vm`, then `omarchy-runtime demo` in the VM. Run directories go to
`/var/lib/omarchy-runtime/runs`, and `omarchy-robot info` names the runtime.

| Scenario | What happens | Outcome |
|---|---|---|
| `nominal_a`, `nominal_b` | `in(cup_1, drying_rack)` on a planar arm, then on a gantry (only `body` differs) | SUCCEEDED |
| `slip_retry` | the cup slips; S1 still claims DONE; the verifier sees it on the table; one retry | FAILED/VERIFICATION_FAILED, then SUCCEEDED |
| `occluded_long` | the rack is hidden for the whole run: the cup is there, nothing shows it | UNVERIFIED |
| `occluded_short` | the rack is hidden for 2.5 s, within the verifier's budget | SUCCEEDED |
| `s1_stall_short`, `s1_stall_long` | S1 stops for 1 s, or for good, while carrying the cup; S0 holds it | SUCCEEDED; FAILED/S1_STALL |
| `fallback_installed` | S1 stalls; S0 runs S2's fallback (sent with the goal, then refined), then the capability's default, never HOLD; S1 resumes | SUCCEEDED |
| `unsupported_clean` | `clean(cup_1)`: no capability, and the resolver's reasons | REJECTED/NO_CAPABILITY |
| `s2_latency_0p1`, `_1`, `_5` | both cups, S2 taking 0.1 s, 1 s or 5 s per decision | same outcomes; longer task, more idle time |
| `s2_placement` | both cups, for the S2 placement experiment below (S2 takes 1 s in one process) | SUCCEEDED twice |
| `timing_observation_age`, `timing_pause_grasp` | observations 20 ms old; the whole robot stopped for 0.3 s during a descent: regressions for two S1 bugs CI met in v0.0.2 part 2 | SUCCEEDED |
| `timing_s0_stall`, `timing_jitter` | S0 stalled for 0.3 s while carrying; S0 skipping 5 % of its ticks, jittery S1 latency and observation age | SUCCEEDED, steps within S0's bound |
| `cup_moved` | a human moves the cup 6 cm during the approach; S1 plans the grasp again, without S2 | SUCCEEDED |
| `slot_blocked`, `fallback_expiry` | a dish appears in the rack spot; S1 gives up, S0 runs S2's fallback, S2 chooses another spot; with a slow S2, S2's fallback runs out first and the capability's default holds the cup | FAILED/IMPLEMENTATION_ERROR, then SUCCEEDED |
| `ghost_verify` | a second detector sees the cup 30 cm off while the verifier looks: VIOLATED although the cup is in the rack, attributed to PERCEPTION | FAILED/VERIFICATION_FAILED, then SUCCEEDED |
| `gripper_stuck` | the gripper is stuck open; S1 plans the grasp again until the deadline; attributed to EXECUTION | FAILED/TIMEOUT |
| `guard_controller_fault` | S0's controller emits NaN for 0.3 s; the guard replaces every command and the trace says so | SUCCEEDED |

S2 can also run in its own process, on the robot or on another machine,
connected to the runtime over TCP; both processes then run on the wall clock,
soft real-time (`omarchy-runtime s2`, `run ... --clock wall --s2 HOST:PORT`).
The OS secures that link: `omarchy.robot.s2Link` carries it over WireGuard, and
the runtime then reaches S2 through the tunnel only (manual, Security).

### S2 placement experiment

> **Emulated network, VMs, soft real-time.** Two QEMU VMs, the network
> between them shaped with `tc netem`, Python on a general-purpose OS. Not a
> real network, nothing real-time, no hardware.

Where should S2 live: on the robot, on an edge server, or in the cloud? In
each cell the robot VM runs one scenario with S2 in its own process, on the
robot VM (`local`) or on a second VM (`edge`), over one network profile,
with one S2 time to first token (TTFT). The scenarios: the two-cup task
(`s2_placement`), and the three scenes that change (`cup_moved`, where S1
reacts alone; `slot_blocked` and `fallback_expiry`, where S0 waits under a
fallback until S2 chooses again). The S2 process's TTFT replaces the
scenario's S2 latency. A TTFT of 0.1 s, 1 s and 5 s stands in for a model
on the robot, on an edge server and in the cloud. Delays are round trips, applied half on each VM; loss applies to
every packet in each direction.

| Profile | Link |
|---|---|
| `local` | S2 on the robot VM, loopback |
| `lan` | about 1 ms |
| `wifi` | about 20 ms, jitter 10 ms, 1 % loss |
| `wan` | about 100 ms, jitter 20 ms |
| `degraded` | about 400 ms, jitter 100 ms, 5 % loss |
| `outage` | `lan`, then every packet dropped for 5 s during the first execution |
| `outage_long` | `lan`, then every packet dropped for 20 s during the first execution: the robot connects again |

```sh
nix build .#checks.x86_64-linux.vm-s2-placement   # in nix flake check: 14 cells (below)
nix build .#s2-placement                          # every scenario x profile x TTFT 0.1, 1, 5 s; hours without KVM
nix run .#omarchy-runtime -- placement-docs result/placement/results.json README.md manual/runtime.md \
  --source "local run <start>, <commit>, <host>"   # writes the table below
```

In `nix flake check`, the two-cup task runs on lan, degraded, outage and
outage_long with a TTFT of 0.1 s and 1 s, and each changing scene on lan and
outage_long with 1 s. The last command writes the tables below, here and in
the manual, from the results of a full run. The experiments run locally
(AGENTS.md, "Checks and releases"); on the development host, without KVM,
their numbers are labelled "measured in VMs on the host, without KVM (QEMU
TCG)". The tables below come from earlier runs on GitHub Actions with KVM,
as their captions say. Each cell must
keep the safety invariants and stay within the stated tolerance of the
same task in one process on simulated time; every recovery must run under a
fallback, and every execution must hold a valid fallback for at least 99 %
of its running time.

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

Before and after the fallback design of v0.0.2 part 5, written by
`omarchy-runtime placement-compare` from the full matrix of v0.0.2 part 4 and the
one of this release (results and traces):

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

What these tables say so far (2026-10-07, emulated network, VMs, soft
real-time; 6 runs per cell, the median with its interval). Safety held in
all 840 runs: no limit violation in ground truth, no S0 starvation, S0
never without a chunk, a fallback or HOLD. Every execution held a valid
fallback for all of its running time, and a fallback engaged in all 420
recoveries, S2's first.

The measured model changes the picture. Qwen2.5-1.5B-Instruct on the
development host's CPU answers in 3.80 s (median TTFT, llama.cpp built for
AVX2) or 8.50 s (as nixpkgs packages it). With it, recovering from a taken
rack spot takes 3.85 to 3.97 s (AVX2) or 8.55 to 8.70 s (as packaged) on
every link up to wan, each interval at most 0.1 s wide; 4.5 to 4.6 s or 9.4
to 9.6 s on the degraded link, where the intervals widen to as much as 1.75
s; the 5 s and 20 s outages add themselves. The two-cup task takes 25.5 s or
36.0 s. Both TTFTs are longer than S2's fallbacks last (3 s, 1.5 s): in
every one of those recoveries S2's fallback ran out and the capability's
default held the cup, so the robot waited safely, never in HOLD, but for as
long as the model took. S1's local reaction to a moved cup stayed at 0.10 s
[0.09, 0.10] in every cell. So far: a model of this size on a desktop CPU is
too slow for S2 to answer within its fallback; where S2 lives matters less
than what it runs on, and the network adds little up to wan.

### Measured S2 latency

The 0.1 s, 1 s and 5 s above stand in for a model's time to first token.
`omarchy-runtime ttft-measure` measures a real one: a representative S2
prompt (choose the rack spot for the cup from S2's belief, answer in JSON),
50 times, streaming, to an OpenAI-compatible endpoint. On the development
host, a small instruct model served by llama.cpp from nixpkgs (as packaged,
and built for AVX2) gave the distributions below, committed with their
provenance in `pkgs/omarchy-runtime/omarchy_runtime/data/latency/` (the
format is in AGENTS.md). No cloud backend was measured: no API key was
provided (`OMARCHY_S2_API_KEY`).

<!-- s2-ttft:begin -->

S2's time to first token, measured with `omarchy-runtime ttft-measure` (from sending a request to the first token of the answer; the prompt and the method are in each file's provenance, with the model file's SHA-256). Measured on the development host, never on a robot.

| distribution | label | backend | model | TTFT median s | p05 s | p95 s | n | failures | network path | date |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen2.5-1.5b-q4km-host | measured on host | llama.cpp llama-server 9190 (b64739e) as packaged by nixpkgs (flake.lock; GGML_NATIVE off, no AVX2), CPU only, 8 threads, 1 slot, context 4096 | Qwen2.5-1.5B-Instruct Q4_K_M | 8.505 | 8.354 | 8.889 | 50 | 0 | loopback on the measuring host | 2026-10-07 |
| qwen2.5-1.5b-q4km-host-avx2 | measured on host | llama.cpp llama-server 9190 (b64739e) from nixpkgs (flake.lock), built with AVX2, FMA, F16C, BMI2 (flake output llama-cpp-avx2), CPU only, 8 threads, 1 slot, context 4096 | Qwen2.5-1.5B-Instruct Q4_K_M | 3.804 | 3.539 | 3.945 | 50 | 0 | loopback on the measuring host | 2026-10-07 |

<!-- s2-ttft:end -->

The manual's [Runtime chapter](https://promaaa.github.io/omarchy-robots/runtime/)
has the concepts, the trace format and the extension points for
collaborators (observation filter, verifier, recovery policy, S2 latency,
chunk corrector, inference trigger).

## Support matrix

Status levels, lowest to highest: **evaluates** (the system, its GPU variant,
flash script and installer image evaluate, with the per-board assertions of
`nix flake check`), **builds** (built on the host named below), **VM-tested**,
**hardware-tested**. No target is hardware-tested yet.

| Target (`board`) | Status | System closure | Built | Last verified |
|---|---|---|---|---|
| `vm` | VM-tested: boot, SSH, firewall, identity, validate; update and rollback across a reboot; installation onto a blank disk; the runbook rehearsed (installer, `nixos-rebuild --target-host`, rollback, cold boots); the runtime's reduced scenario set inside the VM, traces carrying its identity; the runtime on the wall clock with S2 in its own process, on the VM or on a second VM over an emulated network (`vm-s2-placement`) | 1.1 GiB; 1.33 GiB with `runtime.enable` (+230 MiB: Python 3, numpy and its BLAS) | system, VM, installer image (1.5 GiB) | 2026-10-06 |
| `pc` | builds: system, installer image; install path VM-tested: the installer image onto an emulated NVMe drive, boot, validate. Not hardware-tested | 1.9 GiB (all redistributable firmware) | system, installer image (1.5 GiB) | 2026-10-04 |
| `orin-agx-devkit` | builds: system, installer image, flash script | 1.21 GiB | system, installer image (1.41 GiB), flash script (1.74 GiB closure), on the development host under aarch64 emulation; the system also natively, on a GitHub arm64 runner (2026-10-02) | 2026-10-08 |
| `orin-nx-devkit` | builds: system, installer image, flash script | 1.21 GiB | system, installer image (1.41 GiB), flash script (1.73 GiB closure), on the development host under aarch64 emulation | 2026-10-08 |
| `orin-nx-super-devkit` | builds: system, installer image, flash script | 1.21 GiB | system, installer image (1.41 GiB), flash script (1.73 GiB closure), on the development host under aarch64 emulation | 2026-10-08 |
| `orin-nano-devkit` | builds: system, installer image, flash script | 1.21 GiB | system, installer image (1.41 GiB), flash script (1.74 GiB closure), on the development host under aarch64 emulation | 2026-10-08 |
| `orin-nano-super-devkit` | builds: system, installer image, flash script | 1.21 GiB | system, installer image (1.41 GiB), flash script (1.74 GiB closure), on the development host under aarch64 emulation | 2026-10-08 |
| `thor-agx-devkit` | builds: system, installer image, flash script | 1.32 GiB | system, installer image (1.56 GiB), flash script (1.15 GiB closure; JetPack 7), on the development host under aarch64 emulation | 2026-10-08 |

Pinned: nixpkgs nixos-26.05 (`4feb8eb`), jetpack-nixos `825dfea` (JetPack
6.2.3 / L4T 36.5.2 on Orin, JetPack 7.2.1 / L4T 39.2.1 on Thor), disko
`725ea35`.

Measured on the development host (x86_64, Intel i7-9700, 8 threads, 62 GiB
RAM, no KVM, aarch64 through QEMU user emulation), with other builds running:
evaluating one target takes 8 s (vm) to 10 s (Jetson); the `orin-agx-devkit`
flash script built in 50 min from an empty store (cross-compiled kernel,
EDK2, OP-TEE); the vm installer image in under a minute once the system was
built; the VM boots in 3 min 27 s under QEMU TCG (`systemd-analyze`, VM);
the whole `nix flake check` passed here in 67 min (v0.0.1 part 2 tree), most
of it the three VM tests under TCG; the pc installation test alone takes
13 min 24 s here under TCG (2026-10-04).
In CI (GitHub `ubuntu-24.04`, 2 CPUs, 7.8 GiB RAM, KVM) the VM boots in
17.2 s (`systemd-analyze`, VM) and `nix flake check` takes 25 min from an empty
store. Rehearsing the runbook on the vm board under TCG: the installer answers
SSH 154 s after power-on, `omarchy-install` takes 22 min, and
`nixos-rebuild switch --target-host` deploys a time zone change in 56 s.
The `orin-agx-devkit` system built natively on a GitHub `ubuntu-24.04-arm`
runner in about an hour from an empty store, Tegra kernel included (the
1.4 GiB closure predates the rc.2 closure reductions); once the Orin kernel
and firmware were built, each further Orin flash script took 70 to 82 s.
At v0.0.2 (2026-10-08) every Jetson target (system, installer image and
flash script of the six boards, 18 outputs) built on the development host in
7 h 33 min, the Thor kernel compiled in about 4 h under aarch64 emulation;
the Orin kernel came from an earlier build on this host.
With the Omarchy Robots boot screen, the `orin-agx-devkit` flash script rebuilt
in under 8 min (EDK2 again); its firmware shows the Omarchy Robots wordmark.
Boot time, rc.1 against rc.2 (vm board, QEMU TCG, other builds paused, five
boots each from a fresh disk): `systemd-analyze` totals average 66.6 s and
65.9 s, the same within the ±3 s spread; userborn's 1.3 s replaces the Perl
user script of the initrd, and the 1 s boot menu saves 4 s before the kernel.
The runtime (2026-10-04, development host, wall-clock): `omarchy-runtime eval`
runs the twelve scenarios (147 s of simulated time) in 5.4 s; its check
(ruff, mypy, 97 tests) builds in 29 s; per execution it spends about 40 us
resolving, 40 us verifying and 6 ms writing its trace. Inside the vm board
under TCG the same two-scenario evaluation takes 29 s, and the `vm-runtime`
test 3 min 43 s; the `pc` installation test takes 13 min 24 s. The runtime
package builds for aarch64-linux in 44 s under emulation (413 MiB closure),
and that build runs `nominal_a` correctly under QEMU user emulation: on this
x86_64 host, not on a Jetson.
On the wall clock (soft real-time, Python, 2026-10-06), S0 at 1 kHz on the
development host (no VM) overran in 40 of 18 760 steps, interval p99
1.57 ms; in a KVM VM on a GitHub runner (2 CPUs, two VMs of 2 vCPUs, the
full S2 placement matrix) it overran in 6 to 20 % of its steps, interval
p99 2.9 to 7.1 ms, max 37 ms, so the wall clock runs S0 at 200 Hz: in four
full runs there it overran at most twice per cell (of 3 700 to 6 200 steps),
interval p99 5.3 to 6.3 ms, max 13.8 ms. Under QEMU without KVM on the development host,
S0 at 1 kHz overran in a quarter to a half of its steps, and
`vm-s2-placement` (3 profiles x 2 TTFTs) took 6 min; in CI with KVM the
full matrix (18 cells) builds in 10 min. Heartbeat round trips (S2's clock, the network both ways plus
up to one loop period at each end): 1 to 2 ms on loopback in a VM, 3 ms over
the lan profile.

## Bring-up runbook

> **Prepared, not yet run on hardware.** Every command below has been built or
> rehearsed on the vm board or an x86_64 host only. Board facts (buttons, ports,
> USB IDs, part numbers) are taken from NVIDIA's developer kit user guides and
> Jetson Linux r36.5/r39.2 documentation and from jetpack-nixos; none has been
> checked against a physical board yet.

### What to arrange beforehand

- **Host:** an x86_64 Linux machine (NVIDIA's flashing tools are x86_64-only)
  with Nix (flakes enabled), aarch64 emulation for Nix (`extra-platforms =
  aarch64-linux` plus a QEMU binfmt handler), sudo, about 100 GB free, and a
  clone of https://github.com/promaaa/omarchy-robots. Building the Jetson
  system the first time takes hours on such a host (see the support matrix):
  build the day before.
- **Per board:** its power supply; the USB cable for flashing (table below); an
  M.2 Key M NVMe SSD (except Thor, which ships with one); a USB stick of 8 GB or
  more for the installer; an Ethernet cable to a network with DHCP that the host
  can reach; for the Orin NX/Nano carrier, a jumper for the recovery pins.
  Optional but worth having: the serial console (table below), or a DisplayPort
  monitor and a USB keyboard for the UEFI menu.
- **Your SSH public key** in `robot.nix` (`admin.sshKeys`): the robot and the
  installer accept nothing else.

### Identify the board

| | AGX Orin | Orin NX on the Orin Nano carrier | Orin Nano, Orin Nano Super | AGX Thor |
|---|---|---|---|---|
| `board =` | `orin-agx-devkit` | `orin-nx-devkit` | `orin-nano-super-devkit` (or `orin-nano-devkit`) | `thor-agx-devkit` |
| Module on carrier | P3701 on P3737 | P3767-0000 (16 GB) or -0001 (8 GB) on P3768 | P3767-0005 (devkit), -0003 (8 GB), -0004 (4 GB) on P3768 | P3834-0008 (T5000) on P4071 |
| `lsusb` in recovery | `0955:7023` (P3701-0000/0005/0008), `0955:7223` (-0004) | `0955:7323` (16 GB), `0955:7423` (8 GB) | `0955:7523` (8 GB), `0955:7623` (4 GB) | `0955:7026` (T5000) |
| Enter recovery | Powered off: hold Force Recovery (middle button), press Power (left), release both. Powered on: hold Force Recovery and Reset, release both. | Jumper button header J14 pin 9 (GND) to pin 10 (FC REC), then apply power; remove the jumper once `lsusb` shows the board. | as Orin NX | Hold Force Recovery (middle), briefly press Reset (right), release. From off: hold Force Recovery, press Power. |
| Flashing port | USB-C J40 next to the 40-pin header (cable in the box) | USB-C J5; the cable is not in the box | as Orin NX | USB-C next to HDMI (cable in the box); power goes into the other USB-C |
| Serial console | micro-USB J26: `/dev/ttyACM*` on the host, 115200 baud | 3.3 V USB-TTL adapter on J14: pin 3 Jetson RXD, pin 4 Jetson TXD, pin 7 GND (swap 3/4 if silent), 115200 baud | as Orin NX | debug USB-C behind the lid: UEFI on the second port, `/dev/ttyACM1`, 115200 baud |
| NVMe | M.2 Key M J1 | M.2 Key M J11 (2280) or J24 (2230), underside | as Orin NX | J103, 1 TB fitted |
| Flash script | `initrd-flash-orin-agx-devkit` | `initrd-flash-orin-nx-devkit` | `initrd-flash-orin-nano-devkit` | `initrd-flash-thor-agx-devkit` |

Notes from NVIDIA's documentation: an Orin Nano Super kit is the same hardware
as an Orin Nano kit; "super" is a firmware and power configuration (JetPack
6.1+), so either `board` value fits either kit, and the `-super` one enables
the higher power modes. NVIDIA's carrier specification says the Orin NX 40 W
MAXN SUPER mode is not supported on the Orin Nano developer kit carrier, so use
`orin-nx-devkit` there (`orin-nx-super-devkit` exists because jetpack-nixos
offers it). Some Orin Nano kits shipped with JetPack 5 firmware that cannot boot
JetPack 6: flashing from the host (step 2) replaces it. If the USB ID belongs to
none of these boards, stop: there is no mapping for it here.

### Bring-up, step by step

Set `board` in `robot.nix` (and your key), then on the host, in the clone:

```sh
# 1. Build the firmware flash script and the installer (which carries the system).
nix build .#flash -o flash -L
nix build .#packages.aarch64-linux.installer -o installer -L

# 2. Flash the QSPI firmware: board in recovery mode, flashing port to the host.
lsusb | grep -i 0955:                        # the ID from the table
sudo ./flash/bin/initrd-flash-orin-agx-devkit  # the script for your board (table)
# It boots a small Linux on the board over USB, writes the firmware and reboots
# the board; it ends with "Flashing platform firmware successful".

# 3. Write the installer to the USB stick. Check the device name twice.
lsblk
sudo dd if=$(echo installer/iso/*.iso) of=/dev/sdX bs=4M conv=fsync status=progress
```

4. **Boot the installer.** NVMe fitted, Ethernet plugged in, USB stick in, power
   on. On a display, the Omarchy Robots wordmark at power-on means the firmware
   from step 2 is running; NVIDIA's logo means the board still has its old
   firmware. The firmware tries USB first on the AGX kits; otherwise press Esc at the
   NVIDIA logo (DisplayPort monitor, or the serial console), open the Boot
   Manager and choose the USB stick.

```sh
# 5. Install, from the host. The installer announces itself over mDNS; otherwise
#    find its address in the router's DHCP leases or with `nmap -sn <subnet>`.
ssh root@robot-installer.local       # "<name>-installer"
omarchy-install                      # shows the disk, asks "yes", erases it, installs
poweroff                             # then remove the USB stick and power on

# 6. First boot: the robot asks DHCP for an address under its hostname ("robot").
ssh admin@<address>
omarchy-robot info

# 7. Validate, and keep the report.
ssh admin@<address> sudo omarchy-robot validate --json > validate-<board>.json
ssh admin@<address> sudo omarchy-robot validate
```

For the GPU checks set `gpu.enable = true` before step 1 (CUDA and TensorRT
samples are then built and carried by the installer); otherwise `gpu.device`,
`cuda.runtime` and `tensorrt.runtime` report SKIPPED.

```sh
# 8. Update and roll back on hardware; record each result by hand.
#    Make a trivial change in robot.nix, for example timezone = "Asia/Seoul";
nix run nixpkgs#nixos-rebuild -- switch --flake .#robot --target-host admin@<address> --sudo
ssh admin@<address> 'omarchy-robot info; readlink /etc/localtime'   # new generation, Asia/Seoul
ssh admin@<address> sudo reboot
ssh admin@<address> 'omarchy-robot info; readlink /etc/localtime'   # still the new one after reboot
ssh admin@<address> sudo omarchy-robot rollback
ssh admin@<address> sudo reboot
ssh admin@<address> 'omarchy-robot info; readlink /etc/localtime'   # previous generation, UTC
```

`--target-host` builds on the host (reusing what step 1 built) and copies the
closure over SSH; nothing is compiled on the robot.

### What to bring back to the next session

- `validate-<board>.json`, and the output of `omarchy-robot info --json`;
- `nix path-info -Sh .#nixosConfigurations.robot.config.system.build.toplevel`
  (closure size) and `ssh admin@<address> systemd-analyze` (boot time, "hardware");
- the update/rollback results of step 8;
- on any failure: the flash script's output, the serial console log,
  `dmesg`, and `journalctl -b -p warning`.

### Expected failure points

| Symptom | First command |
|---|---|
| Firmware and L4T disagree: `jetson.l4t` fails, NVIDIA's logo instead of ours at power-on, or the board stops in UEFI or early in the kernel | `ota-check-firmware -b` (or `cat /sys/class/dmi/id/bios_version`); flash again with step 2 |
| GPU device node missing: `gpu.device` fails | `lsmod \| grep nvgpu; ls -l /dev/nvgpu/igpu0; dmesg \| grep -i nvgpu` |
| NVMe not seen by UEFI or by the installer | in the installer: `lsblk; dmesg \| grep -i nvme; lspci`; reseat the SSD in the Key M slot |
| The installer does not appear on the network | serial console: `ip -br addr; networkctl` |
| Building on the robot is slow (Orin Nano) | build on the host as above; to see what a deploy would build: `nix build --dry-run .#nixosConfigurations.robot.config.system.build.toplevel` |

## Development

```sh
nix flake check                 # format, statix, deadnix, ruff, mypy, unit tests, the runtime's
                                # scenario matrix, every board evaluated (with and without the
                                # runtime), VM tests (base, update/rollback, installer on vm and pc,
                                # runtime, S2 placement reduced to 4 profiles x 2 TTFTs, S2 link over
                                # WireGuard)
nix fmt                         # nixfmt (RFC style) on every .nix file
nix develop                     # nixfmt, statix, deadnix, ruff, python3 with numpy and mypy
```

Artifacts, all built from `robot.nix` (`<board>` is any Jetson board above):

| Command | Artifact |
|---|---|
| `nix build .#nixosConfigurations.robot.config.system.build.toplevel` | the robot's system, as `robot.nix` declares it |
| `nix build .#nixosConfigurations.<board>.config.system.build.toplevel` | the same system on `<board>` |
| `nix build .#flash` / `.#flash-<board>` | jetpack-nixos's firmware flash script (x86_64 host, USB recovery mode) |
| `nix build .#packages.aarch64-linux.installer` / `.#packages.aarch64-linux.installer-<board>` | installation image for a Jetson; it carries the system it installs |
| `nix build .#installer-vm` / `.#installer-pc` | installation image for the vm or pc board (x86_64) |
| `nix build .#vm` | the vm board as a QEMU VM (`nix run .#vm`) |
| `nix build .#omarchy-robot` | the command alone |
| `nix build .#omarchy-runtime` / `.#packages.aarch64-linux.omarchy-runtime` | the runtime alone (`nix run .#omarchy-runtime -- demo`) |
| `nix build .#s2-placement` | the S2 placement experiment, full matrix: results, table and traces (two VMs; wants KVM) |

`.#flash` and `.#installer` follow the board set in `robot.nix`; with
`board = "vm"` or `"pc"` only `.#installer` exists (x86_64). Building a Jetson system on
an x86_64 host needs `extra-platforms = aarch64-linux` and a QEMU binfmt
handler (on NixOS: `boot.binfmt.emulatedSystems = [ "aarch64-linux" ]`); there
is no public binary cache for jetpack-nixos, so the Tegra kernel compiles under
emulation the first time. NixOS VM tests need the `nixos-test` and `kvm`
system features in `nix.conf`.

Layout: `flake.nix` (inputs pinned in `flake.lock`), `robot.nix`, `modules/`
(options, base, identity, jetson, vm, pc, runtime, installer, boards),
`pkgs/omarchy-robot/` (the command, its unit tests and fixtures),
`pkgs/omarchy-runtime/` (the runtime, its scenarios and tests), `tests/`
(checks and VM tests). [`AGENTS.md`](AGENTS.md) says which directory belongs
to which component and how to extend the runtime.

## Versioning

Before 1.0, releases count up: v0.0.1 part 3 to v0.0.1 were milestone OS-1 (the base system,
VM-validated), v0.0.2 part 1 milestone R1 (the runtime, simulated, and the pc
board), v0.0.2 part 2 is milestone R2 (S2 in its own process, the wall clock, the S2
placement experiment), v0.0.2 part 3 is milestone R3's hardening (bounded S0
steps, timing faults, reconnection, the S2 link over WireGuard), v0.0.2 part 4
milestone R3's experiment (scenes that change, recovery under the fallback,
the S2 placement matrix on them, the asynchronous inference seam), v0.0.2 part 5
milestone R4's first part (fallbacks before they are needed, fallback
coverage), v0.0.2 its second (measured S2 latency, repeated runs with
intervals), v0.0.3 part 1 milestone R6's first part (intent contract v2, the S0
guard and controller, fidelity and attribution). The first board booted and
validated on hardware takes v0.1.0. The validation report schema
(`schema` in its JSON) and the runtime's trace schema (`trace_schema` in
`run.json`) are versioned separately from releases; their changes are noted
in [`CHANGELOG.md`](CHANGELOG.md). Releases are source only: no images, no
NVIDIA binaries.

## License

MIT, see [`LICENSE`](LICENSE). jetpack-nixos is MIT; the NVIDIA components it
packages are under NVIDIA's licenses and are built locally, never
redistributed here.
