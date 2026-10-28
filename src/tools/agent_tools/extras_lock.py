"""`cox setup extras`: the optional extras, pinned and hash-checked from this release's uv.lock.

The shipped file `extras-requirements.txt` is generated from uv.lock by `python -m agent_tools.extras_lock`.
The core renders and parses it and plans the pip call; the edge at the bottom reads the file, the installed
versions and the interpreter, and runs pip.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib.metadata
import importlib.resources
import operator
import os
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

EXTRAS = ("traces", "parquet", "lake", "postgres")
REQUIREMENTS_NAME = "extras-requirements.txt"
DEFAULT_LOCK = "uv.lock"
DEFAULT_OUT = f"src/tools/agent_tools/{REQUIREMENTS_NAME}"
_PACKAGE = "coxswain"
_HEADER = (
    "# Generated from uv.lock by `python -m agent_tools.extras_lock`. Do not edit.",
    "# Every line is pinned and carries the sha256 of each locked wheel; the trailing comment names the extras that need it.",
)
_HASH = r"--hash=sha256:[0-9a-f]{64}"
_LINE = re.compile(
    rf"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>\S+)(?: ; (?P<marker>.+?))? (?P<hashes>{_HASH}(?: {_HASH})*)  # extras: (?P<extras>[a-z0-9_,-]+)$"
)
_CLAUSE = re.compile(r"^\s*(\w+)\s*(==|!=|<=|>=|<|>)\s*['\"]([^'\"]*)['\"]\s*$")
_OPS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}

Edge = tuple[str, str | None, str | None]  # package, extra of that package, marker on the dependency that named it


@dataclasses.dataclass(frozen=True)
class Pin:
    """One locked package. `marker` is a PEP 508 string or None; `hashes` are `sha256:<hex>` strings."""

    name: str
    version: str
    hashes: tuple[str, ...]
    marker: str | None
    extras: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class LockError:
    reason: str


@dataclasses.dataclass(frozen=True)
class Plan:
    """`action` is "present" (nothing to do) or "install"; `argv` is empty for "present"."""

    action: str
    pins: tuple[Pin, ...]
    argv: tuple[str, ...]


def _dep_edges(dep: dict) -> list[Edge]:
    marker = dep.get("marker")
    return [(dep["name"], None, marker), *((dep["name"], x, marker) for x in dep.get("extra", []))]


def _out_edges(packages: Mapping[str, list[dict]], edge: Edge) -> list[Edge]:
    """The edges one step on. A dependency's own marker is kept and its parent's is dropped, which can only widen the set."""
    name, extra, _ = edge
    pkg = (packages.get(name) or [{}])[0]
    deps = pkg.get("dependencies", []) if extra is None else pkg.get("optional-dependencies", {}).get(extra, [])
    return [e for dep in deps for e in _dep_edges(dep)]


def _reach(packages: Mapping[str, list[dict]], edges: frozenset[Edge]) -> frozenset[Edge]:
    grown = edges | frozenset(o for e in edges for o in _out_edges(packages, e))
    return edges if grown == edges else _reach(packages, grown)


def _joined(markers: frozenset[str | None]) -> str | None:
    """None when any path is unconditional; else the markers joined with `or`."""
    if None in markers:
        return None
    ordered = sorted(m for m in markers if m)
    return ordered[0] if len(ordered) == 1 else " or ".join(f"({m})" for m in ordered)


def _pin(name: str, markers: frozenset[str | None], extras: tuple[str, ...], packages: Mapping[str, list[dict]]) -> Pin | LockError:
    entries = packages.get(name, [])
    if len(entries) != 1:
        return LockError(f"{name} is locked {len(entries)} times in uv.lock; this file needs exactly one version")
    wheels = entries[0].get("wheels", [])
    hashes = tuple(w.get("hash", "") for w in wheels)
    if not hashes:
        return LockError(f"{name} {entries[0].get('version')} has no wheel in uv.lock, so there is no hash to verify")
    if not all(re.fullmatch(r"sha256:[0-9a-f]{64}", h) for h in hashes):
        return LockError(f"{name} has a wheel in uv.lock without a sha256 hash")
    return Pin(name, entries[0]["version"], hashes, _joined(markers), extras)


def lock_pins(lock_text: str, extras: tuple[str, ...] = EXTRAS) -> tuple[Pin, ...] | LockError:
    """Every package the named extras of coxswain reach in uv.lock, sorted by name, each with the extras that need it."""
    try:
        doc = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError as err:
        return LockError(f"uv.lock is not valid TOML: {err}")
    packages: dict[str, list[dict]] = {}
    for pkg in doc.get("package", []):
        packages[pkg.get("name", "")] = [*packages.get(pkg.get("name", ""), []), pkg]
    own = packages.get(_PACKAGE, [{}])[0].get("optional-dependencies", {})
    unknown = [x for x in extras if x not in own]
    if unknown:
        return LockError(f"uv.lock has no extra named {', '.join(unknown)}")
    reached = {x: _reach(packages, frozenset(e for dep in own[x] for e in _dep_edges(dep))) for x in extras}
    names = sorted({n for edges in reached.values() for (n, _, _) in edges})
    pins = tuple(
        _pin(
            n,
            frozenset(m for edges in reached.values() for (en, ex, m) in edges if en == n and ex is None),
            tuple(x for x in extras if any(en == n for (en, _, _) in reached[x])),
            packages,
        )
        for n in names
    )
    failed = next((p for p in pins if isinstance(p, LockError)), None)
    return failed if failed is not None else tuple(p for p in pins if isinstance(p, Pin))


def render_pins(pins: tuple[Pin, ...]) -> str:
    def line(pin: Pin) -> str:
        marker = f" ; {pin.marker}" if pin.marker else ""
        hashes = " ".join(f"--hash={h}" for h in pin.hashes)
        return f"{pin.name}=={pin.version}{marker} {hashes}  # extras: {','.join(pin.extras)}"

    return "\n".join([*_HEADER, *map(line, pins)]) + "\n"


def render_requirements(lock_text: str, extras: tuple[str, ...] = EXTRAS) -> str | LockError:
    pins = lock_pins(lock_text, extras)
    return pins if isinstance(pins, LockError) else render_pins(pins)


def parse_requirements(text: str) -> tuple[Pin, ...] | LockError:
    """The pins in a rendered file. A line that is not a pinned, hashed requirement is an error, never skipped."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    matches = [(ln, _LINE.match(ln)) for ln in lines]
    bad = next((ln for ln, m in matches if m is None), None)
    if bad is not None:
        return LockError(f"{REQUIREMENTS_NAME} has a line that is not pinned and hashed: {bad[:60]}")
    return tuple(
        Pin(
            m["name"],
            m["version"],
            tuple(h.removeprefix("--hash=") for h in m["hashes"].split()),
            m["marker"],
            tuple(m["extras"].split(",")),
        )
        for _, m in matches
        if m is not None
    )


