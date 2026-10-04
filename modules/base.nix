# Omarchy Robots defaults: what every robot is, whatever its board.
{
  config,
  lib,
  pkgs,
  modulesPath,
  ...
}:
let
  cfg = config.omarchy.robot;
  isJetson = (import ./boards.nix).${cfg.board} ? som;
in
{
  imports = [ (modulesPath + "/profiles/minimal.nix") ];

  config = {
    assertions = [
      {
        assertion = cfg.board != "vm" -> cfg.admin.sshKeys != [ ];
        message = ''
          omarchy.robot.board = "${cfg.board}" needs at least one key in omarchy.robot.admin.sshKeys.
          A robot has no password login, so without a key nobody could log in to it.'';
      }
      {
        assertion = cfg.gpu.enable -> isJetson;
        message = ''omarchy.robot.gpu.enable is for Jetson boards; board "${cfg.board}" has no GPU stack here.'';
      }
      {
        assertion = cfg.power.mode != null -> isJetson;
        message = ''omarchy.robot.power.mode selects a Jetson nvpmodel mode; board "${cfg.board}" has none.'';
      }
      {
        assertion = (cfg.network.gateway != null || cfg.network.dns != [ ]) -> cfg.network.address != null;
        message = "omarchy.robot.network.gateway and .dns go with a static .address; with DHCP leave them unset.";
      }
    ];

    networking.hostName = cfg.name;
    time.timeZone = cfg.timezone;
    system.stateVersion = "26.05";

    # Wired Ethernet on every Ethernet port, whatever the board names it.
    networking.useNetworkd = true;
    networking.useDHCP = false;
    systemd.network.networks."90-omarchy-wired" = {
      matchConfig.Type = "ether";
      networkConfig =
        if cfg.network.address == null then
          { DHCP = "yes"; }
        else
          {
            Address = cfg.network.address;
            Gateway = lib.mkIf (cfg.network.gateway != null) cfg.network.gateway;
            DNS = cfg.network.dns;
          };
    };
    systemd.network.wait-online.anyInterface = true;
    networking.firewall.enable = true;

    # One administrator, SSH keys only.
    users.mutableUsers = false;
    users.users.${cfg.admin.user} = {
      isNormalUser = true;
      extraGroups = [
        "wheel"
        "video"
        "render"
      ];
      openssh.authorizedKeys.keys = cfg.admin.sshKeys;
    };
    security.sudo.wheelNeedsPassword = false;
    services.openssh = {
      enable = true;
      openFirewall = true;
      settings = {
        PasswordAuthentication = false;
        KbdInteractiveAuthentication = false;
        PermitRootLogin = "no";
      };
    };

    # The installed disk: an EFI system partition and an ext4 root on storage.device.
    disko.devices.disk.main = {
      type = "disk";
      device = cfg.storage.device;
      content = {
        type = "gpt";
        partitions = {
          ESP = {
            size = "1G";
            type = "EF00";
            content = {
              type = "filesystem";
              format = "vfat";
              mountpoint = "/boot";
              mountOptions = [ "umask=0077" ];
            };
          };
          root = {
            size = "100%";
            content = {
              type = "filesystem";
              format = "ext4";
              mountpoint = "/";
            };
          };
        };
      };
    };

    # Boot entries and garbage collection both keep `generations` systems for rollback.
    # The menu shows for one second (any key holds it) instead of five.
    boot.loader.systemd-boot = {
      enable = true;
      configurationLimit = cfg.generations;
    };
    boot.loader.timeout = 1;
    boot.loader.efi.canTouchEfiVariables = true;
    nix.gc = {
      automatic = true;
      dates = "weekly";
    };
    systemd.services.nix-gc.preStart = ''
      ${config.nix.package}/bin/nix-env --profile /nix/var/nix/profiles/system \
        --delete-generations +${toString cfg.generations}
    '';

    nix.settings = {
      experimental-features = [
        "nix-command"
        "flakes"
      ];
      trusted-users = [
        "root"
        "@wheel"
      ];
    };
    nix.channel.enable = false;

    # Lighter and faster: users from userborn instead of the Perl script at every
    # activation, no nixos-generate-config (Perl), and no copy of the nixpkgs
    # source in the system (`nix run nixpkgs#...` then uses the global registry).
    services.userborn.enable = true;
    system.tools.nixos-generate-config.enable = false;
    nixpkgs.flake = {
      setNixPath = false;
      setFlakeRegistry = false;
    };

    # Bounded logs; time comes from systemd-timesyncd, on by default.
    services.journald.extraConfig = "SystemMaxUse=512M";

    environment.systemPackages = with pkgs; [
      curl
      git
      htop
      jq
      lsof
      nvme-cli
      omarchy-robot
      pciutils
      rsync
      tmux
      usbutils
      vim
    ];
  };
}
