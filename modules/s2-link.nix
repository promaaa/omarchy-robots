# omarchy.robot.s2Link: the link to S2 carried over a WireGuard tunnel, so the
# OS owns its transport security and the runtime adds none. The private key
# is a path read at run time: never in the Nix store, never in the
# repository. /etc/omarchy-robot/s2-link.json tells the runtime the tunnel's
# addresses: it then connects (robot) or listens (S2) on them only.
#
# Imported by modules/default.nix for robots, and on its own by an S2
# machine that is not an Omarchy Robots system (role "s2").
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption types;
  cfg = config.omarchy.robot.s2Link;
  key =
    if cfg.peer.publicKey != null then
      lib.escapeShellArg cfg.peer.publicKey
    else
      ''"$(cat ${cfg.peer.publicKeyFile})"'';
in
{
  options.omarchy.robot.s2Link = {
    enable = lib.mkEnableOption "the link to S2 over a WireGuard tunnel";
    role = mkOption {
      type = types.enum [
        "robot"
        "s2"
      ];
      default = "robot";
      description = ''
        "robot" connects out to S2 through the tunnel; "s2" is S2's machine,
        which admits the S2 port on the tunnel only.
      '';
    };
    interface = mkOption {
      type = types.str;
      default = "wg-s2";
      description = "Name of the tunnel's network interface.";
    };
    address = mkOption {
      type = types.str;
      example = "10.77.0.1";
      description = "This machine's IPv4 address inside the tunnel.";
    };
    listenPort = mkOption {
      type = types.port;
      default = 51820;
      description = "UDP port WireGuard listens on (opened in the firewall).";
    };
    privateKeyFile = mkOption {
      type = types.str;
      example = "/var/lib/omarchy-s2-link/private.key";
      description = ''
        This machine's WireGuard private key (`wg genkey`), read when the tunnel
        comes up. Generate it on the machine; never put it in the Nix store or
        in a repository. The tunnel stays down until the file exists.
      '';
    };
    port = mkOption {
      type = types.port;
      default = 7447;
      description = "TCP port of S2 inside the tunnel (omarchy-runtime s2).";
    };
    peer = {
      address = mkOption {
        type = types.str;
        example = "10.77.0.2";
        description = "The other end's IPv4 address inside the tunnel.";
      };
      publicKey = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "The other end's public key (`wg pubkey`); public, it may be committed.";
      };
      publicKeyFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Instead of publicKey: a file holding it, read when the tunnel comes up.";
      };
      endpoint = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "edge.lab:51820";
        description = "Where the other end listens. The robot sets it; S2's machine may leave it null.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = (cfg.peer.publicKey == null) != (cfg.peer.publicKeyFile == null);
        message = "omarchy.robot.s2Link.peer: set publicKey or publicKeyFile, exactly one";
      }
      {
        assertion = !(lib.hasPrefix builtins.storeDir cfg.privateKeyFile);
        message = "omarchy.robot.s2Link.privateKeyFile must not be in the Nix store";
      }
    ];
    environment.systemPackages = [ pkgs.wireguard-tools ];
    networking.firewall.allowedUDPPorts = [ cfg.listenPort ];
    networking.firewall.interfaces.${cfg.interface}.allowedTCPPorts = lib.mkIf (cfg.role == "s2") [
      cfg.port
    ];
    environment.etc."omarchy-robot/s2-link.json".text = builtins.toJSON {
      inherit (cfg)
        role
        interface
        address
        port
        ;
      peer = cfg.peer.address;
    };
    systemd.services.omarchy-s2-link = {
      description = "WireGuard tunnel of the S2 link";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      unitConfig.ConditionPathExists = cfg.privateKeyFile;
      path = [
        pkgs.iproute2
        pkgs.wireguard-tools
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        ip link del ${cfg.interface} 2>/dev/null || true
        ip link add ${cfg.interface} type wireguard
        wg set ${cfg.interface} private-key ${cfg.privateKeyFile} listen-port ${toString cfg.listenPort} \
          peer ${key} allowed-ips ${cfg.peer.address}/32${
            lib.optionalString (
              cfg.peer.endpoint != null
            ) " endpoint ${cfg.peer.endpoint} persistent-keepalive 5"
          }
        ip address add ${cfg.address} peer ${cfg.peer.address} dev ${cfg.interface}
        ip link set ${cfg.interface} up
      '';
      postStop = "ip link del ${cfg.interface} 2>/dev/null || true";
    };
  };
}
