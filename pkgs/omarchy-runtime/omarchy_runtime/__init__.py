"""Omarchy Robots runtime: goals in, verified executions and complete traces out."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("omarchy-runtime")
except PackageNotFoundError:  # running from the source tree
    __version__ = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]["version"]
