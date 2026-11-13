"""Pure scope selector: which `cd <dir> && pytest` checks a patch can affect; every other command passes through."""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_WHOLE = "whole"
_ALWAYS_WHOLE = frozenset({"conftest.py", "pytest.ini"})

Check = Mapping[str, Any]
Plan = tuple[str, "list[str] | None"]


@dataclass(frozen=True)
class ScopedChecks:
    checks: tuple[dict[str, Any], ...]
    selection: dict[str, Any]


def pytest_component(cmd: str) -> str | None:
    """The <dir> of a `cd <dir> && pytest ...` command, else None."""
    steps = [step.strip() for step in cmd.split("&&")]
    try:
        head = shlex.split(steps[0])
        last = shlex.split(steps[-1])
    except ValueError:
        return None
    is_cd = len(steps) > 1 and len(head) == 2 and head[0] == "cd"
    is_pytest = bool(last) and posixpath.basename(last[0]) == "pytest"
    return head[1].rstrip("/") if is_cd and is_pytest else None


def _under(path: str, component: str) -> bool:
    return path.startswith(component + "/")


def _relative(path: str, component: str) -> str:
    return path[len(component) + 1 :]


def _is_test_file(path: str) -> bool:
    name = posixpath.basename(path)
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")


def _affected(components: Mapping[str, Collection[str]], seeds: Mapping[str, str]) -> dict[str, str]:
    """Seeds plus every component that imports an affected one, transitively."""
    new = {
        c: f"imports {sorted(set(deps) & set(seeds))[0]}"
        for c, deps in components.items()
        if c not in seeds and set(deps) & set(seeds)
    }
    return dict(seeds) if not new else _affected(components, {**seeds, **new})


def _fallback_reason(path: str, import_map: Mapping[str, Any], reached: Collection[str]) -> str | None:
    """Why `path` forces the whole component, or None when the map can narrow it."""
    name = posixpath.basename(path)
    if name in _ALWAYS_WHOLE:
        return f"{name} changed"
    if not path.endswith(".py"):
        return f"non-Python file {path}"
    if path in import_map["unresolved"]:
        return f"unresolved {path}"
    if path not in reached:
        return f"unmapped {path}"
    return None


def _narrow(
    component: str, sources: Sequence[str], tests: Sequence[str], import_map: Mapping[str, Any]
) -> tuple[list[str] | None, str]:
    """The component's test files for these changes, or (None, why) for the whole component."""
    reach = import_map["test_reach"]
    reached = frozenset().union(*reach.values())
    why = next((w for w in (_fallback_reason(p, import_map, reached) for p in sources) if w), None)
    hit = sorted(t for t, srcs in reach.items() if _under(t, component) and set(srcs) & set(sources))
    if why:
        return None, why
    if not hit:
        return None, "no test reaches the change"
    return sorted({_relative(p, component) for p in [*hit, *tests]}), "changed"


def _plan(
    component: str,
    changed: bool,
    affected_imports: Sequence[str],
    sources: Sequence[str],
    tests: Sequence[str],
    import_map: Mapping[str, Any],
) -> Plan | None:
    """(reason, narrowed files or None for whole) for a component, None to skip it.

    A component importing an affected one runs whole, even when it also changed itself.
    """
    if affected_imports:
        return (f"imports {affected_imports[0]}", None)
    if changed:
        files, why = _narrow(component, sources, tests, import_map)
        return (why if files is not None else f"fallback {why}", files)
    if tests:
        return ("new test", sorted(_relative(t, component) for t in tests))
    return None


def _narrowed_cmd(cmd: str, files: Sequence[str]) -> str:
    return " ".join([cmd, *(shlex.quote(f) for f in files)])


def _scoped_cmd(cmd: str, plan: Plan | None) -> str:
    return cmd if plan is None or plan[1] is None else _narrowed_cmd(cmd, plan[1])


def _test_counts(import_map: Mapping[str, Any], component: str, files: Sequence[str] | None) -> dict[str, int]:
    """Test files selected out of every test file the map knows under the component; None means it runs whole."""
    total = sum(1 for t in import_map["test_reach"] if _under(t, component))
    return {"selected": total if files is None else len(files), "total": total}


def _full(
    checks: Sequence[Check], components: Sequence[str], why: str, added: Sequence[str], import_map: Mapping[str, Any]
) -> ScopedChecks:
    return ScopedChecks(
        tuple(dict(c) for c in checks),
        {
            "components_run": list(components),
            "components_skipped": [],
            "narrowed": dict.fromkeys(components, _WHOLE),
            "tests": {c: _test_counts(import_map, c, None) for c in components},
            "added_tests": list(added),
            "reason": dict.fromkeys(components, f"fallback {why}"),
        },
    )


def select_checks(
    checks: Sequence[Check],
    changed_paths: Sequence[str],
    added_tests: Collection[str],
    import_map: Mapping[str, Any],
) -> ScopedChecks:
    """Scope `checks` to what the patch can affect; `import_map` holds components, test_reach and unresolved."""
    owners = [(pytest_component(c.get("cmd", "")), c) for c in checks]
    known = [comp for comp, _ in owners if comp]
    added = sorted(added_tests)
    tests_of = {k: [t for t in added if _under(t, k)] for k in known}
    sources = {
        k: [p for p in changed_paths if _under(p, k) and p not in set(added) and not _is_test_file(p)] for k in known
    }
    components = import_map["components"]
    all_components = {*components, *known}
    seeds = {k: "changed" for k in all_components if any(_under(p, k) and p not in set(added) for p in changed_paths)}
    reasons = _affected(components, seeds)
    imports = {k: sorted((set(components.get(k, ())) - {k}) & set(reasons)) for k in known}
    plans = {k: _plan(k, k in seeds, imports[k], sources[k], tests_of[k], import_map) for k in known}
    if not changed_paths:
        return _full(checks, known, "empty patch", added, import_map)
    if not any(plans.values()):
        return _full(checks, known, "no changed path under a known component", added, import_map)
    kept = tuple(
        {**c, "cmd": c["cmd"] if comp is None else _scoped_cmd(c["cmd"], plans[comp])}
        for comp, c in owners
        if comp is None or plans[comp] is not None
    )
    run = [k for k in known if plans[k] is not None]
    return ScopedChecks(
        kept,
        {
            "components_run": run,
            "components_skipped": [k for k in known if plans[k] is None],
            "narrowed": {k: plans[k][1] if plans[k][1] is not None else _WHOLE for k in run},
            "tests": {k: _test_counts(import_map, k, plans[k][1]) for k in run},
            "added_tests": added,
            "reason": {k: plans[k][0] for k in run},
        },
    )


def render_selection(selection: Mapping[str, Any]) -> str:
    """One short line for an evidence row."""

    def label(component: str) -> str:
        files = selection["narrowed"][component]
        if files == _WHOLE:
            return f"{component} (whole)"
        total = selection["tests"][component]["total"]
        return f"{component} (narrowed to {len(files)} of {total} file{'' if total == 1 else 's'})"

    ran = ", ".join(label(c) for c in selection["components_run"])
    skipped = ", ".join(selection["components_skipped"])
    return f"scoped checks: ran {ran}" + (f"; skipped {skipped}" if skipped else "")
