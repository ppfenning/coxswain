"""`cox setup migrate`: moving an install to the current layout."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

from agent_tools.cli_chair_service import _chair_unit_text, _ChairUnitRefusal
from agent_tools.cli_common import STORE_DOUBLE_ENV, _now_iso, _read_text_or_none
from agent_tools.cli_doctor import (
    _doctor_home_rows,
    _install_run,
    _marketplace_listing,
    _marketplace_plugins,
    _migrate_cartridge_links,
    _migrate_install_bins,
    _migrate_link,
    _migrate_observe_hosts,
    _migrate_old_root,
    _migrate_profile,
)
from agent_tools.cli_host import _origin_url
from agent_tools.cli_install import _checkout_facts, _load_lazy_modules, _migrate_kind, _offered_tags
from agent_tools.cli_lake import _lake_provider
from agent_tools.cli_lazy import (
    bounded_run,
    chair_read_lease,
    chair_service,
    chair_unit,
    home_paths,
    install,
    land_repo_lease,
    lane_hosts,
    repo_path,
    route,
    run_store,
    setup_install,
    setup_migrate,
    setup_migrate_hosts,
    store_cli,
    store_dialect,
    upgrade_exec,
    upgrade_plan,
    work_state,
)
from agent_tools.cli_route import _paths_apply_files, _paths_apply_rows
from agent_tools.cli_runs_fetch import _profile_lane_hosts


def _migrate_worktrees(checkout: Path) -> list[str] | None:
    """Edge. Every path `git worktree list` names for the checkout, main one first; None when git cannot list."""
    try:
        done = bounded_run.completed(["git", "-C", str(checkout), "worktree", "list", "--porcelain"],
                              capture_output=True, text=True)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    return [line[len("worktree "):] for line in done.stdout.splitlines() if line.startswith("worktree ")]


def _migrate_checkouts() -> tuple[str, ...]:
    """Pure. The checkout names the migration looks at: the required ones, then the optional umbrella once."""
    return tuple(dict.fromkeys((*setup_migrate.CHECKOUTS, setup_migrate.UMBRELLA)))


def _migrate_checkout_url(checkout: Path) -> str:
    """Edge. The checkout's origin, else the coxswain repository on GitHub."""
    return (_origin_url(str(checkout)) if checkout.is_dir() else None) or f"https://github.com/{install.CHECKOUT_REPO}.git"


def _migrate_target_tag(checkout: Path, url: str, to: str | None) -> str | None:
    """Edge. `--to` as a release tag, else the newest release the checkout's remote offers."""
    if to:
        return upgrade_plan.release_tag(to)
    return upgrade_exec.newest_release(_offered_tags(checkout, None if checkout.is_dir() else url))


def _migrate_locations(locs):
    """Edge. The locations the migration works in: the folder of a `cox install --root` install, read from the
    profile's harness_dir and holding the umbrella checkout, else the home's (`setup_migrate.install_locations`)."""
    folder = setup_migrate.profile_install_folder(_migrate_profile(locs).get("harness_dir"), posixpath.dirname(locs.config))
    present = bool(folder) and Path(folder, setup_migrate.UMBRELLA).is_dir()
    return setup_migrate.install_locations(locs, folder, present)


