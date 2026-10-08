---
layout: default
title: Boards
permalink: /boards/
---

The `board` option in [`robot.nix`](../robot-nix/) says what the robot runs on. Every Jetson value maps onto a developer kit that jetpack-nixos supports; the mapping is in [`modules/boards.nix`](https://github.com/promaaa/omarchy-robots/blob/main/modules/boards.nix). `vm` and `pc` are x86_64.

| `board` | Hardware | JetPack / L4T |
|---|---|---|
| `vm` | QEMU virtual machine on an x86_64 host | none |
| `pc` | A generic x86_64 computer that boots with UEFI | none |
| `orin-agx-devkit` | Jetson AGX Orin developer kit | 6.2.3 / 36.5.2 |
| `orin-nx-devkit` | Jetson Orin NX module on the Orin Nano developer kit carrier | 6.2.3 / 36.5.2 |
| `orin-nx-super-devkit` | The same, with the "super" firmware and power modes | 6.2.3 / 36.5.2 |
| `orin-nano-devkit` | Jetson Orin Nano developer kit | 6.2.3 / 36.5.2 |
| `orin-nano-super-devkit` | Jetson Orin Nano Super developer kit | 6.2.3 / 36.5.2 |
| `thor-agx-devkit` | Jetson AGX Thor developer kit | 7.2.1 / 39.2.1 |

An Orin Nano Super kit is the same hardware as an Orin Nano kit. "Super" is a firmware and power configuration, so either value fits either kit; the `-super` one enables the higher power modes. On the Orin Nano carrier, use `orin-nx-devkit` for an Orin NX module: NVIDIA does not support its 40 W super mode there.

## Support levels

Each target climbs four levels: **evaluates** (the system, its flash script and its installer image evaluate and pass the per-board assertions), **builds**, **VM-tested**, and **hardware-tested**.

| Target | Level, as of v0.0.2 (2026-10-08) |
|---|---|
| `vm` | VM-tested: boot, SSH, firewall, identity, `validate`, update and rollback across a reboot, installation onto a blank disk |
| `pc` | Builds; install path VM-tested (installer image onto an emulated NVMe drive, boot, `validate`). Not hardware-tested |
| `orin-agx-devkit`, `orin-nx-devkit`, `orin-nx-super-devkit`, `orin-nano-devkit`, `orin-nano-super-devkit`, `thor-agx-devkit` | Builds: system, installer image and flash script |

**No target is hardware-tested yet.** The README's [support matrix](https://github.com/promaaa/omarchy-robots#support-matrix) carries the dated measurements behind each row: closure sizes, build times and which host built what.

## The `pc` board

`board = "pc"` is any x86_64 computer that boots with UEFI: a spare desktop, a mini PC, an old laptop. It uses NixOS's own generic hardware support: SATA, NVMe, USB and virtio storage drivers in the initrd, the redistributable firmware that common network adapters need, and Intel and AMD CPU microcode. There is no GPU stack and no power mode on it for now, so `gpu.enable` and `power.mode` are refused, and like a Jetson it refuses to build without an SSH key. The disk defaults to `/dev/nvme0n1`.

## Install on a spare PC

This is the first path to a real hardware result, and nobody has taken it yet. **The installer erases the whole disk** named by `storage.device`.

1. In `robot.nix`, set `board = "pc"` and put your SSH public key in `admin.sshKeys`. If the PC's disk is not NVMe, set `storage.device` too, for example `"/dev/sda"`.
2. Build the installer: `nix build .#installer -o installer` (x86_64, no emulation needed).
3. Write it to a USB stick of 8 GB or more. Check the device name twice: `lsblk`, then `sudo dd if=$(echo installer/iso/*.iso) of=/dev/sdX bs=4M conv=fsync status=progress`.
4. Plug the PC into Ethernet with DHCP. In its firmware setup, turn Secure Boot off (the boot loader is not signed) and boot the USB stick in UEFI mode.
5. From your computer: `ssh root@robot-installer.local`, then `omarchy-install`. It shows the disk and asks for `yes` before it erases anything. If the disk is not the one you expect, answer no, fix `storage.device` and go back to step 2.
6. `poweroff`, remove the stick, power on, then `ssh admin@<address>` and `sudo omarchy-robot validate`.

Bring back `sudo omarchy-robot validate --json`, `omarchy-robot info --json`, `systemd-analyze`, `lspci -nn` and the PC's make and model. With them, `pc` can be marked hardware-tested on that machine.

## Pinned inputs

Every build uses exactly these, locked in `flake.lock`:

- nixpkgs `nixos-26.05`
- jetpack-nixos, for the kernel, firmware and NVIDIA userspace
- disko, for the disk layout

NVIDIA components are built locally from NVIDIA's own sources and debs. Omarchy Robots never redistributes them, and releases are source only.
