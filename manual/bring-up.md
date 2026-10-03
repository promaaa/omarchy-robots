---
layout: default
title: Bring-up on a Jetson
permalink: /bring-up/
---

> **Prepared, not yet run on hardware.** Every command here has been built or rehearsed on the `vm` board or an x86_64 host. Board facts (buttons, ports, USB IDs, part numbers) come from NVIDIA's developer kit guides, the Jetson Linux documentation and jetpack-nixos, and none has been checked against a physical board yet. If something below is wrong, it is the first thing to fix.

Bring-up has three parts: flash the board's firmware from your computer, install the system from a USB stick, then check it. Nothing is compiled on the robot.

## What to have ready

- **A host**: an x86_64 Linux machine (NVIDIA's flashing tools are x86_64 only) with Nix and flakes, aarch64 emulation for Nix (`extra-platforms = aarch64-linux` and a QEMU binfmt handler), sudo, and about 100 GB free. The first Jetson build takes hours: **build the day before**.
- **For the board**: its power supply, the USB cable for flashing, an M.2 Key M NVMe SSD (Thor ships with one), a USB stick of 8 GB or more, and Ethernet to a network with DHCP that your host can reach. For the Orin NX or Nano carrier, a jumper for the recovery pins.
- **Worth having**: the serial console, or a DisplayPort monitor and USB keyboard for the UEFI menu.
- **Your SSH public key** in `robot.nix`. The robot and the installer accept nothing else.

## Identify the board

| | AGX Orin | Orin NX / Orin Nano (Super) | AGX Thor |
|---|---|---|---|
| `board =` | `orin-agx-devkit` | `orin-nx-devkit`, `orin-nano-super-devkit` or `orin-nano-devkit` | `thor-agx-devkit` |
| `lsusb` in recovery | `0955:7023`, or `0955:7223` for P3701-0004 | NX `0955:7323` (16 GB), `0955:7423` (8 GB); Nano `0955:7523` (8 GB), `0955:7623` (4 GB) | `0955:7026` |
| Enter recovery | Powered off: hold Force Recovery (middle), press Power (left), release both | Jumper J14 pin 9 (GND) to pin 10 (FC REC), then apply power; remove it once `lsusb` shows the board | Hold Force Recovery (middle), press Reset (right), release |
| Flashing port | USB-C J40, by the 40-pin header | USB-C J5 (cable not in the box) | USB-C next to HDMI |
| Serial console | micro-USB J26, `/dev/ttyACM*`, 115200 | 3.3 V USB-TTL on J14: pin 3 RXD, pin 4 TXD, pin 7 GND, 115200 | debug USB-C behind the lid, `/dev/ttyACM1`, 115200 |
| NVMe slot | J1 | J11 (2280) or J24 (2230), underside | J103, 1 TB fitted |

If the USB ID matches none of these, stop: there is no mapping for that board here. The README has the [full table](https://github.com/promaaa/omarchy-robots#identify-the-board), with module part numbers.

## Step by step

Set `board` and your key in `robot.nix`. Then, on the host, in the clone:

**1. Build the flash script and the installer.** The installer carries the whole system.

```sh
nix build .#flash -o flash -L
nix build .#packages.aarch64-linux.installer -o installer -L
```

For the GPU checks, set `gpu.enable = true` before this step. Otherwise `validate` will skip them.

**2. Flash the firmware.** Board in recovery mode, flashing port to the host.

```sh
lsusb | grep -i 0955:
sudo ./flash/bin/initrd-flash-orin-agx-devkit   # the script for your board
```

It boots a small Linux on the board over USB, writes the QSPI firmware and reboots the board. It ends with `Flashing platform firmware successful`. Some Orin Nano kits ship with JetPack 5 firmware that can't boot JetPack 6; this step replaces it.

**3. Write the installer to the USB stick.** Check the device name twice.

```sh
lsblk
sudo dd if=$(echo installer/iso/*.iso) of=/dev/sdX bs=4M conv=fsync status=progress
```

**4. Boot the installer.** NVMe fitted, Ethernet in, USB stick in, power on. The Omarchy Robots wordmark at power-on means the new firmware is running; NVIDIA's logo means the board still has its old firmware. The AGX kits try USB first. Otherwise press Esc at the boot screen, open the Boot Manager and pick the USB stick.

**5. Install, from the host.** The installer announces itself over mDNS as `<name>-installer`.

```sh
ssh root@robot-installer.local
omarchy-install        # shows the disk, asks "yes", erases it, installs
poweroff               # then remove the USB stick and power on
```

If mDNS doesn't reach you, find the address in your router's DHCP leases or with `nmap -sn <subnet>`.

**6. First boot.** The robot asks DHCP for an address under its hostname.

```sh
ssh admin@<address>
omarchy-robot info
```

**7. Validate, and keep the report.**

```sh
ssh admin@<address> sudo omarchy-robot validate --json > validate-<board>.json
ssh admin@<address> sudo omarchy-robot validate
```

**8. Update and roll back once**, as in [Updates and rollback](../updates/), and write down what happened.

## Bring back

- `validate-<board>.json` and the output of `omarchy-robot info --json`;
- the closure size, `nix path-info -Sh .#nixosConfigurations.robot.config.system.build.toplevel`, and the boot time, `systemd-analyze` on the robot;
- the update and rollback results;
- on any failure: the flash script's output, the serial console log, `dmesg`, and `journalctl -b -p warning`.

When something goes wrong, [Troubleshooting](../troubleshooting/) lists the likely failure points and the first command for each.