def _migrate_venv_current(checkout: Path) -> bool:
    """Edge. Whether the checkout's venv matches its lock with every extra: `uv sync --check` exits 0, changing nothing."""
    argv = [*setup_install.SYNC_ARGV[:2], "--check", *setup_install.SYNC_ARGV[2:], "--directory", str(checkout)]
    try:
        return bounded_run.completed(argv, capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def _migrate_profile_tracked(installs: Path, provider_profile: str | None) -> bool | None:
    """Edge. Whether git tracks the provider profile in the cartridges checkout: True when `git ls-files
    --error-unmatch` exits 0, False when it exits 1 (untracked), None when the profile is outside that checkout or git
    cannot answer (missing, not a repository, timeout)."""
    if not provider_profile:
        return None
    checkout = installs / "coxswain-cartridges"
    path = Path(provider_profile).expanduser()
    if not path.is_relative_to(checkout):
        return None
    argv = ["git", "-C", str(checkout), "ls-files", "--error-unmatch", str(path.relative_to(checkout))]
    try:
        code = subprocess.run(argv, capture_output=True, text=True, timeout=10).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(code)


def _migrate_checkout_shallow(checkout: Path) -> bool:
    """Edge. Whether git calls the checkout shallow; an absent checkout or a failed git call is False."""
    argv = ["git", "-C", str(checkout), "rev-parse", "--is-shallow-repository"]
    try:
        proc = bounded_run.completed(argv, capture_output=True, text=True)
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _migrate_harness_in_checkout(harness_dir: object, checkout: Path) -> bool:
    """Whether the profile's harness_dir, tilde expanded, lies under the checkout path followed by a slash."""
    return isinstance(harness_dir, str) and os.path.expanduser(harness_dir).startswith(f"{checkout}/")


def _migrate_observe(locs, to: str | None = None) -> dict:
    """Edge. The host as the plain dict `setup_migrate.migrate_plan` takes. The remote is asked for a release tag
    only while the checkout is not yet the monorepo."""
    profile = _migrate_profile(locs)
    installs = Path(locs.installs)
    checkout = Path(setup_install.layout(locs.installs).checkout)
    kind = _migrate_kind(checkout)
    monorepo = kind == "dir" and setup_migrate.read_is_monorepo(str(checkout))
    url = _migrate_checkout_url(checkout)
    components = {name: _migrate_kind(installs / name) for name in setup_migrate.RETIRED}
    listed = {name: _migrate_worktrees(installs / name) for name, k in components.items() if k == "dir"}
    record = _read_text_or_none(Path(locs.record))
    links = _migrate_cartridge_links(profile)
    linux = sys.platform.startswith("linux")
    unit_file = chair_service.unit_path(Path.home())
    unit = _read_text_or_none(unit_file) if linux else None
    drop_ins = _migrate_chair_drop_ins(unit_file.parent) if linux else ()
    claude_on_path = shutil.which("claude") is not None
    bins = _migrate_install_bins()
    return {
        "checkout": {
            "kind": kind,
            "monorepo": monorepo,
            "url": url,
            "dirty": kind == "dir" and bool(_checkout_facts(checkout).get("dirty")),
            "shallow": kind == "dir" and _migrate_checkout_shallow(checkout),
        },
        "harness_in_checkout": _migrate_harness_in_checkout(profile.get("harness_dir"), checkout),
        "target_tag": None if monorepo else _migrate_target_tag(checkout, url, to),
        "components": components,
        "worktrees": {name: paths[1:] for name, paths in listed.items() if paths is not None},
        "venv_source": setup_migrate.read_venv_source(str(checkout)),
        "venv_current": kind == "dir" and _migrate_venv_current(checkout),
        "claude_on_path": claude_on_path,
        "marketplace": (_marketplace_listing() or {}).get(setup_install.MARKETPLACE),
        "marketplace_plugins": _marketplace_plugins(),
        "chair_unit": unit,
        "chair_unit_drop_ins": drop_ins,
        "chair_active": bool(drop_ins) and _migrate_chair_active(),
        "settings": {key: profile[key] for key in (*setup_migrate.SETTING_KEYS, setup_migrate.REPO_MAP_KEY) if key in profile},
        "home_record": record.strip() if record and record.strip() else None,
        "provider_profile_tracked": _migrate_profile_tracked(installs, profile.get("provider_profile")),
        "today": datetime.datetime.now(datetime.UTC).date().isoformat(),
        **({"install_bins": bins} if bins is not None else {}),
        **({"cartridge_links": links} if links is not None else {}),
        **({"workspace": {"path": profile["workspace_dir"]}} if isinstance(profile.get("workspace_dir"), str) else {}),
    }


def _migrate_move(step: dict) -> str | None:
    src, dst = Path(step["src"]), Path(step["dst"])
    if _migrate_kind(dst) != "absent":
        return f"{dst} already exists, so {src} was left where it is"
    if _migrate_kind(src) != "dir":
        return f"{src} is not a directory"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return f"cannot move {src} to {dst}: {exc}"
    print(f"move {src} -> {dst}")
    return None


def _migrate_copy(step: dict) -> str | None:
    src, dst = Path(step["src"]), Path(step["dst"])
    if dst.exists():
        if dst.read_bytes() == src.read_bytes():
            print(f"nothing to do: {dst} already holds the bytes of {src}")
            return None
        return f"{dst} already exists and differs from {src}; nothing was copied"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(src, dst)
    except OSError as exc:
        return f"cannot copy {src} to {dst}: {exc}"
    print(f"copy {src} -> {dst}")
    return None


def _migrate_profile_value(value) -> str:
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False)
    return "[" + ", ".join(str(v) for v in value) + "]" if isinstance(value, list) else str(value)


