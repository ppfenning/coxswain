"""Pure scope selector: which `cd <dir> && pytest` checks a patch can affect; every other command passes through.

A check marked `TOUCHED_ONLY` (`.agent-checks`' `# touched only`, the phase model's task gate) runs on the tests the
task touched (`touched_tests`), or not at all when there are none, under `TOUCHED_TIMEOUT_S`. It runs whole only when the
task changed test machinery the map cannot place (a conftest with no test below it, a helper no test imports, a data
file or `pytest.ini` under tests). Every other check is scoped as before, and may fall back to its whole component."""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TOUCHED_ONLY = "touched_only"
# A touched-only check's timeout unless it names its own: a change to a module most tests import still selects most of
# a suite, and three tasks running theirs at once on one machine outran the 600 s default in E1.
TOUCHED_TIMEOUT_S = 1800
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


def is_test_file(path: str) -> bool:
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


def _in_tests_dir(path: str) -> bool:
    return "tests" in path.split("/")[:-1]


def _infra_tests(path: str, component: str, tests: Collection[str], imports: Mapping[str, Any]) -> set[str] | None:
    """The tests a changed piece of test machinery under `component` reaches, or None when it cannot be mapped (the
    whole suite runs): a conftest reaches every test in its folder and below; a helper module in a tests folder
    reaches the tests whose imports include it; anything else there (a data file, `pytest.ini`) is not mapped."""
    name, folder = posixpath.basename(path), posixpath.dirname(path)
    if name == "conftest.py":
        below = {t for t in tests if _under(t, folder)}
        return below or None
    if name in _ALWAYS_WHOLE or not path.endswith(".py"):
        return None
    users = {t for t, reached in imports.items() if path in reached}
    return users or None


def touched_tests(
    component: str, changed_paths: Collection[str], added_tests: Collection[str], import_map: Mapping[str, Any]
) -> list[str] | None:
    """Pure. The test files under `component`, relative to it, that a task touched: each it added or changed, each
    whose imports reach a Python source it changed (in any component), and each a changed piece of its test machinery
    reaches (`_infra_tests`: a conftest, a helper module beside the tests). A deleted test is not one: `test_reach`
    maps the files the tree holds. None, the whole component, only for test machinery that cannot be mapped; [] when
    the task touched no test of it. `import_map["test_imports"]`, every file a test imports, tests folders included,
    maps the helpers; without it a helper is not mapped."""
    reach: Mapping[str, Any] = import_map["test_reach"]
    imports: Mapping[str, Any] = import_map.get("test_imports") or {}
    changed = set(changed_paths)
    machinery = [p for p in changed if not is_test_file(p) and _under(p, component)
                 and (posixpath.basename(p) in _ALWAYS_WHOLE or _in_tests_dir(_relative(p, component)))]
    sources = {p for p in changed if p.endswith(".py") and not is_test_file(p)}
    reached = {t for t, srcs in reach.items() if t in changed or set(srcs) & sources}
    for path in machinery:
        found = _infra_tests(path, component, reach, imports)
        if found is None:
            return None
        reached |= found
    return sorted(_relative(t, component) for t in reached | set(added_tests) if _under(t, component))


def select_checks(
    checks: Sequence[Check],
    changed_paths: Sequence[str],
    added_tests: Collection[str],
    import_map: Mapping[str, Any],
) -> ScopedChecks:
    """Scope `checks` to what the patch can affect; `import_map` holds components, test_reach and unresolved.

    A `TOUCHED_ONLY` check of a component runs on `touched_tests` alone, or is dropped when there are none; its
    selection is under `touched` (component to files) and `touched_skipped`. Order is the checks' own."""
    owner = {i: comp for i, c in enumerate(checks)
             if c.get(TOUCHED_ONLY) and (comp := pytest_component(str(c.get("cmd", ""))))}
    if not owner:
        return _select(checks, changed_paths, added_tests, import_map)
    plain_checks = [c for i, c in enumerate(checks) if i not in owner]
    scoped = _select(plain_checks, changed_paths, added_tests, import_map)
    files = {i: touched_tests(comp, changed_paths, added_tests, import_map) for i, comp in owner.items()}
    plain = iter(scoped.checks)  # `_select` keeps its checks' order and their names, dropping a skipped one
    upcoming = next(plain, None)
    kept: list[dict[str, Any]] = []
    for i, c in enumerate(checks):
        if i in owner:
            chosen = files[i]
            cmd = str(c["cmd"]) if chosen is None else _narrowed_cmd(str(c["cmd"]), chosen)
            kept += [{**c, "cmd": cmd, "timeout": c.get("timeout", TOUCHED_TIMEOUT_S)}] if chosen != [] else []
        elif upcoming is not None and upcoming.get("name") == c.get("name"):
            kept.append(upcoming)
            upcoming = next(plain, None)
    return ScopedChecks(tuple(kept), {
        **scoped.selection,
        "touched": {owner[i]: _WHOLE if f is None else f for i, f in files.items() if f != []},
        "touched_skipped": [owner[i] for i, f in files.items() if f == []],
    })


def _select(
    checks: Sequence[Check],
    changed_paths: Sequence[str],
    added_tests: Collection[str],
    import_map: Mapping[str, Any],
) -> ScopedChecks:
    """The scoping of every check not `TOUCHED_ONLY`: narrowed, whole, or skipped, per component."""
    owners = [(pytest_component(c.get("cmd", "")), c) for c in checks]
    known = [comp for comp, _ in owners if comp]
    added = sorted(added_tests)
    tests_of = {k: [t for t in added if _under(t, k)] for k in known}
    sources = {
        k: [p for p in changed_paths if _under(p, k) and p not in set(added) and not is_test_file(p)] for k in known
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
    touched = ", ".join(f"{c} (whole: test machinery it cannot map)" if files == _WHOLE
                        else f"{c} ({len(files)} touched test file{'' if len(files) == 1 else 's'})"
                        for c, files in selection.get("touched", {}).items())
    untouched = ", ".join(selection.get("touched_skipped", []))
    return (f"scoped checks: ran {ran}" + (f"; skipped {skipped}" if skipped else "")
            + (f"; touched only: {touched}" if touched else "") + (f"; no touched tests in {untouched}" if untouched else ""))
