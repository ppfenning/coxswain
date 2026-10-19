"""The pure core of `devtools release-check`: drifts computed from facts the
edge gathers. No check here reads a file, runs a command or touches the
network — `cli.py` gathers the facts named by `facts_plan` and calls in."""

from __future__ import annotations

import dataclasses
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

from devtools import release, release_check_cli, release_check_notes
from devtools.release_check_index import check_release_index
from devtools.release_check_manifest import check_manifest
from devtools.release_check_pages import check_pages
from devtools.release_check_readmes import check_readmes


@dataclasses.dataclass(frozen=True)
class Drift:
    check: str
    a_file: str
    a_line: int | None
    b_file: str
    b_line: int | None
    correction: str


def check_versions(facts: Mapping) -> list[Drift]:
    """Every component pyproject and the umbrella's must equal `expected_version`
    (the manifest's `coxswain.version`, gathered by `gather_version_facts`); a
    component whose manifest `tag` is not `v<expected>` is pinned and compares against its own `tag` instead (advisory).
    A component with no pyproject.toml version (an empty or missing `component_pyprojects` entry) compares its
    `component_cargos` entry's `[package].version` instead."""
    expected = facts.get("expected_version")
    if expected is None:
        return []
    manifest_file = facts.get("manifest_path", "manifest.toml")

    def mismatch(label: str, version_file: str, found: str | None) -> Drift | None:
        if found is None or found == expected:
            return None
        return Drift("versions", manifest_file, None, version_file, None,
                     f"{label} {Path(version_file).name} is {found}, manifest wants {expected} "
                     f"(devtools release {expected} performs the bump)")

    umbrella_file = str(Path(facts.get("umbrella", "coxswain")) / "pyproject.toml")
    umbrella_found = facts.get("umbrella_pyproject", {}).get("project", {}).get("version")
    umbrella_drift = mismatch("umbrella", umbrella_file, umbrella_found)

    pinned = {name for name, spec in facts.get("components", {}).items() if spec.get("tag") != f"v{expected}"}
    pyprojects = facts.get("pyprojects", {})
    cargos = facts.get("component_cargos", {})

    def found_version(name: str, pyproject: Mapping | None) -> tuple[str, str | None]:
        """`(file, version)`: the pyproject.toml's version, else the Cargo.toml's
        `[package].version` beside it, so the drift names the file it read."""
        pyproject_file = pyprojects.get(name, f"{name}/pyproject.toml")
        version = (pyproject or {}).get("project", {}).get("version")
        cargo_version = (cargos.get(name) or {}).get("package", {}).get("version")
        if version is None and cargo_version is not None:
            return str(Path(pyproject_file).with_name("Cargo.toml")), cargo_version
        return pyproject_file, version

    component_drifts = [
        d
        for name, pyproject in facts.get("component_pyprojects", {}).items()
        if name not in pinned
        for d in [mismatch(name, *found_version(name, pyproject))]
        if d is not None
    ]
    # Advisory: `devtools release` folds a changed pinned component back in on its own (`rejoin`).
    lockstep_drifts = [
        Drift("lockstep", manifest_file, None, pyprojects.get(name, f"{name}/pyproject.toml"), None,
              f"{name} is pinned at {facts['components'][name]['tag']} but its pyproject.toml is {found}")
        for name in pinned
        for found in [(facts.get("component_pyprojects", {}).get(name) or {}).get("project", {}).get("version")]
        if found is not None and "v" + found != facts["components"][name]["tag"]
    ]
    return ([umbrella_drift] if umbrella_drift is not None else []) + component_drifts + lockstep_drifts


_BLANK_LABELS = {
    "opener": "write the opener paragraph, 3 to 5 sentences introducing the release",
    "known-gaps": "write the Known gaps paragraph, on what this release does not do yet",
}


def check_notes_blanks(facts: Mapping) -> list[Drift]:
    """One drift per blank marker the version's notes text still holds, in `find_blanks` order."""
    notes_path = facts.get("release_notes") or "docs/releases/<version>.md"
    return [
        Drift("notes_blank", notes_path, None, notes_path, None,
              f"notes still hold the {name} blank: {_BLANK_LABELS.get(name, 'write it')}")
        for name in release.blank_names(facts.get("notes_text", ""))
    ]


def check_notes_page(facts: Mapping) -> list[Drift]:
    """The notes page's bullet citations, then its unwritten blanks, as one registered check."""
    return release_check_notes.check_notes(facts) + check_notes_blanks(facts)


