"""Unit tests for omarchy_robot on a fake root: no hardware, no root.

The command outputs in fixtures/ follow the formats printed by cuda-samples'
deviceQuery, TensorRT's sample_onnx_mnist, nvpmodel, jetpack-nixos's
ota-check-firmware and L4T's /etc/nv_tegra_release. Their values are
illustrative; none was measured on a Jetson.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import omarchy_robot as o

FIXTURES = Path(__file__).parent / "fixtures"
GIB = 1024**3
ROOTS = tempfile.TemporaryDirectory()
unittest.addModuleCleanup(ROOTS.cleanup)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def build(board: str = "vm", **robot: Any) -> dict[str, Any]:
    b: dict[str, Any] = {
        "schema": 1,
        "target": "robot",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "robotNix": "ab" * 32,
        "inputs": {"nixpkgs": "1" * 40, "jetpack-nixos": "2" * 40, "disko": "3" * 40},
        "nixos": "26.05",
        "kernel": "6.12.1",
        "robot": {"name": "robot", "board": board, "storage": "/dev/vda", "generations": 10, "gpu": False, "powerMode": None},
        "jetson": None,
    }
    b["robot"].update(robot)
    if board != "vm":
        b["kernel"] = "5.15.148-tegra"
        b["robot"]["storage"] = "/dev/nvme0n1"
        b["jetson"] = {
            "som": "orin-agx",
            "carrierBoard": "devkit",
            "super": False,
            "carrier": "p3737",
            "modules": ["p3701"],
            "cudaCapability": "8.7",
            "jetpack": "6.2.3",
            "l4t": "36.5.2",
            "gpuDriver": "nvgpu",
        }
    return b


class Fake(o.System):
    """A fake root directory plus canned command outputs."""

    def __init__(self, b: dict[str, Any] | None, files: dict[str, str] | None = None,
                 commands: dict[str, tuple[int, str]] | None = None) -> None:
        super().__init__(tempfile.mkdtemp(dir=ROOTS.name))
        jetson = b is not None and b["jetson"] is not None
        disk = "nvme0n1" if jetson else "vda"
        part = "nvme0n1p2" if jetson else "vda2"
        self.files = {
            "/proc/sys/kernel/hostname": "robot\n",
            "/proc/sys/kernel/osrelease": (b["kernel"] if b else "6.12.1") + "\n",
            "/boot/loader/loader.conf": "timeout 5\ndefault nixos-generation-2.conf\n",
            "/boot/loader/entries/nixos-generation-1.conf": "",
            "/boot/loader/entries/nixos-generation-2.conf": "",
            "/etc/ssh/sshd_config": "UsePAM yes\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n",
        }
        if b is not None:
            self.files[o.BUILD_JSON] = json.dumps(b)
        self.commands = {
            "findmnt --noheadings --output SOURCE /": (0, f"/dev/{part}\n"),
            "findmnt --noheadings --output FSTYPE /": (0, "ext4\n"),
            f"lsblk --noheadings --nodeps --output PKNAME /dev/{part}": (0, f"{disk}\n"),
            "systemctl is-active sshd.service": (0, "active\n"),
            "systemctl is-active firewall.service": (0, "active\n"),
        }
        if jetson:
            self.files.update({
                "/proc/device-tree/model": "NVIDIA Jetson AGX Orin Developer Kit\0",
                "/proc/device-tree/compatible": "nvidia,p3737-0000+p3701-0005\0nvidia,p3701-0005\0nvidia,tegra234\0",
                "/etc/nv_tegra_release": fixture("nv_tegra_release"),
                "/dev/nvgpu/igpu0/ctrl": "",
                "/sys/class/thermal/thermal_zone0/type": "cpu-thermal\n",
                "/sys/class/thermal/thermal_zone0/temp": "45250\n",
                "/sys/class/thermal/thermal_zone1/type": "gpu-thermal\n",
                "/sys/class/thermal/thermal_zone1/temp": "44000\n",
            })
            self.commands.update({
                "ota-check-firmware -b": (0, fixture("ota-check-firmware.txt")),
                "nvpmodel -q": (0, fixture("nvpmodel-q.txt")),
                "omarchy-cuda-probe": (0, fixture("deviceQuery-orin.txt")),
                "omarchy-tensorrt-probe": (0, fixture("tensorrt-sample-onnx-mnist.txt")),
            })
        self.files.update(files or {})
        self.commands.update(commands or {})
        for path, text in self.files.items():
            self.path(path).parent.mkdir(parents=True, exist_ok=True)
            self.path(path).write_text(text)
        for link, target in {
            "/run/current-system": "/nix/store/aaaa-nixos-system",
            "/run/booted-system": "/nix/store/aaaa-nixos-system",
            "/nix/var/nix/profiles/system-1-link": "/nix/store/bbbb-nixos-system",
            "/nix/var/nix/profiles/system-2-link": "/nix/store/aaaa-nixos-system",
        }.items():
            self.path(link).parent.mkdir(parents=True, exist_ok=True)
            self.path(link).symlink_to(self.path(target))
        self.path(o.PROFILE).symlink_to("system-2-link")

    def run(self, *cmd: str, timeout: int = 60) -> tuple[int, str]:
        return self.commands.get(" ".join(cmd), (127, f"{cmd[0]}: command not found"))

    def statvfs(self, p: str) -> tuple[int, int]:
        return 50 * GIB, 100 * GIB


def statuses(report: dict[str, Any]) -> dict[str, str]:
    return {c["id"]: c["status"] for c in report["checks"]}


def check(report: dict[str, Any], check_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(c for c in report["checks"] if c["id"] == check_id)
    return found


JETSON = {c[0] for c in o.CHECKS if c[2]}


class ValidateTest(unittest.TestCase):
    def test_vm_generic_pass_and_jetson_not_applicable(self) -> None:
        report = o.validate(Fake(build()))
        self.assertEqual(o.report_errors(report), [])
        for check_id, status in statuses(report).items():
            self.assertEqual(status, o.NOT_APPLICABLE if check_id in JETSON else o.PASS, check_id)
        self.assertEqual(check(report, "boot.entries")["data"]["current"], "system-2-link")
        self.assertEqual(check(report, "boot.root")["detail"], "/ is /dev/vda2 on /dev/vda")

    def test_jetson_all_pass_with_gpu_and_camera(self) -> None:
        cameras = {"/sys/class/video4linux/video0/name": "vi-output, imx219 9-0010\n"}
        report = o.validate(Fake(build("orin-agx-devkit", gpu=True, powerMode=0), files=cameras))
        self.assertEqual(o.report_errors(report), [])
        self.assertEqual(set(statuses(report).values()), {o.PASS}, statuses(report))
        self.assertEqual(check(report, "cuda.runtime")["data"]["device"], "Orin")
        self.assertEqual(check(report, "tensorrt.runtime")["data"]["tensorrt"], "100300")
        self.assertEqual(check(report, "thermal")["data"]["celsius"], {"cpu-thermal": 45.25, "gpu-thermal": 44.0})
        self.assertEqual(check(report, "jetson.l4t")["data"]["l4t"], "36.5.2")

    def test_jetson_without_camera_or_gpu_skips(self) -> None:
        report = o.validate(Fake(build("orin-agx-devkit")))
        self.assertEqual(check(report, "camera"), check(report, "camera") | {"status": o.SKIPPED, "detail": "no camera detected"})
        for check_id in ("gpu.device", "cuda.runtime", "tensorrt.runtime"):
            self.assertEqual(check(report, check_id)["status"], o.SKIPPED, check_id)
        self.assertEqual(report["summary"][o.FAIL], 0)

    def test_firmware_mismatch_fails(self) -> None:
        fake = Fake(build("orin-agx-devkit"), commands={"ota-check-firmware -b": (0, fixture("ota-check-firmware-mismatch.txt"))})
        result = check(o.validate(fake), "jetson.l4t")
        self.assertEqual(result["status"], o.FAIL)
        self.assertIn("flash the firmware", result["detail"])

    def test_firmware_tool_missing_fails(self) -> None:
        fake = Fake(build("orin-agx-devkit"), commands={"ota-check-firmware -b": (127, "ota-check-firmware: command not found")})
        self.assertEqual(check(o.validate(fake), "jetson.l4t")["status"], o.FAIL)

    def test_other_board_fails_model(self) -> None:
        nano = {
            "/proc/device-tree/model": "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super\0",
            "/proc/device-tree/compatible": "nvidia,p3768-0000+p3767-0005-super\0nvidia,p3767-0005\0nvidia,tegra234\0",
        }
        result = check(o.validate(Fake(build("orin-agx-devkit"), files=nano)), "jetson.model")
        self.assertEqual(result["status"], o.FAIL)
        self.assertIn("expects carrier p3737", result["detail"])

    def test_power_mode_mismatch_fails(self) -> None:
        result = check(o.validate(Fake(build("orin-agx-devkit", powerMode=2))), "power.mode")
        self.assertEqual(result["status"], o.FAIL)
        self.assertEqual(result["data"], {"mode": 0, "name": "MAXN", "configured": 2})

    def test_cuda_failure_and_wrong_capability_fail(self) -> None:
        failing = {"omarchy-cuda-probe": (1, "cudaGetDeviceCount returned 100\nResult = FAIL\n")}
        broken = Fake(build("orin-agx-devkit", gpu=True), commands=failing)
        self.assertEqual(check(o.validate(broken), "cuda.runtime")["status"], o.FAIL)
        b = build("orin-agx-devkit", gpu=True)
        b["jetson"]["cudaCapability"] = "11.0"
        self.assertEqual(check(o.validate(Fake(b)), "cuda.runtime")["status"], o.FAIL)

    def test_missing_gpu_node_fails(self) -> None:
        fake = Fake(build("orin-agx-devkit", gpu=True))
        fake.path("/dev/nvgpu/igpu0/ctrl").unlink()
        self.assertEqual(check(o.validate(fake), "gpu.device")["status"], o.FAIL)

    def test_password_login_fails_ssh(self) -> None:
        fake = Fake(build(), files={"/etc/ssh/sshd_config": "KbdInteractiveAuthentication no\n"})
        result = check(o.validate(fake), "services.ssh")
        self.assertEqual(result["status"], o.FAIL)
        self.assertIn("PasswordAuthentication", result["detail"])

    def test_root_on_another_disk_fails(self) -> None:
        fake = Fake(build(), commands={
            "findmnt --noheadings --output SOURCE /": (0, "/dev/sda1\n"),
            "lsblk --noheadings --nodeps --output PKNAME /dev/sda1": (0, "sda\n"),
        })
        self.assertEqual(check(o.validate(fake), "boot.root")["status"], o.FAIL)

    def test_unreadable_boot_skips_entries(self) -> None:
        fake = Fake(build())
        fake.accessible = lambda p: p != "/boot"  # type: ignore[method-assign]
        result = check(o.validate(fake), "boot.entries")
        self.assertEqual((result["status"], result["detail"]), (o.SKIPPED, "/boot is readable by root only: run with sudo"))

    def test_inactive_services_fail(self) -> None:
        fake = Fake(build(), commands={"systemctl is-active firewall.service": (3, "inactive\n")})
        self.assertEqual(check(o.validate(fake), "services.firewall")["status"], o.FAIL)

    def test_dirty_revision_and_pending_reboot_warn(self) -> None:
        b = build()
        b["revision"] = "0123456789ab-dirty"
        fake = Fake(b, files={"/proc/sys/kernel/osrelease": "6.12.0\n"})
        result = check(o.validate(fake), "identity")
        self.assertEqual(result["status"], o.PASS)
        self.assertEqual(len(result["warnings"]), 2, result["warnings"])

    def test_hostname_mismatch_fails_identity(self) -> None:
        fake = Fake(build(), files={"/proc/sys/kernel/hostname": "other\n"})
        self.assertEqual(check(o.validate(fake), "identity")["status"], o.FAIL)

    def test_missing_build_json(self) -> None:
        report = o.validate(Fake(None))
        self.assertEqual(o.report_errors(report), [])
        self.assertEqual(check(report, "identity")["status"], o.FAIL)
        self.assertEqual({s for i, s in statuses(report).items() if i != "identity"}, {o.SKIPPED})

    def test_crashing_check_is_a_failure(self) -> None:
        b = build("orin-agx-devkit")
        del b["jetson"]["carrier"]
        result = check(o.validate(Fake(b)), "jetson.model")
        self.assertEqual(result["status"], o.FAIL)
        self.assertIn("KeyError", result["detail"])


class SchemaTest(unittest.TestCase):
    def test_report_errors_catches_damage(self) -> None:
        report = o.validate(Fake(build()))
        self.assertEqual(o.report_errors(json.loads(json.dumps(report))), [])
        self.assertTrue(o.report_errors([]))
        damages: list[Callable[[dict[str, Any]], object]] = [
            lambda r: r.update(schema=2),
            lambda r: r["checks"].pop(),
            lambda r: r["checks"][0].update(status="WARN"),
            lambda r: r["checks"][1].pop("detail"),
            lambda r: r["summary"].update(PASS=0),
        ]
        for damage in damages:
            broken = json.loads(json.dumps(report))
            damage(broken)
            self.assertTrue(o.report_errors(broken), damage)


class SshdConfigTest(unittest.TestCase):
    def test_first_value_before_match_wins(self) -> None:
        config = "# comment\nPasswordAuthentication=no\npasswordauthentication yes\nMatch User x\n  KbdInteractiveAuthentication no\n"
        self.assertEqual(o.sshd_setting(config, "PasswordAuthentication"), "no")
        self.assertIsNone(o.sshd_setting(config, "KbdInteractiveAuthentication"))


class CommandTest(unittest.TestCase):
    def test_info_names_the_runtime(self) -> None:
        for runtime, expected in ((None, "not installed"), ({"version": "0.0.2"}, "omarchy-runtime 0.0.2")):
            fake = Fake(build() | {"runtime": runtime})
            original = o.System
            o.System = lambda root="/", fake=fake: fake  # type: ignore[assignment,misc]
            try:
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(o.main(["info"]), 0)
            finally:
                o.System = original  # type: ignore[misc]
            self.assertIn(expected, out.getvalue())

    def test_validate_exit_code(self) -> None:
        good = Fake(build())
        bad = Fake(build(), commands={"systemctl is-active sshd.service": (3, "inactive\n")})
        for fake, code in ((good, 0), (bad, 1)):
            original = o.System
            o.System = lambda root="/", fake=fake: fake  # type: ignore[assignment,misc]
            try:
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(o.main(["validate", "--json"]), code)
            finally:
                o.System = original  # type: ignore[misc]
            self.assertEqual(o.report_errors(json.loads(out.getvalue())), [])


if __name__ == "__main__":
    unittest.main()
