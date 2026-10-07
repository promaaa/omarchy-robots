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

**Status:** v0.0.2 part 4 (milestone R3: hardening, scenes that change, the seam
for asynchronous inference) runs and is tested in virtual machines: the base
system, the runtime's simulated scenarios (on a laptop and inside an Omarchy
Robots VM), the runtime on the wall clock with S2 in its own process on a
second VM over an emulated network (the S2 placement experiment), and the
installation of a generic PC onto an emulated disk. The Jetson and pc
targets evaluate and are partly built; none has run on hardware yet (see the
[support matrix](#support-matrix)).

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

S0 never waits: at every tick (1 kHz, simulated) it runs a valid chunk, else
a fallback, else HOLD, and it enforces the body's limits. Its steps are
bounded: never more than the velocity limit times its nominal tick period,
so a late tick never catches up with a jump. Every execution holds a
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
nix build .#s2-placement                          # every scenario x profile x TTFT 0.1, 1, 5 s; wants KVM
nix run .#omarchy-runtime -- placement-docs result/placement/results.json README.md manual/runtime.md \
  --source "CI run <id>, ..."      # writes the table below
```

In `nix flake check`, the two-cup task runs on lan, degraded, outage and
outage_long with a TTFT of 0.1 s and 1 s, and each changing scene on lan and
outage_long with 1 s. The last command writes the tables below, here and in
the manual, from the results of a full run; CI runs the full matrix on demand (workflow
dispatch, `placement`) and keeps its results as an artifact. Each cell must
keep the safety invariants and stay within the stated tolerance of the
same task in one process on simulated time; every recovery must run under a
fallback, and every execution must hold a valid fallback for at least 99 %
of its running time.

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

Before and after the fallback design of v0.0.2 part 5, written by
`omarchy-runtime placement-compare` from the full matrix of v0.0.2 part 4 and the
one of this release (results and traces):

<!-- s2-placement-compare:begin -->
<!-- s2-placement-compare:end -->

What these tables say so far (2026-10-06, emulated network, VMs, soft
real-time; one run per cell). Safety held in all 84 cells: no limit
violation in ground truth, no S0 starvation, S0 never without a chunk, a
fallback or HOLD.

The two-cup task measures latency. Its time follows S2's time to first
token: about 18.6 s, 20.6 s and 28.6 s for 0.1 s, 1 s and 5 s. local, lan,
wifi and wan (up to about 100 ms) take 0 to 0.4 s longer than the same task
in one process on simulated time, the degraded link (about 400 ms, 5 % loss)
1.0 to 1.4 s. A 5 s outage is recorded as one long outage of 5.7 to 5.8 s,
and the robot connects again 2.8 s after it closed the link. A 20 s outage
is recorded as 22.7 to 22.8 s, the robot connects again after 9 attempts,
and the task takes 17.4 to 18.2 s longer.

The scenes that change measure recovery. S1 reacts to a moved cup alone, in
0.09 to 0.10 s in every cell: where S2 lives does not matter there. When a
dish takes the rack spot, S0 waits until S2 chooses another spot, and the
recovery time is S2's time to first token plus the network: 0.15 to 0.35 s,
1.05 to 1.25 s and 5.05 to 5.30 s for 0.1 s, 1 s and 5 s up to wan, where
the simulated run takes 0.15 s, 1.05 s and 5.05 to 5.10 s. The degraded link
takes 0.5 to 1.25 s longer than the simulated run. An outage adds itself:
with a time to first token of 1 s or less, recovery takes 3.55 to 4.85 s
with the 5 s outage and 21.35 to 21.60 s with the 20 s one. S0 runs S2's
fallback meanwhile, if S2 has installed one: in 28 of the 42 recoveries. It
expired in 9 of them: once on the degraded link (the 1.5 s fallback, 2.30 s
to recover), and in every outage cell where it engaged. With a time to first
token of 5 s, S1 gives up before S2 has installed the execution's fallback,
and S0 holds without one in all 14 such cells. So far, for recovery: S2 must
answer within its fallback's duration, which a placement with a time to
first token of 1 s or less and a round trip of 100 ms or less did in every
cell here; a 5 s model leaves each execution without a fallback for its
first 5 s.

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
| `orin-agx-devkit` | builds: system, flash script | 1.4 GiB | system (natively, GitHub arm64 runner), flash script (1.7 GiB closure) | 2026-10-02 |
| `orin-nx-devkit` | builds: flash script | | flash script (1.7 GiB closure) | 2026-10-02 |
| `orin-nx-super-devkit` | builds: flash script | | flash script (1.7 GiB closure) | 2026-10-02 |
| `orin-nano-devkit` | builds: flash script | | flash script (1.7 GiB closure) | 2026-10-02 |
| `orin-nano-super-devkit` | builds: flash script | | flash script (1.7 GiB closure) | 2026-10-02 |
| `thor-agx-devkit` | builds: flash script | | flash script (1.2 GiB closure; JetPack 7, built in 71 min) | 2026-10-02 |

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
the S2 placement matrix on them, the asynchronous inference seam). The first board booted and
validated on hardware takes v0.1.0. The validation report schema
(`schema` in its JSON) and the runtime's trace schema (`trace_schema` in
`run.json`) are versioned separately from releases; their changes are noted
in [`CHANGELOG.md`](CHANGELOG.md). Releases are source only: no images, no
NVIDIA binaries.

## License

MIT, see [`LICENSE`](LICENSE). jetpack-nixos is MIT; the NVIDIA components it
packages are under NVIDIA's licenses and are built locally, never
redistributed here.
