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
| `pkgs/omarchy-runtime/omarchy_runtime/` | the runtime core: `contracts.py` (all data and interfaces), `runtime.py` (resolver, lifecycle, supervisor, verification), `belief.py`, `sched.py` (simulated and wall-clock schedulers), `trace.py`, `config.py`, `wallclock.py`, `link.py` (the link to an S2 in another process: TCP transport, robot and S2 ends) |
| `.../extensions.py` | the collaborator extension points and their registries |
| `.../s2.py` | the scripted S2, a client of the public API |
| `.../sim/` | the simulator: world (ground truth), S0, scripted S1, sensor, kinematics |
| `.../scenario.py`, `evaluate.py`, `placement.py`, `cli.py` | composition (the robot process, the S2 process), evaluation harness, the S2 placement experiment's table, command |
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
   `wallclock.py` reads the wall clock or sleeps, for overhead, wall stamps
   and the wall-clock scheduler.
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
   `RECOVERY`, `LATENCY`). A latency model is made from a median time to
   first token and a spread: `(seconds, spread)`.
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

## Asynchronous inference: a ChunkCorrector or an InferenceTrigger

The seam for asynchronous VLA inference (chunk post-correction, re-inference
triggering). Both run on the robot side, in S1's host, never in the runtime.

- A **ChunkCorrector** (`contracts.ChunkCorrector.correct(chunk, history,
  state, now)`) is applied when a new chunk is handed to S0 and returns the
  chunk S0 executes. It receives the chunk (with `observed_at`, when its
  observation was taken, and `inference_ns`, how long its inference took),
  `history` (an `S0History`: S0's ticks since `observed_at`, the commanded
  and desired positions as arrays) and S0's current `S0Status`.
- An **InferenceTrigger** (`contracts.InferenceTrigger.due(context)`) is
  asked at S1's rate whether S1 infers now. `context` (`InferenceContext`)
  holds S0's status, its last 0.5 s of history, the inferences on their
  way, the last measured inference latency and S1's period.
- Timing budget: each call returns within about 0.5 ms (the contract tests
  measure it). A corrector runs in S0's loop.
- They must not block, must not read ground truth (the simulator, `sim/`,
  the harness), and must not call S0: they receive data and return data. A
  corrector keeps the chunk's seq, source, action space and observation
  time; a trigger must ask for an inference when S0 has no chunk and none
  is on its way. One that raises fails the execution, never S0.
- Baselines in `extensions.py`: correctors `none` and `blend` (a naive
  linear blend, a stand-in for smoothing methods such as Real-Time
  Chunking), triggers `periodic` and `mismatch`, in the registries
  `CORRECTORS` and `INFERENCE_TRIGGERS`. `runtime.toml` chooses them by id
  (`corrector`, `trigger` in `[extensions]`); each factory takes the
  `[s1_host]` table, the place for a new one's parameters.
- Tests: mix `CorrectorContract` or `TriggerContract` from
  `tests/contracts.py` into a test case, as `tests/test_extensions.py` does.
  Then `omarchy-runtime inference-matrix` runs S1's inference delay (50,
  150, 300 ms) x corrector x trigger on nominal_a and cup_moved in
  simulated time and prints success, starvations, inferences, the
  discontinuity at chunk switches, reaction and freeze time, for every
  registered corrector and trigger: a new one joins the matrix once it is
  in its registry. A baseline, not a claim about which method is best.

## Testing

- Each scenario file states the expected outcome of each execution;
  `omarchy-runtime eval` and `tests/test_scenarios.py` hold the runtime to it
  with ground truth, S0's per-tick record and the trace.
- The same scenario and seed give the same trace, wall keys aside
  (`tests/test_trace.py`).
- `tests/runtime.nix` runs nominal_a and slip_retry in a vm-board VM with the
  runtime enabled.
- `tests/s2-placement.nix` runs the S2 placement cells: a robot VM, an edge
  VM, the link shaped with tc netem, S2 in its own process. `nix flake check`
  runs 3 profiles x 2 TTFTs; `nix build .#s2-placement` every cell (KVM).
  Wall-clock runs are not bit-identical: they are held to the simulated
  run's outcomes exactly and to its task time within a stated tolerance.
- Never weaken or delete a check to make it pass unless the check is wrong,
  and say so in the commit.

## Hosts

VM tests want KVM; without it QEMU emulates (TCG) and the full check takes
over an hour instead of about 25 min in CI. Building a Jetson system on an
x86_64 host needs aarch64 emulation (the Tegra kernel takes hours). For long
runs, check a committed revision (`git+file://$PWD?rev=...`) so that edits do
not disturb the evaluation.
