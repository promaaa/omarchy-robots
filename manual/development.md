---
layout: default
title: Development
permalink: /development/
---

Everything is a Nix flake. One command checks the whole project:

```sh
nix flake check     # format, statix, deadnix, ruff, mypy, unit tests,
                    # every board evaluated, VM tests (base, update/rollback, installer)
nix fmt             # nixfmt on every .nix file
nix develop         # a shell with nixfmt, statix, deadnix, ruff, mypy, python3
```

The VM tests need the `nixos-test` and `kvm` system features in `nix.conf`. CI runs `nix flake check` on every push; building the `orin-agx-devkit` system on an arm64 runner is a manual workflow input, because it takes about an hour.

## What you can build

Every artifact comes from `robot.nix`. `<board>` is any Jetson board.

| Command | Artifact |
|---|---|
| `nix build .#nixosConfigurations.robot.config.system.build.toplevel` | the robot's system, as `robot.nix` declares it |
| `nix build .#nixosConfigurations.<board>.config.system.build.toplevel` | the same system on `<board>` |
| `nix build .#flash` or `.#flash-<board>` | the firmware flash script (x86_64 host, USB recovery mode) |
| `nix build .#packages.aarch64-linux.installer` or `.installer-<board>` | the installation image for a Jetson, carrying its system |
| `nix build .#installer-vm` | the installation image for the `vm` board |
| `nix build .#vm` | the `vm` board as a QEMU VM (`nix run .#vm`) |
| `nix build .#omarchy-robot` | the command alone |

`.#flash` and `.#installer` follow the board set in `robot.nix`. With `board = "vm"`, only `.#installer` exists.

Building a Jetson system on an x86_64 host needs `extra-platforms = aarch64-linux` and a QEMU binfmt handler (on NixOS, `boot.binfmt.emulatedSystems = [ "aarch64-linux" ]`). There is no public binary cache for jetpack-nixos, so the Tegra kernel compiles under emulation the first time.

## Layout

```
flake.nix            inputs pinned in flake.lock; every output
robot.nix            the one file a user edits
modules/             options, base, identity, jetson, vm, installer, boards, branding
pkgs/omarchy-robot/  the command, its unit tests and fixtures
tests/               checks and VM tests
branding/            logo and wordmark
manual/              this manual
```

## Versions

Before 1.0, releases count up: v0.0.1 part 3, v0.0.1 part 4, and so on. This milestone is VM-validated: Jetson targets evaluate and build, nothing hardware-tested. **v0.1.0 is reserved for the first board booted and validated on hardware.** The `validate` report schema is versioned separately. Every change is in the [CHANGELOG](https://github.com/promaaa/omarchy-robots/blob/main/CHANGELOG.md).

## This manual

The pages are Markdown in `manual/`, one file per chapter, listed in order in `manual/_data/chapters.yml`. A push to `main` that touches `manual/` publishes it to GitHub Pages. When a change in the code makes a page wrong, fix the page in the same commit.

## License

MIT. jetpack-nixos is MIT too. The NVIDIA components it packages are under NVIDIA's licenses: they are built locally and never redistributed here.
