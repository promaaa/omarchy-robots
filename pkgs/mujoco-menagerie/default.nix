# The Franka Emika Panda model of the MuJoCo Menagerie, the official model
# collection of the MuJoCo project (Google DeepMind), at a pinned commit:
# only `franka_emika_panda/` and the repository's top-level files are
# fetched. The Panda model is Apache-2.0 (franka_emika_panda/LICENSE and its
# README); other models of the Menagerie have their own licences and are not
# fetched. Used by the simulated scrubbing scene (omarchy-runtime-tools).
{ fetchFromGitHub, lib }:
(fetchFromGitHub {
  owner = "google-deepmind";
  repo = "mujoco_menagerie";
  rev = "62ab00f2bc44f6f20c1c9b7a13f3fb1dd41a4e35"; # 2026-10-15
  sparseCheckout = [ "franka_emika_panda" ];
  hash = "sha256-vmug21NrlzDTCD2yTUeCkNMOPIvSZ74nBFSCRJ7HNVc=";
}).overrideAttrs
  (old: {
    meta = (old.meta or { }) // {
      description = "MuJoCo Menagerie: the Franka Emika Panda model (MJCF)";
      homepage = "https://github.com/google-deepmind/mujoco_menagerie/tree/main/franka_emika_panda";
      license = lib.licenses.asl20;
    };
  })
