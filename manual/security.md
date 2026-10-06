---
layout: default
title: Security
permalink: /security/
---

A robot sits on a lab network, often next to other people's machines. The defaults are strict and there is little to configure.

- **Keys only.** SSH accepts public keys and nothing else. Password and keyboard-interactive login are off, and root cannot log in over SSH.
- **One administrator.** The account from `admin.user`, in `wheel`, with passwordless sudo. Users are declared, not edited: `useradd` on the robot is undone by the next switch.
- **No key, no build.** A Jetson or `pc` board refuses to build when `admin.sshKeys` is empty, because nobody could ever log in.
- **Firewall on.** It admits SSH and nothing else.
- **The installer is keys only too.** It announces itself as `<name>-installer` and, over SSH, accepts only your administrator key for `root`.
- **Bounded logs.** The journal keeps at most 512 MB.

`validate` checks the SSH and firewall settings on every run, so a hand edit that weakens them shows up as a `FAIL`.

## Opening a port

Only when you need it, in `robot.nix`:

```nix
networking.firewall.allowedTCPPorts = [ 8080 ];
```

## The S2 link

When S2 runs on another machine (an edge server, a cloud host), the link between the runtime and S2 carries goals, decisions and execution summaries. The OS secures it, not the runtime: `omarchy.robot.s2Link` puts it in a WireGuard tunnel, and the runtime adds no TLS of its own.

```nix
omarchy.robot.s2Link = {
  enable = true;
  address = "10.77.0.1";                        # the robot, inside the tunnel
  privateKeyFile = "/var/lib/omarchy-s2-link/private.key";
  peer = {
    address = "10.77.0.2";                      # S2's machine, inside the tunnel
    publicKey = "base64 public key of S2's machine";
    endpoint = "edge.lab:51820";
  };
};
```

- **The private key never leaves the robot.** Generate it there (`umask 077; wg genkey > /var/lib/omarchy-s2-link/private.key`, then `wg pubkey < ...` for its public key). The option is a path read when the tunnel comes up: the key is never in the Nix store or in the repository, and the build refuses a path in the store. The tunnel stays down until the file exists.
- **Public keys are public.** Put S2's in `robot.nix`, and the robot's in the S2 machine's configuration (the same module works there, with `role = "s2"`).
- **The tunnel only.** With the link enabled, `/etc/omarchy-robot/s2-link.json` names the tunnel's addresses. `omarchy-runtime run --s2` then refuses any address but S2's inside the tunnel and connects from the robot's; `omarchy-runtime s2` listens on the tunnel address only, and S2's machine admits the S2 port on the tunnel interface only. The robot's firewall opens the WireGuard UDP port and nothing more.
- **Checked.** The VM test `vm-s2-link` generates keys at test time, runs a task through the tunnel, captures the LAN to show no S2 traffic in the clear, and checks that S2 is refused outside the tunnel.

## What is not here yet

No secure boot, no disk encryption, no automatic updates. Releases are source only: you build every system yourself from pinned inputs, so what runs on the robot is what `flake.lock` says.
