---
layout: default
title: Security
permalink: /security/
---

A robot sits on a lab network, often next to other people's machines. The defaults are strict and there is little to configure.

- **Keys only.** SSH accepts public keys and nothing else. Password and keyboard-interactive login are off, and root cannot log in over SSH.
- **One administrator.** The account from `admin.user`, in `wheel`, with passwordless sudo. Users are declared, not edited: `useradd` on the robot is undone by the next switch.
- **No key, no build.** A Jetson board refuses to build when `admin.sshKeys` is empty, because nobody could ever log in.
- **Firewall on.** It admits SSH and nothing else.
- **The installer is keys only too.** It announces itself as `<name>-installer` and, over SSH, accepts only your administrator key for `root`.
- **Bounded logs.** The journal keeps at most 512 MB.

`validate` checks the SSH and firewall settings on every run, so a hand edit that weakens them shows up as a `FAIL`.

## Opening a port

Only when you need it, in `robot.nix`:

```nix
networking.firewall.allowedTCPPorts = [ 8080 ];
```

## What is not here yet

No secure boot, no disk encryption, no automatic updates. Releases are source only: you build every system yourself from pinned inputs, so what runs on the robot is what `flake.lock` says.
