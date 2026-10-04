---
layout: default
title: Welcome
permalink: /welcome/
---

Omarchy Robots is an operating system for the computer inside a robot. It is built on [NixOS](https://nixos.org) and [jetpack-nixos](https://github.com/anduril/jetpack-nixos), and it targets NVIDIA Jetson boards first.

The idea is simple. A robot computer should be boring: it boots, you can reach it over SSH, its GPU works, and when an update goes wrong you can go back. You should not have to spend a week setting that up for every new board, and you should be able to say exactly which system an experiment ran on.

So the whole machine is declared in one file, [`robot.nix`](../robot-nix/). Which board, who may log in, the network, the disk, the GPU stack, the power mode. Everything else is a default you don't have to think about.

## What you get

- **Headless and plain.** No desktop, no display server. SSH keys only, and a firewall that admits SSH and nothing else.
- **One file.** Change the board on bring-up day by changing one line.
- **An identity in every build.** Each system records its flake revision, the hash of `robot.nix`, its inputs, kernel and JetPack version, so a result can name the exact system that produced it.
- **Rollback.** A bounded history of system generations stays on disk and in the boot menu.
- **One command.** [`omarchy-robot`](../omarchy-robot/) shows what the system is, checks that it works, updates it and rolls it back.
- **A runtime, simulated for now.** [`omarchy-runtime`](../runtime/) takes goals, runs and verifies them, and records traces that name the system they ran on. Its robots, controller and camera are simulations.

## Where it stands

This is the beginning of the project. Version **0.0.2** (October 2026) adds the first slice of the runtime and a generic `pc` board to the base system of 0.0.1:

| Works today | Not yet |
|---|---|
| The `vm` board boots, installs onto a blank disk, updates and rolls back, all tested in virtual machines | Running on real hardware. Every Jetson target evaluates and builds, and so does `pc`; none has been booted on a real board or PC |
| The `pc` board's installer installs onto an emulated NVMe drive, in a virtual machine | |
| The runtime runs its simulated dishwashing scenarios on a laptop and inside an Omarchy Robots VM | A real robot, a real camera, a learned policy, anything real-time |

The first board booted and validated on real hardware will take version 0.1.0. Until then, treat anything about Jetson or a real PC as prepared, not proven.

## How to read this manual

Start with [Getting started](../getting-started/): it runs the robot as a virtual machine on your laptop in one command. Then read [robot.nix](../robot-nix/) to see what you can change. When you have a board on your desk, follow [Bring-up on a Jetson](../bring-up/).
