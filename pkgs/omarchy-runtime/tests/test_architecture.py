"""Binding principles that the import graph can show: contracts and the runtime
core never import the simulator, nothing imports ROS, only wallclock.py reads
the wall clock, and numpy is the only dependency outside the standard library
(the development tools aside: MuJoCo, in two modules that only a command
imports, inside a function)."""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "omarchy_runtime"
# The control plane and its contracts: what a real robot keeps when the simulator goes.
CORE = {
    "__init__",
    "contracts",
    "belief",
    "runtime",
    "sched",
    "trace",
    "config",
    "extensions",
    "wallclock",
    "s2",
    "link",
    "guard",
    "s0",
    "controllers",
    "attribution",
}
COMPOSITION = (
    "omarchy_runtime.sim",
    "omarchy_runtime.scenario",
    "omarchy_runtime.evaluate",
    "omarchy_runtime.placement",
    "omarchy_runtime.cli",
)


def imports(path: Path) -> set[str]:
    """Every module a file imports, `from a import b` counted as a and a.b.
    Relative imports are refused, so that every import is visible here."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                raise AssertionError(f"{path.name}: relative import (line {node.lineno})")
            names |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
    return names


# The development tools, in the omarchy-runtime-tools package only: what each may import besides,
# and the modules that may import them, never at their top level.
TOOLS = {"sim.panda": {"mujoco"}, "scrub": {"mujoco"}}
TOOL_USERS = {"cli", "scenario", "scrub"}

TMP = tempfile.TemporaryDirectory()
unittest.addModuleCleanup(TMP.cleanup)
MODULES = {p.relative_to(PACKAGE).with_suffix("").as_posix().replace("/", "."): imports(p) for p in PACKAGE.rglob("*.py")}


class TestArchitecture(unittest.TestCase):
    def test_core_never_imports_the_simulator(self) -> None:
        for module in CORE:
            with self.subTest(module):
                bad = {name for name in MODULES[module] if name.startswith(COMPOSITION)}
                self.assertEqual(bad, set())

    def test_the_import_reader_sees_every_form(self) -> None:
        path = Path(TMP.name) / "sample.py"
        path.write_text("import a.b\nfrom omarchy_runtime import scenario\n")
        self.assertEqual(imports(path), {"a.b", "omarchy_runtime", "omarchy_runtime.scenario"})
        path.write_text("from .sim import world\n")
        with self.assertRaises(AssertionError):
            imports(path)

    def test_nothing_imports_ros(self) -> None:
        for module, names in MODULES.items():
            roots = {n.split(".")[0] for n in names}
            self.assertEqual({r for r in roots if r.startswith(("ros", "rcl")) or "dds" in r}, set(), module)

    def test_only_wallclock_reads_the_wall_clock(self) -> None:
        for module, names in MODULES.items():
            if module != "wallclock":
                self.assertEqual(names & {"time", "datetime"}, set(), module)

    def test_numpy_is_the_only_dependency(self) -> None:
        for module, names in MODULES.items():
            roots = {n.split(".")[0] for n in names} - {"omarchy_runtime", "numpy", "__future__"} - TOOLS.get(module, set())
            self.assertEqual(roots - set(sys.stdlib_module_names), set(), module)

    def test_the_robot_package_runs_without_the_tools(self) -> None:
        tools = {f"omarchy_runtime.{m}" for m in TOOLS}
        for path in PACKAGE.rglob("*.py"):
            module = path.relative_to(PACKAGE).with_suffix("").as_posix().replace("/", ".")
            if module in TOOLS:
                continue
            top = set()
            for node in ast.parse(path.read_text()).body:  # the module's top level only
                if isinstance(node, ast.ImportFrom) and node.module:
                    top |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
                elif isinstance(node, ast.Import):
                    top |= {a.name for a in node.names}
            with self.subTest(module):
                self.assertEqual(top & (tools | {"mujoco"}), set())
                if MODULES[module] & tools:
                    self.assertIn(module, TOOL_USERS)


if __name__ == "__main__":
    unittest.main()
