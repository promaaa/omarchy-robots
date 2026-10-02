# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0, see README).

## [Unreleased]


## [0.0.1] - 2026-10-02

### Part 1 (2026-10-02)

First release candidate of milestone OS-1: a declarative, Nix-based robot OS,
VM-validated and ready for Jetson bring-up. Not hardware-tested.

#### Added

- `robot.nix`, the one file that declares a robot, and the `omarchy.robot.*`
  options: name, board, storage device, administrator and SSH keys, wired
  network (DHCP or static), time zone, GPU stack, power mode, generations kept.
- Base system: headless, SSH keys only, firewall admitting SSH only, flakes,
  systemd-boot with a bounded number of entries, weekly garbage collection that
  keeps those generations, bounded journal, disko layout on `storage.device`.
- Boards: `vm` (QEMU, x86_64) and the Jetson developer kits `orin-agx-devkit`,
  `orin-nx-devkit`, `orin-nx-super-devkit`, `orin-nano-devkit`,
  `orin-nano-super-devkit` and `thor-agx-devkit`, mapped onto jetpack-nixos
  (JetPack 6.2.3 / L4T 36.5.2 on Orin, JetPack 7.2.1 / L4T 39.2.1 on Thor).
- Build identity in `/etc/omarchy-robot/build.json`: target, flake revision,
  robot.nix hash, input revisions, kernel, board and JetPack/L4T versions.
- `omarchy-robot` with `info`, `validate`, `update` and `rollback`.
  Validation report schema 1.
- Per-board flash scripts (jetpack-nixos, cross-built on x86_64) and
  installation images that carry the system they install (`omarchy-install`).
- Checks run by `nix flake check`: nixfmt, statix, deadnix, ruff, mypy, unit
  tests on fixtures, per-board evaluation assertions, refusal of invalid
  `robot.nix` combinations, and VM tests for the base system, update and
  rollback across a reboot, and installation onto a blank disk.
- GitHub Actions CI: `nix flake check` on x86_64; a manual arm64 job building
  one Jetson system.

[Unreleased]: https://github.com/promaaa/omarchy-robots/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/promaaa/omarchy-robots/releases/tag/v0.0.1