def gather_version_facts(manifest: Mapping, manifest_path: str, component_dirs: Mapping[str, str], umbrella: str) -> dict:
    """Edge for `check_versions`: the manifest's declared version, and each
    component's and the umbrella's pyproject.toml read off disk (`{}` when
    absent), plus each component's Cargo.toml read the same way into
    `component_cargos` — the fact a Rust-only component's `[package].version`
    is read from, since it carries no pyproject.toml at all."""

    def read(path: Path) -> dict:
        return tomllib.loads(path.read_text()) if path.exists() else {}

    version = manifest.get("coxswain", {}).get("version")
    # Both `release` and `release-check` gather here, so the one notes read for the blank check lives at this edge.
    notes_path = Path(umbrella) / "docs" / "releases" / f"{version}.md" if version else None
    return {
        "notes_text": notes_path.read_text() if notes_path and notes_path.exists() else "",
        "expected_version": version,
        "manifest_path": manifest_path,
        "components": manifest.get("components", {}),
        "component_pyprojects": {name: read(Path(d) / "pyproject.toml") for name, d in component_dirs.items()},
        "component_cargos": {name: read(Path(d) / "Cargo.toml") for name, d in component_dirs.items()},
        "umbrella_pyproject": read(Path(umbrella) / "pyproject.toml"),
    }


CHECKS: tuple[Callable[[Mapping], list[Drift]], ...] = (release_check_cli.check_cli_surface, check_manifest, check_notes_page, check_pages, check_readmes, check_versions, check_release_index)


def run_checks(facts: Mapping, checks: tuple[Callable[[Mapping], list[Drift]], ...] | None = None) -> list[Drift]:
    resolved = CHECKS if checks is None else checks
    return [drift for check in resolved for drift in check(facts)]


def _side(file: str, line: int | None) -> str:
    return f"{file}:{line}" if line is not None else file


def render(drifts: list[Drift], checks_run: int) -> str:
    if checks_run == 0:
        return "no checks registered: nothing measured"
    if not drifts:
        return f"no drift ({checks_run} checks)"
    return "\n".join(
        f"{d.check}: {_side(d.a_file, d.a_line)} <-> {_side(d.b_file, d.b_line)} — {d.correction}"
        for d in drifts
    )


def to_json(drifts: list[Drift]) -> list[dict]:
    return [dataclasses.asdict(d) for d in drifts]


# The folders that were their own repositories through 0.47.0. They ship in this checkout now, never
# pinned; release-check still reads their READMEs (and maps their pre-rename names) from here.
IN_REPO_COMPONENTS = {"tools": "src/tools", "graphs": "src/graphs", "cartridges": "src/cartridges", "crew": "crew"}


def component_dirs_for(umbrella: str, manifest: Mapping, overrides: Mapping[str, str] | None = None,
                       root: str | None = None) -> dict[str, str]:
    """The checkout of each `[components]` entry (dash): `overrides[name]`, else beside the coxswain checkout
    (`root`, default the checkout's parent) as `release.component_dir` resolves it."""
    base = root or str(Path(umbrella).resolve().parent)
    return {name: release.component_dir(base, name, overrides) for name in manifest.get("components", {})}


def facts_plan(umbrella: str, manifest: Mapping, component_dirs: Mapping[str, str] | None = None) -> dict:
    """Where every fact lives for the coxswain checkout at `umbrella`. `component_dirs` holds the checkout of
    each `[components]` entry (default: `component_dirs_for`); the in-repo folders are read from `umbrella`."""
    components = manifest.get("components", {})
    version = manifest.get("coxswain", {}).get("version")
    dirs = dict(component_dirs) if component_dirs is not None else component_dirs_for(umbrella, manifest)
    in_repo = {name: str(Path(umbrella) / folder) for name, folder in IN_REPO_COMPONENTS.items()}
    return {
        "root": umbrella,
        "umbrella": umbrella,
        "cli_docs_dir": str(Path(umbrella) / "docs" / "reference" / "cli"),
        "component_dirs": dirs,
        "component_docs": {name: str(Path(umbrella) / "docs" / "components" / f"{name}.md") for name in components},
        "release_notes": str(Path(umbrella) / "docs" / "releases" / f"{version}.md") if version else None,
        "readmes": {name: str(Path(d) / "README.md") for name, d in {**in_repo, **dirs}.items()},
        "pyprojects": {name: str(Path(d) / "pyproject.toml") for name, d in dirs.items()},
    }
