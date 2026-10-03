---
layout: default
title: Getting started
permalink: /getting-started/
---

The fastest way to meet Omarchy Robots is to run it as a virtual machine. It is the same system that goes on a Jetson, minus the NVIDIA layer.

## What you need

- An x86_64 Linux machine.
- [Nix](https://nixos.org/download) with flakes enabled.
- KVM, ideally. Without it QEMU emulates the whole machine and boot takes minutes instead of seconds.

## Run the robot

```sh
git clone https://github.com/promaaa/omarchy-robots
cd omarchy-robots
nix run .#vm
```

The VM boots through UEFI and systemd-boot, exactly like the hardware does. Its serial console is your terminal, and it logs you in as the administrator.

Now ask the system whether it is healthy:

```sh
sudo omarchy-robot validate
```

Every check prints `PASS`, `FAIL`, `SKIPPED` or `NOT_APPLICABLE`. On the VM the Jetson checks are not applicable, and that is expected. When you're done:

```sh
sudo poweroff        # leaves QEMU
```

The VM keeps its disk in `robot.qcow2` in the current directory. Delete that file to start over from scratch.

## Log in over SSH instead

Put your public key in `robot.nix` before you start the VM:

```nix
admin.sshKeys = [ "ssh-ed25519 AAAA... you@laptop" ];
```

Then `nix run .#vm` and, from another terminal:

```sh
ssh -p 2222 admin@localhost
```

## Without a clone

```sh
nix run github:promaaa/omarchy-robots#vm
```

The repository is private for now, so Nix needs a GitHub token to fetch it:

```sh
nix --option access-tokens github.com=$(gh auth token) run github:promaaa/omarchy-robots#vm
```

## Next

Open [`robot.nix`](../robot-nix/) and change something. The time zone is a good first edit, and [Updates and rollback](../updates/) shows how to deploy it.
