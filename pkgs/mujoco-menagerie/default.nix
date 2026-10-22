# The Franka Emika Panda and Robotiq 2F-85 models of the MuJoCo Menagerie,
# the official model collection of the MuJoCo project (Google DeepMind), at a
# pinned commit: only `franka_emika_panda/`, `robotiq_2f85/` and the
# repository's top-level files are fetched. The Panda model is Apache-2.0
# (franka_emika_panda/LICENSE and its README), the 2F-85 BSD-2-Clause
# (robotiq_2f85/LICENSE, from ROS-Industrial); other models of the Menagerie
# have their own licences and are not fetched. Used by the simulated
# scrubbing scenes (omarchy-runtime-tools); the 2F-85 is drawn only.
{ fetchFromGitHub, lib }:
(fetchFromGitHub {
  owner = "google-deepmind";
  repo = "mujoco_menagerie";
  rev = "62ab00f2bc44f6f20c1c9b7a13f3fb1dd41a4e35"; # 2026-10-15
  sparseCheckout = [
    "franka_emika_panda"
    "robotiq_2f85"
  ];
  hash = "sha256-o0dmet5lnNltBrWCI5jaZaCWAdznMMyDNxv0ZutmXfc=";
}).overrideAttrs
  (old: {
    meta = (old.meta or { }) // {
      description = "MuJoCo Menagerie: the Franka Emika Panda and Robotiq 2F-85 models (MJCF)";
      homepage = "https://github.com/google-deepmind/mujoco_menagerie/tree/main/franka_emika_panda";
      license = [
        lib.licenses.asl20
        lib.licenses.bsd2
      ];
    };
  })
