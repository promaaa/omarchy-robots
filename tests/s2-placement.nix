# Research question RQ5: where should S2 live? The two-cup task (scenario
# s2_placement) and three scenes that change during the task (cup_moved: S1
# reacts alone; slot_blocked, fallback_expiry: S2 must choose again while S0
# waits under the fallback) with S2 in its own process, on the robot
# ("local") or on an edge node, over a link shaped with tc netem. One cell per
# scenario, network profile and S2 time to first token (TTFT), each run on the
# wall clock. The S2 process's TTFT replaces the scenario's S2 latency.
#
# Every cell must keep the safety invariants (no ground-truth limit
# violation, S0 never without a chunk, a fallback or HOLD), validate its
# trace, carry the robot's build.json and stay within the stated tolerance
# of the simulated run; the supervisor must classify the injected outage as
# long; every recovery must run under a fallback, and every execution must
# hold a valid fallback for at least 99 % of its running time. $out/placement holds results.json, table.txt and table.md, $out/runs
# the robot's traces, $out/s2-* the S2 processes' own reports. Every number
# is labelled: emulated network, VMs, soft real-time.
#
# `full = false` (nix flake check, CI): the two-cup task on 4 profiles x 2
# TTFTs, and each dynamic scenario on lan and outage_long with a TTFT of 1 s.
# `full = true` (`nix build .#s2-placement`, the manual CI job): every
# scenario x every profile x 0.1, 1, 5 s.
{
  pkgs,
  robotModules,
  onBoard,
  full ? false,
}:
let
  profiles = [
    "local"
    "lan"
    "wifi"
    "wan"
    "degraded"
    "outage"
    "outage_long"
  ];
  dynamic = [
    "cup_moved"
    "slot_blocked"
    "fallback_expiry"
  ];
  groups =
    if full then
      [
        {
          scenario = [ "s2_placement" ] ++ dynamic;
          profile = profiles;
          ttft = [
            0.1
            1
            5
          ];
        }
      ]
    else
      [
        {
          scenario = [ "s2_placement" ];
          profile = [
            "lan"
            "degraded"
            "outage"
            "outage_long"
          ];
          ttft = [
            0.1
            1
          ];
        }
        {
          scenario = dynamic;
          profile = [
            "lan"
            "outage_long"
          ];
          ttft = [ 1 ];
        }
      ];
  cells = pkgs.lib.concatMap pkgs.lib.cartesianProduct groups;