def _migrate_edit_key(text: str, key: str, new, add: bool = False) -> tuple[str | None, str]:
    """Pure. (edited text, "") with the one top-level `key:` line replaced, or appended when `add` and the profile
    has none, or (None, reason) when no one-line edit reads back as `new`."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.startswith(f"{key}:")]
    line = f"{key}: {_migrate_profile_value(new)}\n"
    if add and not hits:
        edited = text + ("" if not text or text.endswith("\n") else "\n") + line
    elif len(hits) != 1:
        return None, f"the profile has {len(hits)} top-level {key} lines, and one is needed"
    else:
        edited = "".join([*lines[: hits[0]], line, *lines[hits[0] + 1 :]])
    try:
        parsed = yaml.safe_load(edited)
    except yaml.YAMLError:
        parsed = None
    if not isinstance(parsed, Mapping) or parsed.get(key) != new:
        return None, f"{key} cannot be written as one line that reads back as {new!r}"
    if key in route._JSON_KEYS:
        try:
            read_back = json.loads(_migrate_profile_value(new))
        except json.JSONDecodeError:
            read_back = None
        if read_back != new:
            return None, f"{key} cannot be written as one line of JSON that reads back as {new!r}"
    return edited, ""


def _migrate_rewrite_line(path: Path, key: str, new, add: bool = False) -> str | None:
    """Edge. Writes `_migrate_edit_key`'s result; the file is untouched when it refuses."""
    text = _read_text_or_none(path)
    if text is None:
        return f"cannot read the profile at {path}"
    edited, reason = _migrate_edit_key(text, key, new, add)
    if edited is None:
        return f"{path}: {reason}"
    path.write_text(edited, encoding="utf-8")
    print(f"profile {key} -> {_migrate_profile_value(new)}")
    return None


def _migrate_checkout(step: dict) -> str | None:
    """Edge. Runs the step's git commands in order, then checks the checkout now holds the monorepo."""
    for argv in step["argvs"]:
        failure = _install_run({"argv": argv})
        if failure is not None:
            return failure
    if not setup_migrate.read_is_monorepo(step["path"]):
        return f"{step['tag']} is not a monorepo release (its pyproject names no coxswain distribution); pass --to"
    return None


def _migrate_git(step: dict) -> str | None:
    argv = " ".join(step["argv"])
    try:
        done = bounded_run.completed(step["argv"], capture_output=True, text=True)
    except OSError as exc:
        return f"{argv}: {exc}"
    if done.returncode != 0:
        return f"{argv} exited {done.returncode}: " + " ".join(done.stderr.strip().splitlines()[-3:])
    listed = [line[len("worktree "):] for line in done.stdout.splitlines() if line.startswith("worktree ")]
    missing = [p for p in listed if step["op"] == "verify" and not Path(p).exists()]
    if missing:
        return f"a worktree names the missing path {missing[0]}"
    print(f"run: {argv}")
    return None


def _migrate_exec(step: dict, locs) -> str | None:
    """Edge. Applies one local step; None on success, else the reason it failed."""
    op = step["op"]
    if op == "move":
        return _migrate_move(step)
    if op == "copy":
        return _migrate_copy(step)
    if op == "symlink":
        return _migrate_link(step)
    if op == "rewrite_profile_key":
        return _migrate_rewrite_line(Path(locs.profile), step["key"], step["new"], add=step["old"] is None)
    if op == "checkout":
        return _migrate_checkout(step)
    if op == "rewrite_setting":
        record = Path(step["path"])
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(step["new"] + "\n", encoding="utf-8")
        print(f"write {record}")
        return None
    if op in ("worktree_repair", "verify"):
        return _migrate_git(step)
    if op == "chair_unit":
        return _migrate_chair_unit(locs)
    if op == "chair_unit_dropin":
        return _migrate_chair_unit_dropin(step)
    if op == "run":
        return _install_run(step)
    if op == "skip":
        print(f"nothing to do: {step['why']}")
        return None
    if op == "print":
        print(f"note: {step['why']}")
        return None
    return step["why"] if op == "refuse" else f"unknown step op: {op!r}"


def _migrate_systemctl(*argv: str) -> subprocess.CompletedProcess | str:
    """Edge. One `systemctl --user` call, or the reason it could not run."""
    try:
        return bounded_run.completed(["systemctl", "--user", *argv], capture_output=True, text=True, check=False)
    except OSError as exc:
        return f"systemctl --user {' '.join(argv)}: {exc}"


