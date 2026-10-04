# Board "pc": a generic x86_64 UEFI computer. NixOS's own generic hardware
# support (all-hardware: SATA, NVMe, USB and virtio storage in the initrd, and
# the redistributable firmware that common network adapters load), plus CPU
# microcode for Intel and AMD. No GPU stack, no power modes.
{ config, lib, ... }:
{
  config = lib.mkIf (config.omarchy.robot.board == "pc") {
    nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
    hardware.enableAllHardware = true;
    hardware.cpu.intel.updateMicrocode = true;
    hardware.cpu.amd.updateMicrocode = true;
  };
}
