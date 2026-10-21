---
layout: default
title: In the lab
permalink: /lab/
---

> **Prepared, not run on any arm.** The lab GPU server section at the end ran on a lab server (2026-10-20); everything else here is prepared. The backends below are tested against fakes only: a fake Panda behind a libfranka-like interface, and AgileX's own SDK talking to a fake PiPER on a fake CAN bus. Nothing in this repository has opened a connection to a real robot. The facts about each arm come from its maker's documentation and source (links at the end of each section, read on 2026-10-19), and none has been checked against the lab's arms yet.

The lab has two Franka Emika Panda arms (joint torque sensing) and one AgileX PiPER (CAN). This chapter says how Omarchy Robots reaches them: S0 backends behind the intent contract, what each arm's documentation says, where the Panda's 1 kHz loop can run, a first contact that reads state and moves nothing, and what to check in the lab.

## The backends

Each arm is an S0 backend (`arms.py`): a `RobotIO` that S0, the guard and a controller (see [Runtime](../runtime/), "S0: the guard and its controller"), reads the arm through and commands it through. Each has its declaration in `config/bodies.toml` (`franka`, `piper`): what its controller honours, its limits, its adapter settings.

| | Franka Emika Panda | AgileX PiPER |
|---|---|---|
| Interface | libfranka 0.9.2, through a libfranka-like interface (`arms.FrankaLike`) | piper_sdk 0.6.2, `C_PiperInterface_V2`, over CAN |
| Command | joint torques, at 1 kHz, in libfranka's control loop | joint positions (MOVE J), at 200 Hz |
| Controller | `cartesian_impedance` (the scrubbing scene's) | `motion_tracker` |
| Honours | Cartesian motion in `franka.base`; contact on the flange up to 15 N | motion only |
| The guard's limits | the Panda's joint, torque (87 and 12 Nm) and torque-rate (1000 Nm/s) limits, its workspace (from the pose the robot reports), a 15 N threshold on the measured flange force | the PiPER's documented joint limits, rounded inwards; steps of 1 rad/s at most |
| Read without authorization | `FrankaArm.read`, `state` | `PiperArm.state`, `status` |

**Authorization.** Reading state needs nothing. Anything sent to an arm's actuators, enabling the PiPER's motors included, is refused (`NotAuthorized`) unless the arm's adapter table in the configuration names an operator authorization: `operator_authorization = "<who>, <date>, <where>"` in `[franka.kinematics.adapter]` or `[piper.kinematics.adapter]`. None is given in this repository. This is a declaration in the configuration, not an authentication of the operator: anyone who can edit the configuration can write it. It records a decision; it does not check one.

**The guard is additional to each arm's own safety, never a replacement for it.** The Panda's reflexes and the PiPER's collision protection stay on. The 15 N on the Panda's flange is two things: the largest force a contact intent may ask (larger is refused), and a threshold on the measured force (above it, the guard's own hold replaces the controller's torques). It reacts once the estimate has passed 15 N; it does not bound the force physically, and no test here shows the Panda's force regulation (the fake Panda's dynamics are too simple). It lies below the 20 N collision threshold libfranka's examples set, so that the robot's reflex stays the last resort. A torque command starts from zero and changes by at most the torque-rate limit times the tick's period: the first command of every control loop is zero, as the Panda requires (`FrankaArm.run` restarts S0 at each loop, and the robot's time runs on across loops).

**What S0 sees of the Panda.** The flange, as the configuration declares it, whatever end effector Desk sets: `FrankaArm` takes Desk's end effector (`F_T_EE`) out of `O_T_EE`, asks libfranka for the flange's Jacobian, and turns `O_F_ext_hat_K` into this runtime's wrench. libfranka counts the force the robot applies to the environment as positive (its `robot_state.h`); this runtime counts the environment's force on the robot, so the backend negates it, and takes its moment about the flange.

**The PiPER needs fresh feedback.** `PiperArm.state` and `apply` raise `ArmError` and send nothing unless joint feedback arrived within `feedback_timeout_s` (0.05 s; the arm sends it every 5 ms), by the CAN frames' receive time, and the SDK's `isOk()` holds. The SDK keeps its last values when frames stop, and its `isOk()` turned false only 0.3 s after the feedback stopped (against the fake, three times).

**No connection here.** Each backend takes an interface object made elsewhere; no module imports a vendor library (`tests/test_arms.py` checks it).

