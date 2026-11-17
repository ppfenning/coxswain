"""`cox host`: adding, removing, syncing and checking lane hosts."""

from __future__ import annotations

import argparse
import datetime
import json
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path

from agent_tools.cli import (
    _cartridges_checkout,
    _git_out,
    _holder_label,
    _host_repos,
    _leader_runs_dir_or_refuse,
    _now_iso,
    _profile_lane_hosts,
    _profile_path,
    _read_text_or_none,
    bounded_run,
    chair_capacity,
    chair_login_check,
    chair_orchestrate,
    host_cmd,
    lane_hosts,
    remote_doctor,
    run_store,
    store_cli,
)
from agent_tools.cli_route_launch import _lane_repo_dir, _live_by_host_name, _repo_check_tools, _shadowed_note


def _run_ssh(argv: list[str]) -> tuple[int, str]:
    """Edge: the one real subprocess call for `setup doctor --host`; stderr joins the output rows."""
    try:
        done = bounded_run.completed(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return 127, f"{argv[0]}: {exc}"
    return done.returncode, done.stdout


def _run_local(argv: list[str]) -> tuple[int, str]:
    """Edge: an argv's exit code and stdout; a tool that is missing, hangs or cannot run is code 127."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, done.stdout


def _host_store_write(a: argparse.Namespace, argv: list[str]) -> int:
    """Runs `store_cli <argv>` against the store and prints what it answers; non-zero when it refuses."""
    _, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    code, output = store_cli.runner(runs_dir)(argv)
    print(output.strip() if code == 0 else f"host: store_cli exit {code}: {output.strip()}")
    return 0 if code == 0 else 1


def _host_add(a: argparse.Namespace) -> int:
    """After the write, names the profile lane hosts the table now overrides, since they stop being lane hosts.
    Refuses before calling the store at all when `weight`/`capabilities` are not the defaults `add_argv` drops,
    since the hosts table has no column for either and sending them is what `host upsert` rejects.
    """
    if a.weight != 1 or (a.capabilities or "").strip():
        print(f"refused {a.name}: the store has no place for weight or capabilities yet")
        return 1
    code = _host_store_write(a, host_cmd.add_argv(a.name, a.ssh, a.capacity, a.weight, a.capabilities, _holder_label(a)))
    if code != 0:
        return code
    _, runs_dir, _ = _leader_runs_dir_or_refuse(a)
    names = [a.name, *(str(r["name"]) for r in run_store.hosts(runs_dir))]
    note = _shadowed_note(_read_text_or_none(_profile_path(a)) or "", names)
    if note:
        print(f"note: {note}")
    return 0


def _host_drain(a: argparse.Namespace) -> int:
    return _host_store_write(a, host_cmd.set_state_argv(a.name, "draining", _holder_label(a)))


def _host_deploy_only(a: argparse.Namespace) -> int:
    return _host_store_write(a, host_cmd.set_state_argv(a.name, "deploy_only", _holder_label(a)))


def _host_remove(a: argparse.Namespace) -> int:
    """Deletes a draining host's row once no lane runs on it, and prints the row so `host add` can restore it.
    A refusal and `--dry-run` both stop before the store is called."""
    _, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    row = next((r for r in run_store.hosts(runs_dir) if r.get("name") == a.name), None)
    local = socket.gethostname()
    live_runs = [lane.run for lane in run_store.live_lanes(runs_dir, _now_iso()) if (local if lane.host in (None, local) else lane.host) == a.name]
    refusal = host_cmd.remove_refusal(row, live_runs, a.name)
    if refusal is not None or row is None:
        print(refusal)
        return 1
    if a.dry_run:
        print(f"would remove {host_cmd.removed_line(row)}")
        return 0
    code = _host_store_write(a, host_cmd.delete_argv(a.name, _holder_label(a)))
    if code == 0:
        print(f"removed {host_cmd.removed_line(row)}")
    return code


def _host_activate(a: argparse.Namespace) -> int:
    return _host_store_write(a, host_cmd.set_state_argv(a.name, "active", _holder_label(a)))


_ORCHESTRATE_LOCAL_LANES = {"on": "decompose", "off": "any"}


def _host_orchestrate(a: argparse.Namespace) -> int:
    """No mode prints this machine's mode line. `on` is refused, with nothing written, unless another active
    lane host exists; `off` is never refused. Both write `policy.dispatch.local_lanes` through `cox settings set`."""
    profile, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    rows, now = run_store.hosts(runs_dir), _now_iso()
    if a.mode is None:
        configured = chair_capacity.chair_local_lanes(dict(profile))
        print(chair_orchestrate.mode_line(configured, rows, socket.gethostname(), now))
        return 0
    refusal = chair_orchestrate.toggle_refusal(a.mode, rows, now)
    if refusal is not None:
        print(refusal)
        return 1
    from agent_tools import cox_settings
    return cox_settings.run_set(
        _profile_path(a), "cartridge", "policy.dispatch.local_lanes", _ORCHESTRATE_LOCAL_LANES[a.mode], False,
    )


def _host_capacity(a: argparse.Namespace) -> int:
    """Changes only capacity, leaving the row's own ssh, weight and capabilities as they are. This only ever
    writes the store row: a lane already running on the host keeps running either way, and a capacity of 0
    only suspends new placement."""
    _, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    row = next((r for r in run_store.hosts(runs_dir) if r.get("name") == a.name), None)
    if row is None:
        print(f"refused {a.name}: no such host")
        return 1
    argv = host_cmd.capacity_upsert_argv(row, a.n, _holder_label(a))
    if not a.json:
        return _host_store_write(a, argv)
    code, output = store_cli.runner(runs_dir)(argv)
    try:
        written = json.loads(output) if code == 0 else None
    except json.JSONDecodeError:
        written = None
    print(json.dumps(written, indent=2) if written is not None else f"host: store_cli exit {code}: {output.strip()}")
    return 0 if written is not None else 1


def _host_list(a: argparse.Namespace) -> int:
    _, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    rows = run_store.hosts(runs_dir)
    live_by_host = _live_by_host_name(run_store.live_lanes(runs_dir, _now_iso()), socket.gethostname())
    lines = host_cmd.format_host_list(rows, datetime.datetime.now(datetime.UTC), live_by_host)
    print("\n".join(lines) if rows else "no hosts")
    return 0


def _origin_url(path: str) -> str | None:
    """Edge: `git -C path remote get-url origin`, or None when there is no such remote or no checkout there."""
    done = bounded_run.completed(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def _chair_service_host_repos(profile: Mapping) -> dict[str, str]:
    """Edge: `{checkout name: origin url}` for each sibling checkout `_host_repos` names, skipping any with no origin."""
    urls = {Path(p).name: _origin_url(p) for p in _host_repos(profile)}
    return {name: url for name, url in urls.items() if url is not None}


def _package_version() -> str | None:
    from agent_tools.version_info import package_version

    return package_version()


def _beat_argv(profile: Mapping, name: str) -> list[str]:
    """Edge. The store_cli argv that records a beat for `name` with this machine's versions and paths."""
    harness = str(Path(profile["harness_dir"]).expanduser()) if profile.get("harness_dir") else None
    workspace = str(Path(profile["workspace_dir"]).expanduser()) if profile.get("workspace_dir") else None
    versions = host_cmd.beat_versions(
        _run_local, _package_version(), harness, _cartridges_checkout(profile) or None, workspace, _host_repos(profile),
    )
    return host_cmd.beat_argv(name, versions)


def _host_beat(a: argparse.Namespace) -> int:
    refusal = host_cmd.beat_name_refusal(a.name, socket.gethostname())
    if refusal is not None:
        print(refusal)
        return 2
    profile, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    return _host_store_write(a, _beat_argv(profile, a.name or socket.gethostname()))


def _host_login_argv(name: str, login_ok: bool | None, reason: str | None, failures: int, by: str) -> list[str]:
    """The store_cli argv that stores login state and never stamps `beat_at`; `none` is spelled out, never omitted."""
    ok = "none" if login_ok is None else json.dumps(login_ok)
    return ["host", "login", name, "--login-ok", ok, *(["--reason", reason] if reason else []), "--failures", str(failures), "--by", by]


def _host_login(a: argparse.Namespace) -> int:
    """Stores login_ok, reason and the consecutive-failure count on the host's row and leaves `beat_at` as stored,
    so a host that failed its check is not made to look freshly beaten. `host beat` stays the writer of `beat_at`.
    Needs `harness.store_cli host login` (another repo); until it exists the store refuses and this exits 1."""
    return _host_store_write(a, _host_login_argv(a.name, a.login_ok, a.reason, a.failures, _holder_label(a)))


def _host_ssh(a: argparse.Namespace, profile: Mapping, runs_dir: Path) -> lane_hosts.LaneHost | None:
    """The named host from the table (any state), else from the profile's `lane_hosts`."""
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.name), None)
    if row is not None:
        return lane_hosts.LaneHost(str(row["name"]), str(row["ssh"]), "")
    parsed = _profile_lane_hosts(_read_text_or_none(_profile_path(a)) or "")
    return None if isinstance(parsed, lane_hosts.LaneHostError) else lane_hosts.find_lane_host(parsed, a.name)


