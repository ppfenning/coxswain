"""The rehearsal scenarios the release-rehearsal workflow runs, one result line each.

Design: docs/release-rehearsal.md. The pure core builds the plan, refuses bad inputs, judges evidence and builds the
verdict, all from plain data. The edge at the bottom runs the plan's steps as processes and is the only part that
touches the machine. Stdlib only: the workflow runs it with the container's apt python3.

How the candidate commits get installed: the documented install command puts each component in a git checkout under
the root. The fresh scenario installs into the default coxswain home, {home}/data/coxswain, with no root flag; the
released PyPI tool cannot do that, so an `@cox` step installs the candidate tools first. The `@pin` step then checks
every checkout out at its candidate SHA, reinstalls `cox` from the tools SHA the way
docs.yml installs it at a ref, and reads the result back. A scenario passes only when that read-back shows the SHAs in the
run title, so a green run proves what it names. No cox flag that pins a commit exists, so none is used.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

UMBRELLA_URL = "https://github.com/ppfenning/coxswain"
TOOLS_URL = "git+https://github.com/ppfenning/coxswain-tools"
COMPONENTS = ("cartridges", "graphs", "tools", "crew", "dash")
INPUTS = (*(f"{c}_sha" for c in COMPONENTS), "previous_tag", "migrations")
ORDER = ("fresh", "upgrade", "migrate")
SHA = re.compile(r"[0-9a-f]{40}")
POPULATE, PIN, DOCTOR, COX = "@populate", "@pin", "@doctor", "@cox"
HOME, WORKSPACE, ROOT = "{home}", "{workspace}", "{root}"
MIGRATE_APPLY = ("cox", "setup", "migrate", "--apply")
LINT = ("cox", "route", "lint", "{workspace}/work/rehearsal")
# Work items the upgrade and migrate scenarios put in a workspace; the layout is work/<initiative>/<phase>/<task>.md.
WORK_ITEMS = {f"work/rehearsal/p{p}/t{t}.md": f"# Task {p}.{t}\n\nA rehearsal work item.\n" for p in (1, 2) for t in (1, 2)}


class Scenario(NamedTuple):
    name: str
    steps: tuple[tuple[str, ...], ...]


def parse_migrations(raw: str) -> list[str]:
    return [m for m in (part.strip() for part in raw.split(",")) if m]


def refusals(inputs: Mapping[str, str]) -> list[str]:
    """One reason per `*_sha` input that is not a full 40 character lowercase hex SHA; a branch or short SHA can move."""
    return [f"{n}: not a full 40 character SHA: {inputs[n]!r}" for n in INPUTS[:5] if not SHA.fullmatch(inputs[n])]


def pin_problems(heads: Mapping[str, str | None], shas: Mapping[str, str], receipt: str | None) -> list[str]:
    """`heads` is each component's checkout HEAD, None when no checkout was found. `receipt` is the tools uv receipt text."""
    checkouts = [
        f"{c}: no checkout under the install root" if heads.get(c) is None else f"{c}: at {heads[c][:12]}, expected {shas[c][:12]}"
        for c in COMPONENTS
        if heads.get(c) != shas[c]
    ]
    cox = [] if receipt is not None and shas["tools"] in receipt else ["tools: installed cox is not pinned to the candidate SHA"]
    return [*checkouts, *cox]


def _awaits_setup(row: Mapping) -> bool:
    detail = str(row.get("detail", ""))
    return (row.get("check") == "profile" and detail.startswith("missing:")) or detail == "skipped: no profile"


