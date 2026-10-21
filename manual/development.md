---
layout: default
title: Development
permalink: /development/
---

Everything is a Nix flake. One command checks the whole project:

```sh
nix flake check     # format, statix, deadnix, ruff, mypy, unit tests, the runtime's scenario
                    # matrix, every board evaluated (with and without the runtime),
                    # VM tests (base, update/rollback, installer on vm and pc, runtime)
nix fmt             # nixfmt on every .nix file
nix develop         # a shell with nixfmt, statix, deadnix, ruff, python3 with numpy and mypy
```

The runtime has its own check, which runs without a VM in under a minute: `nix build .#checks.x86_64-linux.omarchy-runtime`. Inside `nix develop`, from `pkgs/omarchy-runtime`, `python3 -m unittest discover -s tests -t .` runs the same tests. [AGENTS.md](https://github.com/promaaa/omarchy-robots/blob/main/AGENTS.md) lists the runtime's binding principles and how to add an extension.

The VM tests need the `nixos-test` system feature in `nix.conf`; with `kvm` and `/dev/kvm` they run under KVM, without them under QEMU's emulation (TCG), several times slower. Every check, build, experiment and release check runs locally (AGENTS.md, "Checks and releases"): the relevant checks after each change, the full `nix flake check` on the commit to tag before every release. The GitHub workflows are manual-only and run nothing by themselves. `nix build .#checks.x86_64-linux.manual` builds this manual (open `result/index.html`) and fails if a page keeps Markdown as text.

## What you can build

Every artifact comes from `robot.nix`. `<board>` is any Jetson board.

| Command | Artifact |
|---|---|
| `nix build .#nixosConfigurations.robot.config.system.build.toplevel` | the robot's system, as `robot.nix` declares it |
| `nix build .#nixosConfigurations.<board>.config.system.build.toplevel` | the same system on `<board>` |
| `nix build .#flash` or `.#flash-<board>` | the firmware flash script (x86_64 host, USB recovery mode) |
| `nix build .#packages.aarch64-linux.installer` or `.installer-<board>` | the installation image for a Jetson, carrying its system |
| `nix build .#installer-vm` or `.#installer-pc` | the installation image for the `vm` or `pc` board |
| `nix build .#vm` | the `vm` board as a QEMU VM (`nix run .#vm`) |
| `nix build .#omarchy-robot` | the command alone |
| `nix build .#omarchy-runtime` | the runtime alone; `nix run .#omarchy-runtime -- demo` runs it |

`.#flash` and `.#installer` follow the board set in `robot.nix`. With `board = "vm"` or `"pc"`, only `.#installer` exists.

Building a Jetson system on an x86_64 host needs `extra-platforms = aarch64-linux` and a QEMU binfmt handler (on NixOS, `boot.binfmt.emulatedSystems = [ "aarch64-linux" ]`). There is no public binary cache for jetpack-nixos, so the Tegra kernel compiles under emulation the first time.

## Layout

```
flake.nix            inputs pinned in flake.lock; every output
robot.nix            the one file a user edits
modules/             options, base, identity, jetson, vm, pc, runtime, installer, boards, branding
pkgs/omarchy-robot/  the command, its unit tests and fixtures
pkgs/omarchy-runtime/  the runtime: contracts, control plane, simulator, scenarios, tests
tests/               checks and VM tests
branding/            logo and wordmark
manual/              this manual
```

## Versions

Before 1.0, releases count up: v0.0.1, v0.0.2, v0.0.3. A new release comes only with a major feature. The first board booted and validated on hardware takes v0.1.0. The `validate` report schema and the runtime's trace schema are versioned separately. Each release's changes are in the [CHANGELOG](https://github.com/promaaa/omarchy-robots/blob/main/CHANGELOG.md), and each change in detail in the commit history.

## This manual

The pages are Markdown in `manual/`, one file per chapter, listed in order in `manual/_data/chapters.yml`. A push to `main` that touches `manual/` publishes it to GitHub Pages. When a change in the code makes a page wrong, fix the page in the same commit.

## License

MIT. jetpack-nixos is MIT too. The NVIDIA components it packages are under NVIDIA's licenses: they are built locally and never redistributed here.
