# omarchy.robot.runtime.enable: the runtime as a command. Its run directories
# go to /var/lib/omarchy-runtime/runs, which the administrator (group wheel)
# can write; build.json names its version (identity.nix).
{
  config,
  lib,
  pkgs,
  ...
}:
{
  config = lib.mkIf config.omarchy.robot.runtime.enable {
    environment.systemPackages = [ pkgs.omarchy-runtime ];
    environment.variables.OMARCHY_RUNTIME_RUNS = "/var/lib/omarchy-runtime/runs";
    systemd.tmpfiles.settings.omarchy-runtime = {
      "/var/lib/omarchy-runtime".d.mode = "0755";
      "/var/lib/omarchy-runtime/runs".d = {
        group = "wheel";
        mode = "2775";
      };
    };
  };
}
