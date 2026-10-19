---
layout: default
title: In the lab
permalink: /lab/
---

> **Prepared, not run on any arm or lab machine.** The backends below are tested against fakes only: a fake Panda behind a libfranka-like interface, and AgileX's own SDK talking to a fake PiPER on a fake CAN bus. Nothing in this repository has opened a connection to a real robot. The facts about each arm come from its maker's documentation and source (links at the end of each section, read on 2026-10-19), and none has been checked against the lab's arms yet.

The lab has two Franka Emika Panda arms (joint torque sensing) and one AgileX PiPER (CAN). This chapter says how Omarchy Robots reaches them: S0 backends behind the intent contract, what each arm's documentation says, where the Panda's 1 kHz loop can run, a first contact that reads state and moves nothing, and what to check in the lab.

## The backends

Each arm is an S0 backend (`arms.py`): a `RobotIO` that S0, the guard and a controller (see [Runtime](../runtime/), "S0: the guard and its controller"), reads the arm through and commands it through. Each has its declaration in `config/bodies.toml` (`franka`, `piper`): what its controller honours, its limits, its adapter settings.

| | Franka Emika Panda | AgileX PiPER |
|---|---|---|
| Interface | libfranka 0.9.2, through a libfranka-like interface (`arms.FrankaLike`) | piper_sdk 0.6.2, `C_PiperInterface_V2`, over CAN |
| Command | joint torques, at 1 kHz, in libfranka's control loop | joint positions (MOVE J), at 200 Hz |
| Controller | `cartesian_impedance` (the scrubbing scene's) | `motion_tracker` |
| Honours | Cartesian motion in `franka.base`; contact on the flange up to 15 N | motion only |
| The guard's limits | the Panda's joint, torque (87 and 12 Nm) and torque-rate (1000 Nm/s) limits, its workspace (from the pose the robot reports), 15 N on the flange | the PiPER's documented joint limits, rounded inwards; steps of 1 rad/s at most |
| Read without authorization | `FrankaArm.read`, `state` | `PiperArm.state`, `status` |

**Authorization.** Reading state needs nothing. Anything sent to an arm's actuators, enabling the PiPER's motors included, is refused (`NotAuthorized`) unless the arm's adapter table in the configuration names an operator authorization: `operator_authorization = "<who>, <date>, <where>"` in `[franka.kinematics.adapter]` or `[piper.kinematics.adapter]`. None is given in this repository.

**The guard is additional to each arm's own safety, never a replacement for it.** The Panda's reflexes and the PiPER's collision protection stay on. The guard's 15 N limit on the Panda's flange lies below the 20 N collision threshold libfranka's examples set, so that the robot's reflex stays the last resort. A torque command starts from zero and changes by at most the torque-rate limit times the tick's period: the first command the Panda gets is zero, as it requires.

**No connection here.** Each backend takes an interface object made elsewhere; no module imports a vendor library (`tests/test_arms.py` checks it).

**The PiPER honours motion only.** Its documentation describes no force control. Its SDK reports a joint torque estimate from motor current (`effort`, J1 to J3 current x 1.18125, J4 to J6 x 0.95844), which `PiperArm.state` passes on as measured torques. Its MIT mode (a joint spring, damper and torque per joint) exists, but its torque scaling depends on the firmware and the SDK warns that misuse can damage the arm: this backend does not use it.

### The fakes

**The fake Panda** (`sim/fake_franka.py`) reproduces what libfranka 0.9.2's documentation says, as far as a test needs: the Panda's kinematics (its published DH parameters; they match the MuJoCo Menagerie model at every pose tried) and limits; a control loop at 1 kHz whose first period is zero; NaN or infinity refused; the first torque zero and the torque rate within 1000 Nm/s, else the reflex `controller_torque_discontinuity`; a dropped command (a callback slower than its budget, or a drop injected) replaced by the last one, and 20 drops in a row the reflex `communication_constraints_violation`; a joint past its limit `joint_position_limits_violation`; a force on the flange above the collision threshold `cartesian_reflex`. A reflex stops the loop and leaves the robot in reflex mode until `automatic_error_recovery`. Its dynamics are simple (constant inertia and damping, gravity compensated as the robot does it): enough for the timing and the errors, not for force control.

**The fake PiPER** (`sim/fake_piper.py`) sits under AgileX's own SDK: for a test's duration, python-can's `socketcan` is a fake bus that reaches it, so the SDK's real code encodes and decodes every frame and no socket opens. The arm decodes the SDK's frames by their CAN ids and big-endian layouts and answers as documented: status, joints and motors every 5 ms, drivers (with the enable bit) every 25 ms. It moves only enabled, in CAN command mode MOVE J, towards the last joint targets at its speed percentage. A target past a joint's limit is refused with status 0x04; an emergency stop stops it; a collision shows status 0x07. Two behaviours are assumptions, to check in the lab: the arm holds its last target when commands stop, and its maximum joint speed is 3 rad/s.

The fake PiPER found a mistake: the first configuration put joint 5's limit at 1.222 rad, which is 70.016 degrees, past the arm's 70; the guard clamped targets there, and the arm refused them. The limits are now rounded inwards.

`tests/test_arms.py` runs both backends against their fakes: state without authorization and nothing sent; S0 moving each arm through its interface; the Panda's reflexes (collision, a torque jump, 20 dropped commands, a loop too slow for its budget) and recovery; the PiPER's refused target and collision.

## The Franka Emika Panda: what its documentation says

Franka now calls the Panda "FER" (Franka Emika Robot). Its documentation moved to [frankarobotics.github.io/docs](https://frankarobotics.github.io/docs/).

**Which libfranka.** libfranka 0.9.2 (2022-08-31, Apache-2.0) is the last release for the Panda: it requires robot system 4.2.1 or later. From 0.10.0, libfranka is for Franka Research 3 only, and robot system 5.x does not support the Panda. With system 4.0.x, libfranka 0.8.0. A version mismatch throws `IncompatibleVersionException`. The FCI feature must be installed (Desk, Settings, System, Installed Features), and from system 4.2.0 activated in Desk ("Activate FCI"); on the Panda the FCI pop-up stays open while FCI runs, and FCI excludes Desk and Apps while it runs.

**Control.** Five modes: torque, joint position, joint velocity, Cartesian pose and Cartesian velocity. `robot.control(callback)` calls the callback every millisecond with the robot's state and the time since the last call (zero at the first); returning `MotionFinished` ends the loop. Options: the internal controller for motion generators (joint or Cartesian impedance), the rate limiter (on by default in 0.9.x) and a low-pass filter (100 Hz by default; 1000 Hz turns it off). Franka advises turning both off when testing a new controller; this backend does, its guard limiting the torque rate instead. `readOnce` and `read` cannot run while a control loop runs. `franka::Model` gives the Jacobian, Coriolis, gravity and mass; in 0.9.2 libfranka downloads the model library from the robot's Control at run time.

**Timing.** The round trip, your loop and the robot's processing must fit in 1 ms. Franka's overview gives your loop about 300 us; its troubleshooting page asks for the command within 500 us of the state. A dropped torque command is replaced by the last one; after 20 in a row the robot stops (`communication_constraints_violation`). libfranka enforces real-time priority by default (`RealtimeConfig::kEnforce`) and refuses to run without it.

**What the PC needs.** A Linux kernel with PREEMPT_RT, CPU frequency scaling off (the performance governor), a `realtime` group allowed priority 99 and locked memory, and a direct Ethernet cable to Control's LAN port (the shop floor network), not to the arm's base, without a switch. Franka's examples use 172.16.0.1/24 for the PC and 172.16.0.2 for Control. NVIDIA's drivers are not supported on PREEMPT_RT. The ping test is `sudo ping <control-ip> -i 0.001 -D -c 10000 -s 1200`. **CAUTION:** libfranka's `communication_test` moves the robot; it is not a read-only test.

**Safety.** The robot stops on its own reflexes (joint and Cartesian collision, power limit, self-collision, limits, torque discontinuity, communication), reported in `current_errors` and `last_motion_errors`, and libfranka throws `ControlException`. The robot's modes: idle, move, guiding, reflex, user stopped, automatic error recovery, other. `automaticErrorRecovery()` clears errors. The external activation device disconnects the arm from Control; Franka says it is not an emergency stop. Franka adds that libfranka's checks "are by no means conform with any safety norm".

**Limits (FER).** Joints: -2.8973 to 2.8973 rad (J1, J3, J5, J7), -1.7628 to 1.7628 (J2), -3.0718 to -0.0698 (J4), -0.0175 to 3.7525 (J6); velocities 2.175 rad/s (J1 to J4) and 2.61 (J5 to J7); torques 87 Nm (J1 to J4) and 12 Nm (J5 to J7); torque rate 1000 Nm/s, a necessary condition in torque control (a violation stops the motion), the torque itself only recommended; the first torque command zero.

**Python.** Franka's own Python binding, pylibfranka, starts with libfranka 0.16 and so serves the FR3 only. For the Panda there are community bindings: panda-py (Apache-2.0, ships libfranka 0.9.2) and franky (MIT). `arms.FrankaLike` is the interface a binding would be wrapped to; no binding is wrapped here.

**Packaged.** `nix build .#libfranka` builds libfranka 0.9.2 with its examples (`pkgs/libfranka`; it is not in nixpkgs): built on the development host, x86_64, 2026-10-19 (CMake 4 needs `CMAKE_POLICY_VERSION_MINIMUM=3.5` for it). Not built for aarch64 yet.

**A Python S0 cannot hold the Panda's 1 kHz loop.** One S0 tick of this runtime, the guard, the Cartesian impedance controller and the backend's state, took 500 us at the median and 562 us at the 99th percentile (1901 ticks against the fake Panda, measured on the development host on 2026-10-19, with VM tests running beside it), beside Franka's budget of 300 to 500 us for the whole loop, round trip included; and Python gives no guarantee at all. The guard is plain data so that it can become a native process: that is the step before any torque reaches a real Panda.

Sources: [compatibility](https://frankarobotics.github.io/docs/compatibility.html), [libfranka CHANGELOG](https://github.com/frankarobotics/libfranka/blob/main/CHANGELOG.md), [update history](https://download.franka.de/Franka_Robotics_Update_History.pdf), [getting started](https://frankarobotics.github.io/docs/doc/libfranka/docs/getting_started.html), [overview](https://frankarobotics.github.io/docs/overview.html), [system requirements](https://frankarobotics.github.io/docs/doc/libfranka/docs/system_requirements.html), [real-time kernel](https://frankarobotics.github.io/docs/doc/libfranka/docs/real_time_kernel.html), [troubleshooting](https://frankarobotics.github.io/docs/troubleshooting.html), [robot specifications](https://frankarobotics.github.io/docs/robot_specifications.html), [libfranka 0.9.2 headers](https://github.com/frankarobotics/libfranka/tree/0.9.2/include/franka), [panda-py](https://github.com/JeanElsner/panda-py).

## The AgileX PiPER: what its documentation says

**The SDK.** `piper_sdk` (MIT, pure Python, python-can) 0.6.2 (2026-07-31) is the latest stable release; 0.7.0a1 (2026-10-14) is a pre-release. `C_PiperInterface_V2` is for firmware V1.5-2 and later; 0.7.0a1 adds `C_PiperInterface_V3` for firmware 1.8-8 and later. It is not in nixpkgs: `pkgs/piper-sdk` packages 0.6.2 (it imports typing_extensions without declaring it). AgileX also names a newer SDK, pyAgxArm (LGPL-3.0), without releases.

**CAN.** socketcan at 1 Mbit/s, which "cannot be changed", through the arm's USB-CAN module (the `gs_usb` driver). Bringing the interface up (`ip link set can0 up type can bitrate 1000000`, or the SDK's `can_activate.sh`) needs root; opening the socket does not. On a Jetson with JetPack 6, the kernel needs `CONFIG_CAN_GS_USB` for the module.

**Control.** Mode command (0x151): CAN command mode, and a move mode, MOVE P, J, L, C, M (MIT, firmware V1.5-2 and later) or CPV, at a speed percentage. Joint targets in 0.001 degree (0x155 to 0x157); enabling the motors (0x471, motor 7 for all); emergency stop and resume (0x150). The SDK's demos send the mode before each command, every 5 ms; a maintainer suggests 200 Hz. **CAUTION:** the SDK's `ResetPiper` sends the same frame as resume and makes the arm "immediately lose power and fall down"; disabling the arm cuts motor power at once. Firmware 1.8-5 and earlier cannot switch from MIT mode back.

**Feedback.** Status (0x2A1: control mode, status code such as 0x01 emergency stop, 0x04 target past a limit, 0x07 collision, 0x0E over temperature; motion status; limit and communication flags per joint), end pose, joints (0.001 degree), gripper, motors (speed 0.001 rad/s, current 0.001 A, position, and the SDK's effort), drivers (voltage, temperatures, enable, collision and stall bits). Collision protection is a level from 0 (off) to 8 per joint. About 3040 frames per second in all for one arm, which fits 200 Hz for status, joints and motors.

**Limits.** J1 -150 to 150 degrees, J2 0 to 180, J3 -170 to 0, J4 -100 to 100, J5 -70 to 70, J6 -120 to 120. The SDK clamps nothing by default. No source gives the joints' maximum speed, or what the arm does when commands stop.

**Errors.** With `judge_flag`, a missing or down CAN interface raises `ConnectionError`; a failed send is only logged; the reading thread stops on a CAN error, and `isOk()` turns false after about 250 ms without frames.

Sources: [piper_sdk](https://github.com/agilexrobotics/piper_sdk) (0.6.2, and `asserts/V2/INTERFACE_V2.MD`, the Q&A and the demos of 0.7.0a1), [PyPI](https://pypi.org/project/piper-sdk/), [issue 80](https://github.com/agilexrobotics/piper_sdk/issues/80), [issue 27](https://github.com/agilexrobotics/piper_sdk/issues/27), [pyAgxArm](https://github.com/agilexrobotics/pyAgxArm).

## Where the Panda's 1 kHz loop runs

The Panda needs S0 (the guard and its controller) at 1 kHz on a machine cabled straight to Control, with a real-time kernel. Two places:

| | A: on the Jetson AGX Orin | B: on a dedicated x86 PC (the `pc` board) |
|---|---|---|
| Real-time kernel | jetpack-nixos has it: `hardware.nvidia-jetpack.kernel.realtime = true;` (PREEMPT_RT patches for L4T r35, r36 and r39). An `orin-agx-devkit` system with it evaluates (2026-10-19); its kernel is not built. NVIDIA calls the Jetson real-time kernel developer-preview quality. | Linux 6.12 and later carry PREEMPT_RT; the `pc` board's Linux 6.18 with `PREEMPT_RT` set evaluates (2026-10-19); not built. |
| Franka's position | no statement on aarch64 today; libfranka 0.7.0 added the model library for ARM64, which Control must serve; one user ran an FR3 from an AGX Orin with a direct cable | the documented path: x86, PREEMPT_RT, a good NIC |
| The GPU | NVIDIA's drivers are not supported on PREEMPT_RT: perception and S1 on the same Orin would fight the real-time kernel | the Orin keeps the GPU for perception and S1 |
| What it needs | an Ethernet port of the Orin for Control alone, CPU cores isolated for S0, the performance governor | a PC with a supported NIC, cabled to Control; intents from S1 on the Orin to S0 on the PC over the network: a data-plane link, not built yet |

B is the safer first step: it is Franka's documented setup, and the Orin keeps its GPU. Neither option is part of `robot.nix` yet. In NixOS terms:

```nix
# A, on a Jetson board:
hardware.nvidia-jetpack.kernel.realtime = true;
# B, on the pc board (Linux 6.12 or later):
boot.kernelPatches = [
  {
    name = "preempt-rt";
    patch = null;
    structuredExtraConfig = with lib.kernel; {
      PREEMPT_RT = yes;
      EXPERT = yes;
      PREEMPT_VOLUNTARY = lib.mkForce no;
      PREEMPT = lib.mkForce no;
      PREEMPT_DYNAMIC = lib.mkForce no;
    };
  }
];
```

## First contact: read the Panda's state (untested)

Nothing here moves the arm. `echo_robot_state` calls `robot.read()` and `print_joint_poses` calls `readOnce()` and `loadModel()`: both only read (their sources, libfranka 0.9.2). Bring a person who knows the cell, and keep the external activation device in reach.

1. In Desk, read the robot system version and write it down. With 4.2.1 or later, continue; otherwise stop: this needs libfranka 0.8.0.
2. In Desk, open Settings, System, Installed Features, and check that FCI is there.
3. Cable the lab PC directly to Control's LAN port (not the arm's base, no switch).
4. Give that PC port a static address on Control's network (Franka's example: 172.16.0.1/24 for the PC, Control at 172.16.0.2).
5. In Desk, activate FCI, and leave its pop-up open.
6. On the PC, measure the round trip, and keep the output: `ping -c 100 <control-ip>`.
7. On the PC, build libfranka 0.9.2: `nix build github:promaaa/omarchy-robots/v0.0.12#libfranka -o libfranka`.
8. Read 100 states, and keep the output: `./libfranka/bin/echo_robot_state <control-ip> > robot_state.txt`.
9. Read the joint poses once, and keep the output: `./libfranka/bin/print_joint_poses <control-ip> > joint_poses.txt`.

Do not run libfranka's other examples: they move the robot, `communication_test` included.

**Bring back:** the system version; whether step 8 or 9 failed, with its message (an `IncompatibleVersionException` names the server's version); the ping output; `robot_state.txt` and `joint_poses.txt` (with nothing touching the arm: they hold `q`, `O_T_EE`, `O_F_ext_hat_K`, `robot_mode`); the PC's `uname -a`, its NIC (`lspci | grep -i ethernet`) and whether its kernel is PREEMPT_RT; a photo of the cabling.

## In the lab: open questions

The Panda:

- Q1. Which robot system version does Desk show? 4.2.1 or 4.2.2 means libfranka 0.9.2; 4.0.x means 0.8.0.
- Q2. Is FCI installed, and does its pop-up stay open?
- Q3. What is Control's address, and can a PC be cabled straight to its LAN port?
- Q4. Which end effector and load does Desk set? Zero-torque tests depend on them.
- Q5. Is an external activation device in reach, and is there a separate emergency stop?
- Q6. Can a second client read the state while another one controls? Franka does not document it.
- Q7. What round trip, and what `control_command_success_rate`, does the chosen PC reach?
- Q8. For the Orin: which NIC, does its real-time kernel boot, and does Control serve an ARM64 model library to this Panda?
- Q9. Does `O_F_ext_hat_K` read a positive z when a known weight hangs from the flange? The backend assumes the environment's wrench on the end-effector.

The PiPER:

- Q10. Which firmware does it run (S-V1.x-y)? It decides V2 or V3, MIT's mode number and torque scaling, and the mode-switch bug.
- Q11. What does the arm do when commands stop arriving: hold, time out, or drop?
- Q12. What are its feedback rates, its command-to-motion latency, and its joints' maximum speed?
- Q13. Is its gripper's stroke set, and is it in slave mode (joint feedback needs it)?
- Q14. Which collision levels are set?
- Q15. Does the PC that drives it have `gs_usb`, and does `ip -details link show can0` report 1000000?

## The lab GPU server as an edge machine (prepared, not run)

The milestone asks to measure a local model's time to first token on the lab's GPU server, and to run the S2 placement experiment between the development PC (the robot process) and that server (S2) over the lab's network. Nothing has run: no SSH host for that server is in the development host's SSH configuration, and no password or key belongs in this repository.

**What is needed.** An SSH host alias for the server in `~/.ssh/config` on the development host (for example `lab-gpu`), with key authentication already working; the server's GPU (`nvidia-smi`) and whether Nix is installed there; a port the PC can reach on the server, or permission to forward one over SSH; the model to serve, and whether the server can download it.

**Without root on the server.** If Nix is absent there, the options are: the server's own OpenAI-compatible server if it has one (llama.cpp, vLLM, Ollama), since `ttft-measure` only needs an endpoint; Nix installed without root (nix-portable, or a user-namespace store); or the runtime from a source checkout with the server's Python and numpy, which is all S2 needs. Nothing here installs anything that needs root.

Then, from the development host, once the alias works:

1. On the server, serve the model on its loopback (llama.cpp shown; the model file is the one in `data/latency`'s provenance): `ssh lab-gpu 'llama-server -m qwen2.5-1.5b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 8090 -ngl 99 --parallel 1'`.
2. On the development host, forward that port: `ssh -N -L 8090:127.0.0.1:8090 lab-gpu`.
3. On the development host, measure the time to first token over the lab's network: `nix run .#omarchy-runtime -- ttft-measure --url http://127.0.0.1:8090/v1 --model qwen2.5-1.5b-instruct --n 50 --no-prompt-cache --backend "llama.cpp on the lab GPU server" --model-file <file> --out lab-gpu.json`.
4. On the server, start S2 for one robot run, with that distribution (copied there first), and forward its port: `ssh -L 7447:127.0.0.1:7447 lab-gpu 'omarchy-runtime s2 --listen 127.0.0.1:7447 --ttft-file lab-gpu.json'`. Without Nix on the server, `omarchy-runtime` is `PYTHONPATH=pkgs/omarchy-runtime python3 -m omarchy_runtime` in a checkout of this repository, with numpy.
5. On the development host, run the robot process on the wall clock against it: `nix run .#omarchy-runtime -- run s2_placement --clock wall --s2 127.0.0.1:7447 --tag network="lab, over SSH"`.

Label every number "measured on lab machines, real network" and say that the link ran through SSH port forwarding.
