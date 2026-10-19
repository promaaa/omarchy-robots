"""Binding principles that the import graph can show: contracts and the runtime
core never import the simulator, nothing imports ROS, only wallclock.py reads
the wall clock, and numpy is the only dependency outside the standard library."""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "omarchy_runtime"
# The control plane and its contracts: what a real robot keeps when the simulator goes.
CORE = {"__init__", "contracts", "belief", "runtime", "sched", "trace", "config", "extensions", "wallclock", "s2", "link", "guard", "s0", "controllers", "attribution"}
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
            roots = {n.split(".")[0] for n in names} - {"omarchy_runtime", "numpy", "__future__"}
            self.assertEqual(roots - set(sys.stdlib_module_names), set(), module)


if __name__ == "__main__":
    unittest.main()
