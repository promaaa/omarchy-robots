<h1 align="center"><img src="branding/omarchy-robots-wordmark-dotted.svg" alt="Omarchy Robots" width="640"></h1>

A small, opinionated operating system for robot computers, built on NixOS and
[jetpack-nixos](https://github.com/anduril/jetpack-nixos). One file,
`robot.nix`, declares the whole machine: which board it runs on, who may log
in, its network, its disk, its GPU stack and power mode. Everything else is a
default you do not have to think about, and every build carries its own
identity so an experiment can name the exact system it ran on.

It is headless and deliberately plain: SSH keys only, a firewall that admits
SSH and nothing else, a bounded history of system generations to roll back to,
and one command, `omarchy-robot`, to inspect, validate, update and roll back.
There is no robot runtime here yet; this is the base it will run on. It boots
as Omarchy Robots: its logo on the Jetson's UEFI boot screen, the installer's
menus and the login banners.

**Status:** v0.1.0 (milestone OS-1) runs and is tested in virtual
machines. The Jetson targets evaluate and are partly built; none has run on
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

## Support matrix

Status levels, lowest to highest: **evaluates** (the system, its GPU variant,
flash script and installer image evaluate, with the per-board assertions of
`nix flake check`), **builds** (built on the host named below), **VM-tested**,
**hardware-tested**. No target is hardware-tested yet.

| Target (`board`) | Status | System closure | Built | Last verified |
|---|---|---|---|---|
| `vm` | VM-tested: boot, SSH, firewall, identity, validate; update and rollback across a reboot; installation onto a blank disk; the runbook rehearsed (installer, `nixos-rebuild --target-host`, rollback, cold boots) | 1.1 GiB | system, VM, installer image (1.5 GiB) | 2026-10-02 |
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
the whole `nix flake check` passed here in 67 min (v0.1.0-rc.2 tree), most
of it the three VM tests under TCG.
In CI (GitHub `ubuntu-24.04`, 2 CPUs, 7.8 GiB RAM, KVM) the VM boots in
17.2 s (`systemd-analyze`, VM) and `nix flake check` takes 25 min from an empty
store. Rehearsing the runbook on the vm board under TCG: the installer answers
SSH 154 s after power-on, `omarchy-install` takes 22 min, and
`nixos-rebuild switch --target-host` deploys a time zone change in 56 s.
The `orin-agx-devkit` system built natively on a GitHub `ubuntu-24.04-arm`
runner in about an hour from an empty store, Tegra kernel included (the
1.4 GiB closure predates the rc.2 closure reductions); once the Orin kernel
and firmware were built, each further Orin flash script took 70 to 82 s.
With the Omarchy Robots boot logo, the `orin-agx-devkit` flash script rebuilt
in under 8 min (EDK2 again); its firmware's boot screen is the robot logo.
Boot time, rc.1 against rc.2 (vm board, QEMU TCG, other builds paused, five
boots each from a fresh disk): `systemd-analyze` totals average 66.6 s and
65.9 s, the same within the ±3 s spread; userborn's 1.3 s replaces the Perl
user script of the initrd, and the 1 s boot menu saves 4 s before the kernel.

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
   on. On a display, the Omarchy Robots logo at power-on means the firmware
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
nix flake check                 # format, statix, deadnix, ruff, mypy, unit tests,
                                # every board evaluated, VM tests (base, update/rollback, installer)
nix fmt                         # nixfmt (RFC style) on every .nix file
nix develop                     # nixfmt, statix, deadnix, ruff, mypy, python3
```

Artifacts, all built from `robot.nix` (`<board>` is any Jetson board above):

| Command | Artifact |
|---|---|
| `nix build .#nixosConfigurations.robot.config.system.build.toplevel` | the robot's system, as `robot.nix` declares it |
| `nix build .#nixosConfigurations.<board>.config.system.build.toplevel` | the same system on `<board>` |
| `nix build .#flash` / `.#flash-<board>` | jetpack-nixos's firmware flash script (x86_64 host, USB recovery mode) |
| `nix build .#packages.aarch64-linux.installer` / `.#packages.aarch64-linux.installer-<board>` | installation image for a Jetson; it carries the system it installs |
| `nix build .#installer-vm` | installation image for the vm board (x86_64) |
| `nix build .#vm` | the vm board as a QEMU VM (`nix run .#vm`) |
| `nix build .#omarchy-robot` | the command alone |

`.#flash` and `.#installer` follow the board set in `robot.nix`; with
`board = "vm"` only `.#installer` exists (x86_64). Building a Jetson system on
an x86_64 host needs `extra-platforms = aarch64-linux` and a QEMU binfmt
handler (on NixOS: `boot.binfmt.emulatedSystems = [ "aarch64-linux" ]`); there
is no public binary cache for jetpack-nixos, so the Tegra kernel compiles under
emulation the first time. NixOS VM tests need the `nixos-test` and `kvm`
system features in `nix.conf`.

Layout: `flake.nix` (inputs pinned in `flake.lock`), `robot.nix`, `modules/`
(options, base, identity, jetson, vm, installer, boards), `pkgs/omarchy-robot/`
(the command, its unit tests and fixtures), `tests/` (checks and VM tests).

## Versioning

Semantic versioning before 1.0: v0.1.x is this milestone (VM-validated; Jetson
targets evaluate and build; not hardware-tested), patch releases are verified
fixes, and v0.2.0 is reserved for the first board booted and validated on
hardware. The validation report schema is versioned separately (`schema` in
the JSON); its changes are noted in [`CHANGELOG.md`](CHANGELOG.md). Releases
are source only: no images, no NVIDIA binaries.

## License

MIT, see [`LICENSE`](LICENSE). jetpack-nixos is MIT; the NVIDIA components it
packages are under NVIDIA's licenses and are built locally, never
redistributed here.
