---
layout: default
title: Updates and rollback
permalink: /updates/
---

A robot is never edited in place. You change `robot.nix` on your computer, build a new system there, and switch the robot to it. The old system stays on disk, and in the boot menu, until it falls out of the window of kept generations.

## Deploy a change

Make a small change in `robot.nix`, for example:

```nix
timezone = "Asia/Seoul";
```

Then, from the clone:

```sh
nix run nixpkgs#nixos-rebuild -- switch --flake .#robot --target-host admin@<address> --sudo
```

`--target-host` builds on your computer and copies the result over SSH. Nothing is compiled on the robot. Check it:

```sh
ssh admin@<address> 'omarchy-robot info; readlink /etc/localtime'
```

You should see a new generation and `Asia/Seoul`. It survives a reboot.

## Go back

```sh
ssh admin@<address> sudo omarchy-robot rollback
```

The previous generation is active again and is the boot default. You can also pick any kept generation from the boot menu: it shows for one second at power-on, and any key holds it.

## How much history is kept

`generations` in `robot.nix`, 10 by default. The boot menu lists that many systems, and the weekly garbage collection never deletes them. Everything older is collected.

## What is tested

On the `vm` board, a VM test switches to a new system and checks it, rolls back, reboots, checks the old system is still the one running, and validates. The full sequence with a reboot after the update was also rehearsed by hand on the `vm` board. On a Jetson, this exact sequence is step 8 of the [bring-up](../bring-up/) and has not run yet.

## Updating Omarchy Robots itself

The inputs (nixpkgs, jetpack-nixos, disko) are pinned in `flake.lock`. Pulling a newer Omarchy Robots and redeploying is the same `nixos-rebuild` command. Changing the JetPack line also changes the firmware the board needs: flash it again from the host first, as in step 2 of the bring-up, or `validate` will fail `jetson.l4t`.
