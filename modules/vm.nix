# Board "vm": the robot as a QEMU virtual machine on an x86_64 host.
{ config, lib, ... }:
let
  cfg = config.omarchy.robot;
in
{
  config = lib.mkIf (cfg.board == "vm") {
    nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
    boot.initrd.availableKernelModules = [
      "virtio_pci"
      "virtio_blk"
      "virtio_scsi"
      "virtio_net"
    ];
    boot.kernelParams = [ "console=ttyS0,115200" ];
    # The VM's serial console is local to whoever runs it: log in as the administrator.
    services.getty.autologinUser = cfg.admin.user;

    # `nix run .#vm`: boots through UEFI and systemd-boot, like the hardware.
    virtualisation.vmVariant.virtualisation = {
      useBootLoader = true;
      useEFIBoot = true;
      graphics = false;
      memorySize = 2048;
      cores = 2;
      diskSize = 8192;
      forwardPorts = [
        {
          from = "host";
          host.port = 2222;
          guest.port = 22;
        }
      ];
    };
  };
}
