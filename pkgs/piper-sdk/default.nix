# AgileX's official Python SDK for the PiPER arm, over CAN (python-can,
# socketcan, 1 Mbit/s): release 0.6.2 (2026-07-31), MIT. Not in nixpkgs.
# 0.6.2 imports typing_extensions without declaring it.
{
  buildPythonPackage,
  fetchFromGitHub,
  lib,
  python-can,
  setuptools,
  typing-extensions,
}:
buildPythonPackage {
  pname = "piper-sdk";
  version = "0.6.2";
  pyproject = true;
  src = fetchFromGitHub {
    owner = "agilexrobotics";
    repo = "piper_sdk";
    rev = "c9e8a28174e71eeaac448593cb65f8ab258a92fe"; # tag 0.6.2
    hash = "sha256-5Ntzy6juk67POiO0N4oZbXrtoX6rCDBP9iROTBkQYnk=";
  };
  build-system = [ setuptools ];
  dependencies = [
    python-can
    typing-extensions
  ];
  pythonImportsCheck = [ "piper_sdk" ];
  meta = {
    description = "AgileX PiPER robot arm SDK (CAN)";
    homepage = "https://github.com/agilexrobotics/piper_sdk";
    license = lib.licenses.mit;
  };
}
