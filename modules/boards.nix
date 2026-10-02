# Every value omarchy.robot.board accepts, and what it means.
#
# Jetson entries map onto jetpack-nixos (hardware.nvidia-jetpack.{som,carrierBoard,super});
# the combinations are the developer kits jetpack-nixos lists in its flake.nix.
# `carrier` and `modules` are device-tree part numbers that
# `omarchy-robot validate` looks for in /proc/device-tree/compatible.
# `cudaCapability` is the SoC's CUDA architecture (jetpack-nixos modules/cuda.nix).
let
  orin = {
    carrierBoard = "devkit";
    super = false;
    cudaCapability = "8.7";
  };
  orinNanoCarrier = orin // {
    carrier = "p3768";
  };
  orinNx = orinNanoCarrier // {
    som = "orin-nx";
    modules = [
      "p3767-0000"
      "p3767-0001"
    ];
  };
  orinNano = orinNanoCarrier // {
    som = "orin-nano";
    modules = [
      "p3767-0003"
      "p3767-0004"
      "p3767-0005"
    ];
  };
in
{
  # A QEMU virtual machine on an x86_64 host: everything but the Jetson layer.
  vm = { };

  # Jetson AGX Orin developer kit (P3701 module on the P3737 carrier).
  orin-agx-devkit = orin // {
    som = "orin-agx";
    carrier = "p3737";
    modules = [ "p3701" ];
  };

  # Jetson Orin NX module (P3767-0000/0001) on the Orin Nano developer kit carrier (P3768).
  orin-nx-devkit = orinNx;
  orin-nx-super-devkit = orinNx // {
    super = true;
  };

  # Jetson Orin Nano developer kit (P3767-0003/0004/0005 on P3768).
  # "super" is the Orin Nano Super developer kit firmware and power configuration.
  orin-nano-devkit = orinNano;
  orin-nano-super-devkit = orinNano // {
    super = true;
  };

  # Jetson AGX Thor developer kit (P3834 module on the P4071 carrier), JetPack 7.
  thor-agx-devkit = {
    som = "thor-agx";
    carrierBoard = "devkit";
    super = false;
    carrier = "p4071";
    modules = [ "p3834" ];
    cudaCapability = "11.0";
  };
}
