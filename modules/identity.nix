# /etc/omarchy-robot/build.json: which build this system is, and what
# `omarchy-robot validate` should find on it.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.omarchy.robot;
  board = (import ./boards.nix).${cfg.board};
  jetpack = config.hardware.nvidia-jetpack;
in
{
  options.omarchy.robot.build = lib.mkOption {
    type = lib.types.attrsOf lib.types.anything;
    internal = true;
    default = { };
    description = "Set by flake.nix: target name, Omarchy Robots version, flake revision, robot.nix hash and input revisions.";
  };

  config.environment.etc."omarchy-robot/build.json".text = builtins.toJSON (
    cfg.build
    // {
      schema = 1;
      nixos = config.system.nixos.version;
      kernel = config.boot.kernelPackages.kernel.modDirVersion;
      robot = {
        inherit (cfg) name board generations;
        storage = cfg.storage.device;
        gpu = cfg.gpu.enable;
        powerMode = cfg.power.mode;
      };
      jetson =
        if cfg.board == "vm" then
          null
        else
          {
            inherit (jetpack) som carrierBoard super;
            inherit (board) carrier modules cudaCapability;
            jetpack = pkgs.nvidia-jetpack.jetpackMajorMinorPatchVersion;
            l4t = pkgs.nvidia-jetpack.l4tMajorMinorPatchVersion;
            inherit (pkgs.nvidia-jetpack) gpuDriver;
          };
    }
  );
}
