---
layout: default
title: robot.nix
permalink: /robot-nix/
---

`robot.nix` is the one file that declares a robot. Everything else in the repository is the system it configures.

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

That's a complete robot. The flake output for this file is `.#robot`.

## Options

Every option lives under `omarchy.robot`. Types and defaults are in [`modules/options.nix`](https://github.com/promaaa/omarchy-robots/blob/main/modules/options.nix), which is the reference if this page and the code ever disagree.

| Option | Default | What it does |
|---|---|---|
| `name` | `"robot"` | Hostname. Lowercase letters, digits and dashes. |
| `board` | `"vm"` | What the robot runs on. See [Boards](../boards/). Changing boards is this one line. |
| `storage.device` | `/dev/vda` on `vm`, `/dev/nvme0n1` on a Jetson | The whole disk the installer **erases** and installs onto: an EFI partition and an ext4 root. |
| `admin.user` | `"admin"` | The administrator login. In `wheel`, with passwordless sudo. |
| `admin.sshKeys` | `[ ]` | Public keys that may log in as the administrator. A Jetson board refuses to build without at least one, because nobody could ever log in. |
| `network.address` | `null` (DHCP) | Static IPv4 address with prefix, like `"192.168.1.20/24"`. |
| `network.gateway` | `null` | Default gateway, only with a static address. |
| `network.dns` | `[ ]` | DNS servers, only with a static address. |
| `timezone` | `"UTC"` | Time zone of the clock and the logs, like `"Asia/Seoul"`. |
| `gpu.enable` | `false` | Jetson only. Installs the NVIDIA GPU userspace and the CUDA and TensorRT samples that `validate` runs. Large. |
| `power.mode` | `null` (board default) | Jetson only. The `nvpmodel` mode ID applied at boot. IDs differ per board: `nvpmodel -p --verbose` on the robot lists them. |
| `generations` | `10` | Systems kept for rollback, between 2 and 100. Both the boot menu and the weekly garbage collection respect it. |

## Guard rails

The build stops with a plain message instead of producing a robot that can't work:

- a Jetson board with no SSH key;
- `gpu.enable` or `power.mode` on the `vm` board;
- a gateway or DNS servers without a static address.

## Anything else

`robot.nix` is an ordinary NixOS module. When nothing under `omarchy.robot` fits, set any NixOS option right there:

```nix
{
  omarchy.robot = { ... };
  services.tailscale.enable = true;
}
```

Keep those additions few. The point of the file is that someone else can read it in a minute and know the machine.
