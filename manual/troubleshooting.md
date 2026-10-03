---
layout: default
title: Troubleshooting
permalink: /troubleshooting/
---

Start with the checklist. It names what is wrong more often than not:

```sh
sudo omarchy-robot validate
```

These are the failure points expected at bring-up. None has been met on hardware yet, so this list will change after the first board.

| Symptom | First command |
|---|---|
| Firmware and L4T disagree: `jetson.l4t` fails, NVIDIA's logo instead of ours at power-on, or the board stops in UEFI or early in the kernel | `ota-check-firmware -b` (or `cat /sys/class/dmi/id/bios_version`), then flash again with step 2 of the [bring-up](../bring-up/) |
| `gpu.device` fails, no GPU device node | `lsmod \| grep nvgpu; ls -l /dev/nvgpu/igpu0; dmesg \| grep -i nvgpu` |
| The NVMe is not seen by UEFI or the installer | In the installer: `lsblk; dmesg \| grep -i nvme; lspci`. Reseat the SSD in the Key M slot |
| The installer does not appear on the network | On the serial console: `ip -br addr; networkctl` |
| The board's USB ID is in no table | Stop. There is no mapping for that board here |
| Building on the robot is slow (Orin Nano) | Build on your computer and deploy with `--target-host`. To see what a deploy would build: `nix build --dry-run .#nixosConfigurations.robot.config.system.build.toplevel` |
| A change broke the robot | `sudo omarchy-robot rollback`, or pick the previous generation in the boot menu |

## The VM

- **Boot takes minutes.** No KVM, so QEMU emulates. Check `ls -l /dev/kvm`.
- **Start over.** Delete `robot.qcow2` in the directory where you ran `nix run .#vm`.
- **SSH refuses you.** Your key must be in `robot.nix` before the VM is built. Edit, then `nix run .#vm` again.

## Reporting a problem

Bring the output of `omarchy-robot info --json` and `validate --json`, plus, on a board, the flash script's output, the serial console log, `dmesg` and `journalctl -b -p warning`. Open an issue on [GitHub](https://github.com/promaaa/omarchy-robots/issues).