def doctor_problem(text: str) -> str | None:
    """None when every `cox setup doctor --json` row passes or only awaits setup and one passes, as install-proof judges it."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        return f"doctor: invalid JSON: {exc}"
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return "doctor: no rows"
    bad = [r for r in rows if not (isinstance(r, dict) and (r.get("ok") is True or _awaits_setup(r)))]
    if bad:
        row = bad[0] if isinstance(bad[0], dict) else {}
        return f"doctor: {row.get('check', '?')} failed: {row.get('detail', bad[0])}"
    return None if any(r.get("ok") is True for r in rows) else "doctor: no row passed"


def tools_install_argv(sha: str) -> tuple[str, ...]:
    return ("uv", "tool", "install", "--force", "--python", "3.14", f"{TOOLS_URL}@{sha}")


def fill_paths(scenario: str, home: Path) -> dict[str, str]:
    """Only the fresh scenario installs into the default coxswain home, {home}/data/coxswain, and its default workspace."""
    base = f"{home}/data/coxswain"
    root, workspace = (f"{base}/installs", f"{base}/workspaces/main") if scenario == "fresh" else (f"{home}/root", f"{home}/workspace")
    return {HOME: str(home), WORKSPACE: workspace, ROOT: root}


def _workspace_steps() -> tuple[tuple[str, ...], ...]:
    return (
        ("mkdir", "-p", *(f"{WORKSPACE}/{d}" for d in ("work", "runs", "intake", "cartridges"))),
        ("git", "init", WORKSPACE),
        ("cartridge", "init", "rehearse", "--cartridges-dir", f"{WORKSPACE}/cartridges"),
    )


def _fresh_install(tools_sha: str) -> tuple[tuple[str, ...], ...]:
    """The released tools cannot install without a root flag, so `@cox` swaps in the candidate tools before `cox install`."""
    return (
        ("uv", "tool", "install", "--python", "3.14", "coxswain-tools"),
        (COX, tools_sha),
        ("git", "clone", UMBRELLA_URL, f"{ROOT}/coxswain"),
        ("cox", "install", "--team", "rehearse", "--with", "crew", "--with", "dash"),
        *_workspace_steps(),
    )


def _install(previous: str | None) -> tuple[tuple[str, ...], ...]:
    """The sequence docs/install/index.md gives. `previous` is a tag such as v0.40.0; None installs the default release."""
    tools = "coxswain-tools" if previous is None else f"coxswain-tools=={previous.removeprefix('v')}"
    branch = () if previous is None else ("--branch", previous)
    return (
        ("uv", "tool", "install", "--python", "3.14", tools),
        ("git", "clone", *branch, UMBRELLA_URL, f"{ROOT}/coxswain"),
        ("cox", "install", "--root", ROOT, "--team", "rehearse", "--workspace", WORKSPACE, "--with", "crew", "--with", "dash"),
        *_workspace_steps(),
    )


def plan(inputs: Mapping[str, str]) -> list[Scenario]:
    pin = (PIN, *(f"{c}={inputs[f'{c}_sha']}" for c in COMPONENTS))
    ids = parse_migrations(inputs["migrations"])
    fresh = Scenario("fresh", (*_fresh_install(inputs["tools_sha"]), pin, (DOCTOR,)))
    upgrade = Scenario("upgrade", (*_install(inputs["previous_tag"]), (POPULATE,), pin, MIGRATE_APPLY, LINT, (DOCTOR,)))
    # unknown: how a migration id maps to a command; confirm against the release notes of the release that declares it
    each = tuple(s for i in ids for s in (("cox", "migrate", i, "--workspace", WORKSPACE), LINT))
    migrate = Scenario("migrate", (*_install(None), (POPULATE,), pin, *each) if ids else ())
    return [fresh, upgrade, migrate]


def verdict(results: Mapping[str, str], declared: Sequence[str]) -> tuple[int, list[str]]:
    lines = [f"{name}: {results[name]}" for name in ORDER if name in results]
    skipped_with_declared = results.get("migrate") == "skip" and bool(declared)
    failed = any(r not in ("pass", "skip") for r in results.values())
    return (1 if failed or skipped_with_declared else 0), lines


# ---- edge: processes and files ----


def _env(home: Path) -> dict[str, str]:
    # a scratch HOME per scenario; uv's tool dir is fixed so the tools receipt can be read back,
    # and its tool bin dir is the one PATH already searches
    path = f"{home}/.local/bin:{os.environ.get('PATH', '')}"
    return {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": f"{home}/config", "XDG_DATA_HOME": f"{home}/data",
            "XDG_STATE_HOME": f"{home}/state", "XDG_CACHE_HOME": f"{home}/cache", "UV_TOOL_DIR": f"{home}/uv-tools",
            "UV_TOOL_BIN_DIR": f"{home}/.local/bin", "PATH": path}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def _checkouts(root: Path) -> dict[str, Path]:
    # `cox install` clones each component to <root>/coxswain-<name>
    return {c: root / f"coxswain-{c}" for c in COMPONENTS if (root / f"coxswain-{c}" / ".git").exists()}


def _populate(workspace: Path) -> None:
    for rel, text in WORK_ITEMS.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _pin(shas: Mapping[str, str], root: Path, home: Path, env: Mapping[str, str]) -> bool:
    repos = _checkouts(root)
    for component, repo in repos.items():
        _git(repo, "fetch", "--quiet", "origin", shas[component])
        _git(repo, "checkout", "--quiet", "--detach", shas[component])
    subprocess.run(["uv", "tool", "install", "--force", f"{TOOLS_URL}@{shas['tools']}"], env=env)
    receipt = home / "uv-tools" / "coxswain-tools" / "uv-receipt.toml"
    heads = {c: _git(r, "rev-parse", "HEAD") for c, r in repos.items()}
    problems = pin_problems(heads, shas, receipt.read_text() if receipt.exists() else None)
    sys.stderr.write("".join(f"{p}\n" for p in problems))
    return not problems


def _doctor(env: Mapping[str, str]) -> bool:
    # doctor exits 1 until setup runs, so its rows are judged and its exit code is not
    problem = doctor_problem(subprocess.run(["cox", "setup", "doctor", "--json"], capture_output=True, text=True, env=env).stdout)
    sys.stderr.write(f"{problem}\n" if problem else "")
    return problem is None


def _run_step(step: tuple[str, ...], scenario: str, home: Path, env: Mapping[str, str]) -> bool:
    fill = fill_paths(scenario, home)
    workspace, root = Path(fill[WORKSPACE]), Path(fill[ROOT])
    try:
        if step[0] == POPULATE:
            _populate(workspace)
            return True
        if step[0] == PIN:
            return _pin(dict(a.split("=") for a in step[1:]), root, home, env)
        if step[0] == DOCTOR:
            return _doctor(env)
        if step[0] == COX:
            return subprocess.run(tools_install_argv(step[1]), env=env).returncode == 0
        argv = [re.sub(r"\{(home|workspace|root)\}", lambda m: fill[m.group(0)], a) for a in step]
        return subprocess.run(argv, env=env).returncode == 0
    except OSError as exc:  # a command missing from PATH is a failed scenario with a line, not a traceback
        print(f"{step[0]}: {exc}", file=sys.stderr)
        return False


def execute(scenario: Scenario, home: Path) -> str:
    if not scenario.steps:
        return "skip"
    fill = fill_paths(scenario.name, home)
    for key in (WORKSPACE, ROOT):
        Path(fill[key]).mkdir(parents=True, exist_ok=True)
    env = _env(home)
    return "pass" if all(_run_step(step, scenario.name, home, env) for step in scenario.steps) else "fail"


def _rehearse(scenario: Scenario) -> str:
    with tempfile.TemporaryDirectory() as home:
        return execute(scenario, Path(home))


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the release rehearsal scenarios.")
    for name in INPUTS:
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, default="" if name == "migrations" else None, required=name != "migrations")
    parser.add_argument("--scenario", choices=ORDER, help="Run one scenario; default is all three.")
    args = parser.parse_args(argv)
    inputs = {name: getattr(args, name) for name in INPUTS}
    chosen = [s for s in plan(inputs) if args.scenario in (None, s.name)]
    refused = refusals(inputs)
    sys.stderr.write("".join(f"{r}\n" for r in refused))
    results = {s.name: "fail" if refused else _rehearse(s) for s in chosen}
    code, lines = verdict(results, parse_migrations(inputs["migrations"]))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
