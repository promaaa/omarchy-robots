#!/usr/bin/env python3
"""omarchy-robot: identity, bring-up validation, update and rollback.

The validation report (`omarchy-robot validate --json`) is versioned by its
top-level "schema" field; report_errors() below is its definition.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = 1
BUILD_JSON = "/etc/omarchy-robot/build.json"
PROFILE = "/nix/var/nix/profiles/system"
PASS, FAIL, SKIPPED, NOT_APPLICABLE = "PASS", "FAIL", "SKIPPED", "NOT_APPLICABLE"
STATUSES = (PASS, FAIL, SKIPPED, NOT_APPLICABLE)

Build = dict[str, Any]


class System:
    """Everything the checks read from the machine; tests substitute fixtures."""

    def __init__(self, root: str = "/") -> None:
        self.root = Path(root)

    def path(self, p: str) -> Path:
        return self.root / p.lstrip("/")

    def read(self, p: str) -> str | None:
        try:
            return self.path(p).read_text(errors="replace")
        except OSError:
            return None

    def glob(self, pattern: str) -> list[str]:
        return sorted("/" + str(m.relative_to(self.root)) for m in self.root.glob(pattern.lstrip("/")))

    def realpath(self, p: str) -> str:
        return "/" + str(self.path(p).resolve().relative_to(self.root.resolve()))

    def accessible(self, p: str) -> bool:
        return os.access(self.path(p), os.R_OK | os.X_OK)

    def readlink(self, p: str) -> str:
        try:
            return os.readlink(self.path(p))
        except OSError:
            return ""

    def run(self, *cmd: str, timeout: int = 60) -> tuple[int, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError:
            return 127, f"{cmd[0]}: command not found"
        except subprocess.TimeoutExpired:
            return 124, f"{cmd[0]}: timed out after {timeout} s"
        return r.returncode, r.stdout + r.stderr

    def statvfs(self, p: str) -> tuple[int, int]:
        st = os.statvfs(self.path(p))
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


@dataclass
class Result:
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def tail(text: str, lines: int = 5) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])


# Generic checks: they must pass on every board, the VM included.


def check_identity(s: System, b: Build | None) -> Result:
    if b is None:
        return Result(FAIL, f"{BUILD_JSON} is missing or not valid JSON")
    missing = [k for k in ("schema", "target", "revision", "robotNix", "inputs", "kernel", "robot") if k not in b]
    if missing:
        return Result(FAIL, f"{BUILD_JSON} lacks {', '.join(missing)}")
    if b["schema"] != 1:
        return Result(FAIL, f"{BUILD_JSON} has schema {b['schema']}, this tool reads 1")
    host = (s.read("/proc/sys/kernel/hostname") or "").strip()
    if host != b["robot"]["name"]:
        return Result(FAIL, f"hostname is {host!r} but this build is {b['robot']['name']!r}")
    warnings = []
    if "dirty" in b["revision"]:
        warnings.append("built from a tree with uncommitted changes: the revision does not identify the source")
    running = (s.read("/proc/sys/kernel/osrelease") or "").strip()
    if running != b["kernel"]:
        warnings.append(f"running kernel {running}, this build's kernel {b['kernel']}: reboot pending")
    if s.realpath("/run/current-system") != s.realpath("/run/booted-system"):
        warnings.append("the current system is not the booted one: reboot pending")
    return Result(
        PASS,
        f"target {b['target']}, board {b['robot']['board']}, revision {b['revision'][:12]}, robot.nix {b['robotNix'][:12]}",
        {"target": b["target"], "revision": b["revision"], "robotNix": b["robotNix"]},
        warnings,
    )


def check_boot_root(s: System, b: Build) -> Result:
    want = s.realpath(b["robot"]["storage"])
    _, src = s.run("findmnt", "--noheadings", "--output", "SOURCE", "/")
    src = src.strip().split("[")[0]
    _, parent = s.run("lsblk", "--noheadings", "--nodeps", "--output", "PKNAME", src)
    disk = "/dev/" + parent.strip() if parent.strip() else src
    data = {"source": src, "disk": disk, "configured": b["robot"]["storage"]}
    if want in (src, disk):
        return Result(PASS, f"/ is {src} on {disk}", data)
    return Result(FAIL, f"/ is {src} on {disk}, robot.nix says {b['robot']['storage']} ({want})", data)


def check_boot_entries(s: System, b: Build) -> Result:
    if not s.accessible("/boot"):
        return Result(SKIPPED, "/boot is readable by root only: run with sudo")
    entries = s.glob("/boot/loader/entries/*.conf")
    generations = s.glob(PROFILE + "-*-link")
    current = s.readlink(PROFILE)
    default = re.search(r"^default\s+(\S+)", s.read("/boot/loader/loader.conf") or "", re.MULTILINE)
    data = {
        "entries": len(entries),
        "generations": len(generations),
        "limit": b["robot"]["generations"],
        "current": current,
        "default": default.group(1) if default else None,
    }
    if not entries:
        return Result(FAIL, "no systemd-boot entries in /boot/loader/entries", data)
    warnings = []
    if len(entries) > b["robot"]["generations"]:
        warnings.append(f"{len(entries)} boot entries exceed the limit of {b['robot']['generations']}")
    return Result(
        PASS,
        f"{len(entries)} boot entries, {len(generations)} generations kept (limit {b['robot']['generations']}), current {current}",
        data,
        warnings,
    )


def sshd_setting(config: str, key: str) -> str | None:
    """First value of `key` before any Match block: the one sshd applies."""
    for line in config.splitlines():
        words = line.strip().replace("=", " ").split()
        if not words or words[0].startswith("#"):
            continue
        if words[0].lower() == "match":
            return None
        if words[0].lower() == key.lower() and len(words) > 1:
            return words[1].lower()
    return None


def check_ssh(s: System, b: Build) -> Result:
    _, state = s.run("systemctl", "is-active", "sshd.service")
    if state.strip() != "active":
        return Result(FAIL, f"sshd.service is {state.strip() or 'unknown'}")
    config = s.read("/etc/ssh/sshd_config") or ""
    on = [k for k in ("PasswordAuthentication", "KbdInteractiveAuthentication") if sshd_setting(config, k) != "no"]
    if on:
        return Result(FAIL, f"sshd is active but {' and '.join(on)} is not 'no' in /etc/ssh/sshd_config")
    return Result(PASS, "sshd active; password and keyboard-interactive login off")


def check_firewall(s: System, b: Build) -> Result:
    _, state = s.run("systemctl", "is-active", "firewall.service")
    if state.strip() == "active":
        return Result(PASS, "firewall.service active")
    return Result(FAIL, f"firewall.service is {state.strip() or 'unknown'}")


def check_storage(s: System, b: Build) -> Result:
    _, fstype = s.run("findmnt", "--noheadings", "--output", "FSTYPE", "/")
    free, total = s.statvfs("/")
    gib = 1024**3
    pct = 100 * free / total if total else 0
    data = {"fstype": fstype.strip(), "free_bytes": free, "total_bytes": total}
    warnings = [f"only {pct:.0f}% free on /"] if pct < 10 else []
    return Result(PASS, f"/ is {fstype.strip()}, {free / gib:.1f} GiB free of {total / gib:.1f} GiB", data, warnings)


# Jetson checks: NOT_APPLICABLE on the vm board.


def check_jetson_model(s: System, b: Build) -> Result:
    j = b["jetson"]
    model = (s.read("/proc/device-tree/model") or "").strip("\0\n ")
    compatible = [c for c in (s.read("/proc/device-tree/compatible") or "").split("\0") if c]
    if not model:
        return Result(FAIL, "no /proc/device-tree/model")
    data = {"model": model, "compatible": compatible}
    carrier = any(j["carrier"] in c for c in compatible)
    module = any(m in c for c in compatible for m in j["modules"])
    if carrier and module:
        return Result(PASS, f"{model} ({compatible[0]})", data)
    expected = f"carrier {j['carrier']} with module {' or '.join(j['modules'])}"
    return Result(FAIL, f"{model} ({', '.join(compatible)}), board {b['robot']['board']} expects {expected}", data)


def check_jetson_l4t(s: System, b: Build) -> Result:
    want = b["jetson"]["l4t"]
    release = re.search(r"#\s*R(\d+)\s*\(release\),\s*REVISION:\s*(\d+)\.(\d+)", s.read("/etc/nv_tegra_release") or "")
    if not release:
        return Result(FAIL, "/etc/nv_tegra_release is missing or unreadable")
    l4t = ".".join(release.groups())
    code, out = s.run("ota-check-firmware", "-b")
    current = re.search(r"Current firmware version is\s*:\s*(\S+)", out)
    expected = re.search(r"Expected firmware version is\s*:\s*(\S+)", out)
    data = {"l4t": l4t, "pinned": want, "firmware": current and current.group(1), "firmware_expected": expected and expected.group(1)}
    if l4t != want:
        return Result(FAIL, f"userspace L4T {l4t}, this build pins {want}", data)
    if not (current and expected):
        return Result(FAIL, f"L4T {l4t}; firmware version unknown: ota-check-firmware -b exited {code}: {tail(out)}", data)
    if current.group(1) != expected.group(1):
        return Result(FAIL, f"L4T {l4t} but firmware {current.group(1)}, expected {expected.group(1)}: flash the firmware", data)
    return Result(PASS, f"L4T {l4t}, firmware {current.group(1)}", data)


def check_gpu_device(s: System, b: Build) -> Result:
    if skipped := gpu_disabled(b):
        return skipped
    driver = b["jetson"]["gpuDriver"]
    node = {"nvgpu": "/dev/nvgpu/igpu0/ctrl", "openrm": "/dev/nvidiactl"}.get(driver)
    if node is None:
        return Result(FAIL, f"no known device node for GPU driver {driver!r}")
    if s.path(node).exists():
        return Result(PASS, f"{node} present ({driver})", {"driver": driver, "node": node})
    return Result(FAIL, f"{node} missing ({driver} driver not loaded?)", {"driver": driver, "node": node})


def gpu_disabled(b: Build) -> Result | None:
    if b["robot"]["gpu"]:
        return None
    return Result(SKIPPED, "omarchy.robot.gpu.enable = false: no GPU driver, CUDA or TensorRT on this build")


def check_cuda(s: System, b: Build) -> Result:
    if skipped := gpu_disabled(b):
        return skipped
    code, out = s.run("omarchy-cuda-probe", timeout=300)
    name = re.search(r'Device 0: "([^"]+)"', out)
    capability = re.search(r"CUDA Capability Major/Minor version number:\s*(\S+)", out)
    versions = re.search(r"CUDA Driver Version / Runtime Version\s+(\S+) / (\S+)", out)
    if code != 0 or "Result = PASS" not in out or not (name and capability and versions):
        return Result(FAIL, f"deviceQuery exited {code}: {tail(out)}")
    data = {"device": name.group(1), "capability": capability.group(1), "driver": versions.group(1), "runtime": versions.group(2)}
    detail = f"{name.group(1)}, capability {capability.group(1)}, driver {versions.group(1)}, runtime {versions.group(2)}"
    if capability.group(1) != b["jetson"]["cudaCapability"]:
        return Result(FAIL, f"{detail}; this build targets {b['jetson']['cudaCapability']}", data)
    return Result(PASS, detail, data)


def check_tensorrt(s: System, b: Build) -> Result:
    if skipped := gpu_disabled(b):
        return skipped
    code, out = s.run("omarchy-tensorrt-probe", timeout=900)
    passed = re.search(r"&&&& PASSED (\S+)(?: \[TensorRT v(\d+)\])?", out)
    if code != 0 or not passed:
        return Result(FAIL, f"sample_onnx_mnist exited {code}: {tail(out)}")
    return Result(PASS, f"{passed.group(1)} passed", {"sample": passed.group(1), "tensorrt": passed.group(2)})


def check_camera(s: System, b: Build) -> Result:
    devices = {d.rsplit("/", 1)[-1]: (s.read(d + "/name") or "?").strip() for d in s.glob("/sys/class/video4linux/video*")}
    if not devices:
        return Result(SKIPPED, "no camera detected")
    return Result(PASS, ", ".join(f"/dev/{d}: {n}" for d, n in devices.items()), {"devices": devices})


def check_power_mode(s: System, b: Build) -> Result:
    code, out = s.run("nvpmodel", "-q")
    mode = re.search(r"NV Power Mode:\s*(.+)\n\s*(\d+)", out)
    if code != 0 or not mode:
        return Result(FAIL, f"nvpmodel -q exited {code}: {tail(out)}")
    name, number = mode.group(1).strip(), int(mode.group(2))
    want = b["robot"]["powerMode"]
    data = {"mode": number, "name": name, "configured": want}
    if want is None:
        return Result(PASS, f"mode {number} ({name}); robot.nix leaves the board default", data)
    if number != want:
        return Result(FAIL, f"mode {number} ({name}), robot.nix sets {want}", data)
    return Result(PASS, f"mode {number} ({name}), as robot.nix sets", data)


def check_thermal(s: System, b: Build) -> Result:
    temps = {}
    for zone in s.glob("/sys/class/thermal/thermal_zone*"):
        kind, temp = s.read(zone + "/type"), s.read(zone + "/temp")
        if kind and temp and temp.strip().lstrip("-").isdigit():
            temps[kind.strip()] = int(temp) / 1000
    if not temps:
        return Result(FAIL, "no readable thermal zone in /sys/class/thermal")
    return Result(PASS, ", ".join(f"{k} {v:.1f} °C" for k, v in temps.items()), {"celsius": temps})


CHECKS: list[tuple[str, str, bool, Callable[[System, Build], Result]]] = [
    ("boot.root", "root filesystem is on the configured storage device", False, check_boot_root),
    ("boot.entries", "bootloader entries exist; kept generations", False, check_boot_entries),
    ("services.ssh", "sshd active, password login off", False, check_ssh),
    ("services.firewall", "firewall active", False, check_firewall),
    ("storage", "root filesystem type and free space", False, check_storage),
    ("jetson.model", "device tree model matches the configured board", True, check_jetson_model),
    ("jetson.l4t", "L4T userspace and firmware match the pinned release", True, check_jetson_l4t),
    ("gpu.device", "GPU device node present", True, check_gpu_device),
    ("cuda.runtime", "CUDA deviceQuery runs on the GPU", True, check_cuda),
    ("tensorrt.runtime", "TensorRT sample_onnx_mnist runs", True, check_tensorrt),
    ("camera", "V4L2 camera devices", True, check_camera),
    ("power.mode", "nvpmodel power mode matches robot.nix", True, check_power_mode),
    ("thermal", "thermal zones readable", True, check_thermal),
]


def load_build(s: System) -> Build | None:
    try:
        build = json.loads(s.read(BUILD_JSON) or "")
    except ValueError:
        return None
    return build if isinstance(build, dict) else None


def validate(s: System) -> dict[str, Any]:
    build = load_build(s)
    results = [("identity", "build.json present and consistent with the running system", check_identity(s, build))]
    for check_id, description, jetson, check in CHECKS:
        if build is None or "robot" not in build:
            result = Result(SKIPPED, f"{BUILD_JSON} unusable, see identity")
        elif jetson and build.get("jetson") is None:
            result = Result(NOT_APPLICABLE, f"board {build['robot']['board']} is not a Jetson")
        else:
            try:
                result = check(s, build)
            except Exception as e:  # noqa: BLE001 - a crashing check is a failed check, never a missing one
                result = Result(FAIL, f"check crashed: {type(e).__name__}: {e}")
        results.append((check_id, description, result))
    checks = [
        {"id": i, "description": d, "status": r.status, "detail": r.detail, "data": r.data, "warnings": r.warnings}
        for i, d, r in results
    ]
    return {
        "schema": SCHEMA,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": (s.read("/proc/sys/kernel/hostname") or "").strip(),
        "kernel": (s.read("/proc/sys/kernel/osrelease") or "").strip(),
        "build": build,
        "checks": checks,
        "summary": {st: sum(c["status"] == st for c in checks) for st in STATUSES},
    }


def report_errors(report: Any) -> list[str]:
    """Structural errors against schema 1; an empty list means the report is valid."""
    if not isinstance(report, dict):
        return ["report is not an object"]
    errors = []
    expected: dict[str, type | tuple[type, ...]] = {
        "schema": int, "timestamp": str, "host": str, "kernel": str, "build": (dict, type(None)), "checks": list, "summary": dict
    }
    for key, kind in expected.items():
        if not isinstance(report.get(key), kind):
            errors.append(f"{key}: missing or wrong type")
    if report.get("schema") != SCHEMA:
        errors.append(f"schema: expected {SCHEMA}")
    checks: list[Any] = report["checks"] if isinstance(report.get("checks"), list) else []
    for n, c in enumerate(checks):
        fields = {"id": str, "description": str, "status": str, "detail": str, "data": dict, "warnings": list}
        if not isinstance(c, dict) or any(not isinstance(c.get(k), t) for k, t in fields.items()):
            errors.append(f"checks[{n}]: missing or wrong field")
        elif c["status"] not in STATUSES:
            errors.append(f"checks[{n}]: unknown status {c['status']}")
    ids = [c.get("id") for c in checks if isinstance(c, dict)]
    if ids != ["identity"] + [c[0] for c in CHECKS]:
        errors.append("checks: ids missing, extra or out of order")
    if isinstance(report.get("summary"), dict) and report["summary"] != {
        st: sum(isinstance(c, dict) and c.get("status") == st for c in checks) for st in STATUSES
    }:
        errors.append("summary: does not match checks")
    return errors


def print_report(report: dict[str, Any]) -> None:
    b = report["build"] or {}
    robot = b.get("robot", {})
    print(f"{report['host']} ({robot.get('board', '?')})  revision {str(b.get('revision', '?'))[:12]}  "
          f"kernel {report['kernel']}  {report['timestamp']}")
    for c in report["checks"]:
        print(f"  {c['status']:<15} {c['id']:<18} {c['detail']}")
        for w in c["warnings"]:
            print(f"  {'':<15} {'':<18} warning: {w}")
    print("  " + ", ".join(f"{n} {st}" for st, n in report["summary"].items()))


def cmd_validate(args: argparse.Namespace) -> int:
    report = validate(System())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 1 if report["summary"][FAIL] else 0


def cmd_info(args: argparse.Namespace) -> int:
    s = System()
    build = load_build(s)
    info = {
        "build": build,
        "kernel": (s.read("/proc/sys/kernel/osrelease") or "").strip(),
        "generation": s.readlink(PROFILE),
        "current": s.realpath("/run/current-system"),
        "booted": s.realpath("/run/booted-system"),
    }
    if args.json:
        print(json.dumps(info, indent=2))
        return 0 if build else 1
    if build is None:
        print(f"{BUILD_JSON} missing or invalid", file=sys.stderr)
        return 1
    robot, jetson = build["robot"], build.get("jetson")
    print(f"robot       {robot['name']} ({robot['board']}), target .#{build['target']}, Omarchy Robots {build.get('version', '?')}")
    print(f"revision    {build['revision']}  robot.nix sha256 {build['robotNix']}")
    print("inputs      " + "  ".join(f"{k} {v[:12]}" for k, v in build["inputs"].items()))
    print(f"nixos       {build['nixos']}  kernel {info['kernel']}")
    runtime = build.get("runtime")
    print("runtime     " + (f"omarchy-runtime {runtime['version']}" if runtime else "not installed (omarchy.robot.runtime.enable)"))
    if jetson:
        print(f"jetson      {jetson['som']}/{jetson['carrierBoard']}  JetPack {jetson['jetpack']}  L4T {jetson['l4t']}")
    print(f"generation  {info['generation']}  current {info['current']}")
    if info["current"] != info["booted"]:
        print(f"booted      {info['booted']} (reboot pending)")
    return 0


def run(*cmd: str) -> int:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=False).returncode


def need_root() -> bool:
    if os.geteuid() != 0:
        print("omarchy-robot: run this as root (sudo)", file=sys.stderr)
        return False
    return True


def cmd_update(args: argparse.Namespace) -> int:
    if not need_root():
        return 2
    system = Path(args.source)
    if (system / "bin" / "switch-to-configuration").exists():
        return run("nix-env", "--profile", PROFILE, "--set", str(system)) or run(
            f"{PROFILE}/bin/switch-to-configuration", "switch"
        )
    build = load_build(System())
    ref = args.source if "#" in args.source or build is None else f"{args.source}#{build['target']}"
    return run("nixos-rebuild", "switch", "--flake", ref)


def cmd_rollback(args: argparse.Namespace) -> int:
    if not need_root():
        return 2
    return run("nix-env", "--profile", PROFILE, "--rollback") or run(f"{PROFILE}/bin/switch-to-configuration", "switch")


def main(argv: list[str] | None = None) -> int:
    os.environ["PATH"] = "/run/wrappers/bin:/run/current-system/sw/bin:" + os.environ.get("PATH", "")
    parser = argparse.ArgumentParser(prog="omarchy-robot", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("info", help="show this system's build identity")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("validate", help="run the bring-up checklist; exits 1 if a check fails")
    p.add_argument("--json", action="store_true", help="print the versioned JSON report")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("update", help="switch to a new system: a flake reference or a built system path")
    p.add_argument("source", help="flake (target appended unless given: github:me/robot) or /nix/store/...-nixos-system-...")
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("rollback", help="switch back to the previous generation")
    p.set_defaults(func=cmd_rollback)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
