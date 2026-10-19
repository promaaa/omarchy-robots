# libfranka 0.9.2 (2022-08-31), the last release for the Franka Emika Panda
# (robot system 4.2.1 and later; 0.10 and later are for Franka Research 3
# only), Apache-2.0. Not in nixpkgs. With its examples: `echo_robot_state`
# reads the robot's state without moving it (the first-contact runbook);
# the others move the robot. Built, never run against a robot here.
{
  cmake,
  eigen,
  fetchFromGitHub,
  lib,
  poco,
  stdenv,
}:
stdenv.mkDerivation {
  pname = "libfranka";
  version = "0.9.2";
  src = fetchFromGitHub {
    owner = "frankarobotics";
    repo = "libfranka";
    rev = "0.9.2";
    fetchSubmodules = true;
    hash = "sha256-xPzzJ4YlRz7MVRgcZaV3QhlOrUFlajJLaArchBylCQM=";
  };
  nativeBuildInputs = [ cmake ];
  buildInputs = [
    eigen
    poco
  ];
  cmakeFlags = [
    (lib.cmakeBool "BUILD_TESTS" false)
    (lib.cmakeBool "BUILD_EXAMPLES" true)
    (lib.cmakeFeature "CMAKE_BUILD_TYPE" "Release")
    # 0.9.2 asks for CMake 3.4; CMake 4 accepts nothing older than 3.5.
    (lib.cmakeFeature "CMAKE_POLICY_VERSION_MINIMUM" "3.5")
    # The examples are copied from the build tree: they find the library where it is installed.
    (lib.cmakeBool "CMAKE_BUILD_WITH_INSTALL_RPATH" true)
    (lib.cmakeFeature "CMAKE_INSTALL_RPATH" "${placeholder "out"}/lib")
  ];
  postInstall = ''
    mkdir -p $out/bin
    find examples -maxdepth 1 -type f -executable -exec cp {} $out/bin/ \;
  '';
  meta = {
    description = "C++ library for the Franka Control Interface (FCI), for the Franka Emika Panda";
    homepage = "https://github.com/frankarobotics/libfranka";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux;
  };
}
