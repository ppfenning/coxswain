"""The rehearsal scenarios the release-rehearsal workflow runs, one result line each.

Design: docs/release-rehearsal.md. The pure core builds the plan, refuses bad inputs, judges evidence and builds the
verdict, all from plain data. The edge at the bottom runs the plan's steps as processes and is the only part that
touches the machine. Stdlib only: the workflow runs it with the container's apt python3.

The candidate is one commit of the coxswain repository (`--sha`), and optionally one of coxswain-dash (`--dash-sha`)
when the release tags dash; an empty dash SHA means the release keeps dash's pinned tag.

How the candidate gets installed: the coxswain checkout is the install (`<root>/coxswain`, the contract's
`<home>/installs/coxswain`). A `@candidate` step moves that checkout to the candidate SHA and installs `cox` from it
editable, the way a host's `cox` is installed. `fresh` clones the repository there first and then runs the
candidate's own `cox install`. `upgrade` and `migrate` start from a real install of the previous release (the
multi-repo `coxswain-tools` line for a 0.x tag), move its checkout to the candidate, then run
`cox setup migrate --apply`. A scenario passes only when the `@verify` read-back shows the checkout at the SHA in the
run title and `cox` installed from that checkout, so a green run proves what it names. No cox flag that pins a
commit exists, so none is used.
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
DISTRIBUTION = "coxswain"
LEGACY_DISTRIBUTION = "coxswain-tools"
INPUTS = ("sha", "dash_sha", "previous_tag", "migrations")
OPTIONAL = ("dash_sha", "migrations")
ORDER = ("fresh", "upgrade", "migrate")
SHA = re.compile(r"[0-9a-f]{40}")
RELEASE_TAG = re.compile(r"v\d+\.\d+\.\d+")
# A CalVer tag's first number is a year; the 0.x line before it shipped as coxswain-tools across five repositories.
CALVER_TAG = re.compile(r"v\d{4}\.\d+\.\d+")
POPULATE, CANDIDATE, DASH, VERIFY, DOCTOR = "@populate", "@candidate", "@dash", "@verify", "@doctor"
HOME, WORKSPACE, ROOT = "{home}", "{workspace}", "{root}"
CHECKOUT = f"{ROOT}/coxswain"
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
    """One reason per bad input: `sha` must be a full 40 character lowercase hex SHA (a branch or short SHA can move),
    `dash_sha` the same or empty, and `previous_tag` a release tag such as v0.47.0 or v2026.10.0."""
    sha = [] if SHA.fullmatch(inputs["sha"] or "") else [f"sha: not a full 40 character SHA: {inputs['sha']!r}"]
    dash = inputs.get("dash_sha") or ""
    dash_bad = [] if not dash or SHA.fullmatch(dash) else [f"dash_sha: not a full 40 character SHA: {dash!r}"]
    tag = inputs["previous_tag"] or ""
    tag_bad = [] if RELEASE_TAG.fullmatch(tag) else [f"previous_tag: not a release tag: {tag!r}"]
    return [*sha, *dash_bad, *tag_bad]


def verify_problems(head: str | None, sha: str, receipt: str | None, checkout: str,
                    dash_head: str | None = None, dash_sha: str = "") -> list[str]:
    """`head` is the coxswain checkout's HEAD (None when there is no checkout), `receipt` the `coxswain` uv tool
    receipt's text. Pinned when the checkout is at `sha` and the receipt installs from that checkout (or names the
    SHA). With `dash_sha`, dash's checkout (`dash_head`) must be at it too."""
    at = ([f"coxswain: no checkout at {checkout}"] if head is None else
          [] if head == sha else [f"coxswain: at {head[:12]}, expected {sha[:12]}"])
    pinned = receipt is not None and (sha in receipt or checkout in receipt)
    cox = [] if pinned else ["cox: the installed coxswain is not the candidate checkout"]
    dash = ([] if not dash_sha or dash_head == dash_sha else
            ["dash: no checkout under the install root"] if dash_head is None else
            [f"dash: at {dash_head[:12]}, expected {dash_sha[:12]}"])
    return [*at, *cox, *dash]


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


def candidate_install_argv(checkout: str) -> tuple[str, ...]:
    """`cox` installed editable from the candidate checkout, replacing whatever `cox` the previous release put there."""
    return ("uv", "tool", "install", "--force", "--python", "3.14", "--editable", checkout)


def previous_package(previous: str) -> str:
    """The PyPI requirement a host of `previous` installed: `coxswain-tools==0.47.0` for the 0.x line,
    `coxswain==2026.10.0` from the first CalVer release on."""
    bare = previous.removeprefix("v")
    return f"{DISTRIBUTION}=={bare}" if CALVER_TAG.fullmatch(previous) else f"{LEGACY_DISTRIBUTION}=={bare}"


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


