---
layout: default
title: omarchy-robot
permalink: /omarchy-robot/
---

Every robot carries one command for looking after itself. It has four verbs.

| Command | What it does |
|---|---|
| `omarchy-robot info [--json]` | What this system is: board, target, Omarchy Robots version, flake revision, `robot.nix` hash, input revisions, NixOS and kernel, JetPack and L4T, the runtime's version if it is installed, current and booted generation. |
| `sudo omarchy-robot validate [--json]` | The bring-up checklist. Exits 1 if any check fails. |
| `sudo omarchy-robot update <flake-or-system>` | Switch to a new system. |
| `sudo omarchy-robot rollback` | Switch back to the previous generation. It also becomes the boot default. |

## info

`info` reads `/etc/omarchy-robot/build.json`, which every build writes. It is the answer to "which system did this experiment run on?". Save `omarchy-robot info --json` next to your results. If the running system differs from the one that booted, `info` says a reboot is pending.

## validate

Run it with `sudo`: `/boot` and some Jetson tools are root only. Each check reports one of four statuses:

- `PASS` and `FAIL`, measured now;
- `SKIPPED`, with the reason (no camera plugged in, `gpu.enable` off);
- `NOT_APPLICABLE`, like a Jetson check on the `vm` board.

A check that did not run is never a `PASS`.

| Check | Measures | Applies to |
|---|---|---|
| `identity` | `build.json` is present and matches the running host; warns on a dirty revision or a pending reboot | all |
| `boot.root` | the root filesystem is on `storage.device` | all |
| `boot.entries` | systemd-boot entries exist; kept generations, current and default | all |
| `services.ssh` | sshd is active; password and keyboard-interactive login are off | all |
| `services.firewall` | the firewall is active | all |
| `storage` | root filesystem type and free space | all |
| `jetson.model` | the device tree names the board's carrier and module part numbers | Jetson |
| `jetson.l4t` | `/etc/nv_tegra_release` is the pinned L4T, and the firmware is the one this L4T expects | Jetson |
| `gpu.device` | the GPU device node exists | Jetson, `gpu.enable` |
| `cuda.runtime` | CUDA `deviceQuery` passes with the expected compute capability | Jetson, `gpu.enable` |
| `tensorrt.runtime` | TensorRT `sample_onnx_mnist` passes | Jetson, `gpu.enable` |
| `camera` | V4L2 devices are present; `SKIPPED` when none is connected | Jetson |
| `power.mode` | `nvpmodel -q` matches `power.mode` | Jetson |
| `thermal` | thermal zones are readable; temperatures recorded | Jetson |

`--json` prints a report with a `schema` version (currently 1), the build identity, a timestamp, the kernel, and every check with its data. Schema changes are listed in the [CHANGELOG](https://github.com/promaaa/omarchy-robots/blob/main/CHANGELOG.md).

## update

`update` takes either a flake reference or a system that is already built:

```sh
sudo omarchy-robot update github:me/robot          # the build's target is appended: github:me/robot#robot
sudo omarchy-robot update /nix/store/...-nixos-system-robot-...
```

A flake reference builds on the robot. On a small board, prefer building on your computer and pushing the result, as shown in [Updates and rollback](../updates/).

## rollback

```sh
sudo omarchy-robot rollback
```

Switches to the previous generation right away and makes it the boot default.