def _migrate_chair_drop_ins(unit_dir: Path) -> tuple[tuple[str, str], ...]:
    """Edge. The `*.conf` drop-ins of the chair unit as (file name, text), by file name."""
    texts = ((conf.name, _read_text_or_none(conf)) for conf in sorted((unit_dir / chair_unit.DROP_IN_DIR).glob("*.conf")))
    return tuple((name, text) for name, text in texts if text is not None)


def _migrate_chair_active() -> bool:
    """Edge. Whether `systemctl --user is-active` says the chair service is running."""
    active = _migrate_systemctl("is-active", chair_service.UNIT_NAME)
    return not isinstance(active, str) and active.stdout.strip() == "active"


def _migrate_chair_unit_dropin(step: dict) -> str | None:
    """Edge. Rewrites the old checkout folders in the one drop-in the step names, reloads systemd and restarts the
    chair only when the step says it was running. The unit file and other drop-ins are not touched."""
    path = chair_service.unit_path(Path.home()).parent / chair_unit.DROP_IN_DIR / Path(step["dropin"]).name
    text = _read_text_or_none(path)
    if text is None:
        print(f"chair unit: drop-in {path} is unreadable; left as is")
        return None
    path.write_text(setup_migrate.rewrite_dropin_path(text, tuple((o, n) for o, n in step["pairs"])), encoding="utf-8")
    print(f"chair unit drop-in -> {path}")
    reloaded = _migrate_systemctl("daemon-reload")
    if isinstance(reloaded, str):
        return reloaded
    if reloaded.returncode != 0:
        return f"systemctl --user daemon-reload exited {reloaded.returncode}: {reloaded.stderr.strip()}"
    if not step["restart"]:
        return None
    restarted = _migrate_systemctl("restart", chair_service.UNIT_NAME)
    if isinstance(restarted, str):
        return restarted
    if restarted.returncode != 0:
        return f"systemctl --user restart {chair_service.UNIT_NAME} exited {restarted.returncode}: {restarted.stderr.strip()}"
    return None


def _migrate_chair_unit(locs) -> str | None:
    """Edge. Rebuilds the chair unit from the arguments the installed one carries, writes it, reloads systemd and
    restarts the chair only when it was running. None on success or skip, else the reason a systemctl call failed."""
    path = chair_service.unit_path(Path.home())
    args = chair_service.unit_args(_read_text_or_none(path) or "")
    if args is None:
        print(f"chair unit: no `chair run` line in {path}; left as is")
        return None
    missing = [name for name in ("label", "interval") if not args[name]]
    if missing or not str(args["interval"]).isdigit():
        print(f"chair unit: {path} has no usable {', '.join(missing) or 'interval'}; left as is")
        return None
    profile = Path(args["profile"]) if args["profile"] else Path(locs.profile)
    text = _chair_unit_text(args["label"], args["interval"], args["env_file"], profile)
    if isinstance(text, _ChairUnitRefusal):
        print(f"chair unit: {text.message}; {path} left as is")
        return None
    path.write_text(text, encoding="utf-8")
    print(f"chair unit -> {path}")
    reloaded = _migrate_systemctl("daemon-reload")
    if isinstance(reloaded, str):
        return reloaded
    if reloaded.returncode != 0:
        return f"systemctl --user daemon-reload exited {reloaded.returncode}: {reloaded.stderr.strip()}"
    active = _migrate_systemctl("is-active", chair_service.UNIT_NAME)
    if isinstance(active, str) or active.stdout.strip() != "active":
        return active if isinstance(active, str) else None
    restarted = _migrate_systemctl("restart", chair_service.UNIT_NAME)
    if isinstance(restarted, str):
        return restarted
    if restarted.returncode != 0:
        return f"systemctl --user restart {chair_service.UNIT_NAME} exited {restarted.returncode}: {restarted.stderr.strip()}"
    return None