def _host_doctor(a: argparse.Namespace) -> int:
    profile, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.name), None)
    print(host_cmd.doctor_line(row, datetime.datetime.now(datetime.UTC)))
    host = _host_ssh(a, profile, runs_dir)
    if host is None:
        print(f"unknown host: {a.name}")
        return 1
    repo_checks = [(_repo_check_tools(repo), _lane_repo_dir(repo, remote=True)) for repo in _host_repos(profile)]
    code, rows = remote_doctor.doctor_on_host(
        host, _run_ssh, argv_for=lambda argv: chair_login_check._remote_argv(host.name, host.ssh, argv), repo_checks=repo_checks,
    )
    print(f"doctor on {host.name} ({host.ssh})")
    print("\n".join(rows))
    return code


def _chair_heads(profile: Mapping) -> dict[str, host_cmd.ChairHead]:
    """Edge. `{checkout name: the commit it runs}` for this machine's checkouts (`_host_repos`), the commits
    `cox host sync` brings a lane host to; a checkout git cannot read is left out."""
    def head(path: str) -> host_cmd.ChairHead | None:
        sha = _git_out(Path(path), "rev-parse", "HEAD")
        return host_cmd.ChairHead(sha, _git_out(Path(path), "branch", "--show-current") or None) if sha else None
    heads = {Path(p).name: head(p) for p in _host_repos(profile)}
    return {name: h for name, h in heads.items() if h is not None}


def _host_sync(a: argparse.Namespace) -> int:
    profile, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    host = _host_ssh(a, profile, runs_dir)
    if host is None:
        print(f"unknown host: {a.name}")
        return 1
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.name), None)
    repos = host_cmd.recorded_repos(row)
    if not repos:
        print(f"paths: this machine's, since {a.name} has not beaten with its own; run `cox host beat {a.name}` there")
    code, lines = host_cmd.sync_host(host.ssh, repos or _host_repos(profile), _run_ssh, _chair_heads(profile))
    print("\n".join(lines))
    return code