def select(pins: tuple[Pin, ...], extras: tuple[str, ...]) -> tuple[Pin, ...]:
    return tuple(p for p in pins if set(p.extras) & set(extras))


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(m.group()) for part in text.split(".") if (m := re.match(r"\d+", part)))


def _clause_holds(text: str, env: Mapping[str, str]) -> bool:
    m = _CLAUSE.match(text)
    if m is None or m[1] not in env:
        return True
    var, op, literal = m.groups()
    left, right = (_version_tuple(env[var]), _version_tuple(literal)) if var.endswith("version") else (env[var], literal)
    return _OPS[op](left, right)


def marker_applies(marker: str | None, env: Mapping[str, str]) -> bool:
    """Whether a PEP 508 marker holds in `env`. Anything this cannot read counts as holding, so pip decides it."""
    if marker is None or "(" in marker:
        return True
    return any(all(_clause_holds(c, env) for c in alt.split(" and ")) for alt in marker.split(" or "))


def pip_argv(prefix: str, requirements_path: str) -> tuple[str, ...]:
    return (
        str(Path(prefix) / "bin" / "python"), "-m", "pip", "install", "--require-hashes", "--only-binary", ":all:",
        "--no-deps", "--disable-pip-version-check", "-r", requirements_path,
    )


def plan_install(
    pins: tuple[Pin, ...], extras: tuple[str, ...], prefix: str, requirements_path: str,
    installed: Mapping[str, str], env: Mapping[str, str],
) -> Plan:
    """Nothing to do when every pin that applies here is installed at its locked version; else the pip call."""
    chosen = select(pins, extras)
    missing = [p for p in chosen if marker_applies(p.marker, env) and installed.get(p.name) != p.version]
    if not missing:
        return Plan("present", chosen, ())
    return Plan("install", chosen, pip_argv(prefix, requirements_path))


