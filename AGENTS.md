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
nix run .#omarchy-runtime-tools -- run scrub_contact  # the simulated Panda (MuJoCo); also scrub-experiment, scrub-render
```

In `pkgs/omarchy-runtime`, inside `nix develop`:
`python3 -m unittest discover -s tests -t .`, `ruff check .`, `ruff format .`,
`mypy omarchy_runtime tests`.

## Layout

| Path | Component |
|---|---|
| `robot.nix`, `modules/` | the OS: options, base, boards (`vm`, `pc`, Jetson), identity (`build.json`), the runtime option |
| `pkgs/omarchy-robot/` | `omarchy-robot`: info, validate, update, rollback |
| `pkgs/omarchy-runtime/omarchy_runtime/` | the runtime core: `contracts.py` (all data and interfaces: intents, controller capabilities, the S0 and controller contracts), `runtime.py` (resolver, lifecycle, supervisor, verification), `belief.py`, `sched.py` (simulated and wall-clock schedulers), `trace.py`, `config.py`, `wallclock.py`, `link.py` (the link to an S2 in another process: TCP transport, robot and S2 ends) |
| `.../guard.py`, `.../s0.py`, `.../controllers.py` | S0: the guard (Omarchy's half: what S0 executes, limits, bounded steps, its own safe command), the loop that joins it to a controller and a robot (`S0Loop`, fidelity counters), the controllers (`motion_tracker`) |
| `.../attribution.py` | the fidelity check and failure attribution |
| `.../extensions.py` | the collaborator extension points and their registries |
| `.../s2.py` | the scripted S2, a client of the public API |
| `.../sim/` | the simulator: world (ground truth), S0, scripted S1, sensor, kinematics; `panda.py` the MuJoCo scene of the Franka Panda (tools only), `wipe.py` the scripted wiper (S1) |
| `.../scrub.py` | the scrubbing experiment, its tables and its videos (tools only) |
| `pkgs/mujoco-menagerie/` | the Menagerie's Panda model, pinned, fetched by Nix (Apache-2.0) |
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
7. S0 never waits: a valid chunk, else a valid fallback (S2's, else the
   capability's default), else HOLD. S0 is a guard and a controller: the
   guard, Omarchy's, picks what S0 executes and checks every command the
   controller returns (limits, bounded step, NaN, stale, force) before the
   actuators; no controller bypasses it. A controller honours only what its
   capability declares: anything else is refused (UNSUPPORTED_INTENT) or
   degraded in a mode the configuration names, and recorded.
   Every running execution holds a valid fallback from its start: the
   capability's default, computed on the robot side, and S2's prediction,
   sent with the goal. No gap: a fallback is replaced, never removed first.
8. Explicit failure: `OmarchyError` categories and outcome categories; no
   bare except, no silent pass, no None as an error.
9. Honesty: never call anything real-time; label every number (simulated,
   wall-clock in a VM, wall-clock on the host); never claim hardware support
   or a test that did not run.
10. Minimal: numpy is the only dependency of the robot's package; no plugin
    framework, message bus, database, web framework or ROS. The development
    tools (`sim/panda.py`, `scrub.py`: MuJoCo, the pinned Menagerie model,
    software rendering) are in `omarchy-runtime-tools` only, imported inside
    the command or the function that needs them (`tests/test_architecture.py`).
    Add an abstraction when two implementations or a named collaborator
    need it.
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

## Adding an S0 controller

1. Write a class with a `capability` (a `ControllerCapability`) and
   `command(intent, state, scene, now)` returning a `Command` of its declared
   kind, in `controllers.py`, and add it to `CONTROLLERS`. Plain numpy: it
   reads only the `RobotState` and the scene inputs it declares, never ground
   truth, and never touches the actuators.
2. Declare it for a body in `config/bodies.toml` (`[<body>.s0]`): command,
   rows' spacing (`dt_s`), motion spaces and frames, object forms, contact,
   scene streams. Degraded modes (`degraded`) only when a refusal would be
   wrong for that body; each is recorded.
3. The guard does not change for a new controller. In torque control the
   body gives the guard its torque and force limits and hold gains
   (`[<body>.kinematics.limits]`).
4. Test it behind the guard: `tests/test_guard.py` shows the adversarial
   pattern; a scenario shows it end to end with fidelity and attribution.

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

## Measured latencies: the distribution file

Measured delays enter the runtime as one JSON file each, in one format for an
S2 backend's time to first token and an S1 model's inference delay
(`omarchy_runtime/latency.py`). The runtime samples them at random with
replacement, from the run's seed; it never invents or fits a distribution.

```text
{
  "format": "omarchy-latency/1",
  "kind": "s1_inference",
  "label": "measured on board",
  "unit": "s",
  "samples": [<seconds>, <seconds>, ...],
  "provenance": {
    "date": "<YYYY-MM-DD>",
    "host": {"cpu": "...", "gpu": "...", "os": "..."},
    "model": {"name": "...", "weights": "...", "sha256": "..."},
    "method": "what was timed, from which event to which, how many runs, warm-up",
    "input": "what the model was given: image size, prompt, action horizon"
  }
}
```

- `kind`: `s2_ttft` (written by `omarchy-runtime ttft-measure`) or
  `s1_inference` (from a collaborator's model). `samples`: at least 5
  successful measurements, in seconds; failures are not samples (a file may
  list them under its own key). `label` starts with "measured" and says
  where: "measured on host", "measured on board". An `s2_ttft` file also names
  `backend`, `prompt_sha256` and `network_path` in its provenance.
- Use: `omarchy-runtime inference-matrix --delay-file FILE` adds a row of
  S1 delays drawn from the file to the inference matrix; `[s1] latency_file`
  in a scenario does the same for one run; `--ttft-file FILE` (run, s2) and
  the placement experiment draw S2's TTFT from an `s2_ttft` file. A file
  committed under `omarchy_runtime/data/latency/` can be named without its
  path. `latency.load` refuses a file that breaks these rules.
- Minjae Lee's VLA inference delays go in an `s1_inference` file with his
  own provenance; nothing in this repository stands in for them.

## Testing

- Each scenario file states the expected outcome of each execution;
  `omarchy-runtime eval` and `tests/test_scenarios.py` hold the runtime to it
  with ground truth, S0's per-tick record and the trace.
- The same scenario and seed give the same trace, wall keys aside
  (`tests/test_trace.py`).
- `tests/test_intent.py` holds contract v2 to its rules (each channel
  validated alone, v1 as a motion-only intent, refusals and degraded modes
  recorded); `tests/test_guard.py` drives the guard with adversarial
  controllers, property style; `tests/test_attribution.py` produces each
  attribution category from a scenario.
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
75 to 85 min on the development host instead of about 25 min with KVM.
Building a Jetson system on an x86_64 host needs aarch64 emulation (the
Tegra kernel takes hours; the development host has qemu-aarch64 in
binfmt_misc and `extra-platforms = aarch64-linux`). For long runs, check a
committed revision (`git+file://$PWD?rev=...`) so that edits do not disturb
the evaluation.

