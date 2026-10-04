# Working on Omarchy Robots

For humans and coding agents. Omarchy Robots is a NixOS robot OS on
jetpack-nixos (`robot.nix` declares a robot) and, on top of it, a runtime
between intelligence and robot bodies. The runtime is simulated: no robot,
nothing real-time. README holds the runbook and the support matrix, the
manual (`manual/`) the user guide, CHANGELOG the releases.

## Rules

- **No em-dashes**, anywhere: code, comments, docs, commit messages, tags,
  release notes. Use a colon, a comma, parentheses or a new sentence.
- Never claim a level you did not verify (evaluates, builds, VM-tested,
  hardware-tested). No Jetson and no PC has run this yet.
- Small, coherent commits that pass `nix flake check`, in Conventional
  Commits style; `flake.lock` updates in their own commits. Do not
  force-push or rewrite published history without asking.
- Releases are source only: no images, no NVIDIA binaries. A release bumps
  `version` in `flake.nix` and in `pkgs/omarchy-runtime/pyproject.toml` (a
  check keeps them equal), adds its CHANGELOG section and an annotated tag.
  The first board validated on hardware takes v0.1.0.
- The logos in `branding/` come from the maintainer; regenerate `boot.svg`
  and `logo.txt` when they change. See NOTICE for the trademark note.

## Commands

```sh
nix flake check                    # everything, VM tests included
nix build .#checks.x86_64-linux.omarchy-runtime   # the runtime: lint, types, all tests, under a minute
nix fmt                            # nixfmt
nix develop                        # nixfmt, statix, deadnix, ruff, python3 with numpy and mypy
nix run .#omarchy-runtime -- demo  # or eval, run <scenario>, show <run>, validate-trace <run>
```

In `pkgs/omarchy-runtime`, inside `nix develop`:
`python3 -m unittest discover -s tests -t .`, `ruff check .`, `ruff format .`,
`mypy omarchy_runtime tests`.

## Layout

| Path | Component |
|---|---|
| `robot.nix`, `modules/` | the OS: options, base, boards (`vm`, `pc`, Jetson), identity (`build.json`), the runtime option |
| `pkgs/omarchy-robot/` | `omarchy-robot`: info, validate, update, rollback |
| `pkgs/omarchy-runtime/omarchy_runtime/` | the runtime core: `contracts.py` (all data and interfaces), `runtime.py` (resolver, lifecycle, supervisor, verification), `belief.py`, `sched.py`, `trace.py`, `config.py`, `wallclock.py` |
| `.../extensions.py` | the collaborator extension points and their registries |
| `.../s2.py` | the scripted S2, a client of the public API |
| `.../sim/` | the simulator: world (ground truth), S0, scripted S1, sensor, kinematics |
| `.../scenario.py`, `evaluate.py`, `cli.py` | composition, evaluation harness, command |
| `.../config/`, `.../scenarios/` | bodies, capabilities and bindings, runtime parameters, scenarios (TOML) |
| `pkgs/omarchy-runtime/tests/` | unit, contract, scenario, trace and architecture tests |
| `tests/` | flake checks and NixOS VM tests |
| `manual/`, `branding/` | the manual (GitHub Pages), logos |

`.github/CODEOWNERS` gives everything to @promaaa for now. Add collaborators
there when their handles are known.

## Binding principles of the runtime

1. Control plane and data plane apart: chunks go from S1 to S0 directly; S0
   ticks never enter events or logs; the runtime samples S0's status at its
   own rate.
2. Contracts apart from implementations: core modules never import `sim/`,
   `scenario`, `evaluate` or `cli` (`tests/test_architecture.py`).
3. Goals at the boundary: applications and S2 send goal predicates in JSON,
   never robot verbs.
4. Belief, not ground truth: the runtime knows only filtered observations;
   only `sim/` and the evaluation harness read the simulator's world.
5. Independent verification: claim and verdict are recorded apart;
   SUCCEEDED needs a SATISFIED verdict from observations taken after the
   claim; INCONCLUSIVE is never success.
6. Injected time: components get `now` from the scheduler; only
   `wallclock.py` reads the wall clock, for overhead and wall stamps.
7. S0 never waits: a valid chunk, else a valid fallback, else HOLD; S0
   enforces the body's limits itself.
8. Explicit failure: `OmarchyError` categories and outcome categories; no
   bare except, no silent pass, no None as an error.
9. Honesty: never call anything real-time; label every number (simulated,
   wall-clock in a VM, wall-clock on the host); never claim hardware support
   or a test that did not run.
10. Minimal: numpy is the only dependency; no plugin framework, message bus,
    database, web framework or ROS. Add an abstraction when two
    implementations or a named collaborator need it.
11. One product: the runtime is a Nix package enabled from `robot.nix`; its
    traces name the OS build, or say there is none.

## Adding a Verifier, ObservationFilter, RecoveryPolicy or latency model

1. Write a class with `id`, `version` and the method its interface in
   `contracts.py` names, in a new module next to `extensions.py`.
2. Add it to the registry in `extensions.py` (`VERIFIERS`, `FILTERS`,
   `RECOVERY`, `LATENCY`).
3. Select it by id in `config/runtime.toml` `[extensions]`, or in one
   scenario's `[extensions]` table. Verifiers are chosen per predicate.
4. Reuse the contract tests: mix `VerifierContract`, `FilterContract`,
   `RecoveryContract` or `LatencyContract` from `tests/contracts.py` into a
   `unittest.TestCase` with a `make()` method, as `tests/test_extensions.py`
   does for the defaults.
5. Run `omarchy-runtime eval`: every trace header names the new id and version.

`runtime.py`, `belief.py` and `contracts.py` do not change for this. A change
to a contract changes every implementation: say so in the commit and the
CHANGELOG.

## Testing

- Each scenario file states the expected outcome of each execution;
  `omarchy-runtime eval` and `tests/test_scenarios.py` hold the runtime to it
  with ground truth, S0's per-tick record and the trace.
- The same scenario and seed give the same trace, wall keys aside
  (`tests/test_trace.py`).
- `tests/runtime.nix` runs nominal_a and slip_retry in a vm-board VM with the
  runtime enabled.
- Never weaken or delete a check to make it pass unless the check is wrong,
  and say so in the commit.

## Hosts

VM tests want KVM; without it QEMU emulates (TCG) and the full check takes
over an hour instead of about 25 min in CI. Building a Jetson system on an
x86_64 host needs aarch64 emulation (the Tegra kernel takes hours). For long
runs, check a committed revision (`git+file://$PWD?rev=...`) so that edits do
not disturb the evaluation.