def _migrate_edit_lane_host(text: str, name: str, workspace_dir: str) -> tuple[str | None, str]:
    """Pure. (edited text, "") with one block-style lane_hosts entry's workspace_dir line re-pointed and checked by
    re-parsing, or (None, reason)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith("lane_hosts:")), None)
    if start is None:
        return None, "the profile has no lane_hosts block"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and not lines[i][0].isspace() and not lines[i].startswith("-")), len(lines))
    starts = [i for i in range(start + 1, end) if lines[i].lstrip().startswith("- ")]
    for first, last in zip(starts, [*starts[1:], end]):
        named = [i for i in range(first, last) if re.match(r"^\s*(-\s+)?name:\s*['\"]?" + re.escape(name) + r"['\"]?\s*$", lines[i])]
        field = [i for i in range(first, last) if re.match(r"^\s*(-\s+)?workspace_dir:", lines[i])]
        if named and len(field) == 1:
            prefix = re.match(r"^(\s*(?:-\s+)?)", lines[field[0]]).group(1)
            lines[field[0]] = f"{prefix}workspace_dir: {workspace_dir}\n"
            out = "".join(lines)
            parsed = _profile_lane_hosts(out)
            found = None if isinstance(parsed, lane_hosts.LaneHostError) else lane_hosts.find_lane_host(parsed, name)
            if found is None or found.workspace_dir != workspace_dir:
                return None, f"the lane_hosts entry for {name} does not read back with workspace_dir {workspace_dir}"
            return out, ""
    return None, f"the profile has no block-style lane_hosts entry for {name} with one workspace_dir line"


def _migrate_rewrite_lane_host(path: Path, name: str, workspace_dir: str) -> str | None:
    """Edge. Writes `_migrate_edit_lane_host`'s result; the file is untouched when it refuses."""
    text = _read_text_or_none(path)
    if text is None:
        return f"cannot read the profile at {path}"
    edited, reason = _migrate_edit_lane_host(text, name, workspace_dir)
    if edited is None:
        return f"{path}: {reason}"
    path.write_text(edited, encoding="utf-8")
    return None


def _migrate_run_host_step(step, locs) -> tuple[bool, str]:
    """Edge. Applies one host step and returns (succeeded, what happened). A skip or print step does nothing and
    says its reason, so a host the planner could not judge is never reported as migrated. Tests replace this."""
    if step.op == "remote_migrate":
        argv = shlex.join(step.argv)
        try:
            done = bounded_run.completed(step.argv, capture_output=True, text=True)
        except OSError as exc:
            return False, f"{argv}: {exc}"
        if done.returncode != 0:
            return False, f"{argv} exited {done.returncode}: " + " ".join(done.stderr.strip().splitlines()[-3:])
        return True, f"ran {argv}"
    if step.op == "rewrite_host_workspace":
        failure = _migrate_rewrite_lane_host(Path(locs.profile), step.host, step.payload["workspace_dir"])
        return (False, failure) if failure else (True, f"lane_hosts workspace_dir set to {step.payload['workspace_dir']}")
    if step.op in ("skip", "print"):
        return True, step.why
    return False, f"unknown host step op: {step.op!r}"


def _migrate_needs_preflight(plan, host_plan, hosts: bool) -> bool:
    """Pure. True when a local step, or a host step under --hosts, does anything beyond skip and print."""
    local = (step["op"] for step in plan)
    remote = (step.op for group in host_plan for step in group) if hosts else ()
    return any(op not in ("skip", "print") for op in (*local, *remote))


def _migrate_acquire(runs_dir: Path, paths, holder: str) -> list:
    """Edge. One repo lease per checkout path as (LeaseOutcome, epoch or None); an exception is an error outcome.

    A machine with no Postgres store has nothing to lease in: no chair or lane can be coordinating through a store
    that is not there. Its migration goes ahead unleased, and says so. This is the upgrade from a release whose
    default store was a local sqlite file."""
    try:
        shared = store_dialect.is_postgres(run_store._store_url(runs_dir))
    except Exception:
        shared = False
    if not shared and os.environ.get(STORE_DOUBLE_ENV) != "sqlite":
        print("migrate: no Postgres store is configured, so there is no lease to take; migrating this machine unleased", file=sys.stderr)
        return [(setup_migrate.LeaseOutcome(path, True, holder), None) for path in paths]
    taken = []
    for path in paths:
        try:
            got = land_repo_lease.acquire(runs_dir, path, holder, 3600)
        except Exception as exc:  # any failure to lease must refuse the migration, not crash it
            taken.append((setup_migrate.LeaseOutcome(path, False, None, f"{type(exc).__name__}: {exc}"), None))
            continue
        if isinstance(got, store_cli.LeaseGranted):
            taken.append((setup_migrate.LeaseOutcome(path, True, got.holder), got.epoch))
        elif isinstance(got, store_cli.LeaseRefused):
            taken.append((setup_migrate.LeaseOutcome(path, False, got.holder), None))
        else:
            taken.append((setup_migrate.LeaseOutcome(path, False, None, str(getattr(got, "detail", None) or getattr(got, "reason", None) or type(got).__name__)), None))
    return taken


def _release_lines(outcomes) -> list[str]:
    """Pure. Stands in for `setup_migrate.release_lines` until that lands: one line per outcome not released."""
    return [f"migrate: could not release the lease on {path}: {detail}" for path, released, detail in outcomes if not released]


