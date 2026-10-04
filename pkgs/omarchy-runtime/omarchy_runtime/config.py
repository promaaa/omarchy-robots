"""Configuration: TOML files in config/ and scenarios/, merged into one
effective configuration per run, whose hash goes into every trace."""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from omarchy_runtime.belief import ObjectSpec, Region, Scene, Surface
from omarchy_runtime.contracts import BodySpec, ConfigError, Goal, Json, ns
from omarchy_runtime.runtime import Binding, Capability, Params

HERE = Path(__file__).parent
CONFIG = HERE / "config"
SCENARIOS = HERE / "scenarios"
OVERRIDABLE = ("runtime", "verification", "extensions")  # scenario tables merged into runtime.toml


def load(path: Path) -> Json:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e


def scenario_names() -> list[str]:
    return sorted(p.stem for p in SCENARIOS.glob("*.toml"))


def scenario_path(name: str) -> Path:
    path = Path(name)
    if path.suffix == ".toml":
        return path
    if name not in scenario_names():
        raise ConfigError(f"no scenario {name!r}; there are {', '.join(scenario_names())}")
    return SCENARIOS / f"{name}.toml"


def _merge(base: Json, over: Json) -> Json:
    out = copy.deepcopy(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def effective(scenario: str) -> Json:
    """Everything one run depends on, after defaults and overrides."""
    path = scenario_path(scenario)
    s = load(path)
    bodies = load(CONFIG / "bodies.toml")
    body = s.get("body")
    if body not in bodies:
        raise ConfigError(f"{path}: body {body!r} is not in bodies.toml ({', '.join(bodies)})")
    runtime = _merge(load(CONFIG / "runtime.toml"), {k: s.pop(k) for k in OVERRIDABLE if k in s})
    return {
        "scenario": {"name": path.stem} | s,
        "world": load(CONFIG / "world.toml"),
        "body": {"id": body} | bodies[body],
        "capabilities": load(CONFIG / "capabilities.toml"),
        "runtime": runtime,
    }


def digest(cfg: Json) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def get(table: Any, key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(table, dict) or key not in table:
        raise ConfigError(f"{where}: missing {key}")
    value = table[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, kind) or (isinstance(value, bool) and kind in (int, float)):
        raise ConfigError(f"{where}.{key}: expected {kind}, got {value!r}")
    return value


def floats(table: Any, key: str, where: str, n: int | None = None) -> tuple[float, ...]:
    value = get(table, key, list, where)
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value) or (n is not None and len(value) != n):
        raise ConfigError(f"{where}.{key}: expected {n or 'a list of'} numbers, got {value!r}")
    return tuple(float(v) for v in value)


def strings(table: Any, key: str, where: str) -> tuple[str, ...]:
    value = get(table, key, list, where)
    if not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}.{key}: expected a list of strings, got {value!r}")
    return tuple(value)


def scene(cfg: Json) -> Scene:
    w = cfg["world"]
    regions = {k: Region(*(get(v, a, float, f"regions.{k}") for a in ("x0", "x1", "z0", "z1"))) for k, v in w["regions"].items()}
    surfaces = {k: Surface(*(get(v, a, float, f"surfaces.{k}") for a in ("x0", "x1", "z"))) for k, v in w["surfaces"].items()}
    objects = {
        k: ObjectSpec(get(v, "width", float, f"objects.{k}"), get(v, "height", float, f"objects.{k}")) for k, v in w["objects"].items()
    }
    return Scene(regions, surfaces, objects)


def body(cfg: Json) -> BodySpec:
    b, where = cfg["body"], f"bodies.{cfg['body']['id']}"
    dims = strings(b, "dims", where)
    if "gripper" not in dims:
        raise ConfigError(f"{where}: dims must include gripper")
    n = len(dims)
    ws = floats(b, "workspace", where, 4)
    return BodySpec(
        id=b["id"],
        kind=get(b, "kind", str, where),
        action_space=get(b, "action_space", str, where),
        dims=dims,
        lower=floats(b, "lower", where, n),
        upper=floats(b, "upper", where, n),
        max_velocity=floats(b, "max_velocity", where, n),
        workspace=(ws[0], ws[1], ws[2], ws[3]),
        features=strings(b, "features", where),
        home=floats(b, "home", where, n),
        safe_pose=floats(b, "safe_pose", where, n),
        kinematics=get(b, "kinematics", dict, where),
    )


def capabilities(cfg: Json) -> list[Capability]:
    caps = []
    for cid, c in get(cfg["capabilities"], "capabilities", dict, "capabilities.toml").items():
        where = f"capabilities.{cid}"
        pre = []
        for p in strings(c, "preconditions", where):
            kind, _, param = p.partition(" ")
            pre.append((kind, param))
        caps.append(Capability(cid, get(c, "achieves", str, where), strings(c, "params", where), strings(c, "requires", where), tuple(pre)))
    return caps


def bindings(cfg: Json) -> list[Binding]:
    out = []
    for n, b in enumerate(get(cfg["capabilities"], "bindings", list, "capabilities.toml")):
        where = f"bindings[{n}]"
        out.append(
            Binding(
                get(b, "capability", str, where),
                get(b, "implementation", str, where),
                get(b, "version", str, where),
                strings(b, "bodies", where),
                get(b, "priority", int, where),
            )
        )
    return out


def params(cfg: Json) -> Params:
    r, v = cfg["runtime"]["runtime"], cfg["runtime"]["verification"]
    return Params(
        deadline=ns(get(r, "deadline_s", float, "runtime")),
        s1_stall_timeout=ns(get(r, "s1_stall_timeout_s", float, "runtime")),
        freshness=ns(get(r, "freshness_s", float, "runtime")),
        settle=ns(get(v, "settle_s", float, "verification")),
        verify_budget=ns(get(v, "budget_s", float, "verification")),
    )


def goals(table: Any, where: str) -> list[Goal]:
    return [Goal(get(g, "predicate", str, where), strings(g, "args", where)) for g in get(table, "goals", list, where)]
