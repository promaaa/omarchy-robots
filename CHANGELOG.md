# Changelog

Every release of Omarchy Robots, newest first. The same text is on each
[GitHub release](https://github.com/promaaa/omarchy-robots/releases).

## Unreleased

## v0.0.3

This release lets the robot ask its arm for more than a path. The motion policy (S1) can now tell the arm's controller (S0) to press at about 10 N, to keep contact, or to move an object, and a guard checks every command before it reaches the motors. It adds a simulated Franka Panda that scrubs a plate, and the first drivers for a real Franka Panda and an AgileX PiPER. Everything runs in simulation, in virtual machines, or against fake arms; no real arm has run it yet.

### Highlights

#### Ask the arm for force, contact and object motion

A command can now carry motion, an object's target pose, and contact: a target force, its bounds and a stiffness. Each controller declares what it supports. A request it cannot honour is refused, or reduced only when the configuration allows it.

#### Put a guard in front of every controller

The guard checks every command against the arm's limits on position, speed, workspace, torque and force. It replaces a command that is broken or out of date with a safe one, and it counts every time it steps in.

#### Know why a task failed

Each failed task is put in one of four groups: the infrastructure (a stall, a lost link), perception (the robot saw it wrong), execution (the arm did not do what it was asked), or the request itself (the arm did it, and the goal was still missed).

#### Scrub a plate on a simulated Franka Panda

A simulated Panda in MuJoCo wipes a plate that it believes is up to 2 cm higher or lower than it is. When the plate is lower than it believes, moving alone never cleans it. Pressing at 10 N cleans it in every run.

#### Drive the lab's Franka Panda and AgileX PiPER

The Panda driver uses libfranka 0.9.2, with torque control at 1 kHz and contact up to 15 N. The PiPER driver uses AgileX's SDK, with joint motion at 200 Hz. Both are tested against fake arms. Nothing is sent to a motor unless the configuration names an operator authorization.

#### Run the planner on a lab GPU server

The planner's model answers in 0.044 s on one GPU of a lab server, and in 0.087 s from a laptop through an SSH tunnel, against 3.8 s on the laptop's CPU. On the lab's real network, all 12 test runs stayed within their time limit.

### Improvements

- Add `omarchy-runtime-tools`, with MuJoCo and video rendering, kept out of the robot's system
- Add `omarchy-runtime scrub-render` to turn a recorded run into a video
- Add the manual chapter "In the lab": the lab's arms, where the Panda's 1 kHz loop can run, and a first read-only test
- Add `ttft-measure --network-path` to name the path to the model, such as an SSH tunnel
- Build libfranka and the PiPER SDK with Nix
- Run every check and release locally, so GitHub Actions only runs when started by hand

### Fixes

- Fix the Panda driver reading the contact force with the wrong sign
- Fix the Panda driver taking the tool's frame for the flange
- Fix a second control run tripping a Panda safety reflex
- Fix the PiPER driver acting on feedback that had stopped
- Fix contact or object requests that were never measured counting as done
- Fix two tables that did not appear in the manual

## v0.0.2

This release adds the robot runtime. An agent asks for a goal, such as "put the cup in the rack", and the runtime carries it out, checks the result on fresh observations, and records a trace of what happened. The planner can now run on another machine. Everything runs in simulation and in virtual machines; no real robot has run it yet.

### Highlights

#### Carry out a goal and check the result

The runtime takes a goal in JSON, chooses how to do it, and checks the result from what the robot sees, not from what the arm claims. A cup that slips out of the gripper is caught and tried again. Every run leaves a trace that names the exact system it ran on.

#### Run the planner on another machine

The planner (S2) can run in its own process, on the robot or on another machine, over TCP. Declare a WireGuard tunnel for it in `robot.nix` with `omarchy.robot.s2Link`. After a network outage the robot reconnects by itself, without losing or repeating a goal.

#### Keep the robot safe while the planner thinks

Every task starts with a fallback already in place. When a step fails, the arm holds still or lifts 2 cm until the planner answers, instead of waiting with nothing to do.

#### Measure where the planner should live

The placement experiment runs the same tasks with the planner on a second virtual machine, over an emulated LAN, Wi-Fi, WAN, a degraded link and outages. `omarchy-runtime ttft-measure` measures how long a real model takes to answer: 3.8 s for Qwen2.5 1.5B on a laptop CPU.

#### Install on a spare PC

The new `pc` board installs Omarchy Robots on any x86_64 UEFI computer.

### Improvements

- Add scenes that change during a task: a cup that moves, a dish that blocks the free spot
- Add timing faults that can be replayed exactly: late observations, stalls, pauses
- Add a real-time mode, next to the default simulated time
- Limit how far the arm moves in one step, even after a late tick
- Speed up each runtime step from 156 to 95 µs

### Fixes

- Fix the gripper closing above the cup when an observation arrived late
- Fix the arm going back to the planner's safe pose after every short gap in its commands

## v0.0.1

The first release of Omarchy Robots: a headless operating system for robots, built on NixOS and declared by one file, `robot.nix`. It runs in a virtual machine today. It builds for six NVIDIA Jetson developer kits, but it has not yet booted on a real board.

### Highlights

#### Declare a robot in one file

`robot.nix` sets the robot's name, board, disk, administrator and SSH keys, network, time zone, GPU stack and power mode. A combination that cannot work is refused when the system is built, not when the robot boots.

#### Update and roll back

`omarchy-robot update` switches the robot to a new system. `omarchy-robot rollback` returns to the previous one, also after a reboot. `omarchy-robot info` shows what runs, and `omarchy-robot validate` checks it.

#### Build for Jetson

A system, a flash script and an installer image for the Orin AGX, Orin NX, Orin NX Super, Orin Nano, Orin Nano Super and Thor AGX developer kits, on JetPack 6.2.3 (Orin) and JetPack 7.2.1 (Thor).

#### Start from a locked-down base

No desktop, SSH keys only, a firewall that admits only SSH, a bounded journal, and old systems cleaned up every week.

### Improvements

- Add the Omarchy Robots logo on the boot screen, the installer menus and the login banners
- Add the manual at https://promaa.github.io/omarchy-robots/
- Add installer images that carry the system they install
- Make the boot menu wait 1 s instead of 5
- Reduce the virtual machine's system from 1.3 to 1.1 GiB
- Test the base system, updates, rollback and installation in virtual machines in every check