def render_plan(plan: Plan, extras: tuple[str, ...], prefix: str, dry_run: bool) -> str:
    if plan.action == "present":
        return f"extras {', '.join(extras)} are already installed at the locked versions in {prefix}; nothing to do"
    verb = "would install" if dry_run else "installing"
    pins = [f"  {p.name}=={p.version}" for p in plan.pins]
    return "\n".join([f"{verb} extras {', '.join(extras)} into {prefix}: {len(pins)} packages, every wheel hash-checked", *pins])


def write_requirements(lock_path: Path, out_path: Path) -> int:
    """Edge. Writes nothing when uv.lock cannot be rendered."""
    rendered = render_requirements(lock_path.read_text())
    if isinstance(rendered, LockError):
        print(f"refusing: {rendered.reason}", file=sys.stderr)
        return 1
    out_path.write_text(rendered)
    return 0


def current_env() -> dict[str, str]:
    return {
        "sys_platform": sys.platform,
        "implementation_name": sys.implementation.name,
        "python_full_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_python_implementation": platform.python_implementation(),
        "os_name": os.name,
    }


def installed_versions(pins: tuple[Pin, ...]) -> dict[str, str]:
    """Edge. The version of each pinned package installed in this interpreter; a missing one is left out."""
    found: dict[str, str] = {}
    for pin in pins:
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            found = {**found, pin.name: importlib.metadata.version(pin.name)}
    return found


def shipped_requirements() -> str:
    return importlib.resources.files("agent_tools").joinpath(REQUIREMENTS_NAME).read_text()


def run_extras(
    extras: tuple[str, ...], dry_run: bool, *, prefix: str, requirements_text: str,
    runner: Callable[[list[str]], subprocess.CompletedProcess],
    installed: Callable[[tuple[Pin, ...]], Mapping[str, str]], env: Mapping[str, str], out: Callable[[str], None],
) -> int:
    """Edge. Exit 2 for a refusal, pip's own code for a failed install, else 0. `--dry-run` never calls `runner`."""
    unknown = [x for x in extras if x not in EXTRAS]
    if unknown:
        out(f"refusing: unknown extra {', '.join(unknown)}; choose from {', '.join(EXTRAS)}")
        return 2
    pins = parse_requirements(requirements_text)
    if isinstance(pins, LockError):
        out(f"refusing: {pins.reason}")
        return 2
    plan = plan_install(pins, extras, prefix, "<the selected pins, written to a temporary file>", installed(select(pins, extras)), env)
    out(render_plan(plan, extras, prefix, dry_run))
    if plan.action == "present" or dry_run:
        if plan.action == "install":
            out("pip argv: " + " ".join(plan.argv))
        return 0
    with tempfile.TemporaryDirectory(prefix="cox-extras-") as tmp:
        path = Path(tmp) / REQUIREMENTS_NAME
        path.write_text(render_pins(plan.pins))
        return runner([*plan.argv[:-1], str(path)]).returncode


def setup_extras(with_extras: list[str] | None, dry_run: bool) -> int:
    return run_extras(
        tuple(with_extras or EXTRAS), dry_run, prefix=sys.prefix, requirements_text=shipped_requirements(),
        runner=lambda argv: subprocess.run(argv, check=False), installed=installed_versions, env=current_env(), out=print,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_tools.extras_lock", description="Regenerate the shipped extras requirements from uv.lock.")
    parser.add_argument("--lock", default=DEFAULT_LOCK, type=Path)
    parser.add_argument("--out", default=DEFAULT_OUT, type=Path)
    args = parser.parse_args(argv)
    return write_requirements(args.lock, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
