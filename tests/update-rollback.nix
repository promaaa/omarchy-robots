# M5: switch to a new generation, verify it, roll back, reboot, and verify the
# previous generation is the one running, through `omarchy-robot`.
{
  robotModules,
  onBoard,
}:
{
  name = "omarchy-update-rollback";
  node.pkgsReadOnly = false;

  nodes.robot =
    { extendModules, lib, ... }:
    let
      # The trivial change being deployed: the robot's time zone.
      next =
        (extendModules {
          modules = [ { omarchy.robot.timezone = lib.mkForce "Asia/Seoul"; } ];
        }).config.system.build.toplevel;
    in
    {
      imports = robotModules "vm" ++ [ (onBoard "vm") ];
      options.omarchyTest.next = lib.mkOption {
        type = lib.types.package;
        default = next;
        internal = true;
      };
      config.virtualisation = {
        useBootLoader = true;
        useEFIBoot = true;
        memorySize = 2048;
        additionalPaths = [ next ];
      };
    };

  testScript =
    { nodes, ... }:
    ''
      import json

      original = "${nodes.robot.system.build.toplevel}"
      new = "${nodes.robot.omarchyTest.next}"

      def generation():
          link = robot.succeed("readlink /nix/var/nix/profiles/system").strip()
          return int(link.split("-")[1])

      robot.wait_for_unit("multi-user.target")
      robot.succeed(f"test $(readlink -f /run/current-system) = {original}")
      robot.succeed("readlink /etc/localtime | grep -F zoneinfo/UTC")
      first = generation()

      with subtest("update: switch to a new generation"):
          robot.succeed(f"omarchy-robot update {new} >&2")
          robot.succeed(f"test $(readlink -f /run/current-system) = {new}")
          robot.succeed("readlink /etc/localtime | grep -F zoneinfo/Asia/Seoul")
          assert generation() == first + 1
          robot.succeed(f"grep -x 'default nixos-generation-{first + 1}.conf' /boot/loader/loader.conf")

      with subtest("rollback: back to the previous generation, also after a reboot"):
          robot.succeed("omarchy-robot rollback >&2")
          robot.succeed(f"test $(readlink -f /run/current-system) = {original}")
          robot.succeed("readlink /etc/localtime | grep -F zoneinfo/UTC")
          robot.succeed(f"grep -x 'default nixos-generation-{first}.conf' /boot/loader/loader.conf")
          robot.shutdown()  # then a cold boot through UEFI and systemd-boot's default entry
          robot.start()
          robot.wait_for_unit("multi-user.target")
          robot.succeed(f"test $(readlink -f /run/booted-system) = {original}")
          robot.succeed("readlink /etc/localtime | grep -F zoneinfo/UTC")

      with subtest("validate after rollback"):
          report = json.loads(robot.succeed("omarchy-robot validate --json"))
          entries = next(c for c in report["checks"] if c["id"] == "boot.entries")
          assert entries["status"] == "PASS" and entries["data"]["entries"] == 2, entries
          assert entries["data"]["current"] == f"system-{first}-link", entries
          print(robot.succeed("omarchy-robot validate"))
    '';
}
