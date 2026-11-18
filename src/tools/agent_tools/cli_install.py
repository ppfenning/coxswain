"""`cox install`, `upgrade`, `versions` and `release`: the checkouts, the binaries and the edge channel."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_tools.cli import _LAZY_MODULES, _dev_moved
from agent_tools.cli_chair_service import (
    _chair_service_environment_file,
    _chair_unit_inputs,
    _ChairUnitRefusal,
    _write_private,
)
from agent_tools.cli_common import _now_iso, _profile_path, _read_text_or_none
from agent_tools.cli_lazy import (
    bounded_run,
    chair_service,
    dash_binary,
    doctor,
    forge_auto,
    forge_github,
    home_paths,
    install,
    install_exec,
    records,
    remote_argv,
    route,
    schema,
    setup_install,
    setup_receipts,
    upgrade_edge,
    upgrade_exec,
    upgrade_plan,
)
from agent_tools.cli_setup_store import _unit_env_loaded


def _migrate_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "dir"
    return "file" if path.exists() else "absent"


def _load_manifest(path: Path) -> dict | None:
    """The parsed manifest, or None on a missing or unparseable file — never
    a traceback; the caller turns None into a named, exit-2 refusal."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _checkout_facts(path: Path, fetch: bool = False) -> dict:
    """A component's checkout state. Present only when `<path>/.git`
    exists: `git -C` walks up to the nearest enclosing repository, so a
    bare directory — or one merely nested inside some other checkout —
    must never borrow that repository's tag or dirty state."""
    if not (path / ".git").exists():
        return {"present": False, "tag": None, "dirty": False}
    describe = bounded_run.completed(["git", "-C", str(path), "describe", "--tags", "--exact-match"],
                               capture_output=True, text=True)
    tag = describe.stdout.strip() if describe.returncode == 0 else None
    status = bounded_run.completed(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True)
    def git(*args: str) -> subprocess.CompletedProcess:
        return bounded_run.completed(["git", "-C", str(path), *args], capture_output=True, text=True)

    branch = git("branch", "--show-current").stdout.strip()
    fresh = not fetch or (bool(branch) and git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}").returncode == 0)
    tip = git("rev-parse", f"origin/{branch}")
    at_tip = bool(branch) and fresh and tip.returncode == 0 and tip.stdout.strip() == git("rev-parse", "HEAD").stdout.strip()
    return {"present": True, "tag": tag, "dirty": bool(status.stdout.strip()), "branch": branch or None,
            "branch_tip": at_tip}


def _gather_checkout_facts(root: Path, components: dict, fetch: bool = False) -> dict:
    """The one checkout and every manifest component, plus any other git checkout actually
    present under `root` — the `extra` rows `install.rows` can then report (a component
    checkout of the old layout not yet retired shows up there).
    `fetch` refreshes each declared checkout's remote branch first (edge)."""
    options = {"fetch": True} if fetch else {}
    declared_dirs = {name: Path(install_exec.clone_dir(str(root), name)) for name in (install.CHECKOUT, *components)}
    declared = {name: _checkout_facts(path, **options) for name, path in declared_dirs.items()}
    if not root.exists():
        return declared
    prefixed = {path.name for path in declared_dirs.values()}
    undeclared = [p for p in root.iterdir() if p.is_dir() and p.name not in prefixed and (p / ".git").exists()]
    # A bare `cartridges` directory would overwrite the declared key and `install.rows` skips declared names,
    # so a name that collides is keyed `./cartridges` to stay visible as an extra.
    return {**declared, **{(f"./{p.name}" if p.name in components else p.name): _checkout_facts(p) for p in undeclared}}


def _manifest_provider_command(manifest: dict, provider: str) -> str:
    """The executable to look up on PATH: the manifest's own `command` for
    this provider when it names one, else the provider key itself."""
    return manifest.get("providers", {}).get(provider, {}).get("command", provider)


