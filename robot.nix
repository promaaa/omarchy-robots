# robot.nix: the one file that declares this robot.
#
# Set the options below; every other file in this repository is the system they
# configure. This file is a NixOS module, so any plain NixOS option can also be
# set here when nothing under omarchy.robot fits.
#
# Build and deploy: see README.md. The flake output for this file is .#robot.
{
  omarchy.robot = {
    # Hostname.
    name = "robot";

    # What the robot runs on: one line to change on bring-up day.
    #   "vm"                      QEMU virtual machine on an x86_64 host
    #   "pc"                      generic x86_64 UEFI computer (not hardware-tested)
    #   "orin-agx-devkit"         Jetson AGX Orin developer kit
    #   "orin-nx-devkit"          Jetson Orin NX module on the Orin Nano developer kit carrier
    #   "orin-nx-super-devkit"    the same, with the "super" firmware and power modes
    #   "orin-nano-devkit"        Jetson Orin Nano developer kit
    #   "orin-nano-super-devkit"  Jetson Orin Nano Super developer kit
    #   "thor-agx-devkit"         Jetson AGX Thor developer kit (JetPack 7)
    board = "vm";

    # Whole disk the installer erases and installs onto. Defaults to /dev/vda on
    # the vm board and /dev/nvme0n1 on a Jetson or a pc.
    # storage.device = "/dev/nvme0n1";

    # The administrator account. There are no passwords: a Jetson or pc board
    # refuses to build without at least one key.
    admin.user = "admin";
    admin.sshKeys = [
      # The maintainer's workstation key. Replace it with your own public key(s).
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF3xSzpL+yEHf6RlCz/hPswiPiJloVmAZPaY8B9y9Y/g promaa@archy"
    ];

    # Wired Ethernet uses DHCP. For a static address set all three:
    # network.address = "192.168.1.20/24";
    # network.gateway = "192.168.1.1";
    # network.dns = [ "1.1.1.1" ];

    timezone = "UTC";

    # Jetson only: NVIDIA GPU userspace plus the CUDA and TensorRT samples that
    # `omarchy-robot validate` runs. Large download and build.
    gpu.enable = false;

    # Jetson only: nvpmodel power mode ID (board specific; null keeps the default).
    power.mode = null;

    # System generations kept for rollback (boot entries, garbage collection).
    generations = 10;
  };
}
