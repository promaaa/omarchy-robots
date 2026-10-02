# Jetson boards: glue from omarchy.robot.* onto jetpack-nixos, which owns the
# kernel, firmware, flashing and NVIDIA userspace.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.omarchy.robot;
  board = (import ./boards.nix).${cfg.board};
  cudaPackages = pkgs.nvidia-jetpack.cudaPackages;

  # What `omarchy-robot validate` runs for cuda.runtime and tensorrt.runtime.
  cudaProbe = pkgs.writeShellScriptBin "omarchy-cuda-probe" ''
    exec "$(find ${cudaPackages.cuda-samples} -type f -name deviceQuery -perm -u+x | head -n 1)" "$@"
  '';
  tensorrtProbe = pkgs.writeShellScriptBin "omarchy-tensorrt-probe" ''
    exec ${lib.getExe cudaPackages.tensorrt-samples.passthru.testers.sample_onnx_mnist.default} "$@"
  '';

  # NVIDIA's licenses (CUDA, cuDNN, TensorRT...; all published on nvidia.com)
  # are the only unfree ones a robot accepts.
  nvidiaLicense = l: l.free or true || lib.hasInfix "nvidia.com" (l.url or "");
in
{
  config = lib.mkIf (cfg.board != "vm") {
    nixpkgs.hostPlatform = lib.mkDefault "aarch64-linux";

    hardware.nvidia-jetpack = {
      enable = true;
      inherit (board) som carrierBoard super;
      # Turning this on sets cudaSupport for all of nixpkgs and rebuilds much of it
      # from source. The CUDA packages only need the capability below.
      configureCuda = false;
    };
    nixpkgs.config = lib.mkMerge [
      {
        allowUnfreePredicate = pkg: lib.all nvidiaLicense (lib.toList (pkg.meta.license or [ ]));
        # JetPack 6's TensorRT is flagged for CVE-2026-24188 (out-of-bounds write when
        # parsing a model); the fix (10.16.1) exists only for JetPack 7. Accepted for the
        # validation sample only, by exact name, so a TensorRT bump forces a new decision.
        permittedInsecurePackages = lib.optionals cfg.gpu.enable [ "cuda12.6-tensorrt-10.7.0.23" ];
      }
      # Selects the Jetson builds of NVIDIA's CUDA libraries and the GPU
      # architecture CUDA code is compiled for; cudaSupport stays off. Not when
      # cross-building (the flash scripts): they compile no CUDA, and their x86_64
      # build-platform CUDA set rejects a Jetson capability.
      (lib.mkIf (config.nixpkgs.buildPlatform.system == config.nixpkgs.hostPlatform.system) {
        cudaCapabilities = [ board.cudaCapability ];
      })
    ];

    hardware.graphics.enable = cfg.gpu.enable;
    environment.systemPackages = lib.optionals cfg.gpu.enable [
      cudaProbe
      tensorrtProbe
    ];

    services.nvpmodel.profileNumber = cfg.power.mode;
  };
}