def _real_run(argv: list, cwd: str | None) -> tuple:
    """The subprocess wrapper `install_exec.execute` calls at the edge;
    stdout and stderr are folded together since the caller only prints. A command that cannot start (a missing
    program or working folder) is exit 127 with the reason, never a traceback. Every step is bounded by
    `bounded_run.completed`: git and gh by their bounds, a venv sync or a tool install by the step bound, so a hung
    step is exit 124 with the failure line and the edge upgrade goes on to roll back or report."""
    try:
        result = bounded_run.completed(argv, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        return 127, f"{argv[0]}: {exc}"
    return result.returncode, result.stdout + result.stderr


def _load_lazy_modules() -> None:
    """Import every lazy module now: setup replaces the tool's environment, and a later import would fail."""
    for module in _LAZY_MODULES:
        module._lazy_load()


def _install_execute(steps: list, manifest: dict, options: dict, root: Path, *, name_stale: bool = False) -> int:
    """Runs a planned install/upgrade for real: enrich the planner's steps
    with the repo/tag/team/workspace `install_exec` needs, execute them,
    report one ok/FAILED/skipped/refused line per step, then the versions
    table built from checkouts gathered again after execution — the facts
    gathered before a single step ran are not evidence of what landed.
    A `refuse` step is reported as `refused`, not `FAILED` — it never
    called `run` and its non-zero exit is a decision, not a command that
    broke. Exits 0 only when no step failed fatally; a failing doctor alone
    exits 0 and adds one closing line."""
    if any(step["kind"] == "setup_install" for step in steps):
        _load_lazy_modules()
    exec_steps = install_exec.from_plan(steps, manifest=manifest, options=options)
    results = install_exec.execute(exec_steps, root=str(root), run=_real_run)
    for result in results:
        step = result["step"]
        label = f"{step['kind']} {step.get('component', '')}".strip()
        if step["kind"] == "skip" or result["exit"] is None:
            print(f"skipped: {label}")
            if step["kind"] == "skip" and manifest.get("components", {}).get(step.get("component"), {}).get("provides"):
                print(f"    {step['detail']}")
        elif step["kind"] == "refuse":
            print(f"refused: {label}: {result['output']}")
        elif result["exit"] == 0:
            print(f"ok: {label}")
        else:
            print(f"FAILED: {label}")
            for line in (result["output"] or "").rstrip().splitlines():
                print(f"    {line}")
    post_facts = {"root": str(root), "checkouts": _gather_checkout_facts(root, manifest.get("components", {})),
                  **{key: value for key, value in _host_binary_facts(manifest).items()
                     if key in ("binaries", "killed_binaries")}}
    table = install.rows(manifest, post_facts, options.get("channel", "release"))
    display = [{"component": c, "pinned_tag": p or "", "installed_tag": i or "", "status": s} for c, p, i, s in table]
    print(records.format_table(display, ["component", "pinned_tag", "installed_tag", "status"]))
    for note in _killed_notes(table):
        print(note)
    if any(result["step"]["kind"] == "doctor" and result["exit"] not in (0, None) for result in results):
        print(_DOCTOR_FAILING_LINE)
    stale = _stale_binary_line(steps, manifest, options, table)
    if stale and name_stale:
        print(stale)
    return 2 if stale or any(install_exec.is_fatal(result) for result in results) else 0


def _stale_binary_line(steps: list, manifest: dict, options: dict, table: list) -> str | None:
    """A line naming each command a release install planned a binary (or skipped it for want of an asset) for
    whose row is not ok afterwards, else None. Edge builds from source, so its rows never decide."""
    if options.get("channel", "release") == "edge":
        return None
    components = manifest.get("components", {})
    commands = {components[step["component"]]["provides"] for step in steps
                if step["kind"] in ("binary", "skip") and components.get(step["component"], {}).get("provides")}
    lines = [f"upgrade: {name} is at {installed or 'unreadable'} but the manifest pins {pinned}"
             for name, pinned, installed, status in table if name in commands and status != "ok"]
    return "; ".join(lines) or None


def _binary_rows_not_ok(steps: list, manifest: dict, options: dict, table: list) -> bool:
    """True when `_stale_binary_line` has a row to name."""
    return _stale_binary_line(steps, manifest, options, table) is not None


_DOCTOR_FAILING_LINE = ("doctor reports failing rows: create your workspace folders and team cartridge "
                        "if you have not, then run cox setup doctor")

_NO_MANIFEST = "refusing: no manifest at {}; run `cox install` to fetch the components, or pass --manifest"
_NO_HOME_MANIFEST = _NO_MANIFEST + "; clone the umbrella there first"


def _install_root(a: argparse.Namespace) -> tuple[Path, str | None] | None:
    """The root and, when it came from the home default, that home. None after printing a refusal."""
    if a.root:
        return Path(a.root), None
    locs = home_paths.resolve_here(a.home)
    if isinstance(locs, home_paths.Refused):
        print(f"refusing: {locs.reason}")
        return None
    return Path(locs.installs), locs.home


def _with_flags(a: argparse.Namespace) -> list[str]:
    """Edge. The `--with` flags the planner sees; a retired one is dropped with its one-line note."""
    flags, notes = install.drop_retired_flags(a.with_ or [])
    for note in notes:
        print(f"note: {note}")
    return flags


def _install_clone(a: argparse.Namespace, home: str | None, manifest_path: Path) -> int | None:
    """Edge. Clone the checkout at its release tag when the home default points at none. None means fall through
    to the manifest handling; an int is the exit code."""
    if not home or a.manifest or manifest_path.exists():
        return None
    from agent_tools.version_info import _installed
    checkout = manifest_path.parent
    argv = install.clone_argv(_migrate_kind(checkout), _installed(), install.CHECKOUT_REPO, str(checkout))
    if isinstance(argv, str):
        print(argv)
        return 2
    if not argv:
        return None
    print(f"clone coxswain: {argv[argv.index('--branch') + 1]}")
    if a.dry_run:
        return 0
    code, output = _real_run(argv, None)
    if code != 0:
        print(f"refusing: git clone failed with exit {code}: {output.strip()}")
        return 2
    return None


def _install(a: argparse.Namespace) -> int:
    resolved = _install_root(a)
    if resolved is None:
        return 2
    root, home = resolved
    manifest_path = Path(a.manifest) if a.manifest else _checkout_path(root) / "manifest.toml"
    cloned = _install_clone(a, home, manifest_path)
    if cloned is not None:
        return cloned
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print((_NO_HOME_MANIFEST if home and not a.manifest else _NO_MANIFEST).format(manifest_path))
        return 2
    channel = "edge" if a.edge else "release"
    facts = {
        "root": str(root),
        "checkouts": _gather_checkout_facts(root, manifest.get("components", {}), fetch=a.edge),
        "provider_cli_on_path": shutil.which(_manifest_provider_command(manifest, a.provider)) is not None,
        "cargo": shutil.which("cargo") is not None,
        **_host_binary_facts(manifest),
    }
    with_flags = _with_flags(a)
    options = {"provider": a.provider, "with": with_flags, "root": str(root), "team": a.team,
               "workspace": a.workspace, "channel": channel, **({"home": home} if home else {})}
    schema_state, schema_detail = schema.status(_checkout_schema_versions(root))
    if schema_state != "ok":
        print(f"schema  WARN  {schema_detail}")
    steps = install.plan(manifest, facts, options)
    print(f"channel: {channel}")
    for step in steps:
        print(f"{step['kind']} {step['component']}: {step['detail']}")
    if a.dry_run:
        return 2 if any(step["kind"] == "refuse" for step in steps) else 0
    if home:
        root.mkdir(parents=True, exist_ok=True)
    return _install_execute(steps, manifest, options, root)


def _offered_tags(path: Path, url: str | None = None) -> tuple[str, ...]:
    """Tags offered by `url`, else by the remote of the checkout at `path`; newest first, empty when unreachable."""
    ask = ["ls-remote", "--tags", "--sort=-v:refname"]
    argv = ["git", *ask, url] if url else ["git", "-C", str(path), *ask, "origin"]
    code, output = _real_run(argv, None)
    return upgrade_exec.offered_tags(output) if code == 0 else ()


def _clone_url(spec: dict) -> str | None:
    """The URL `install_exec` clones a component from, or None when the manifest names no repo."""
    return f"https://github.com/{spec['repo']}.git" if spec.get("repo") else None


def _binary_version(command: str, run: Callable, on_path: bool, bin_dir: str) -> tuple[str | None, bool]:
    """`(version, killed)`: what `<command> --version` prints, parsed, None when it will not run or exits non-zero;
    killed is True when the exit code was a signal kill. A command off PATH is looked up in `bin_dir`."""
    code, output = run([command if on_path else os.path.join(bin_dir, command), "--version"], None)
    return (dash_binary.installed_version(output) if code == 0 else None), dash_binary.killed_by_signal(code)


def _killed_notes(table: list) -> list[str]:
    """One line per row whose status is killed, naming the command and why."""
    return [f"{name}: {dash_binary.KILLED_DETAIL}" for name, _, _, status in table if status == "killed"]


def _binary_facts(manifest: dict, run: Callable, system: str, machine: str, which: Callable, home: str) -> dict:
    """Plain data for `install.plan` and `install.rows`: the release target of this machine, the directory the
    binary lives in (that of the first provided command on PATH, else `~/.local/bin`), and the version each
    command a component `provides` reports now, plus the sorted commands whose probe was killed by a signal."""
    provided = [spec["provides"] for spec in manifest.get("components", {}).values() if spec.get("provides")]
    found = {command: which(command) for command in provided}
    on_path = [path for path in found.values() if path]
    bin_dir = os.path.dirname(on_path[0]) if on_path else os.path.join(home, ".local", "bin")
    probes = {command: _binary_version(command, run, bool(found[command]), bin_dir) for command in provided}
    return {"target": dash_binary.host_target(system, machine), "bin_dir": bin_dir,
            "binaries": {command: version for command, (version, _) in probes.items()},
            "killed_binaries": sorted(command for command, (_, killed) in probes.items() if killed)}


def _host_binary_facts(manifest: dict) -> dict:
    """Edge. `_binary_facts` for this machine."""
    return _binary_facts(manifest, _real_run, platform.system(), platform.machine(), shutil.which, str(Path.home()))


def _upgrade_facts(manifest: dict, root: Path, provider: str) -> dict:
    return {
        "root": str(root),
        "checkouts": _gather_checkout_facts(root, manifest.get("components", {})),
        "provider_cli_on_path": shutil.which(_manifest_provider_command(manifest, provider)) is not None,
        **_host_binary_facts(manifest),
    }


def _upgrade_refused(manifest: dict, root: Path, options: dict) -> bool:
    """Print and return True when an upgrade must not start: a dirty checkout, the one checkout's included,
    or a plan refusal such as an unsupported provider or an unknown --with flag."""
    facts = _upgrade_facts(manifest, root, options["provider"])
    dirty = sorted(name for name, checkout in facts["checkouts"].items() if checkout.get("dirty"))
    if dirty:
        declared = dirty[0] == install.CHECKOUT or dirty[0] in manifest.get("components", {})
        where = install_exec.clone_dir(str(root), dirty[0]) if declared else root / dirty[0]
        print(f"refusing: {where} is dirty; refusing to upgrade it")
        return True
    refusals = [step for step in install.plan(manifest, facts, options) if step["kind"] == "refuse"]
    for step in refusals:
        print(f"refused: {step['component']}: {step['detail']}")
    return bool(refusals)


def _upgrade_plan_for(manifest: dict, root: Path, target: str, checkout_tag: str | None, with_flags: list) -> tuple:
    """The planner's plan for `manifest`'s optional components (required, asked for with --with, or already
    present) against what is installed and what each remote offers now, and the clone URL of every component
    that has no checkout yet. The checkout itself is the plan's first step."""
    checkouts = _gather_checkout_facts(root, manifest.get("components", {}))
    components = {name: spec for name, spec in manifest.get("components", {}).items()
                  if spec.get("required") or spec.get("flag") in with_flags or checkouts[name]["present"]}
    urls = {name: _clone_url(spec) for name, spec in components.items() if not checkouts[name]["present"]}
    plan = upgrade_plan.plan_upgrade(
        target,
        checkout_tag or "untagged",
        {name: spec.get("tag") for name, spec in components.items()},
        {name: checkouts[name].get("tag") for name in components},
        {name: frozenset(_offered_tags(Path(install_exec.clone_dir(str(root), name)), urls.get(name))) for name in components},
    )
    return plan, {name: url for name, url in urls.items() if url}


def _checkout_path(root: Path) -> Path:
    return Path(install_exec.clone_dir(str(root), install.CHECKOUT))


def _upgrade_after_move(root: Path) -> list[dict]:
    """The steps that follow the checkout's move: sync its venv, reinstall the cox tool with the extras its
    receipt carries, link the crew seats."""
    checkout = str(_checkout_path(root))
    extras = setup_receipts.read_tool_extras(
        setup_receipts.default_tool_dir(os.environ, str(Path.home())), setup_install.TOOL)
    return [setup_install.sync_step(checkout), setup_install.tool_step(checkout, extras), setup_install.crew_step(checkout)]


def _upgrade(a: argparse.Namespace) -> int:
    """`--install-timer` and `--uninstall-timer` only write or remove the timer files. Otherwise, with the units' env
    file loaded under any variable the process lacks (`_unit_env_loaded`), the machine's channel decides: edge without `--to` resolves the newest green commit on main (`_upgrade_edge`); anything else is the
    release path, `_upgrade_move`. `--scheduled`, what the timer runs, moves only an edge machine."""
    if a.install_timer:
        return _upgrade_timer_install(a)
    if a.uninstall_timer:
        return _upgrade_timer_uninstall()
    # A hand run (`ssh host cox upgrade`) has no unit env; the doctor it triggers must see what the timer's unit sees.
    with _unit_env_loaded(print):
        return _upgrade_on_channel(a)


def _upgrade_on_channel(a: argparse.Namespace) -> int:
    channel = _upgrade_channel(a)
    if channel is None:
        return 2
    if a.scheduled and channel != "edge":
        print(f"upgrade: this machine's channel is {channel}; the scheduled upgrade moves only an edge machine")
        return 0
    if channel == "edge" and not a.to:
        return _upgrade_edge(a)
    return _upgrade_move(a)


def _upgrade_channel(a: argparse.Namespace) -> str | None:
    """Edge. The profile's `upgrade_channel`; stable with no profile. A profile that does not parse is stable for a
    hand-run upgrade, as before the setting existed, and None (after printing why) for `--scheduled`, which must not
    guess. The parse error is not printed: a profile line may carry a URL."""
    path = _profile_path(a)
    text = _read_text_or_none(path)
    if text is None:
        return upgrade_edge.DEFAULT_CHANNEL
    try:
        return upgrade_edge.channel(route.parse_profile(text))
    except route.ProfileError:
        if a.scheduled:
            print(f"refusing: the profile at {path} does not parse, so this machine's upgrade channel is unknown")
            return None
        print(f"note: the profile at {path} does not parse; upgrading on the {upgrade_edge.DEFAULT_CHANNEL} channel")
        return upgrade_edge.DEFAULT_CHANNEL


def _edge_run(argv: list, cwd: str | None = None) -> tuple[int, str]:
    """Edge. Every command the edge upgrade runs (git, gh, systemctl, the doctor), bounded by
    `bounded_run.default_timeout`: a hung one is killed with its process group and its output is the failure line,
    `gh run timed out after 120 seconds`, so the timer's oneshot reports it and returns instead of wedging."""
    limit = bounded_run.default_timeout(argv, os.environ)
    result = bounded_run.run(argv, limit, Path(cwd) if cwd else None)
    if result.timed_out or result.returncode == bounded_run.UNRUNNABLE_RETURNCODE:
        return result.returncode, bounded_run.failure_line(argv, result, limit)
    return result.returncode, result.output


def _edge_git(checkout: Path, *args: str) -> tuple[int, str]:
    return _edge_run(["git", "-C", str(checkout), *args])


def _edge_target(checkout: Path, slug: str) -> tuple[str, str, list[str]] | str:
    """Edge. `(head, target, main's first-parent history)` after fetching main, where target is the newest commit of
    that history whose `upgrade_edge.WORKFLOW` push run passed; else why there is none."""
    branch = upgrade_edge.BRANCH
    if not (checkout / ".git").exists():
        return f"no checkout at {checkout}"
    code, out = _edge_git(checkout, "fetch", "origin", branch)
    if code != 0:
        return f"git fetch origin {branch} in {checkout} exited {code}: {out.strip()}"
    code, history = _edge_git(checkout, "rev-list", "--first-parent", f"--max-count={upgrade_edge.HISTORY}", f"origin/{branch}")
    if code != 0:
        return f"git rev-list origin/{branch} in {checkout} exited {code}: {history.strip()}"
    code, head = _edge_git(checkout, "rev-parse", "HEAD")
    if code != 0:
        return f"git rev-parse HEAD in {checkout} exited {code}: {head.strip()}"
    first_parent = [line.strip() for line in history.splitlines() if line.strip()]
    green = _edge_green(checkout, slug, branch, first_parent)
    if isinstance(green, str):
        return green
    target = upgrade_edge.newest_green(first_parent, green)
    if target is None:
        return f"none of the newest {upgrade_edge.HISTORY} commits on origin/{branch} has a passing {upgrade_edge.WORKFLOW} run"
    return head.strip(), target, first_parent


def _edge_green(checkout: Path, slug: str, branch: str, first_parent: list[str]) -> tuple[str, ...] | str:
    """Edge. The commits of `first_parent` whose CI passed, from the forge the checkout's `origin` is on
    (`forge_auto.forge_of`) when that forge answers `green_commits` (Forgejo, for an origin on the `FORGEJO_BASE_URL`
    host), else the `upgrade_edge.WORKFLOW` push runs on GitHub by `gh`. A string says why they could not be read."""
    module = forge_auto.forge_of(checkout)
    if hasattr(module, "green_commits"):
        green = module.green_commits(checkout, first_parent)
        return f"cannot read the commit statuses on {branch} of {checkout}: {green}" if isinstance(green, str) else green
    code, runs = _edge_run(forge_github.ci_runs_argv(slug, upgrade_edge.WORKFLOW, branch))
    if code != 0:
        return f"cannot list the {upgrade_edge.WORKFLOW} runs on {branch} of {slug}: {runs.strip()}"
    return forge_github.parse_green_shas(runs)


def _edge_added_migrations(checkout: Path, head: str, target: str) -> tuple[str, ...] | str:
    """Edge. The store migrations `target` adds over `head`, read from git; why not when a listing fails."""
    listings = []
    for sha in (head, target):
        code, out = _edge_git(checkout, "ls-tree", "--name-only", sha, upgrade_edge.MIGRATIONS_DIR)
        if code != 0:
            return f"git ls-tree {sha} {upgrade_edge.MIGRATIONS_DIR} exited {code}: {out.strip()}"
        listings.append(out.splitlines())
    return upgrade_edge.added_migrations(*listings)


def _edge_units() -> list[str]:
    """Edge. The chair and lander user units this machine has a file for, in that order."""
    from agent_tools import lander_cli

    home = Path.home()
    return [path.name for path in (chair_service.unit_path(home), lander_cli.unit_path(home)) if path.exists()]


def _edge_restart(units: list[str]) -> str | None:
    """Edge. `try-restart` restarts only the units that are running, so a unit stopped on purpose stays stopped.
    None on success or with no unit, else why it failed."""
    if not units:
        return None
    code, out = _edge_run(["systemctl", "--user", "try-restart", *units])
    return None if code == 0 else f"systemctl --user try-restart {' '.join(units)} exited {code}: {out.strip()}"


def _edge_unit_states(units: list[str]) -> dict[str, str]:
    """Edge. What `systemctl --user is-active` prints for each unit, in order; a unit it printed nothing for is absent.
    is-active exits nonzero when any unit is not active, so its code is not read."""
    if not units:
        return {}
    _code, out = _edge_run(["systemctl", "--user", "is-active", *units])
    return dict(zip(units, (line.strip() for line in out.splitlines() if line.strip()), strict=False))


def _edge_venv_check(checkout: Path) -> int:
    """Edge. The exit code of `uv sync --check` for the checkout's venv: 0 when it matches the checkout's lock."""
    argv = [*setup_install.SYNC_ARGV[:2], "--check", *setup_install.SYNC_ARGV[2:], "--directory", str(checkout)]
    return _edge_run(argv)[0]


def _edge_doctor(before: Mapping[str, str] | None = None, units: list[str] | None = None) -> str | None:
    """Edge. Runs the moved install's doctor (`cox` on PATH is the checkout's own) and reads the units' states;
    `doctor.edge_verdict` judges them. None when the required tier passes; an advisory failure only prints a warning."""
    argv = [*remote_argv.doctor_argv(), "--json"]
    code, out = _edge_run(argv)
    failure, warning = doctor.edge_verdict(argv, code, out, units or [], before or {}, _edge_unit_states(units or []))
    if warning:
        print(warning)
    return failure


def _edge_record_read(path: Path) -> dict | None:
    text = _read_text_or_none(path)
    try:
        data = json.loads(text) if text is not None else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _edge_record_write(path: Path, previous: str, chosen: str, outcome: str, detail: str = "") -> None:
    path.write_text(json.dumps(upgrade_edge.record(_now_iso(), previous, chosen, outcome, detail), indent=2) + "\n",
                    encoding="utf-8")


def _upgrade_edge(a: argparse.Namespace) -> int:
    """Move an edge machine to the newest green commit on main by `_upgrade_move`, the `--to` path. When the commit
    changed: restart the running chair and lander units, then run the doctor. A failed move, restart or doctor moves
    back to the previous commit and restarts again, unless the units ran a commit that adds store migrations (see
    `upgrade_edge.may_roll_back`). The outcome is recorded beside the checkout, where `cox versions` shows it."""
    resolved = _install_root(a)
    if resolved is None:
        return 2
    root, home = resolved
    checkout = _checkout_path(root)
    manifest_path = Path(a.manifest) if a.manifest else checkout / "manifest.toml"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print((_NO_HOME_MANIFEST if home and not a.manifest else _NO_MANIFEST).format(manifest_path))
        return 2
    found = _edge_target(checkout, install.checkout_spec(manifest)["repo"])
    if isinstance(found, str):
        print(f"refusing: {found}")
        return 2
    head, target, first_parent = found
    record_path = root / upgrade_edge.RECORD_FILE
    last = _edge_record_read(record_path)
    print(f"channel: edge; the newest green commit on main is {target[:12]}; this machine is at {head[:12]}")
    verdict = upgrade_edge.decide(head, target, first_parent, last)
    if verdict != upgrade_edge.MOVE:
        print(upgrade_edge.verdict_line(verdict, head, target, last))
        if upgrade_edge.needs_resync(verdict, _edge_venv_check(checkout)):
            return _edge_resync(a, head)
        return 0
    added = _edge_added_migrations(checkout, head, target)
    if isinstance(added, str):
        print(f"refusing: {added}")
        return 2
    if added:
        print(f"edge: {target[:12]} adds store migrations {', '.join(added)}; a restarted unit applies them to the "
              "store, and a failed doctor then cannot roll back past them")
    units = _edge_units()
    before = _edge_unit_states(units)
    unknown = doctor.unreadable_units(units, before)
    if unknown:
        # After the move there would be no telling whether the upgrade stopped these units, so do not move.
        print(f"refusing: systemctl --user is-active gave no state for {', '.join(unknown)}")
        return 2
    if a.dry_run:
        print(f"would restart: {', '.join(units) or 'no chair or lander unit'}; would run cox setup doctor")
        return _upgrade_move(argparse.Namespace(**{**vars(a), "to": target}))
    code = _upgrade_move(argparse.Namespace(**{**vars(a), "to": target}))
    if code != 0 and _edge_git(checkout, "rev-parse", "HEAD")[1].strip() == head:
        # Refused or failed before the checkout moved (a dirty checkout, a failed fetch): nothing ran after it, so
        # there is nothing to move back, and no record holds the commit against the next run.
        print(f"edge: nothing moved; cox upgrade --to {target} exited {code}")
        return 2
    failure = None if code == 0 else f"cox upgrade --to {target} exited {code}"
    restarted = failure is None and bool(units)
    failure = failure or _edge_restart(units) or _edge_doctor(before, units)
    if failure is None:
        _edge_record_write(record_path, head, target, upgrade_edge.UPGRADED)
        print(f"edge: upgraded {head[:12]} -> {target[:12]}")
        return 0
    print(f"edge: {failure}")
    if not upgrade_edge.may_roll_back(added, restarted):
        detail = (f"{failure}; not rolled back to {head[:12]}: {target[:12]} adds store migrations {', '.join(added)}, "
                  f"the restarted units may have applied them, and {head[:12]} cannot open a store that has them")
        _edge_record_write(record_path, head, target, upgrade_edge.NOT_ROLLED_BACK, detail)
        print(f"edge: {detail}")
        return 1
    back = _upgrade_move(argparse.Namespace(**{**vars(a), "to": head}))
    again = (_edge_restart(units) if restarted else None) if back == 0 else f"cox upgrade --to {head} exited {back}"
    outcome = upgrade_edge.ROLLED_BACK if again is None else upgrade_edge.ROLLBACK_FAILED
    detail = failure if again is None else f"{failure}; rolling back failed: {again}"
    _edge_record_write(record_path, head, target, outcome, detail)
    print(f"edge: rolled back {target[:12]} -> {head[:12]}: {failure}" if again is None
          else f"edge: rollback to {head[:12]} FAILED: {again}")
    return 1


def _edge_resync(a: argparse.Namespace, head: str) -> int:
    """Edge. The checkout is where it should be but its venv is not synced to it (a move whose sync was killed, or a
    systemd timeout mid-upgrade): run the after-move steps at the same commit, restart the running units and judge the
    doctor. Nothing to roll back to, so a failure is reported and left for the next run or a person."""
    print(f"edge: the venv is not synced to {head[:12]}; syncing it")
    if a.dry_run:
        return 0
    units = _edge_units()
    before = _edge_unit_states(units)
    code = _upgrade_move(argparse.Namespace(**{**vars(a), "to": head}))
    failure = (None if code == 0 else f"cox upgrade --to {head} exited {code}") or _edge_restart(units) or _edge_doctor(
        before, units)
    print(f"edge: resynced {head[:12]}" if failure is None else f"edge: resync of {head[:12]} failed: {failure}")
    return 0 if failure is None else 1


def _upgrade_timer_install(a: argparse.Namespace) -> int:
    """Edge. Writes the upgrade schedule (a systemd service and timer on Linux, a launchd agent on macOS) and prints
    the lines that activate it; runs no launchd or systemd tool. Both read the one env file the other units read."""
    if sys.platform not in ("linux", "darwin"):
        print(f"refusing: the upgrade schedule is a systemd timer or a launchd agent, and {sys.platform} has neither")
        return 2
    if a.interval_minutes < 1:
        print("refusing: --interval-minutes must be at least 1")
        return 2
    resolved = _chair_unit_inputs(None, _chair_service_environment_file(a), _profile_path(a))
    if isinstance(resolved, _ChairUnitRefusal):
        print(resolved.message)
        return 2
    files = upgrade_edge.schedule_files(sys.platform, Path.home(), resolved.cox, resolved.profile, resolved.path,
                                        a.interval_minutes, resolved.env_file)
    for path, text in files:
        _write_private(path, text)
        print(path)
    print("\n".join(upgrade_edge.activation_lines(sys.platform, files[0][0])))
    channel = _upgrade_channel(a)
    if channel != "edge":
        print(f"note: this machine's channel is {channel}; the timer moves nothing until "
              "`cox settings set profile upgrade_channel edge`")
    return 0


def _upgrade_timer_uninstall() -> int:
    """Edge. Removes this platform's schedule files that exist; runs no launchd or systemd tool, so a schedule still
    loaded keeps its last state until `systemctl --user disable --now coxswain-upgrade.timer` or `launchctl bootout`."""
    present = [p for p in upgrade_edge.schedule_paths(sys.platform, Path.home()) if p.exists()]
    for path in present:
        path.unlink()
        print(f"removed {path}")
    if not present:
        print("no upgrade timer installed")
    return 0


def _upgrade_move(a: argparse.Namespace) -> int:
    """Move the one checkout to the target (`--to`, a release or a commit, else the newest release tag), sync its
    venv, reinstall cox and the crew links, then move any optional component the moved manifest pins, then run the
    doctor."""
    resolved = _install_root(a)
    if resolved is None:
        return 2
    root, home = resolved
    checkout = _checkout_path(root)
    manifest_path = Path(a.manifest) if a.manifest else checkout / "manifest.toml"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print((_NO_HOME_MANIFEST if home and not a.manifest else _NO_MANIFEST).format(manifest_path))
        return 2
    with_flags = _with_flags(a)
    options = {"provider": a.provider, "with": with_flags, "root": str(root), "team": a.team, "workspace": a.workspace,
               **({"home": home} if home else {})}
    if _upgrade_refused(manifest, root, options):
        return 2
    checkout_tag = _checkout_facts(checkout)["tag"]
    target = upgrade_plan.target_ref(a.to) if a.to else upgrade_exec.newest_release(_offered_tags(checkout))
    if target is None:
        print(f"refusing: {checkout} offers no release tag to upgrade to; pass --to")
        return 2
    after_move = _upgrade_after_move(root)
    if a.dry_run:
        print("dry run: component steps use the pins in the current manifest; a real run re-reads them after the checkout moves")
        plan, urls = _upgrade_plan_for(manifest, root, target, checkout_tag, with_flags)
        return upgrade_exec.run_plan(plan, root=root, run=_real_run, dry_run=True, clone_urls=urls, after_move=after_move)
    _load_lazy_modules()  # the tool reinstall replaces this cox's own environment
    checkout_only = upgrade_plan.plan_upgrade(target, checkout_tag or "untagged", {}, {}, {})
    code = upgrade_exec.run_plan(checkout_only, root=root, run=_real_run, dry_run=False, after_move=after_move)
    if code != 0:
        return code
    moved_path = checkout / "manifest.toml"
    moved = _load_manifest(moved_path)
    if moved is None:
        print(_NO_MANIFEST.format(moved_path))
        return 2
    if _upgrade_refused(moved, root, options):
        return 2
    plan, urls = _upgrade_plan_for(moved, root, target, target, with_flags)
    code = upgrade_exec.run_plan(dataclasses.replace(plan, steps=plan.steps[1:]), root=root, run=_real_run, dry_run=False, clone_urls=urls)
    if code != 0:
        return code
    facts = _upgrade_facts(moved, root, a.provider)
    tail_options = _with_present(moved, facts, options)
    steps = install.plan(moved, facts, tail_options)
    last = {step["component"]: at for at, step in enumerate(steps)}
    tail = [step for at, step in enumerate(steps)
            if step["kind"] in ("binary", "doctor", "desktop")
            or (step["kind"] == "skip" and moved.get("components", {}).get(step["component"], {}).get("provides")
                and last[step["component"]] == at)]
    return _install_execute(tail, moved, tail_options, root, name_stale=True)


def _with_present(manifest: dict, facts: dict, options: dict) -> dict:
    """`options` whose `with` also names the flag of every component whose checkout is present, so the planner
    treats what `_upgrade_plan_for` moved as requested and plans its binary."""
    present = [spec["flag"] for name, spec in manifest.get("components", {}).items()
               if spec.get("flag") and facts["checkouts"].get(name, {}).get("present")]
    return {**options, "with": list(dict.fromkeys([*options["with"], *present]))}


def _checkout_schema_versions(root: Path) -> dict[str, str | None]:
    """The schema versions read from the one checkout under `root`."""
    paths = setup_install.layout(str(root))
    return _schema_versions(paths.harness_dir, paths.provider_profile, [paths.skills_root])


def _schema_versions(harness_dir: str = "", provider_profile: str = "", skills_roots: list[str] | None = None
                      ) -> dict[str, str | None]:
    """The schema each of the three tools was built against, read from the
    profile's (or `--root`'s) checkouts first: on an unmigrated host graphs and
    cartridges are not installed in this venv, and on a migrated one the
    profile's checkout is the one the harness runs."""
    return {
        "cartridges": schema.cartridges_schema(provider_profile, skills_roots or []),
        "graphs": schema.graphs_schema(harness_dir),
        "tools": schema.TOOLS_SCHEMA,
    }


def _versions(a: argparse.Namespace) -> int:
    if a.manifest and not a.root:
        manifest_path = Path(a.manifest)
        root = manifest_path.resolve().parent.parent
    else:
        resolved = _install_root(a)
        if resolved is None:
            return 2
        root = resolved[0]
        manifest_path = Path(a.manifest) if a.manifest else _checkout_path(root) / "manifest.toml"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print(_NO_MANIFEST.format(manifest_path))
        return 2
    facts = {"root": str(root), "checkouts": _gather_checkout_facts(root, manifest.get("components", {})),
              "provider_cli_on_path": False}
    schema_versions = _checkout_schema_versions(root)
    from agent_tools import versions_hint
    from agent_tools.version_info import UNKNOWN, package_version

    def git_out(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    umbrella_tag = git_out(str(_checkout_path(root)), "describe", "--tags") or None
    release = package_version()
    release_tag = None if release == UNKNOWN else release
    hint = versions_hint.umbrella_hint(umbrella_tag, release_tag)

    def offered(component: str) -> frozenset[str]:
        return frozenset((git_out(install_exec.clone_dir(str(root), component), "tag", "--list") or "").split())

    def label(component: str, pinned: str | None, status: str) -> str:
        # Only a drift row pinned at the umbrella's own release is explained by a stale umbrella; a component
        # with its own numbering keeps "drift". `git tag --list` runs for those rows alone.
        if hint is None or umbrella_tag is None or release_tag is None or status != "drift" or not pinned:
            return status
        if not versions_hint.same_release(pinned, umbrella_tag):
            return status
        return versions_hint.status_label(status, pinned, release_tag, offered(component))

    # The checkout carries all three schemas, so its cell is their agreement; an optional component has none.
    schema_cell = {install.CHECKOUT: schema.status(schema_versions)[0]}
    display = [{"component": c, "pinned_tag": p or "", "installed_tag": i or "", "status": label(c, p, s),
                "schema": schema_cell.get(c, "?")}
               for c, p, i, s in install.rows(manifest, facts)]
    if hint:
        print(hint)
    print(records.format_table(display, ["component", "pinned_tag", "installed_tag", "status", "schema"]))
    edge = _edge_record_read(root / upgrade_edge.RECORD_FILE)
    if edge is not None:
        print(upgrade_edge.record_line(edge))
    return 0


RELEASE_DEPLOY_UNAVAILABLE = (
    "cox release deploy: not available. Its host-by-host edge (agent_tools.release_deploy_edge) was never built, so this"
    " machine cannot upgrade the others yet; run `cox upgrade` on each host instead."
)


def _release_deploy(a: argparse.Namespace) -> int:
    """`release deploy VERSION` refuses with exit 2 and says why. The command shipped (#900) wired to an edge module
    that never landed, so it raised ImportError; the per-host core, release_deploy.py, waits for that edge."""
    print(RELEASE_DEPLOY_UNAVAILABLE, file=sys.stderr)
    return 2


def _release(a: argparse.Namespace) -> int:
    """`release deploy VERSION` deploys; every other `release ...` is an old maintainer command and gets the moved pointer.
    `rest` is the unparsed remainder, so an old subcommand or flag never meets argparse's invalid-choice error."""
    if a.rest[:1] != ["deploy"]:
        return _dev_moved(a)
    deploy_p = argparse.ArgumentParser(prog="cox release deploy", description="upgrade every host to VERSION and verify each")
    deploy_p.add_argument("version", metavar="VERSION", help="the release tag, e.g. 2026.11.6")
    deploy_p.add_argument("--profile", help="the profile whose run store holds the hosts")
    parsed = deploy_p.parse_args(a.rest[1:])
    return _release_deploy(argparse.Namespace(profile=parsed.profile or getattr(a, "launcher_profile", None), version=parsed.version))
