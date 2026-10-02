# M2/M4: the vm board boots through UEFI, takes an SSH key login from another
# machine, firewalls everything else, carries its identity, and validates.
{
  self,
  pkgs,
  robotModules,
  onBoard,
  version,
}:
let
  keys = import "${pkgs.path}/nixos/tests/ssh-keys.nix" pkgs;
  expected = builtins.toJSON {
    target = "vm";
    inherit version;
    revision = self.rev or self.dirtyRev or "dirty";
    robotNix = builtins.hashFile "sha256" ../robot.nix;
  };
in
{
  name = "omarchy-base";
  node.pkgsReadOnly = false;

  nodes.robot = {
    imports = robotModules "vm" ++ [ (onBoard "vm") ];
    omarchy.robot.admin.sshKeys = [ keys.snakeOilEd25519PublicKey ];
    virtualisation = {
      useBootLoader = true;
      useEFIBoot = true;
      memorySize = 2048;
    };
  };

  nodes.operator =
    { pkgs, ... }:
    {
      environment.systemPackages = [ pkgs.netcat ];
    };

  testScript = ''
    import importlib
    import json
    import sys

    sys.path.insert(0, "${../pkgs/omarchy-robot}")
    tool = importlib.import_module("omarchy_robot")

    expected = json.loads('${expected}')
    jetson_checks = {c[0] for c in tool.CHECKS if c[2]}
    ssh = "ssh -i /root/key -o StrictHostKeyChecking=no admin@robot"

    start_all()
    robot.wait_for_unit("multi-user.target")
    robot.wait_for_unit("sshd.service")
    operator.wait_for_unit("multi-user.target")

    with subtest("identity: build.json describes this build"):
        build = json.loads(robot.succeed("cat /etc/omarchy-robot/build.json"))
        for key, value in expected.items():
            assert build[key] == value, f"build.json {key} = {build[key]!r}, expected {value!r}"
        assert build["robot"]["board"] == "vm" and build["jetson"] is None, build
        assert build["kernel"] == robot.succeed("uname -r").strip()
        assert set(build["inputs"]) == {"nixpkgs", "jetpack-nixos", "disko"}, build["inputs"]

    with subtest("base defaults"):
        robot.succeed("test \"$(hostname)\" = robot")
        robot.succeed("test -d /sys/firmware/efi && bootctl is-installed")
        robot.succeed("nix config show experimental-features | grep -w flakes")
        robot.succeed("systemctl is-enabled nix-gc.timer")
        robot.succeed("grep -x SystemMaxUse=512M /etc/systemd/journald.conf")
        robot.succeed("getent shadow admin | cut -d: -f2 | grep -x '!'")

    with subtest("ssh: key login over the network, no password methods offered"):
        operator.succeed("install -m 600 ${keys.snakeOilEd25519PrivateKey} /root/key")
        operator.succeed(f"{ssh} true")
        refused = operator.fail(
            "ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o PubkeyAuthentication=no admin@robot true 2>&1"
        )
        assert "Permission denied (publickey)." in refused, refused

    with subtest("firewall: only ssh is reachable"):
        robot.succeed("systemctl is-active firewall.service")
        robot.succeed("systemd-run --unit=listener ${pkgs.python3Minimal.interpreter} -m http.server 8080")
        robot.wait_for_open_port(8080)
        operator.succeed("nc -z -w 5 robot 22")
        operator.fail("nc -z -w 5 robot 8080")

    with subtest("validate: generic checks pass, Jetson checks are not applicable"):
        report = json.loads(operator.succeed(f"{ssh} omarchy-robot validate --json"))
        assert tool.report_errors(report) == [], tool.report_errors(report)
        for c in report["checks"]:
            want = "NOT_APPLICABLE" if c["id"] in jetson_checks else "PASS"
            assert c["status"] == want, f"{c['id']}: {c['status']} ({c['detail']}), expected {want}"
        print(operator.succeed(f"{ssh} omarchy-robot validate"))
        print(robot.succeed("omarchy-robot info"))

    with subtest("boot time (VM)"):
        print(robot.succeed("systemd-analyze; systemd-analyze blame | head -n 12"))
  '';
}