def _migrate_release(runs_dir: Path, taken, holder: str) -> None:
    """Edge. Releases every granted lease; one failed release does not stop the rest."""
    outcomes = []
    for outcome, epoch in taken:
        if outcome.granted and epoch is not None:
            try:
                result = land_repo_lease.release(runs_dir, outcome.path, holder, epoch)
            except Exception as exc:  # the lease expires on its ttl, and the other releases still matter
                outcomes.append((outcome.path, False, str(exc)))
                continue
            if isinstance(result, (store_cli.LeaseGranted, store_cli.LeaseReleased)):
                outcomes.append((outcome.path, True, ""))
            else:
                detail = getattr(result, "detail", None) or getattr(result, "reason", None) or type(result).__name__
                outcomes.append((outcome.path, False, str(detail)))
    for line in _release_lines(outcomes):
        print(line)


def _migrate_read_holders(runs_dir: Path, hostname: str) -> tuple:
    """Edge. Live lanes, the chair lease and this host's hosts-table row as the plain types `preflight_refusals` takes."""
    lanes = tuple(setup_migrate.Lane(lane.run, lane.host) for lane in run_store.live_lanes(runs_dir, _now_iso()))
    row = chair_read_lease.read_lease(runs_dir, datetime.datetime.now(datetime.UTC))
    chair_lease = setup_migrate.ChairLease(bool(row["released"]), bool(row["stale"]), row["host"] or None)
    rows = tuple(setup_migrate.HostRow(str(r["name"]), str(r["state"])) for r in run_store.hosts(runs_dir) if str(r["name"]) == hostname)
    return lanes, chair_lease, rows


def _migrate_runs_dir(locs) -> Path:
    workspace = _migrate_profile(locs).get("workspace_dir")
    return Path(workspace if isinstance(workspace, str) else locs.workspaces).expanduser() / "runs"


_OPERATOR_SUFFIXES = frozenset({".sh", ".py", ".zsh", ".service", ".timer", ".plist", ".conf"})
_OPERATOR_MAX_BYTES = 1024 * 1024


def _migrate_operator_dirs(locs) -> list[Path]:
    """Edge. Folders searched flat for operator files: home, the user systemd folders, LaunchAgents, state and its `cox` sibling."""
    home, state = Path.home(), Path(locs.state)
    systemd = home / ".config" / "systemd" / "user"
    drop_ins = sorted(p for p in systemd.iterdir() if p.is_dir() and p.name.endswith(".d")) if systemd.is_dir() else []
    return [home, systemd, *drop_ins, home / "Library" / "LaunchAgents", state, state.parent / "cox"]


def _migrate_operator_files(locs) -> dict[str, str]:
    """Edge. Path to text for each regular file under 1 MiB with an operator suffix; unreadable files and missing folders are skipped."""
    found: dict[str, str] = {}
    for folder in _migrate_operator_dirs(locs):
        try:
            children = sorted(folder.iterdir())
        except OSError:
            continue
        for path in children:
            try:
                if path.suffix not in _OPERATOR_SUFFIXES or path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size >= _OPERATOR_MAX_BYTES:
                    continue
                found[str(path)] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return found


def _migrate_stale_lines(locs) -> list[str]:
    """Edge. Lines for operator files still naming an old checkout; none when `migrate.json` is absent or names no old root."""
    marker = _read_text_or_none(Path(locs.state) / "migrate.json")
    try:
        data = json.loads(marker) if marker is not None else None
    except ValueError:
        return []
    old_root = data.get("old_root") if isinstance(data, dict) else None
    if not isinstance(old_root, str) or not old_root:
        return []
    olds = tuple(os.path.join(old_root, name) for name in _migrate_checkouts())
    return setup_migrate.stale_script_lines(_migrate_operator_files(locs), olds, str(Path.home()))


