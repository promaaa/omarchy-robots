# The omarchy.robot.* interface: the only options robot.nix is meant to set.
# Their descriptions are the configuration reference.
{ config, lib, ... }:
let
  inherit (lib) mkOption types;
  cfg = config.omarchy.robot;
  boards = import ./boards.nix;
in
{
  options.omarchy.robot = {
    name = mkOption {
      type = types.strMatching "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?";
      default = "robot";
      description = "Hostname of the robot (lowercase letters, digits and dashes).";
    };

    board = mkOption {
      type = types.enum (builtins.attrNames boards);
      default = "vm";
      description = ''
        What the robot runs on. "vm" is a QEMU virtual machine on an x86_64 host,
        "pc" a generic x86_64 UEFI computer; every other value is a Jetson
        developer kit, mapped onto jetpack-nixos in modules/boards.nix. Changing
        boards is this one line.
      '';
    };

    storage.device = mkOption {
      type = types.str;
      default = if cfg.board == "vm" then "/dev/vda" else "/dev/nvme0n1";
      defaultText = lib.literalExpression ''if board == "vm" then "/dev/vda" else "/dev/nvme0n1"'';
      description = ''
        Whole disk the system is installed on. The installer erases it and creates
        an EFI system partition and an ext4 root; `omarchy-robot validate` checks
        that the root filesystem lives on it.
      '';
    };

    admin = {
      user = mkOption {
        type = types.strMatching "[a-z_][a-z0-9_-]*";
        default = "admin";
        description = "Login of the administrator account (in group wheel, passwordless sudo).";
      };
      sshKeys = mkOption {
        type = types.listOf types.singleLineStr;
        default = [ ];
        description = ''
          SSH public keys allowed to log in as the administrator. Logging in with a
          password is impossible, so every board but "vm" refuses to build without
          at least one key.
        '';
      };
    };

    network = {
      address = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "192.168.1.20/24";
        description = "Static IPv4 address with prefix length for wired Ethernet. null means DHCP.";
      };
      gateway = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "192.168.1.1";
        description = "Default gateway, with a static address.";
      };
      dns = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "1.1.1.1" ];
        description = "DNS servers, with a static address (DHCP supplies its own).";
      };
    };

    timezone = mkOption {
      type = types.str;
      default = "UTC";
      example = "Asia/Seoul";
      description = "Time zone of the system clock and logs.";
    };

    gpu.enable = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Jetson only: install the NVIDIA GPU userspace and the CUDA and TensorRT
        samples that `omarchy-robot validate` runs. Large; it does not turn on CUDA
        support across nixpkgs.
      '';
    };

    power.mode = mkOption {
      type = types.nullOr types.ints.unsigned;
      default = null;
      example = 0;
      description = ''
        Jetson only: nvpmodel power mode ID applied at boot (jetpack-nixos
        services.nvpmodel.profileNumber). IDs and their wattage differ per board;
        `nvpmodel -p --verbose` on the robot lists them. null keeps the board's
        default mode.
      '';
    };

    runtime.enable = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Install omarchy-runtime, the execution runtime (goals in, verified
        executions and traces out), with /var/lib/omarchy-runtime/runs for its
        run directories, writable by the administrator. A command, not a
        service. Everything it runs today is simulated.
      '';
    };

    build = mkOption {
      type = types.attrsOf types.anything;
      internal = true;
      default = { };
      description = "Set by flake.nix: target name, Omarchy Robots version, flake revision, robot.nix hash and input revisions.";
    };

    generations = mkOption {
      type = types.ints.between 2 100;
      default = 10;
      description = ''
        System generations kept for rollback: boot menu entries, and generations
        the weekly garbage collection never deletes.
      '';
    };
  };
}
