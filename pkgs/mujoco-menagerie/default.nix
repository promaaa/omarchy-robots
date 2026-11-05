# The Franka Emika Panda, Robotiq 2F-85 and AgileX PiPER models of the MuJoCo
# Menagerie, the official model collection of the MuJoCo project (Google
# DeepMind), at a pinned commit: only `franka_emika_panda/`, `robotiq_2f85/`,
# `agilex_piper/` and the repository's top-level files are fetched. The Panda
# model is Apache-2.0 (franka_emika_panda/LICENSE and its README), the 2F-85
# BSD-2-Clause (robotiq_2f85/LICENSE, from ROS-Industrial), the PiPER MIT
# (agilex_piper/LICENSE); other models of the Menagerie have their own
# licences and are not fetched. Used by the simulated scrubbing scenes and the
# lab's arms picking a cup (omarchy-runtime-tools).
{ fetchFromGitHub, lib }:
(fetchFromGitHub {
  owner = "google-deepmind";
  repo = "mujoco_menagerie";
  rev = "62ab00f2bc44f6f20c1c9b7a13f3fb1dd41a4e35"; # 2026-10-15
  sparseCheckout = [
    "franka_emika_panda"
    "robotiq_2f85"
    "agilex_piper"
  ];
  hash = "sha256-AAZ2IVWOgpW9Me4WwPD5p9qBBty/CM/GXn/eig9k5/o=";
}).overrideAttrs
  (old: {
    meta = (old.meta or { }) // {
      description = "MuJoCo Menagerie: the Franka Emika Panda, Robotiq 2F-85 and AgileX PiPER models (MJCF)";
      homepage = "https://github.com/google-deepmind/mujoco_menagerie/tree/main/franka_emika_panda";
      license = [
        lib.licenses.asl20
        lib.licenses.bsd2
        lib.licenses.mit
      ];
    };
  })
