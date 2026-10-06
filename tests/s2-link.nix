# The S2 link over WireGuard (omarchy.robot.s2Link). A robot VM (Omarchy
# Robots, vm board, runtime enabled) and an S2 VM, each with keys generated
# at test time, never in the Nix store. S2 traffic works through the tunnel,
# never crosses the LAN in the clear, and is refused outside the tunnel.
{
  pkgs,
  robotModules,
  onBoard,
}:
let
  robotAddress = "10.77.0.1";
  s2Address = "10.77.0.2";
  key = "/var/lib/omarchy-s2-link/private.key";
  peerKey = "/var/lib/omarchy-s2-link/peer.pub";
in
{
  name = "omarchy-s2-link";
  node.pkgsReadOnly = false;
  defaults = ./slow-host.nix;

  nodes.robot =
    { lib, ... }:
    {
      imports = robotModules "vm" ++ [ (onBoard "vm") ];
      omarchy.robot.runtime.enable = lib.mkForce true;
      omarchy.robot.s2Link = {
        enable = true;
        address = robotAddress;
        privateKeyFile = key;
        peer = {
          address = s2Address;
          publicKeyFile = peerKey; # generated at test time
          endpoint = "edge:51820";
        };
      };
      environment.systemPackages = [
        pkgs.tcpdump
        pkgs.netcat
      ];
      virtualisation.memorySize = 2048;
    };

  # S2's machine: plain NixOS with the runtime's package and the same module.
  nodes.edge = {
    imports = [ ../modules/s2-link.nix ];
    environment.systemPackages = [ pkgs.omarchy-runtime ];
    omarchy.robot.s2Link = {
      enable = true;
      role = "s2";
      address = s2Address;
      privateKeyFile = key;
      peer = {
        address = robotAddress;
        publicKeyFile = peerKey;
      };
    };
  };

  testScript = ''
    start_all()
    for m in (robot, edge):
        m.wait_for_unit("multi-user.target")

    with subtest("keys generated at test time; the tunnel comes up"):
        public = {}
        for m in (robot, edge):
            m.succeed("mkdir -p -m 700 /var/lib/omarchy-s2-link")
            m.succeed("umask 077; wg genkey > ${key}")
            public[m.name] = m.succeed("wg pubkey < ${key}").strip()
        robot.succeed(f"echo {public['edge']} > ${peerKey}")
        edge.succeed(f"echo {public['robot']} > ${peerKey}")
        for m in (robot, edge):
            m.succeed("systemctl restart omarchy-s2-link")
            m.wait_for_unit("omarchy-s2-link")
        robot.wait_until_succeeds("ping -c 1 -W 1 ${s2Address}", timeout=120)
        print(robot.succeed("wg show wg-s2"))

    with subtest("S2 listens on the tunnel only"):
        edge.fail("omarchy-runtime s2 --listen 0.0.0.0:7447 --timeout 5 2>&1")
        edge.succeed("(omarchy-runtime s2 --ttft 0.1 --timeout 300 > /tmp/s2.json 2> /tmp/s2.err; echo $? > /tmp/s2.rc) &")
        edge.wait_until_succeeds("ss -ltn | grep -q '${s2Address}:7447'", timeout=120)

    with subtest("outside the tunnel, S2 is refused"):
        robot.fail("nc -z -w 3 edge 7447")
        out = robot.fail("su - admin -c 'omarchy-runtime run nominal_a --clock wall --s2 edge:7447' 2>&1")
        assert "the S2 link is enabled" in out, out

    with subtest("through the tunnel, a run succeeds and nothing crosses the LAN in the clear"):
        robot.succeed("(tcpdump -i eth1 -n -l 'tcp port 7447' > /tmp/leak.txt 2> /tmp/leak.err &)")
        robot.wait_until_succeeds("grep -q 'listening on eth1' /tmp/leak.err", timeout=60)
        before = int(robot.succeed("wg show wg-s2 transfer | awk '{print $2 + $3}'"))
        out = robot.succeed("su - admin -c 'omarchy-runtime run nominal_a --clock wall --s2 ${s2Address}:7447' 2>&1")
        print(out)
        assert "1 of 1 scenarios as expected" in out, out
        edge.wait_until_succeeds("test -s /tmp/s2.rc", timeout=120)
        assert edge.succeed("cat /tmp/s2.rc").strip() == "0", edge.succeed("cat /tmp/s2.err")
        after = int(robot.succeed("wg show wg-s2 transfer | awk '{print $2 + $3}'"))
        assert after - before > 10000, (before, after)
        robot.succeed("pkill tcpdump; sleep 1")
        leak = robot.succeed("cat /tmp/leak.txt")
        assert leak.strip() == "", f"S2 traffic on eth1 outside the tunnel: {leak}"
        run = robot.succeed("ls -d /var/lib/omarchy-runtime/runs/run-*/nominal_a").strip()
        header = robot.succeed(f"cat {run}/run.json")
        assert '"address": "${s2Address}:7447"' in header, header
  '';
}