def _migrate_beat(locs) -> str:
    """Edge. Beats for this machine through the installed `cox host beat`; `beat: ok`, or `beat: failed: <detail>; <rerun hint>`.

    A subprocess, not an in-process call: the apply may have reinstalled cox editable from the moved checkout, which
    removes the package this process imported from, so any lazy import here would fail."""
    hint = "run `cox host beat` on this machine"
    cox = shutil.which("cox")
    if cox is None:
        return f"beat: failed: cox not on PATH; {hint}"
    env = {**os.environ, "AGENT_TOOLS_PROFILE": str(locs.profile)}
    try:
        done = subprocess.run([cox, "host", "beat"], capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"beat: failed: {exc}; {hint}"
    if done.returncode == 0:
        return "beat: ok"
    detail = (done.stderr or done.stdout).strip()
    return f"beat: failed: cox host beat exit {done.returncode}: {detail}; {hint}"


def _migrate_finish_queue(ws: Path, store_mode: bool) -> tuple[list, dict[str, str]] | None:
    """Edge. The queue rows (store mode) or the work files (file mode) `finish` rewrites; None when the store cannot be read."""
    from agent_tools import store_fill  # imported here: cli loads a command module only when it runs

    runs_dir = ws / "runs"
    rows = run_store.read_queue(runs_dir) if store_mode else []
    if store_mode and not rows and store_fill._read_store(runs_dir) is None:
        return None
    files = {} if store_mode else {"/".join(parts): text for parts, text in store_fill._workspace_files(ws)}
    return rows, files


class _FinishStopped(RuntimeError):
    """A queue read or write failed inside `finish`; args are (message, exit code). No link or marker is removed after it."""


def _migrate_unreadable(ws: Path) -> str:
    return f"finish: the store under {ws / 'runs'} cannot be read; no link or marker was removed"


def _migrate_finish_edge(ws: Path, store_mode: bool, home: str, locs) -> setup_migrate.FinishEdge:
    """Edge. Each move re-reads the queue, since a row with a repo and a surface under different checkouts takes one upsert per move."""

    def read() -> tuple[list, dict[str, str]]:
        queue = _migrate_finish_queue(ws, store_mode)
        if queue is None:
            raise _FinishStopped(_migrate_unreadable(ws), 2)
        return queue

    def write(moves: tuple[tuple[str, str], ...]) -> None:
        for move in moves:
            rows, files = read()
            code = _paths_apply_rows(ws / "runs", rows, home, move) if store_mode else _paths_apply_files(ws, files, home, move)
            if code:
                raise _FinishStopped("finish: rewriting the queue failed; no link or marker was removed", 1)

    return setup_migrate.FinishEdge(
        write,
        read,
        setup_migrate.remove_link,
        lambda: setup_migrate.delete_marker(setup_migrate.marker_path(locs)),
    )


def _migrate_links(old_root: str, home: str) -> tuple:
    """Edge. What sits at each old checkout path, the umbrella's compatibility link included."""
    seen = setup_migrate.observe_links(old_root, home)
    root = repo_path.expand(old_root, home) if old_root.startswith("~") else old_root
    named = {link.name for link in seen}
    extra = tuple(
        setup_migrate.LinkFact(name, path, _migrate_kind(Path(path)), os.readlink(path) if Path(path).is_symlink() else None)
        for name in _migrate_checkouts()
        if name not in named
        for path in [os.path.join(root, name)]
    )
    return (*seen, *extra)


def _migrate_finish(a: argparse.Namespace, locs) -> int:
    """Edge. `--finish`: rewrite the queue's old-root paths, then remove the old-path links. Plans unless `--apply`."""
    home = os.path.expanduser("~")
    marker = _read_text_or_none(Path(setup_migrate.marker_path(locs)))
    old_root = setup_migrate.resolve_old_root(a.old_root, marker)
    ws = _migrate_runs_dir(locs).parent
    provider, problem = _lake_provider(argparse.Namespace(profile=str(locs.profile)))
    if old_root and problem:
        print(f"finish: cannot tell where the queue lives ({problem}); no link or marker was removed")
        return 2
    store_mode = work_state.work_state_mode(provider) == "store"
    queue = _migrate_finish_queue(ws, store_mode) if old_root else ([], {})
    if queue is None:
        print(_migrate_unreadable(ws))
        return 2
    rows, files = queue
    if old_root and a.apply:
        lanes, chair_lease, host_rows = _migrate_read_holders(ws / "runs", socket.gethostname())
        if busy := setup_migrate.preflight_refusals((), lanes, socket.gethostname(), chair_lease, host_rows):
            print("\n".join(busy))
            return 1
    facts = setup_migrate.FinishFacts(
        a.old_root, marker, locs.installs, home,
        setup_migrate.migrate_plan(_migrate_observe(locs), locs),
        _doctor_home_rows(_migrate_profile(locs)),
        _migrate_observe_hosts(locs), rows, files,
        _migrate_links(old_root, home) if old_root else (),
    )
    try:
        result = setup_migrate.finish(facts, a.apply and not a.dry_run, _migrate_finish_edge(ws, store_mode, home, locs))
    except _FinishStopped as stop:
        print(stop.args[0])
        return stop.args[1]
    print("\n".join(result.lines))
    return result.code


def _setup_extras(a: argparse.Namespace) -> int:
    from agent_tools import extras_lock  # deferred: only this command reads the lock data
    return extras_lock.setup_extras(a.with_extras, a.dry_run)


def _setup_migrate(a: argparse.Namespace) -> int:
    """Plans by default; --apply executes the local steps and --apply --hosts also the host steps. --dry-run is the default spelled out.
    Under --apply a plan with any real step first takes a repo lease per old checkout and refuses while a lane, the chair or this host is live.
    --finish is its own mode: it takes --apply, --dry-run and --old-root; --hosts and a bare --old-root are refused."""
    locs = home_paths.resolve_here(a.home)
    if isinstance(locs, home_paths.Refused):
        print(f"refusing: {locs.reason}")
        return 2
    if a.finish and a.hosts:
        print("refusing: --finish is its own mode; --hosts belongs to the plain apply mode, so run them separately")
        return 2
    if a.old_root and not a.finish:
        print("refusing: --old-root belongs to --finish; pass --finish or drop --old-root")
        return 2
    locs = _migrate_locations(locs)
    if a.finish:
        return _migrate_finish(a, locs)
    observed = _migrate_observe(locs, getattr(a, "to", None))
    plan = setup_migrate.migrate_plan(observed, locs)
    host_plan = setup_migrate_hosts.host_migrate_plan(_migrate_observe_hosts(locs), locs)
    if not a.apply:
        print(setup_migrate.render_plan(plan))
        print("\n".join(setup_migrate_hosts.render_hosts_plan(host_plan)))
        print("no changes made; run with --apply to migrate")
        return 0
    taken: list = []
    runs_dir = _migrate_runs_dir(locs)
    holder = f"migrate@{socket.gethostname()}:{os.getpid()}"
    # split_pre_lease is added by the sibling task in setup_migrate.py; until it lands the whole plan runs after the lease.
    split = getattr(setup_migrate, "split_pre_lease", None)
    pre_lease, rest = split(plan, observed.get("harness_in_checkout", False)) if split else ([], plan)

    def run_steps(steps, first: int) -> int | None:
        for number, step in enumerate(steps, first):
            failure = _migrate_exec(step, locs)
            if failure is not None:
                print(f"FAILED step {number} {step['op']}: {failure}")
                return 1
        return None

    try:
        if any(step["op"] == "run" for step in plan):
            _load_lazy_modules()  # the reinstall replaces this tool's own environment, so import everything first
        stopped = run_steps(pre_lease, 1)
        if stopped is not None:
            return stopped
        if _migrate_needs_preflight(plan, host_plan, a.hosts):
            leased = (*(n for n in setup_migrate.RETIRED if observed["components"].get(n) == "dir"), setup_migrate.UMBRELLA)
            taken = _migrate_acquire(runs_dir, [str(Path(locs.installs) / name) for name in leased], holder)
            lanes, chair_lease, host_rows = _migrate_read_holders(runs_dir, socket.gethostname())
            refusals = setup_migrate.preflight_refusals(tuple(o for o, _ in taken), lanes, socket.gethostname(), chair_lease, host_rows)
            if refusals:
                print("\n".join(refusals))
                return 1
            old_root = _migrate_old_root(_migrate_profile(locs), locs)
            if old_root != locs.installs:  # a re-run after the move must keep the marker that names the real old root
                state = Path(locs.state)
                state.mkdir(parents=True, exist_ok=True)
                (state / "migrate.json").write_text(
                    json.dumps({"old_root": old_root, "new_root": locs.installs}), encoding="utf-8")
        stopped = run_steps(rest, len(pre_lease) + 1)
        if stopped is not None:
            return stopped
        failed = 0
        if not a.hosts:
            print("hosts: not touched; rerun with --apply --hosts on the chair's machine")
        for group in host_plan if a.hosts else ():
            for number, step in enumerate(group, 1):
                done, text = _migrate_run_host_step(step, locs)
                if not done:
                    print(f"host {step.host}: FAILED step {number} {step.op}: {text}")
                    failed += 1
                    break
                print(f"host {step.host}: {step.op}: {text}")
        for line in _migrate_stale_lines(locs):
            print(line)
        print(_migrate_beat(locs))
        print("verify with: cox setup doctor")
        return 1 if failed else 0
    finally:
        _migrate_release(runs_dir, taken, holder)
