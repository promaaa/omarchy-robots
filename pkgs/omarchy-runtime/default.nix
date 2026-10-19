# The runtime as a Python application: numpy is its only dependency. Its tests
# run in the flake check `omarchy-runtime`, not here, so the package also
# builds quickly for aarch64.
#
# With `tools`, the development tools besides: the MuJoCo scene of the
# simulated Franka Panda (the scrubbing scenarios, their experiment and
# videos), with the pinned Menagerie model and software rendering (Mesa's
# llvmpipe through EGL: no display, no GPU). They are never in a robot's
# system; omarchy.robot.runtime installs the package without them.
{
  lib,
  python3,
  ffmpeg-headless,
  libglvnd,
  mesa,
  mujoco-menagerie,
  revision ? null,
  tools ? false,
}:
python3.pkgs.buildPythonApplication {
  pname = "omarchy-runtime" + lib.optionalString tools "-tools";
  inherit ((lib.importTOML ./pyproject.toml).project) version;
  pyproject = true;
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./omarchy_runtime
    ];
  };
  build-system = [ python3.pkgs.setuptools ];
  dependencies = [
    python3.pkgs.numpy
  ]
  ++ lib.optionals tools (
    with python3.pkgs;
    [
      mujoco
      # The PiPER's SDK and CAN, for the fake arm's tests and the lab's first contact.
      python-can
      piper-sdk
    ]
  );
  # The source revision goes into every trace header ("<rev>" or "<rev>-dirty").
  makeWrapperArgs =
    lib.optionals (revision != null) [
      "--set"
      "OMARCHY_RUNTIME_REVISION"
      revision
    ]
    ++ lib.optionals tools [
      "--set"
      "OMARCHY_MENAGERIE"
      "${mujoco-menagerie}"
      "--set-default"
      "MUJOCO_GL"
      "egl"
      "--set-default"
      "__EGL_VENDOR_LIBRARY_FILENAMES"
      "${mesa}/share/glvnd/egl_vendor.d/50_mesa.json"
      "--prefix"
      "LD_LIBRARY_PATH"
      ":"
      (lib.makeLibraryPath [
        libglvnd
        mesa
      ])
      "--prefix"
      "PATH"
      ":"
      (lib.makeBinPath [ ffmpeg-headless ])
    ];
  pythonImportsCheck = [ "omarchy_runtime" ] ++ lib.optionals tools [ "omarchy_runtime.sim.panda" ];
  meta = {
    description =
      "Omarchy Robots runtime: goals in, verified executions and complete traces out (simulated)"
      + lib.optionalString tools "; with the MuJoCo scene of the simulated Franka Panda";
    license = lib.licenses.mit;
    mainProgram = "omarchy-runtime";
  };
}
