# Omarchy Robots, not NixOS: the system's name, and the Omarchy Robots logo on
# the login banners and, on Jetson boards, in the UEFI firmware. Static files only.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.omarchy.robot;
  isJetson = (import ./boards.nix).${cfg.board} ? som;
  version = cfg.build.version or "dev";
  esc = builtins.fromJSON ''"\u001b"'';
  green = s: "${esc}[38;2;158;206;106m${s}${esc}[0m";
  logo = lib.splitString "\n" (lib.removeSuffix "\n" (builtins.readFile ../branding/logo.txt));
  # The logo, with up to eight lines of text beside it.
  banner =
    text:
    lib.concatLines (
      lib.zipListsWith (l: t: green l + lib.optionalString (t != "") "   ${t}") logo (
        text ++ lib.genList (_: "") (lib.length logo - lib.length text)
      )
    );
in
{
  options.omarchy.branding.motd = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    internal = true;
    description = "Lines printed beside the logo at SSH login.";
  };

  config = {
    system.nixos = {
      distroName = "Omarchy Robots";
      distroId = "omarchy-robots";
      vendorName = "Omarchy Robots";
      vendorId = "omarchy-robots";
      extraOSReleaseArgs = {
        PRETTY_NAME = "Omarchy Robots ${version} (NixOS ${config.system.nixos.release})";
        LOGO = "omarchy-robots";
        HOME_URL = "https://github.com/promaaa/omarchy-robots";
        ANSI_COLOR = "38;2;158;206;106";
      };
    };

    omarchy.branding.motd = lib.mkDefault [
      "Omarchy Robots ${version}"
      (
        "${cfg.name} · ${cfg.board}"
        + lib.optionalString isJetson " · JetPack ${pkgs.nvidia-jetpack.jetpackMajorMinorPatchVersion}"
      )
      ""
      "omarchy-robot info            this build"
      "sudo omarchy-robot validate   bring-up checklist"
      "sudo omarchy-robot rollback   previous generation"
    ];
    users.motd = banner config.omarchy.branding.motd;
    # agetty: \n is the hostname, \l the terminal.
    services.getty.greetingLine = banner [
      "Omarchy Robots ${version}"
      "\\n · \\l"
    ];

    hardware.nvidia-jetpack.firmware.uefi.logo = lib.mkIf isJetson "${pkgs.omarchy-branding}/boot.png";
  };
}