in
{
  name = "omarchy-s2-placement${if full then "-full" else ""}";
  node.pkgsReadOnly = false;
  defaults = ./slow-host.nix;
  # The full matrix has 84 cells: more than the driver's default hour.
  globalTimeout = if full then 3 * 3600 else 3600;

  nodes.robot =
    { lib, ... }:
    {
      imports = robotModules "vm" ++ [ (onBoard "vm") ];
      omarchy.robot.runtime.enable = lib.mkForce true; # over robot.nix
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };
    };

  # S2's machine: plain NixOS with the runtime's package. S2 listens; the
  # robot connects out, so the robot's firewall keeps admitting SSH only.
  nodes.edge = {
    environment.systemPackages = [ pkgs.omarchy-runtime ];
    networking.firewall.allowedTCPPorts = [ 7447 ];
    virtualisation.cores = 2;
  };

  testScript = ''
    import json
    import shlex
    import time

    CELLS = json.loads('${builtins.toJSON cells}')
    # netem on both nodes' link interfaces: half the round-trip delay and
    # jitter each way, so both directions see the same; loss per packet, in
    # each direction. With jitter, netem also reorders packets.
    PROFILES = {
        "local": None,  # S2 on the robot node, over the loopback interface
        "lan": "delay 0.5ms",  # about 1 ms round trip
        "wifi": "delay 10ms 5ms loss 1%",  # about 20 +- 10 ms, 1 % loss
        "wan": "delay 50ms 10ms",  # about 100 +- 20 ms
        "degraded": "delay 200ms 50ms loss 5%",  # about 400 +- 100 ms, 5 % loss
        "outage": "delay 0.5ms",  # lan, then 100 % loss for OUTAGES["outage"] seconds mid-task
        "outage_long": "delay 0.5ms",  # lan, then 100 % loss for 20 s: the robot must reconnect
    }
    OUTAGES = {"outage": 5, "outage_long": 20}  # seconds of 100 % loss, from 2 s into the first execution
    RUNS = "/var/lib/omarchy-runtime/runs"

    start_all()
    robot.wait_for_unit("multi-user.target")
    edge.wait_for_unit("multi-user.target")
    build = json.loads(robot.succeed("cat /etc/omarchy-robot/build.json"))
    links = {
        m.name: m.succeed("ip -br -4 addr | awk '/ 192[.]168[.]1[.]/ {print $1}'").strip()
        for m in (robot, edge)
    }
    print("link interfaces:", links, "virtualization:", robot.execute("systemd-detect-virt")[1].strip())

    def shape(spec):
        for m in (robot, edge):
            if spec:
                m.succeed(f"tc qdisc replace dev {links[m.name]} root netem {spec}")
            else:
                m.succeed(f"tc qdisc del dev {links[m.name]} root 2>/dev/null || true")

    def background(machine, command, log):
        machine.succeed(f"(set +e; {command}; echo $? > {log}.rc) > {log} 2> {log}.err &")

    def cell(scenario, profile, ttft):
        name = f"{scenario}-{profile}-ttft{ttft}"
        spec = PROFILES[profile]
        shape(spec)
        local = profile == "local"
        s2_node = robot if local else edge
        listen = "127.0.0.1:7447" if local else "0.0.0.0:7447"
        target = "127.0.0.1:7447" if local else "edge:7447"
        s2_node.succeed("mkdir -p /tmp/s2")
        background(s2_node, f"omarchy-runtime s2 --listen {listen} --ttft {ttft} --timeout 300", f"/tmp/s2/{name}.json")
        tags = f"--tag profile={profile} --tag netem={shlex.quote(spec or 'none')}"
        run = f"omarchy-runtime run {scenario} --clock wall --s2 {target} --runs {RUNS}/{name} {tags}"
        background(robot, f"su - admin -c {shlex.quote(run)}", f"/tmp/robot-{name}.log")
        if profile in OUTAGES:
            robot.wait_until_succeeds(f"grep -qs '\"to\":\"RUNNING\"' {RUNS}/{name}/run-*/{scenario}/events.jsonl", timeout=600)
            time.sleep(2)  # into the first execution
            shape(f"{spec} loss 100%")
            time.sleep(OUTAGES[profile])
            shape(spec)
        robot.wait_until_succeeds(f"test -s /tmp/robot-{name}.log.rc", timeout=900)
        s2_node.wait_until_succeeds(f"test -s /tmp/s2/{name}.json.rc", timeout=120)
        shape(None)
        print(robot.succeed(f"cat /tmp/robot-{name}.log /tmp/robot-{name}.log.err"))
        if profile in OUTAGES:  # how the link went down and came back
            print(robot.succeed(f"grep -h '\"type\":\"s2.link\"' {RUNS}/{name}/run-*/{scenario}/events.jsonl || true"))
        print(s2_node.succeed(f"cat /tmp/s2/{name}.json.err; tail -c 1500 /tmp/s2/{name}.json"))
        run_dir = robot.succeed(f"ls -d {RUNS}/{name}/run-*/{scenario}").strip()
        with subtest(f"{name}: the trace validates and carries this robot's identity"):
            robot.succeed(f"omarchy-runtime validate-trace {run_dir}")
            header = json.loads(robot.succeed(f"cat {run_dir}/run.json"))
            assert header["os"] == build, (header["os"], build)
            assert header["clock"] == "wall" and header["s2"]["placement"] == "remote", header["s2"]
            assert header["extensions"]["s2_latency"]["seconds"] == ttft, header["extensions"]
        return name

    names = {cell(c["scenario"], c["profile"], c["ttft"]): c["profile"] for c in CELLS}
    results = " ".join(f"{RUNS}/{n}/run-*/results.json" for n in names)
    status, table = robot.execute(f"omarchy-runtime placement {results} --out /tmp/placement")
    print(table)
    robot.copy_from_machine("/tmp/placement")
    robot.copy_from_machine(RUNS)
    robot.succeed("mkdir -p /tmp/s2 && cp -r /tmp/s2 /tmp/s2-robot")
    robot.copy_from_machine("/tmp/s2-robot")
    edge.succeed("mkdir -p /tmp/s2 && cp -r /tmp/s2 /tmp/s2-edge")
    edge.copy_from_machine("/tmp/s2-edge")

    with subtest("every cell keeps the safety invariants and the tolerance"):
        placement = json.loads(robot.succeed("cat /tmp/placement/results.json"))
        assert status == 0 and placement["passed"], [(c["scenario"], c["profile"], c["ttft_s"], c["problems"]) for c in placement["cells"]]
        assert len(placement["cells"]) == len(CELLS), len(placement["cells"])
        assert all(c["violations"] == 0 for c in placement["cells"])
        assert "VMs" in placement["label"] and "soft real-time" in placement["label"], placement["label"]

    with subtest("both processes report success"):
        for n, profile in names.items():
            assert robot.succeed(f"cat /tmp/robot-{n}.log.rc").strip() == "0", n
            s2_node = robot if profile == "local" else edge
            assert s2_node.succeed(f"cat /tmp/s2/{n}.json.rc").strip() == "0", n

    with subtest("the supervisor classifies the injected outages as long, they end, and the robot reconnects"):
        for c in placement["cells"]:
            if c["profile"] in OUTAGES:
                long = [o for o in c["outages"] if o["class"] == "long"]
                assert long and long[0]["duration_s"] >= OUTAGES[c["profile"]] - 1, c["outages"]
                assert all(o["class"] != "unrecovered" for o in c["outages"]), c["outages"]
            if c["profile"] == "outage_long":
                events = robot.succeed(f"cat {c['run_dir']}/events.jsonl")
                assert '"state":"reconnected"' in events, f"{c['scenario']} {c['profile']} {c['ttft_s']}: no reconnection"

    with subtest("the dynamic scenarios react, and every recovery runs under a fallback"):
        for c in placement["cells"]:
            name = (c["scenario"], c["profile"], c["ttft_s"])
            if c["scenario"] == "cup_moved":
                assert c["reaction_s"] is not None, name
            if c["scenario"] in ("slot_blocked", "fallback_expiry"):
                # S1 gives up: a fallback engages (S2's, sent with the goal, else the capability's default), whatever the TTFT.
                assert c["recovery"] and c["recovery"]["fallback_engaged"], (name, c["recovery"])

    with subtest("fallback coverage: at least 99 % of every execution's running time"):
        for c in placement["cells"]:
            assert c["fallback_coverage"] is not None and c["fallback_coverage"] >= 0.99, (c["scenario"], c["profile"], c["ttft_s"], c["fallback_coverage"])
  '';
}
