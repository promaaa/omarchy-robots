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

**Status:** v0.0.2 part 2 (milestone R2) runs and is tested in virtual machines: the
base system, the runtime's simulated scenarios (on a laptop and inside an
Omarchy Robots VM), the runtime on the wall clock with S2 in its own process
on a second VM over an emulated network (the S2 placement experiment), and
the installation of a generic PC onto an emulated disk. The Jetson and pc targets evaluate and are partly built; none has run on
hardware yet (see the [support matrix](#support-matrix)).

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
the fallback S2 installed, else HOLD, and it enforces the body's limits. Its
steps are bounded: never more than the velocity limit times its nominal tick
period, so a late tick never catches up with a jump.

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
| `fallback_installed` | S1 stalls; S0 runs S2's fallback, then HOLD, then S1 resumes | SUCCEEDED |
| `unsupported_clean` | `clean(cup_1)`: no capability, and the resolver's reasons | REJECTED/NO_CAPABILITY |
| `s2_latency_0p1`, `_1`, `_5` | both cups, S2 taking 0.1 s, 1 s or 5 s per decision | same outcomes; longer task, more idle time |
| `s2_placement` | both cups, for the S2 placement experiment below (S2 takes 1 s in one process) | SUCCEEDED twice |

S2 can also run in its own process, on the robot or on another machine,
connected to the runtime over TCP; both processes then run on the wall clock,
soft real-time (`omarchy-runtime s2`, `run ... --clock wall --s2 HOST:PORT`).

### S2 placement experiment

> **Emulated network, VMs, soft real-time.** Two QEMU VMs, the network
> between them shaped with `tc netem`, Python on a general-purpose OS. Not a
> real network, nothing real-time, no hardware.

Where should S2 live: on the robot, on an edge server, or in the cloud? In
each cell the robot VM runs the two-cup task (`s2_placement`) with S2 in its
own process, on the robot VM (`local`) or on a second VM (`edge`), over one
network profile, with one S2 time to first token (TTFT). A TTFT of 0.1 s,
1 s and 5 s stands in for a model on the robot, on an edge server and in the
cloud. Delays are round trips, applied half on each VM; loss applies to
every packet in each direction.

| Profile | Link |
|---|---|
| `local` | S2 on the robot VM, loopback |
| `lan` | about 1 ms |
| `wifi` | about 20 ms, jitter 10 ms, 1 % loss |
| `wan` | about 100 ms, jitter 20 ms |
| `degraded` | about 400 ms, jitter 100 ms, 5 % loss |
| `outage` | `lan`, then every packet dropped for 5 s during the first execution |

```sh
nix build .#checks.x86_64-linux.vm-s2-placement   # lan, degraded, outage x TTFT 0.1, 1 s (in nix flake check)
nix build .#s2-placement                          # every profile x TTFT 0.1, 1, 5 s; wants KVM
nix run .#omarchy-runtime -- placement-docs result/placement/results.json README.md manual/runtime.md \
  --source "CI run <id>, ..."      # writes the table below
```

The last command writes the table below, here and in the manual, from the
results of a full run; CI runs the full matrix on demand (workflow
dispatch, `placement`) and keeps its results as an artifact. Each cell must
keep the safety invariants and stay within the stated tolerance of the
same task in one process on simulated time.

<!-- s2-placement:begin -->
S2 placement, two-cup task (emulated network (tc netem), VMs, soft real-time). Measured 2026-10-06 with omarchy-runtime 0.0.2 (88ca35d9d22a), Omarchy Robots 0.0.2 (board vm), S0 at 200 Hz; soft real-time, Python, wall-clock in an Omarchy Robots VM (0.0.2, Python 3.13.15, 2 CPUs, virtualization kvm). Task and HOLD times are wall-clock; simulated is the same task with S2 in one process on simulated time. S2 RTT: heartbeat echo measured by S2: the network both ways, plus up to one loop period at each end. Source: CI run 37412196261 (https://github.com/promaaa/omarchy-robots/actions/runs/37412196261), the full matrix alone on a GitHub ubuntu-24.04 runner, 2 CPUs, KVM; each VM 2 vCPUs.

| profile | TTFT s | outcomes | violations | S0 starvations | fallback s | HOLD s | task s | simulated s | S2 RTT p50/p99/max ms | outages | S0 interval p99/max ms | S0 overruns |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| local | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.19 | 18.45 | 18.45 | 2.0/3.4/3.6 | none | 5.44/8.08 | 0 of 3712 |
| local | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.00 | 20.40 | 20.35 | 2.0/2.1/3.2 | none | 5.36/7.68 | 0 of 4094 |
| local | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.24 | 28.65 | 28.30 | 1.0/1.5/3.1 | none | 5.31/6.58 | 0 of 5755 |
| lan | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.34 | 18.80 | 18.45 | 3.0/4.8/5.0 | none | 5.36/6.51 | 0 of 3789 |
| lan | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.09 | 20.55 | 20.35 | 3.0/4.0/7.7 | none | 5.32/7.18 | 0 of 4136 |
| lan | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.24 | 28.65 | 28.30 | 3.0/5.0/6.0 | none | 5.30/6.21 | 0 of 5752 |
| wifi | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.39 | 18.70 | 18.45 | 22.9/148.3/157.0 | none | 5.39/6.90 | 0 of 3780 |
| wifi | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.24 | 20.60 | 20.35 | 22.0/141.0/150.0 | none | 5.39/7.53 | 0 of 4155 |
| wifi | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.04 | 28.40 | 28.30 | 22.2/152.7/367.8 | none | 5.40/7.45 | 0 of 5711 |
| wan | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.29 | 18.60 | 18.45 | 101.8/120.0/120.0 | none | 5.39/7.76 | 0 of 3752 |
| wan | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.19 | 20.60 | 20.35 | 101.0/119.0/120.1 | none | 5.43/6.70 | 0 of 4154 |
| wan | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.14 | 28.50 | 28.30 | 102.9/119.3/120.2 | none | 5.32/6.58 | 0 of 5740 |
| degraded | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 1.99 | 19.30 | 18.45 | 648.0/1455.6/1554.0 | short 0.7 s, short 0.6 s, short 0.8 s, short 0.9 s, short 0.6 s, short 0.8 s, short 0.6 s, short 0.7 s | 5.30/6.76 | 0 of 4155 |
| degraded | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 3.59 | 20.95 | 20.35 | 598.0/1331.0/1503.7 | short 0.6 s, short 0.6 s, short 0.7 s, short 0.6 s, short 0.7 s, short 0.6 s, short 0.6 s | 5.32/6.36 | 0 of 4366 |
| degraded | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 12.09 | 29.45 | 28.30 | 554.0/2479.1/2777.0 | short 0.6 s, short 0.7 s, short 0.7 s, short 0.7 s, short 0.6 s, short 0.6 s, short 0.6 s, short 0.7 s, short 0.6 s, short 0.7 s | 5.34/6.29 | 0 of 6063 |
| outage | 0.1 | 2x SUCCEEDED | 0 | 0 | 0.00 | 2.39 | 19.75 | 18.45 | 3.0/6203.0/6400.9 | long 6.5 s | 5.40/6.52 | 0 of 3974 |
| outage | 1.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 4.84 | 22.25 | 20.35 | 3.1/6413.1/6630.3 | long 6.4 s | 5.34/6.64 | 0 of 4479 |
| outage | 5.0 | 2x SUCCEEDED | 0 | 0 | 0.00 | 11.24 | 28.65 | 28.30 | 4.0/6137.7/6422.0 | long 6.5 s | 5.41/7.70 | 0 of 5758 |

Tolerance: Wall-clock runs are not bit-identical. Every cell must give the simulated run's outcomes, claims, verdicts and ground truth exactly, no safety violation, and no S0 tick without a chunk, a fallback or HOLD; outages must be classified by the deadlines. Task time: local and lan within 15 % of the simulated run with the same time to first token when S0 kept its rate (overruns in under 1 % of its steps), within 30 % when it did not (a late loop delays every step, as on an emulated CPU); every profile at least 85 % of it.
<!-- s2-placement:end -->

What this table says so far (2026-10-06, emulated network, VMs, soft
real-time; one run per cell). Safety held in every cell: no limit violation
in ground truth, no S0 starvation, S0 never without a chunk, a fallback or
HOLD. The task time follows S2's time to first token, not the network:
about 18.6 s, 20.5 s and 28.5 s for 0.1 s, 1 s and 5 s, because the robot
waits in HOLD while S2 decides. Against the same task in one process on
simulated time, local, lan, wifi and wan (up to about 100 ms) take 0 to
0.35 s longer, and the degraded link (about 400 ms, 5 % loss) 0.6 to
1.15 s, although TCP retransmission there makes the supervisor record 7 to
10 short outages per run. A 5 s outage during an execution is recorded as
one long outage of 6.4 to 6.5 s (TCP's retransmission backoff adds the
rest); the execution continues, and the task takes 0.35 to 1.9 s longer.
S2 decides four times (two goal requests, two fallback installs), only the
requests make the robot wait, and S1 never stalls, so the fallback is never
used: this measures where S2 should live for latency, not for recovery.

The manual's [Runtime chapter](https://promaaa.github.io/omarchy-robots/runtime/)
has the concepts, the trace format and the extension points for
collaborators (observation filter, verifier, recovery policy, S2 latency).

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
                                # runtime, S2 placement reduced to 3 profiles x 2 TTFTs)
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
placement experiment). The first board booted and
validated on hardware takes v0.1.0. The validation report schema
(`schema` in its JSON) and the runtime's trace schema (`trace_schema` in
`run.json`) are versioned separately from releases; their changes are noted
in [`CHANGELOG.md`](CHANGELOG.md). Releases are source only: no images, no
NVIDIA binaries.

## License

MIT, see [`LICENSE`](LICENSE). jetpack-nixos is MIT; the NVIDIA components it
packages are under NVIDIA's licenses and are built locally, never
redistributed here.
