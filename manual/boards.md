---
layout: default
title: Boards
permalink: /boards/
---

The `board` option in [`robot.nix`](../robot-nix/) says what the robot runs on. Every Jetson value maps onto a developer kit that jetpack-nixos supports; the mapping is in [`modules/boards.nix`](https://github.com/promaaa/omarchy-robots/blob/main/modules/boards.nix).

| `board` | Hardware | JetPack / L4T |
|---|---|---|
| `vm` | QEMU virtual machine on an x86_64 host | none |
| `orin-agx-devkit` | Jetson AGX Orin developer kit | 6.2.3 / 36.5.2 |
| `orin-nx-devkit` | Jetson Orin NX module on the Orin Nano developer kit carrier | 6.2.3 / 36.5.2 |
| `orin-nx-super-devkit` | The same, with the "super" firmware and power modes | 6.2.3 / 36.5.2 |
| `orin-nano-devkit` | Jetson Orin Nano developer kit | 6.2.3 / 36.5.2 |
| `orin-nano-super-devkit` | Jetson Orin Nano Super developer kit | 6.2.3 / 36.5.2 |
| `thor-agx-devkit` | Jetson AGX Thor developer kit | 7.2.1 / 39.2.1 |

An Orin Nano Super kit is the same hardware as an Orin Nano kit. "Super" is a firmware and power configuration, so either value fits either kit; the `-super` one enables the higher power modes. On the Orin Nano carrier, use `orin-nx-devkit` for an Orin NX module: NVIDIA does not support its 40 W super mode there.

## Support levels

Each target climbs four levels: **evaluates** (the system, its flash script and its installer image evaluate and pass the per-board assertions), **builds**, **VM-tested**, and **hardware-tested**.

| Target | Level, as of v0.0.1 part 3 (2026-10-03) |
|---|---|
| `vm` | VM-tested: boot, SSH, firewall, identity, `validate`, update and rollback across a reboot, installation onto a blank disk |
| `orin-agx-devkit` | Builds: system and flash script |
| `orin-nx-devkit`, `orin-nx-super-devkit`, `orin-nano-devkit`, `orin-nano-super-devkit`, `thor-agx-devkit` | Builds: flash script |

**No target is hardware-tested yet.** The README's [support matrix](https://github.com/promaaa/omarchy-robots#support-matrix) carries the dated measurements behind each row: closure sizes, build times and which host built what.

## Pinned inputs

Every build uses exactly these, locked in `flake.lock`:

- nixpkgs `nixos-26.05`
- jetpack-nixos, for the kernel, firmware and NVIDIA userspace
- disko, for the disk layout

NVIDIA components are built locally from NVIDIA's own sources and debs. Omarchy Robots never redistributes them, and releases are source only.