def _fresh_install(sha: str) -> tuple[tuple[str, ...], ...]:
    """The candidate is cloned where the install lives, then its own `cox install` runs with no root flag."""
    return (
        ("git", "clone", UMBRELLA_URL, CHECKOUT),
        (CANDIDATE, sha),
        ("cox", "install", "--team", "rehearse", "--with", "dash"),
        *_workspace_steps(),
    )


def _install(previous: str) -> tuple[tuple[str, ...], ...]:
    """The sequence the previous release's docs/install/index.md gave, for the tag `previous` (such as v0.47.0)."""
    flags = ("--with", "crew", "--with", "dash") if not CALVER_TAG.fullmatch(previous) else ("--with", "dash")
    return (
        ("uv", "tool", "install", "--python", "3.14", previous_package(previous)),
        ("git", "clone", "--branch", previous, UMBRELLA_URL, CHECKOUT),
        ("cox", "install", "--root", ROOT, "--team", "rehearse", "--workspace", WORKSPACE, *flags),
        *_workspace_steps(),
    )


def plan(inputs: Mapping[str, str]) -> list[Scenario]:
    sha, dash_sha, previous = inputs["sha"], inputs.get("dash_sha") or "", inputs["previous_tag"]
    dash = ((DASH, dash_sha),) if dash_sha else ()
    verify = ((VERIFY, sha, *((dash_sha,) if dash_sha else ())), (DOCTOR,))
    ids = parse_migrations(inputs.get("migrations") or "")
    upgraded = (*_install(previous), (POPULATE,), (CANDIDATE, sha), *dash, MIGRATE_APPLY, LINT)
    fresh = Scenario("fresh", (*_fresh_install(sha), *dash, *verify))
    upgrade = Scenario("upgrade", (*upgraded, *verify))
    # unknown: how a migration id maps to a command; confirm against the release notes of the release that declares it
    each = tuple(s for i in ids for s in (("cox", "migrate", i, "--workspace", WORKSPACE), LINT))
    migrate = Scenario("migrate", (*upgraded, *each, *verify) if ids else ())
    return [fresh, upgrade, migrate]


def verdict(results: Mapping[str, str], declared: Sequence[str]) -> tuple[int, list[str]]:
    lines = [f"{name}: {results[name]}" for name in ORDER if name in results]
    skipped_with_declared = results.get("migrate") == "skip" and bool(declared)
    failed = any(r not in ("pass", "skip") for r in results.values())
    return (1 if failed or skipped_with_declared else 0), lines


# ---- edge: processes and files ----


def _env(home: Path) -> dict[str, str]:
    # a scratch HOME per scenario; uv's tool dir is fixed so the receipt can be read back,
    # and its tool bin dir is the one PATH already searches
    path = f"{home}/.local/bin:{os.environ.get('PATH', '')}"
    return {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": f"{home}/config", "XDG_DATA_HOME": f"{home}/data",
            "XDG_STATE_HOME": f"{home}/state", "XDG_CACHE_HOME": f"{home}/cache", "UV_TOOL_DIR": f"{home}/uv-tools",
            "UV_TOOL_BIN_DIR": f"{home}/.local/bin", "PATH": path}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def _populate(workspace: Path) -> None:
    for rel, text in WORK_ITEMS.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _move(repo: Path, sha: str) -> bool:
    _git(repo, "fetch", "--quiet", "origin", sha)
    _git(repo, "checkout", "--quiet", "--detach", sha)
    return _git(repo, "rev-parse", "HEAD") == sha


def _candidate(sha: str, checkout: Path, env: Mapping[str, str]) -> bool:
    return _move(checkout, sha) and subprocess.run(candidate_install_argv(str(checkout)), env=env).returncode == 0


def _verify(sha: str, dash_sha: str, root: Path, home: Path) -> bool:
    checkout = root / "coxswain"
    head = _git(checkout, "rev-parse", "HEAD") or None if (checkout / ".git").exists() else None
    dash = root / "coxswain-dash"
    dash_head = _git(dash, "rev-parse", "HEAD") or None if (dash / ".git").exists() else None
    receipt = home / "uv-tools" / DISTRIBUTION / "uv-receipt.toml"
    problems = verify_problems(head, sha, receipt.read_text() if receipt.exists() else None, str(checkout),
                               dash_head, dash_sha)
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
        if step[0] == CANDIDATE:
            return _candidate(step[1], root / "coxswain", env)
        if step[0] == DASH:
            return _move(root / "coxswain-dash", step[1])
        if step[0] == VERIFY:
            return _verify(step[1], step[2] if len(step) > 2 else "", root, home)
        if step[0] == DOCTOR:
            return _doctor(env)
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
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, default="" if name in OPTIONAL else None,
                            required=name not in OPTIONAL)
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
