# P9: the runtime on Omarchy Robots. With omarchy.robot.runtime.enable on the
# vm board, the administrator runs the reduced scenario set (nominal_a and
# slip_retry, simulated); every trace validates and carries this system's
# build.json, and `omarchy-robot info` names the runtime.
{
  pkgs,
  robotModules,
  onBoard,
}:
{
  name = "omarchy-runtime";
  node.pkgsReadOnly = false;
  defaults = ./slow-host.nix;

  nodes.robot =
    { lib, ... }:
    {
      imports = robotModules "vm" ++ [ (onBoard "vm") ];
      omarchy.robot.runtime.enable = lib.mkForce true; # over robot.nix
      virtualisation.memorySize = 2048;
    };

  testScript = ''
    import json

    version = "${pkgs.omarchy-runtime.version}"
    robot.wait_for_unit("multi-user.target")

    with subtest("installed, and named by the build identity"):
        build = json.loads(robot.succeed("cat /etc/omarchy-robot/build.json"))
        assert build["runtime"] == {"version": version}, build["runtime"]
        assert f"omarchy-runtime {version}" in robot.succeed("omarchy-runtime --version")
        info = robot.succeed("omarchy-robot info")
        print(info)
        assert f"runtime     omarchy-runtime {version}" in info, info

    with subtest("the administrator runs the reduced scenario set"):
        print(robot.succeed("su - admin -c 'omarchy-runtime eval nominal_a slip_retry' 2>&1"))
        runs = sorted(robot.succeed("ls -d /var/lib/omarchy-runtime/runs/eval-*/*/").split())
        assert len(runs) == 2, runs

    with subtest("every trace validates and carries this system's identity"):
        print(robot.succeed("omarchy-runtime validate-trace " + " ".join(runs)))
        for run in runs:
            header = json.loads(robot.succeed(f"cat {run}run.json"))
            assert header["os"] == build, (header["os"], build)
            assert header["runtime"]["version"] == version, header["runtime"]
        results = json.loads(robot.succeed("cat /var/lib/omarchy-runtime/runs/eval-*/results.json"))
        assert results["passed"], [s["problems"] for s in results["scenarios"]]
        assert [s["scenario"] for s in results["scenarios"]] == ["nominal_a", "slip_retry"]
        assert "in an Omarchy Robots VM" in results["overhead"]["measured"], results["overhead"]
        print(robot.succeed(f"omarchy-runtime show {runs[1]}"))
  '';
}
