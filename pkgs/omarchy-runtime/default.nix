# The runtime as a Python application: numpy is its only dependency. Its tests
# run in the flake check `omarchy-runtime`, not here, so the package also
# builds quickly for aarch64.
{
  lib,
  python3,
  revision ? null,
}:
python3.pkgs.buildPythonApplication {
  pname = "omarchy-runtime";
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
  dependencies = [ python3.pkgs.numpy ];
  # The source revision goes into every trace header ("<rev>" or "<rev>-dirty").
  makeWrapperArgs = lib.optionals (revision != null) [
    "--set"
    "OMARCHY_RUNTIME_REVISION"
    revision
  ];
  pythonImportsCheck = [ "omarchy_runtime" ];
  meta = {
    description = "Omarchy Robots runtime: goals in, verified executions and complete traces out (simulated)";
    license = lib.licenses.mit;
    mainProgram = "omarchy-runtime";
  };
}