**The PiPER honours motion only.** Its documentation describes no force control. Its SDK reports a joint torque estimate from motor current (`effort`, J1 to J3 current x 1.18125, J4 to J6 x 0.95844), which `PiperArm.state` passes on as measured torques. Its MIT mode (a joint spring, damper and torque per joint) exists, but its torque scaling depends on the firmware and the SDK warns that misuse can damage the arm: this backend does not use it.

### The fakes

**The fake Panda** (`sim/fake_franka.py`) reproduces what libfranka 0.9.2's documentation says, as far as a test needs: the Panda's kinematics (its published DH parameters; they match the MuJoCo Menagerie model at every pose tried) and limits; an end effector away from the flange when the test sets one (Franka's hand, 10.34 cm along z); the external wrench with libfranka's sign; a control loop at 1 kHz whose first period is zero; NaN or infinity refused; the first torque of each loop zero and the torque rate within 1000 Nm/s, else the reflex `controller_torque_discontinuity`; a dropped command (a callback slower than its budget, or a drop injected) replaced by the last one, and 20 drops in a row the reflex `communication_constraints_violation`; a joint past its limit `joint_position_limits_violation`; a force on the flange above the collision threshold `cartesian_reflex`. A reflex stops the loop and leaves the robot in reflex mode until `automatic_error_recovery`. Its dynamics are simple (constant inertia and damping, gravity compensated as the robot does it): enough for the timing and the errors, not for force control.

**The fake PiPER** (`sim/fake_piper.py`) sits under AgileX's own SDK: for a test's duration, python-can's `socketcan` is a fake bus that reaches it, so the SDK's real code encodes and decodes every frame and no socket opens. The arm decodes the SDK's frames by their CAN ids and big-endian layouts and answers as documented: status, joints and motors every 5 ms, drivers (with the enable bit) every 25 ms. It moves only enabled, in CAN command mode MOVE J, towards the last joint targets at its speed percentage. A target past a joint's limit is refused with status 0x04; an emergency stop stops it; a collision shows status 0x07. Its frames carry their receive time, as socketcan stamps it, and a test can cut its feedback. Two behaviours are assumptions, to check in the lab: the arm holds its last target when commands stop, and its maximum joint speed is 3 rad/s.

The fake PiPER found a mistake: the first configuration put joint 5's limit at 1.222 rad, which is 70.016 degrees, past the arm's 70; the guard clamped targets there, and the arm refused them. The limits are now rounded inwards.

`tests/test_arms.py` runs both backends against their fakes: state without authorization and nothing sent; S0 moving each arm through its interface; the Panda's reflexes (collision, a torque jump, a first torque that is not zero, 20 dropped commands, a loop too slow for its budget) and recovery; two control loops in a row with the same S0; the flange and the wrench's sign with Franka's hand set in Desk; a contact channel without a wrench, never faithful; the PiPER's refused target and collision, and its feedback cut before the first command (nothing sent).

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

**A Python S0 cannot hold the Panda's 1 kHz loop.** One S0 tick of this runtime, the guard, the Cartesian impedance controller and the backend's state, took 500 us at the median and 562 us at the 99th percentile (1901 ticks against the fake Panda, measured on the development host on 2026-10-19, with VM tests running beside it), beside Franka's budget of 300 to 500 us for the whole loop, round trip included; and Python gives no guarantee at all. Those 500 us include the fake Panda's own Python (its state and its dynamics), which a real robot does not run. The guard is plain data so that it can become a native process; that alone would not show that the whole callback fits, since the controller and the binding run in it too. Measuring the whole callback, native, on the lab's PC, is the step before any torque reaches a real Panda.

Sources: [compatibility](https://frankarobotics.github.io/docs/compatibility.html), [libfranka CHANGELOG](https://github.com/frankarobotics/libfranka/blob/main/CHANGELOG.md), [update history](https://download.franka.de/Franka_Robotics_Update_History.pdf), [getting started](https://frankarobotics.github.io/docs/doc/libfranka/docs/getting_started.html), [overview](https://frankarobotics.github.io/docs/overview.html), [system requirements](https://frankarobotics.github.io/docs/doc/libfranka/docs/system_requirements.html), [real-time kernel](https://frankarobotics.github.io/docs/doc/libfranka/docs/real_time_kernel.html), [troubleshooting](https://frankarobotics.github.io/docs/troubleshooting.html), [robot specifications](https://frankarobotics.github.io/docs/robot_specifications.html), [libfranka 0.9.2 headers](https://github.com/frankarobotics/libfranka/tree/0.9.2/include/franka), [panda-py](https://github.com/JeanElsner/panda-py).

## The AgileX PiPER: what its documentation says

**The SDK.** `piper_sdk` (MIT, pure Python, python-can) 0.6.2 (2026-07-31) is the latest stable release; 0.7.0a1 (2026-10-14) is a pre-release. `C_PiperInterface_V2` is for firmware V1.5-2 and later; 0.7.0a1 adds `C_PiperInterface_V3` for firmware 1.8-8 and later. It is not in nixpkgs: `pkgs/piper-sdk` packages 0.6.2 (it imports typing_extensions without declaring it). AgileX also names a newer SDK, pyAgxArm (LGPL-3.0), without releases.

**CAN.** socketcan at 1 Mbit/s, which "cannot be changed", through the arm's USB-CAN module (the `gs_usb` driver). Bringing the interface up (`ip link set can0 up type can bitrate 1000000`, or the SDK's `can_activate.sh`) needs root; opening the socket does not. On a Jetson with JetPack 6, the kernel needs `CONFIG_CAN_GS_USB` for the module.

**Control.** Mode command (0x151): CAN command mode, and a move mode, MOVE P, J, L, C, M (MIT, firmware V1.5-2 and later) or CPV, at a speed percentage. Joint targets in 0.001 degree (0x155 to 0x157); enabling the motors (0x471, motor 7 for all); emergency stop and resume (0x150). The SDK's demos send the mode before each command, every 5 ms; a maintainer suggests 200 Hz. **CAUTION:** the SDK's `ResetPiper` sends the same frame as resume and makes the arm "immediately lose power and fall down"; disabling the arm cuts motor power at once. Firmware 1.8-5 and earlier cannot switch from MIT mode back.

**Feedback.** Status (0x2A1: control mode, status code such as 0x01 emergency stop, 0x04 target past a limit, 0x07 collision, 0x0E over temperature; motion status; limit and communication flags per joint), end pose, joints (0.001 degree), gripper, motors (speed 0.001 rad/s, current 0.001 A, position, and the SDK's effort), drivers (voltage, temperatures, enable, collision and stall bits). Collision protection is a level from 0 (off) to 8 per joint. About 3040 frames per second in all for one arm, which fits 200 Hz for status, joints and motors.

**Limits.** J1 -150 to 150 degrees, J2 0 to 180, J3 -170 to 0, J4 -100 to 100, J5 -70 to 70, J6 -120 to 120. The SDK clamps nothing by default. No source gives the joints' maximum speed, or what the arm does when commands stop.

**Errors.** With `judge_flag`, a missing or down CAN interface raises `ConnectionError`; a failed send is only logged; the reading thread stops on a CAN error, and `isOk()` turns false about 0.3 s after frames stop (0.305 s against the fake).

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
7. On the PC, build libfranka 0.9.2: `nix build github:promaaa/omarchy-robots/v0.0.3#libfranka -o libfranka`.
8. Read 100 states, and keep the output: `./libfranka/bin/echo_robot_state <control-ip> > robot_state.txt`.
9. Read the joint poses once, and keep the output: `./libfranka/bin/print_joint_poses <control-ip> > joint_poses.txt`.

Do not run libfranka's other examples: they move the robot, `communication_test` included.

**Bring back:** the system version; whether step 8 or 9 failed, with its message (an `IncompatibleVersionException` names the server's version); the ping output; `robot_state.txt` and `joint_poses.txt` (with nothing touching the arm: they hold `q`, `O_T_EE`, `O_F_ext_hat_K`, `robot_mode`); the PC's `uname -a`, its NIC (`lspci | grep -i ethernet`) and whether its kernel is PREEMPT_RT; a photo of the cabling.

## In the lab: open questions

The Panda:

- Q1. Which robot system version does Desk show? 4.2.1 or 4.2.2 means libfranka 0.9.2; 4.0.x means 0.8.0.
- Q2. Is FCI installed, and does its pop-up stay open?
- Q3. What is Control's address, and can a PC be cabled straight to its LAN port?
- Q4. Which end effector and load does Desk set? Zero-torque tests depend on them, and the backend removes that end effector (`F_T_EE`) to find the flange.
- Q5. Is an external activation device in reach, and is there a separate emergency stop?
- Q6. Can a second client read the state while another one controls? Franka does not document it.
- Q7. What round trip, and what `control_command_success_rate`, does the chosen PC reach?
- Q8. For the Orin: which NIC, does its real-time kernel boot, and does Control serve an ARM64 model library to this Panda?
- Q9. With a known weight m hanging from the flange, and Desk's load set without it, does `O_F_ext_hat_K` read about +m g in z, as libfranka's header says (the robot's force on the environment positive)? The backend negates it.

The PiPER:

- Q10. Which firmware does it run (S-V1.x-y)? It decides V2 or V3, MIT's mode number and torque scaling, and the mode-switch bug.
- Q11. What does the arm do when commands stop arriving: hold, time out, or drop?
- Q12. What are its feedback rates, its command-to-motion latency, and its joints' maximum speed?
- Q13. Is its gripper's stroke set, and is it in slave mode (joint feedback needs it)?
- Q14. Which collision levels are set?
- Q15. Does the PC that drives it have `gs_usb`, and does `ip -details link show can0` report 1000000?

## The lab GPU server as an edge machine

> **Measured on lab machines, real network.** One GPU of the lab server laplace, and the development host, on the lab's LAN (ping 0.64 ms), 2026-10-20. No robot, no Orin.

The milestone asks for a local model's time to first token on a lab GPU server, and for the S2 placement experiment between the development host (the robot process) and that server (S2) over the lab's network. Both ran on laplace (8 RTX 6000 Ada, driver 550.107.02, Ubuntu 22.04; other users' jobs on 4 of its GPUs at the time), reached with the development host's own SSH key; no password or key is in this repository.

**Nix without root.** No lab server has Nix, and their Python (3.6 to 3.10) is older than the runtime's. nix-portable (v012, MIT, its official release) runs Nix in the home directory through user namespaces. It built the runtime v0.0.3 part 4 and llama.cpp 9190 with CUDA 12.9, both from the nixpkgs this repository pins: CUDA's licence allowed for the CUDA packages only, built for the RTX 6000 Ada (sm_89), 9 minutes on laplace, 8.7 GB in nix-portable's store. A Nix-built program finds the server's NVIDIA driver through `LD_LIBRARY_PATH` set to a folder of links to its libraries (`libcuda`, `libnvidia-ptxjitcompiler`, `libnvidia-ml`). The model is the file of the development host's measurement (SHA-256 6a1a2eb6), downloaded again from Qwen's repository and checked.

**S2's time to first token** (the table in Runtime, "Measured latencies"): 0.044 s median on laplace, against 3.8 s on the development host's CPU (built for AVX2) and 8.5 s (as packaged). From the development host through an SSH tunnel: 0.087 s. The extra 43 ms is not the LAN: a tiny request through the same tunnel takes 3 ms, against 0.9 ms on laplace itself. It is probably the tunnel holding the first streamed chunk of the answer (Nagle's algorithm meeting delayed acknowledgements, 40 ms on Linux). That is not tested: the direct test needs a model server listening on the lab network, which was not opened.

**S2 placement on the real network.** The robot process on the development host (runtime at 0584de1, not in a VM), S2 on laplace (v0.0.3 part 4), the S2 link through an SSH tunnel over the lab LAN. S2 takes each decision's delay from the distribution measured on laplace. The four scenarios of the placement experiment, three runs each (`profile` lab-lan: the real LAN, not the emulated `lan`):

S2 placement, the two-cup task and the dynamic scenarios (no emulated network, not in VMs, soft real-time). Measured 2026-10-20 with omarchy-runtime 0.0.3 (0584de1d9a1a), no Omarchy Robots identity, S0 at 200 Hz; soft real-time, Python, wall-clock on a host without Omarchy Robots identity (x86_64, Python 3.13.15, 8 CPUs, virtualization none). Times are wall-clock; in parentheses and in the simulated column, the same task with S2 in one process on simulated time. Values: median of a cell's runs, and in brackets a distribution-free confidence interval for that median from order statistics: the narrowest pair (k-th smallest, k-th largest) whose binomial coverage is at least 95 % (with 6 runs, the smallest and the largest, 96.9 %); no interval below 6 runs. Reaction, cup_moved: from the move to S0 executing the first chunk computed from an observation taken after it. Recovery, slot_blocked, fallback_expiry: from S1 giving up to the retry running; expired: S2's fallback (3 s in slot_blocked, 1.5 s in fallback_expiry) ran out before the retry started, and S0 went on under the capability's default. Fallback coverage, the least fraction of an execution's running time during which S0 held a valid fallback, as S0 counted it. S2 RTT: heartbeat echo measured by S2: the network both ways, plus up to one loop period at each end.

| profile | TTFT s | two-cup task s | cup_moved reaction s | slot_blocked recovery s | fallback_expiry recovery s | S2 fallbacks expired | fallback coverage | violations | S0 starvations | runs within tolerance |
|---|---|---|---|---|---|---|---|---|---|---|
| lab-lan | qwen2.5-1.5b-q4km-lab-gpu (median 0.04 s) | 18.45 (18.45) | 0.10 (0.10) | 0.20 (0.15) | 0.20 (0.15) | 0 of 6 | 100.0 % | 0 | 0 | 12 of 12 |

<details markdown="1"><summary>Every cell (12)</summary>

| scenario | profile | TTFT s | runs | outcomes | violations | S0 starvations | task s (simulated) | reaction s | recovery s | fallback engaged | S2 fallbacks expired | fallback coverage | S2 RTT p50 ms | long outage s | S0 overruns, most | runs within tolerance |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cup_moved | lab-lan | qwen2.5-1.5b-q4km-lab-gpu (median 0.04 s) | 3 | SUCCEEDED | 0 | 0 | 9.20 (9.15) | 0.10 | - | - | - | 100.0 % | 3.01 | - | 0 | 3 of 3 |
| fallback_expiry | lab-lan | qwen2.5-1.5b-q4km-lab-gpu (median 0.04 s) | 3 | FAILED SUCCEEDED | 0 | 0 | 8.15 (8.05) | - | 0.20 | 3 of 3 | 0 of 3 | 100.0 % | 3.00 | - | 0 | 3 of 3 |
| s2_placement | lab-lan | qwen2.5-1.5b-q4km-lab-gpu (median 0.04 s) | 3 | SUCCEEDED SUCCEEDED | 0 | 0 | 18.45 (18.45) | - | - | - | - | 100.0 % | 3.01 | - | 0 | 3 of 3 |
| slot_blocked | lab-lan | qwen2.5-1.5b-q4km-lab-gpu (median 0.04 s) | 3 | FAILED SUCCEEDED | 0 | 0 | 8.20 (8.10) | - | 0.20 | 3 of 3 | 0 of 3 | 100.0 % | 3.04 | - | 0 | 3 of 3 |

</details>

Tolerance: Wall-clock runs are not bit-identical. Every cell must give the simulated run's outcomes, claims, verdicts and ground truth exactly, no safety violation, and no S0 tick without a chunk, a fallback or HOLD; outages must be classified by the deadlines; every execution's fallback coverage at least 99 %. Task time: local and lan within 15 % of the simulated run with the same time to first token when S0 kept its rate (overruns in under 1 % of its steps), within 30 % when it did not (a late loop delays every step, as on an emulated CPU); every profile at least 85 % of it.

What it shows: on the lab's LAN, with a model on one GPU, S2 answers before any fallback runs out (0 of 6 expired). With the same model on the development host's CPU (3.8 s), S2's fallback ran out in every recovery of the VM experiment, and a recovery took 3.85 to 3.97 s. Here a recovery takes 0.20 s, 0.05 s more than in one process on simulated time; S2's heartbeat round trip through the tunnel is 3.0 ms (the network both ways, plus up to one loop period at each end). Every run gave the simulated run's outcomes, with no violation and no starvation. What it does not show: a robot, the Orin, a wireless or wide-area link, a direct connection without the tunnel, or S2 calling the model during a run (S2 waits a delay drawn from the measured distribution, as in the VM experiment). With a sub-second S2, this experiment says little more than the emulated `lan` profile.

**Reproduce** (the scripts and logs are on the development host, in `~/src/omarchy-robots-evidence/R6-lab-gpu/`):

1. On the server, install nix-portable in `~/.local/bin` and build llama.cpp with CUDA from the pinned nixpkgs (`llama-cuda.nix` there; `NP_RUNTIME=bwrap nix-portable nix build --impure --file llama-cuda.nix`).
2. On the server, serve the model on one free GPU, on loopback only: `llama-server -m qwen2.5-1.5b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 8090 -ngl 99 -c 4096 --parallel 1`, inside `nix-portable nix shell`, with `CUDA_VISIBLE_DEVICES` and `LD_LIBRARY_PATH` set.
3. Measure on the server: `omarchy-runtime ttft-measure --url http://127.0.0.1:8090/v1 --model qwen2.5-1.5b-instruct --n 50 --no-prompt-cache ...`.
4. Measure from the development host through `ssh -N -L 8090:127.0.0.1:8090 <server>`, with `--network-path` naming the tunnel.
5. For each run, start `omarchy-runtime s2 --listen 127.0.0.1:7447 --ttft-file <the server's distribution>` on the server, then `omarchy-runtime run <scenario> --clock wall --s2 127.0.0.1:7447 --tag profile=lab-lan` on the development host, through `ssh -N -L 7447:127.0.0.1:7447 <server>`.
6. Gather: `omarchy-runtime placement runs/*/run-*/results.json --out table`.
