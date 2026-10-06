# S1: boot a board's installation image (UEFI, CD-ROM) next to a blank disk,
# install with `omarchy-install`, boot the installed disk, and validate. The
# vm board's disk is virtio (/dev/vda); the pc board's is an emulated NVMe
# drive (/dev/nvme0n1), as on most PCs.
{
  pkgs,
  lib,
  robot,
  installerModules,
  board,
}:
let
  # Lets the test driver run commands in the installer and the installed system.
  instrumentation = pkgs.path + "/nixos/modules/testing/test-instrumentation.nix";
  target = robot board [
    { omarchy.robot.board = lib.mkForce board; }
    instrumentation
  ];
  drive =
    {
      vm = "-drive file={disk},if=virtio";
      pc = "-drive file={disk},if=none,id=disk0 -device nvme,serial=omarchy0,drive=disk0";
    }
    .${board};
  iso =
    (lib.nixosSystem { modules = installerModules target ++ [ instrumentation ]; })
    .config.system.build.isoImage;
  qemu =
    (import (pkgs.path + "/nixos/lib/qemu-common.nix") {
      inherit lib;
      inherit (pkgs) stdenv;
    }).qemuBinary
      pkgs.qemu_test;
in
{
  name = if board == "vm" then "omarchy-installer" else "omarchy-installer-${board}";
  nodes = { };

  testScript = ''
    import importlib
    import json
    import os
    import shutil
    import subprocess
    import sys
    import tempfile

    sys.path.insert(0, "${../pkgs/omarchy-robot}")
    tool = importlib.import_module("omarchy_robot")

    state = tempfile.mkdtemp()
    disk, efivars = f"{state}/disk.qcow2", f"{state}/efivars.fd"
    subprocess.run(["${pkgs.qemu_test}/bin/qemu-img", "create", "-f", "qcow2", disk, "8G"], check=True)
    shutil.copy("${pkgs.OVMF.variables}", efivars)
    os.chmod(efivars, 0o644)

    def machine(name, extra=""):
        return create_machine(
            f"${qemu} -m 3072 -smp 2 -netdev user,id=net0 -device virtio-net-pci,netdev=net0"
            f" -drive if=pflash,format=raw,unit=0,readonly=on,file=${pkgs.OVMF.firmware}"
            f" -drive if=pflash,format=raw,unit=1,file={efivars}"
            f" ${drive} {extra}",
            name=name,
        )

    with subtest("install from the image onto the blank disk"):
        installer = machine("installer", "-cdrom ${iso}/iso/${iso.isoName}")
        installer.start()
        installer.wait_for_unit("multi-user.target")
        installer.succeed("test \"$(hostname)\" = robot-installer")
        installer.succeed("omarchy-install --yes >&2")
        installer.shutdown()

    with subtest("boot the installed disk and validate"):
        robot = machine("robot")
        robot.start()
        robot.wait_for_unit("multi-user.target")
        robot.succeed("test \"$(readlink -f /run/booted-system)\" = ${target.config.system.build.toplevel}")
        report = json.loads(robot.succeed("omarchy-robot validate --json"))
        assert tool.report_errors(report) == [], tool.report_errors(report)
        jetson_checks = {c[0] for c in tool.CHECKS if c[2]}
        for c in report["checks"]:
            want = "NOT_APPLICABLE" if c["id"] in jetson_checks else "PASS"
            assert c["status"] == want, f"{c['id']}: {c['status']} ({c['detail']}), expected {want}"
        print(robot.succeed("omarchy-robot validate"))
        robot.shutdown()
  '';
}