## Checks and releases

Everything runs locally, on the development host; nothing relies on GitHub
Actions. The workflows in `.github/workflows` are manual-only
(`workflow_dispatch`) and stay so: do not add push, pull request or schedule
triggers, and do not change repository or billing settings.

- After each change: the relevant checks, `nix build -L
  .#checks.x86_64-linux.<check>` (the runtime's tests: `omarchy-runtime`;
  formatting: `format`, `statix`, `deadnix`; the manual: `manual`; each VM
  test the change touches).
- Before every release: the full `nix flake check -L --keep-going --max-jobs
  1 "git+file://$PWD?rev=<commit>"` on the commit to tag. Tag only when it
  passes.
- Release evidence replaces CI links, in the release notes and the docs:
  the commit, the command, the start time and duration, the host (CPU, RAM,
  KVM or not) and the result. The logs stay outside the repository, in
  `~/src/omarchy-robots-evidence/<version>/`, with a README listing the runs.
- Jetson builds: locally, by aarch64 emulation, in the background (hours).
- Experiments (the placement and inference matrices): locally. Label every
  number from VMs on this host "measured in VMs on the host, without KVM
  (QEMU TCG)". When a wall-clock tolerance fails only because of the
  emulation, report it and ask before changing the tolerance.
- The manual: `nix build .#checks.x86_64-linux.manual` builds it and fails
  if a page keeps Markdown as text; check it renders (`result/index.html`)
  without depending on the website.
