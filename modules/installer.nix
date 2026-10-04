# The installation image for a robot system `target` (a nixosSystem built from
# robot.nix). It boots on the robot's board with jetpack-nixos's kernel, takes
# the administrator's SSH keys, and carries the target system, so
# `omarchy-install` erases storage.device and installs with no network and no
# build on the robot.
target:
{
  lib,
  modulesPath,
  pkgs,
  ...
}:
let
  robot = target.config.omarchy.robot;
  isJetson = (import ./boards.nix).${robot.board} ? som;
  install = pkgs.writeShellApplication {
    name = "omarchy-install";
    text = ''
      device=${robot.storage.device}
      if [ "''${1:-}" != --yes ]; then
        lsblk "$device"
        read -r -p "Erase $device and install ${robot.name} (${robot.board})? Type yes: " answer
        [ "$answer" = yes ] || exit 1
      fi
      ${target.config.system.build.diskoScript}
      # Everything comes from this image's store: no binary cache, no network.
      nixos-install --no-root-passwd --no-channel-copy --option substituters "" \
        --system ${target.config.system.build.toplevel}
      echo "Installed ${robot.name} on $device. Remove the installation medium and reboot."
    '';
  };
in
{
  imports = [
    (modulesPath + "/installer/cd-dvd/installation-cd-minimal.nix")
    ./options.nix
    ./jetson.nix
    ./branding.nix
  ];

  # Same board, hence same jetpack-nixos kernel and firmware expectations.
  omarchy.robot = {
    inherit (robot) board;
    build.version = robot.build.version or "dev";
  };
  omarchy.branding.motd = [
    "Omarchy Robots ${robot.build.version or "dev"} installer"
    "${robot.name} · ${robot.board} → ${robot.storage.device}"
    ""
    "omarchy-install   erase ${robot.storage.device} and install ${robot.name}"
  ];
  nixpkgs.hostPlatform = target.pkgs.stdenv.hostPlatform.system;
  # Like jetpack-nixos's installer: the Tegra kernel lacks generic PC drivers.
  hardware.enableAllHardware = lib.mkIf isJetson (lib.mkForce false);
  boot.supportedFilesystems.zfs = lib.mkForce false;

  networking.hostName = "${robot.name}-installer";
  # Headless: reach it as root@<name>-installer.local with an administrator key.
  users.users.root.openssh.authorizedKeys.keys = robot.admin.sshKeys;
  # Keys only: the installer profile gives root an empty password.
  services.openssh.settings = {
    PasswordAuthentication = false;
    KbdInteractiveAuthentication = false;
  };
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    publish = {
      enable = true;
      addresses = true;
    };
  };

  environment.systemPackages = [ install ];
  isoImage.storeContents = [ target.config.system.build.toplevel ];
  # The Omarchy boot screen instead of NixOS's menu artwork.
  isoImage.grubTheme = null;
  isoImage.efiSplashImage = "${pkgs.omarchy-branding}/boot.png";
  isoImage.splashImage = "${pkgs.omarchy-branding}/splash-640x480.png";
  # Faster to build than the default on an emulated aarch64 builder; the image is larger.
  isoImage.squashfsCompression = "zstd -Xcompression-level 6";
  image.baseName = lib.mkForce "omarchy-${robot.name}-${robot.board}-installer";
}
