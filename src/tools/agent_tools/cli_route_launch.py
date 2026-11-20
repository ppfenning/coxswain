"""`cox route launch`: placing a run on a lane host and starting it."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from agent_tools.cli_board import _launch_rows_then_export, _merge_initiative_tickets, _merging_into
from agent_tools.cli_common import _now_iso, _profile_path, _read_text_or_none, _stored_work_items, workspace_git_on
from agent_tools.cli_lake import _lake_provider
from agent_tools.cli_land import (
    _initiative_target_branch,
    _leader_guard_or_refuse,
    _leader_pid_alive,
    _leader_read_or_refuse,
    _phase_land_at,
)
from agent_tools.cli_land_steps import _taken_run_names
from agent_tools.cli_lazy import (
    base_branch,
    bounded_run,
    chair,
    chair_capacity,
    chair_exec,
    chair_read_lease,
    host_cmd,
    host_load,
    install_root,
    intake_queue,
    lane_hosts,
    lane_python,
    lane_toolchain,
    launch_place,
    queue_export,
    queue_rows,
    remote_argv,
    remote_doctor,
    remote_lane,
    remote_launch,
    repo_path,
    route,
    run_store,
    work_state,
)
from agent_tools.cli_route import _initiative_path, _refuse_if_already_running
from agent_tools.cli_route_sync import _harness_ready_or_refuse, _lane_model, _local_max_in_flight
from agent_tools.cli_runs import _leader_heartbeat_minutes, _waiting_lines
from agent_tools.cli_runs_fetch import _holder_label, _lane_host_candidates, _profile_lane_hosts, _remote_edge
from agent_tools.cli_stats import _resolve_profile_or_refuse
from agent_tools.cli_usage import _usage_assessment


def _recorded_chair_model(runs_dir: Path) -> str:
    """The `model` the chair record names, else "". Silent on an unreadable record: `_leader_guard_or_refuse` reports that once."""
    try:
        record = chair.read(runs_dir)
    except (OSError, json.JSONDecodeError):
        return ""
    model = record.get("model") if isinstance(record, dict) else None
    return model if isinstance(model, str) else ""


def _leader_launched_by(record: dict | None, pid_alive_: bool, now: datetime.datetime, host: str, heartbeat_minutes: int) -> str | None:
    """The label to stamp on a run launched right now: the lock's own holder when it
    reads live, `None` when it reads stale, crashed, or none — never a guess."""
    if chair.liveness(record, pid_alive_, now, host, heartbeat_minutes) != "live":
        return None
    return record.get("session")


_LAUNCH_SETTLE_S = 2.0  # how long a fresh launch is watched for an immediate death before it is reported as started


# The line the harness prints once its run holds the lease (src/graphs/harness/cli_launch.py); seeing it ends the wait early.
_LAUNCH_STARTED_MARKER = "graph started: "
_IDEA_FILE_SUFFIXES = frozenset({".md", ".markdown", ".txt"})


def _idea_is_text(value: str, exists: bool) -> bool:
    """True for prose passed where `--idea` wants a file path: a value that is not a file and spans lines, or
    contains whitespace without looking like a path (a leading `/`, `~`, `./`, `../` or an idea-file suffix)."""
    looks_like_path = value.startswith(("/", "~", "./", "../")) or Path(value).suffix.lower() in _IDEA_FILE_SUFFIXES
    return not exists and ("\n" in value or (any(ch.isspace() for ch in value) and not looks_like_path))


def _last_line(text: str) -> str:
    """The last non-blank line of `text`, or "" when there is none."""
    return next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")


def _wait_for_exit(
    proc: subprocess.Popen, started: Callable[[], bool], timeout_s: float, poll_s: float = 0.05,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> int | None:
    """Edge: the child's exit code if it ends before `started()` turns true or `timeout_s` runs out, else None."""
    deadline = clock() + timeout_s
    code = proc.poll()
    while code is None and not started() and clock() < deadline:
        sleep(poll_s)
        code = proc.poll()
    return code


def _lock_snapshot(runs_dir: Path) -> dict | None:
    """The loop lock record as it stands, or None when there is none or it cannot be read."""
    try:
        return chair.read(runs_dir)
    except (OSError, json.JSONDecodeError):
        return None


def _decompose_repo_refusal(a: argparse.Namespace) -> str | None:
    """Edge. The refusal line for a decompose launch whose readable intake has no `repo:`; None otherwise.

    An idea that is text or a missing file is left to the launch, which refuses it in its own words."""
    if a.graph != "decompose" or _idea_is_text(a.idea, Path(a.idea).exists()):
        return None
    idea_path = Path(a.idea)
    text = _read_text_or_none(idea_path)
    return None if text is None else route.missing_repo_refusal(idea_path.stem, route.parse_frontmatter(text)[0])


def lane_capacity_env(env: Mapping[str, str], capacity: int) -> dict[str, str]:
    """A copy of `env` carrying `COX_LANE_CAPACITY=<capacity>`; a value already set, even empty, is kept."""
    return {"COX_LANE_CAPACITY": str(capacity), **env}


def _route_launch(a: argparse.Namespace) -> int:
    phases = list(dict.fromkeys(getattr(a, "phase", None) or []))
    if phases and a.graph != "epic":
        print("routing: --phase applies to the epic graph only")
        return 2
    if len(phases) > 1 and getattr(a, "run_id", None) is not None:
        print("routing: --run-id names one run; it cannot go with more than one --phase")
        return 2
    if len(phases) > 1:
        # One run per phase, each placing its own host from live load, so a later phase sees the earlier launches.
        for phase in phases:
            code = _route_launch(argparse.Namespace(**{**vars(a), "phase": [phase]}))
            if code:
                return code
        return 0
    phase = phases[0] if phases else None
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    # Before the host is placed, any claim or lease is taken, a run id is chosen or the stub is written.
    repo_refusal = _decompose_repo_refusal(a)
    if repo_refusal is not None:
        print(repo_refusal)
        return 2
    host, host_rc = _lane_host_or_refuse(a, profile)
    if host_rc is not None:
        return host_rc
    harness_dir = profile.get("harness_dir", "")
    venv_rc = _harness_ready_or_refuse(harness_dir)
    if venv_rc is not None:
        return venv_rc
    runs_dir = Path(profile["workspace_dir"]).expanduser() / "runs"
    launch_dir = _initiative_path(runs_dir, a.initiative) if a.graph in ("epic", "rescue") else None
    stub_written = None  # the initiative.md a decompose launch created, named in the report if the launch then dies
    via_rows = False  # a decompose that runs to completion here and writes its plan as store rows
    rows_when_detached = False  # the same decompose asked for with --detach: it is run in the background, rows and all
    if a.graph == "epic":
        initiative_id = launch_dir.name
        launch_land_at = _phase_land_at({}, profile, runs_dir.parent / "work", initiative_id)  # initiative.md, else cartridge, else ticket
        mode = work_state.work_state_mode(_lake_provider(a)[0])
        if mode == "store":
            # The chair is the machine the chair lease names; a lane host reads rows but never exports, since only the chair commits.
            lease = chair_read_lease.read_lease(runs_dir, datetime.datetime.now(datetime.UTC))
            is_chair = route.is_chair_machine(lease["host"], socket.gethostname())
            rows_code, rows_lines = _launch_rows_then_export(runs_dir, initiative_id, a.repo, is_chair, land_at=launch_land_at)
            for line in rows_lines:
                print(line)
            if rows_code:
                return rows_code
        # The merge result, not a reread: a refused merge never wrote its files.
        with _merging_into(runs_dir):
            merged = _merge_initiative_tickets(launch_dir)
        known_phases = sorted({str(item["phase"]) for item in merged})
        if phase is not None and phase not in known_phases:
            print(f"routing: {initiative_id} has no phase {phase}; phases: {', '.join(known_phases) or 'none'}")
            return 2
        # Only --include-blocked lifts this guard; --force never does.
        held = route.launch_blockers(merged)
        if held and not a.include_blocked:
            print(f"routing: {initiative_id} has a ready task behind blocked {', '.join(held)}; pass --include-blocked to launch anyway")
            return 2
        if held:
            print(f"override: launching {initiative_id} despite blocked {', '.join(held)} (--include-blocked)")
        for line in _waiting_lines(runs_dir, initiative_id, _stored_work_items(runs_dir.parent, mode), launch_land_at):
            print(line)
    # Read before the guard: a claim replaces the record with one that has no `model` key.
    chair_model = _recorded_chair_model(runs_dir)
    lock_before = _lock_snapshot(runs_dir)
    guard_rc = _leader_guard_or_refuse(runs_dir, _holder_label(a), a.force, claim=not a.no_claim, explicit_label=getattr(a, "label", None))
    if guard_rc is not None:
        return guard_rc
    usage_code, usage_lines = route.launch_gate(
        _usage_assessment(
            runs_dir, profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd"), profile.get("weekly_reset"),
            profile.get("monthly_budget_usd"), profile.get("budget_timezone"),
        ), a.force
    )
    for line in usage_lines:
        print(line)
    if usage_code is not None:
        return usage_code
    overlaid_provider_profile = None
    if a.tier_ceiling is not None or a.effort_ceiling is not None:
        provider_path = Path(profile["provider_profile"]).expanduser()
        provider_text = _read_text_or_none(provider_path)
        try:
            parsed = yaml.safe_load(provider_text) if provider_text is not None else None
        except yaml.YAMLError as exc:
            print(f"routing: provider profile at {provider_path} is not valid YAML: {exc}")
            return 2
        if not isinstance(parsed, dict):
            print(f"routing: no provider profile to apply a ceiling to at {provider_path}")
            return 2
        overlaid_provider_profile = route.overlay(parsed, a.tier_ceiling, a.effort_ceiling)
        if isinstance(overlaid_provider_profile, str):
            print(overlaid_provider_profile)
            return 2
    runs_dir.mkdir(parents=True, exist_ok=True)

    if a.graph in ("epic", "rescue"):
        initiative_dir = launch_dir
        initiative_md = initiative_dir / "initiative.md"
        text = _read_text_or_none(initiative_md)
        if text is None:
            print(f"routing: no initiative.md at {initiative_md}")
            return 2
        fields, _ = route.parse_frontmatter(text)
        repo = a.repo or fields.get("repo")
        if not repo:
            print(f"routing: no --repo given and no repo: in {initiative_md}")
            return 2
        home = os.path.expanduser("~")
        local_repo = repo_path.expand(repo, home)
        try:
            status = bounded_run.completed(
                ["git", "status", "--porcelain"], cwd=local_repo, capture_output=True, text=True, check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"routing: cannot read git status for {repo}: {exc}")
            return 2
        if status.stdout.strip():
            print(f"routing: {repo} has uncommitted changes:\n{status.stdout}")
            return 2
        prefix = initiative_dir.name if phase is None else route.phase_run_prefix(initiative_dir.name, phase)
        already = _refuse_if_already_running(runs_dir, prefix)
        if already is not None:
            print(already)
            return 2
        if getattr(a, "run_id", None) is None:
            run_id = route.next_run_id(_taken_run_names(runs_dir), prefix)
        else:
            # The background rerun of a detached decompose is handed its run id by the launch that named it and
            # already opened its log, so that one id is its own and not "taken".
            own = os.environ.get(_OWN_RUN_ID_ENV) == a.run_id
            taken = None if own else remote_lane.refuse_taken_run_id(a.run_id, set(_taken_run_names(runs_dir)))
            if taken is not None:
                print(taken)
                return 2
            run_id = a.run_id
        needs = {"initiative": str(initiative_dir), "repo": local_repo if host is None else repo}
        if a.graph == "rescue":
            needs["task"] = a.task
        elif a.fix_attempts is not None:
            needs["fix_attempts"] = a.fix_attempts
        if phase is not None:
            needs["phase"] = phase
        if host is None:
            # A remote lane runs the host's own `cox route launch` on the synced repo, which resolves the repo's `base:`
            # setting itself through this same branch; only an explicit `--base` is lost there, so `_dropped_ceilings` refuses it.
            base_flag = getattr(a, "base", None)
            if not base_flag:
                base_flag, refusal = _initiative_target_branch(profile, Path(local_repo), runs_dir.parent, initiative_dir.name)
                if refusal:
                    print(f"routing: {refusal}")
                    return 2
            try:
                base = base_branch.base_ref(str(local_repo), base_flag)
            except base_branch.BaseError as exc:
                print(f"routing: {exc}")
                return 2
            # A repo with no origin gets a bare name back. Pass it only when somebody chose it (`--base`, a target branch or `base:`),
            # since the default `main` would hide a repo whose local branch is `master` from the harness's own fallback.
            if base.startswith("origin/") or base_flag or base_branch.configured_base(str(local_repo)):
                needs["base"] = base
        env_repo = needs["repo"]
    elif a.graph == "decompose":
        if _idea_is_text(a.idea, Path(a.idea).exists()):
            print("routing: --idea takes a file path; got text (write it to a file or use route file --intake)")
            return 2
        idea_path = Path(a.idea)
        if not idea_path.exists():
            print(f"routing: no idea file at {idea_path}")
            return 2
        workspace_dir = Path(profile["workspace_dir"]).expanduser()
        initiative_dir = workspace_dir / "work" / a.initiative_id
        initiative_md = initiative_dir / "initiative.md"
        idea_fields, idea_body = route.parse_frontmatter(idea_path.read_text(encoding="utf-8"))
        intake = os.path.relpath(idea_path, workspace_dir)
        store_mode = work_state.work_state_mode(_lake_provider(a)[0])
        intake_row = (
            route.select_intake_row(run_store.read_queue(runs_dir, kind="intake"), idea_path.stem)
            if store_mode == "store" and not a.dry_run else None
        )
        if intake_row is not None and intake_queue.stage(intake_row.get("extra") or {}) == "draft":
            print(f"routing: intake {idea_path.stem} is a draft; ready it with `cox route ready {idea_path.stem}`")
            return 2
        via_rows = _decompose_via_rows(store_mode, a.dry_run, host, intake_row, detach=bool(getattr(a, "detach", False)))
        # Decided here, once: the stub the chair writes below carries it to a hosted or detached run, which reads it back.
        stub_text = _read_text_or_none(initiative_md)
        lane_model = queue_rows.launch_lane_model(
            route.parse_frontmatter(stub_text)[0] if stub_text is not None else None, _lane_model(profile),
        )
        rows_when_detached = not via_rows and _decompose_via_rows(store_mode, a.dry_run, host, intake_row)
        if via_rows:
            pass  # the plan lands as rows after the run; the chair's export writes the files
        elif initiative_md.exists() and any(initiative_dir.glob("*/*.md")):
            print("routing: initiative.md kept: tickets exist")
        elif initiative_md.exists():
            fields, body = route.parse_frontmatter(initiative_md.read_text(encoding="utf-8"))
            fresh = idea_path.stat().st_mtime <= initiative_md.stat().st_mtime and idea_body == body
            if fresh:
                print(f"routing: {initiative_md} already exists, left untouched")
            elif a.dry_run:
                print(f"dry-run: would write {initiative_md}")
            else:
                content = route.initiative_text(
                    fields.get("id", a.initiative_id), fields.get("title", ""),
                    fields.get("repo", ""), fields.get("intake", intake), idea_body, lane_model,
                )
                initiative_md.write_text(content, encoding="utf-8")
                print(f"routing: initiative.md refreshed from {idea_path}")
        else:
            initiative_content = route.initiative_text(
                a.initiative_id, idea_fields.get("title", ""), idea_fields.get("repo", ""), intake, idea_body, lane_model,
            )
            if a.dry_run:
                print(f"dry-run: would write {initiative_md}")
            else:
                initiative_md.parent.mkdir(parents=True, exist_ok=True)
                initiative_md.write_text(initiative_content, encoding="utf-8")
                queue_export.record_written_paths(workspace_dir, [initiative_md])
                stub_written = initiative_md
        # A remote lane's `decompose_launch_argv` passes the run id the chair recorded; without the flag every one failed.
        if getattr(a, "run_id", None) is None:
            run_id = route.next_run_id(_taken_run_names(runs_dir), a.initiative_id)
        else:
            # The background rerun of a detached decompose is handed its run id by the launch that named it and
            # already opened its log, so that one id is its own and not "taken".
            own = os.environ.get(_OWN_RUN_ID_ENV) == a.run_id
            taken = None if own else remote_lane.refuse_taken_run_id(a.run_id, set(_taken_run_names(runs_dir)))
            if taken is not None:
                print(taken)
                return 2
            run_id = a.run_id
        needs = {"idea": a.idea, "initiative_id": a.initiative_id}
        if getattr(a, "task_ids", None):
            needs["task_ids"] = a.task_ids
        # Without the intake's repo the decompose seats could read only the workspace and planned blind.
        idea_repo = idea_fields.get("repo")
        if isinstance(idea_repo, str) and idea_repo:
            needs["repo"] = repo_path.expand(idea_repo, os.path.expanduser("~")) if host is None else idea_repo
        env_repo = needs.get("repo", "")
    else:  # cos
        already = _refuse_if_already_running(runs_dir, "cos")
        if already is not None:
            print(already)
            return 2
        run_id = route.next_run_id(_taken_run_names(runs_dir), "cos")
        needs = {}
        env_repo = ""

    launch_profile = profile
    overlaid_path = None
    if overlaid_provider_profile is not None:
        overlaid_path = runs_dir / f"{run_id}.provider-profile.yaml"
        launch_profile = {**profile, "provider_profile": str(overlaid_path)}
    harness_python = str(install_root.harness_python(harness_dir)) if harness_dir else None
    result_path = runs_dir / f"{run_id}.result.json"
    argv = route.harness_argv(
        launch_profile, a.graph, run_id, python=harness_python, result_out=str(result_path) if via_rows else None, **needs,
    )
    log_path = runs_dir / f"{run_id}.log"
    pid_path = runs_dir / f"{run_id}.pid"
    trace_dir = runs_dir / f"{run_id}-trace"
    child_env_args = {
        "harness_bin": str(Path(harness_python).parent) if harness_python else "", "repo": env_repo,
        "trace_dir": str(trace_dir), "chair_model": chair_model,
    }

    # The lane host syncs the git checkout that holds the harness: the coxswain checkout once migrated (harness_dir
    # is its src/graphs, not a checkout root), else the harness checkout itself. The host's own `cox route launch`
    # then picks its interpreter with the same install_root rule.
    launch_harness = str(install_root.checkout_root(harness_dir) or Path(harness_dir).expanduser()) if harness_dir else None
    if host is not None and a.dry_run:
        checkouts = _host_checkouts(run_store.hosts(runs_dir), host.name)
        # The sync steps are listed only for a host with recorded checkouts: test_cli_launch_on pins the bare rsync + ssh plan otherwise.
        plan = remote_launch.launch_plan(
            host, a.initiative_id if a.graph == "decompose" else launch_dir.name, run_id, _holder_label(a), None,
            (env_repo or None) if checkouts else None,
            launch_harness if checkouts else None, home=os.path.expanduser("~"), host_checkouts=checkouts,
            graph="decompose" if a.graph == "decompose" else "epic", intake=intake if a.graph == "decompose" else None,
            cartridges=_workspace_cartridges(runs_dir), phase=phase,
        )
        for remote_argv in plan:
            print(f"dry-run: {shlex.join(remote_argv)}")
        print(f"host {host.name}")
        return 0

    if a.dry_run:
        print(f"dry-run: {' '.join(argv)}")
        print(f"pid {pid_path}")
        print(f"log {log_path}")
        print(f"trace {trace_dir}")
        print(f"model {chair_model or 'none'}")
        return 0

    if host is not None:
        # A placed host was probed by `_place_or_refuse`; only a host the caller named with --on is probed here.
        pinned = bool(env_repo) and getattr(a, "on", None) is not None
        return _route_launch_on_host(
            a, host, runs_dir, run_id, env_repo or None, launch_harness, home=os.path.expanduser("~"),
            tools=_repo_check_tools(env_repo).found if pinned else (),
            graph="decompose" if a.graph == "decompose" else "epic", intake=intake if a.graph == "decompose" else None,
            phase=phase,
        )

    lane = _local_lane_python(socket.gethostname(), os.environ.get("PATH", ""))
    if isinstance(lane, lane_python.LaneRefusal):
        print(f"routing: {lane.message}")
        _print_held_claim_hint(a, runs_dir, lock_before)
        return 2
    local_tools = _repo_check_tools(env_repo).found if env_repo else ()
    local_refusal = _toolchain_refusal(
        socket.gethostname(), None, local_tools, env={**os.environ, "PATH": lane.path}, repo=env_repo,
    )
    if local_refusal is not None:
        print(f"routing: {local_refusal}")
        _print_held_claim_hint(a, runs_dir, lock_before)
        return 2
    # `lane.path` puts the lane interpreter's folder first, so child_env's prefixes (spec §4: the repository venv
    # bin, then the harness bin) land ahead of it and the interpreter's folder precedes the caller's own entries.
    env = route.child_env({**os.environ, "PATH": lane.path}, **child_env_args)

    if overlaid_path is not None:
        overlaid_path.write_text(yaml.safe_dump(overlaid_provider_profile, sort_keys=True), encoding="utf-8")
        (runs_dir / f"{run_id}.ceiling.json").write_text(
            json.dumps({
                "requested": {"tier": a.tier_ceiling, "effort": a.effort_ceiling},
                "applied": {"tier": a.tier_ceiling, "effort": a.effort_ceiling},
                "profile": str(overlaid_path),
            }, indent=2),
            encoding="utf-8",
        )

    # A decompose starts in the work root, the directory its `--workdir` names. Launched over ssh, this process stands
    # in the login directory, and a store-relative `work/...` path resolved there once put every task file under
    # `~/work/` while the settle step read the work root and found none of them.
    start_in = runs_dir.parent if a.graph == "decompose" else None

    if via_rows:
        (runs_dir / f"{run_id}.launched.json").write_text(
            json.dumps({"initiative": a.initiative_id, "at": datetime.datetime.now(datetime.UTC).isoformat()}), encoding="utf-8",
        )
        exit_code, result = _run_decompose_for_result(argv, env, log_path, result_path, pid_path, cwd=start_in)
        if exit_code or result is None:
            # The harness writes the result before its own failure checks, so a failed run can leave a valid file: no rows then.
            print(f"routing: decompose exited {exit_code}; no rows written" if exit_code else f"routing: decompose wrote no readable result at {result_path}")
            print(f"log {log_path}")
            if not a.no_claim and _lock_snapshot(runs_dir) != lock_before:
                print("routing: the loop claim this launch took is still held with no run behind it; release it with `cox route release`")
            return 1
        plan = route.plan_from_result(
            result, a.initiative_id, idea_fields.get("title", ""), idea_body, repo=idea_fields["repo"],
        )
        rows_code, rows_lines = route.decompose_into_rows(runs_dir, idea_path.stem, lambda _intake: plan, lane_model)
        for line in rows_lines:
            print(line)
        return rows_code

    background = detached_rows_argv(sys.argv, run_id) if rows_when_detached else None
    if background is not None:
        # A detached decompose writes its plan as store rows too. This command runs again in the background without
        # --detach, to completion: the store gets the rows from whichever machine ran it, and no ticket file has to be
        # carried back to the chair and loaded.
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                background, env={**os.environ, _OWN_RUN_ID_ENV: run_id}, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, cwd=start_in,
            )
        pid_path.write_text(str(proc.pid))
        print(f"run {run_id}")
        print(f"pid {pid_path}")
        print(f"log {log_path}")
        return 0

    launch_env = (
        lane_capacity_env(env, _local_max_in_flight(runs_dir, dict(profile), socket.gethostname()))
        if a.graph == "epic" else env
    )
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            argv, env=launch_env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=start_in,
        )
    exit_code = _wait_for_exit(
        proc, lambda: _LAUNCH_STARTED_MARKER in (_read_text_or_none(log_path) or ""), _LAUNCH_SETTLE_S,
    )
    if exit_code:
        print(f"routing: launch failed: {_last_line(_read_text_or_none(log_path) or '') or f'exit {exit_code}'}")
        print(f"log {log_path}")
        # Per-run files written for a run that never began; the log stays as the evidence.
        for leftover in (runs_dir / f"{run_id}.provider-profile.yaml", runs_dir / f"{run_id}.ceiling.json"):
            leftover.unlink(missing_ok=True)
        # The initiative stub stays: queue_export has recorded it as written, so deleting it would leave a dangling record.
        if stub_written is not None:
            print(f"routing: {stub_written} was written before the launch and is left in place")
        if not a.no_claim and _lock_snapshot(runs_dir) != lock_before:
            print("routing: the loop claim this launch took is still held with no run behind it; release it with `cox route release`")
        return 1
    pid_path.write_text(str(proc.pid))
    now = datetime.datetime.now(datetime.UTC)
    lock_record, lock_refuse_rc = _leader_read_or_refuse(runs_dir)
    launched_by = None if lock_refuse_rc is not None else _leader_launched_by(lock_record, _leader_pid_alive(lock_record), now, socket.gethostname(), _leader_heartbeat_minutes())
    launched_payload = {"launched_by": launched_by} if launched_by is not None else {}
    # `route remove` reads this to tell which live runs are on the initiative it drops; null is a run on none (cos).
    initiative = Path(needs["initiative"]).name if "initiative" in needs else needs.get("initiative_id")
    (runs_dir / f"{run_id}.launched.json").write_text(
        json.dumps({**launched_payload, "initiative": initiative, "at": now.isoformat()}), encoding="utf-8",
    )
    print(f"run {run_id}")
    print(f"pid {pid_path}")
    print(f"log {log_path}")
    return 0


_OWN_RUN_ID_ENV = "COXSWAIN_OWN_RUN_ID"  # set for the background rerun of a detached decompose: the run id it was given


def detached_rows_argv(argv: Sequence[str], run_id: str) -> list[str] | None:
    """Pure. This `cox route launch decompose --detach ...` command line as the one to run in the background: the same
    arguments without `--detach`, with the run id pinned so both processes name one run. None when `argv` is not such
    a command line (the handler was called some other way), so the caller falls back to launching the harness itself."""
    if "--detach" not in argv or "decompose" not in argv:
        return None
    kept = [arg for arg in argv if arg != "--detach"]
    if "--run-id" in kept:
        at = kept.index("--run-id")
        kept[at + 1:at + 2] = [run_id]
    elif not any(arg.startswith("--run-id=") for arg in kept):
        kept += ["--run-id", run_id]
    return kept


def _decompose_via_rows(mode: str, dry_run: bool, host: object, intake_row: Mapping | None, *, detach: bool = False) -> bool:
    """True when a decompose launch runs to completion here and writes rows: store mode, a real run, this machine, an intake row, not detached."""
    return mode == "store" and not dry_run and host is None and intake_row is not None and not detach


_DECOMPOSE_TIMEOUT_S = 3600


def _run_decompose_for_result(
    argv: list[str], env: Mapping[str, str], log_path: Path, result_path: Path, pid_path: Path, *, cwd: Path | None = None,
) -> tuple[int, dict | None]:
    """Edge: run the decompose to completion, its pid in `pid_path` while it runs and its output in the log.

    `cwd` is the directory the run starts in, the work root; None keeps this process's own.
    Returns `(exit code, result)`; the result is the `--result-out` JSON, None when missing, invalid or not an object.
    A run past the timeout is killed and reports exit 124.
    """
    result_path.unlink(missing_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=cwd,
        )
        pid_path.write_text(str(proc.pid))
        try:
            code = proc.wait(timeout=_DECOMPOSE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            code = 124
    pid_path.unlink(missing_ok=True)
    text = _read_text_or_none(result_path)
    try:
        parsed = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        return code, None
    return code, parsed if isinstance(parsed, dict) else None


def _shadowed_note(profile_text: str, table_names: Sequence[str]) -> str | None:
    """One line naming the profile lane hosts the table overrides, or None when the table is empty or overrides none."""
    parsed = _profile_lane_hosts(profile_text)
    names = [] if not table_names or isinstance(parsed, lane_hosts.LaneHostError) else host_cmd.shadowed(parsed, table_names)
    return f"profile lane_hosts not in the hosts table are not lane hosts while it has rows: {', '.join(names)}; add them with `cox host add`" if names else None


def _dropped_ceilings(a: argparse.Namespace) -> list[str]:
    """The flags the remote command does not carry; a ceiling dropped silently would run uncapped, a base dropped would cut from another branch."""
    return [flag for flag, value in (
        ("--base", getattr(a, "base", None)),
        ("--tier-ceiling", getattr(a, "tier_ceiling", None)),
        ("--effort-ceiling", getattr(a, "effort_ceiling", None)),
        ("--fix-attempts", getattr(a, "fix_attempts", None)),
    ) if value is not None]


def _repo_check_tools(repo: str) -> lane_toolchain.CheckTools:
    """Edge. The tools the repo's `.agent-checks` run; `repo` is read here, as recorded, so a tilde or another machine's home is mapped."""
    text = _read_text_or_none(Path(repo_path.expand(repo, os.path.expanduser("~"))) / ".agent-checks")
    return lane_toolchain.parse_check_tools(lane_toolchain.check_lines(text or ""))


def _running_python() -> Path:
    """Edge. The interpreter running this process."""
    return Path(sys.executable)


def _install_checkout() -> Path | None:
    """Edge. The coxswain checkout this module was installed from, whose venv the install step built; None when there is none."""
    return install_root.checkout_root(Path(__file__).parent)


def _local_lane_python(host: str, caller_path: str) -> lane_python.LanePython | lane_python.LaneRefusal:
    """Edge. The python a local lane runs: the running interpreter, else the install's venv python, whichever exists."""
    running = _running_python()
    root = _install_checkout()
    existing = {c for c in lane_python.candidates(running, root) if c.is_file()}
    return lane_python.resolve_lane_python(running, root, caller_path, existing, host=host)


def _print_held_claim_hint(a: argparse.Namespace, runs_dir: Path, lock_before) -> None:
    """Edge. After a refused launch, says so when the loop claim this launch took is still held."""
    if not a.no_claim and _lock_snapshot(runs_dir) != lock_before:
        print("routing: the loop claim this launch took is still held with no run behind it; release it with `cox route release`")


def _lane_repo_dir(repo: str | None, remote: bool) -> str:
    """Edge. The repo directory as the toolchain lookup sees it: absolute here, home-relative for a remote login shell."""
    if not repo:
        return ""
    home = os.path.expanduser("~")
    return repo_path.host_form(repo, home) if remote else repo_path.expand(repo, home)


def _toolchain_refusal(
    name: str, ssh: str | None, tools: Sequence[str], env: Mapping[str, str] | None = None, *, repo: str | None = None,
) -> str | None:
    """Edge. The first tool `name`'s lane PATH and `repo`'s venv both lack, as a refusal line. `ssh` None looks in the
    launching environment, under `env` when given; a remote lookup never takes `env`."""
    repo_dir = _lane_repo_dir(repo, ssh is not None)
    argv = (
        lane_toolchain.lookup_argv(tools, repo_dir) if ssh is None
        else remote_argv.ssh_argv(ssh, lane_toolchain.lane_argv(tools, repo_dir))
    )
    probe = _host_env_probe if env is None or ssh is not None else lambda a: _host_env_probe(a, env)
    lines = lane_toolchain.refusals(name, tools, argv, probe, repo_dir)
    return lines[0] if lines else None


def _epic_repo(a: argparse.Namespace, profile: Mapping) -> str | None:
    """Edge. The repo an epic launch builds: `--repo`, else the initiative's `repo:` field."""
    token = getattr(a, "initiative", None)
    if not token:
        return None
    runs_dir = Path(profile.get("workspace_dir", "")).expanduser() / "runs"
    text = _read_text_or_none(_initiative_path(runs_dir, token) / "initiative.md")
    fields, _ = route.parse_frontmatter(text) if text is not None else ({}, "")
    return getattr(a, "repo", None) or fields.get("repo") or None


def _intake_repo(a: argparse.Namespace) -> str | None:
    """Edge. The `repo:` field of the intake a decompose launch reads, as recorded; None when there is no readable idea file."""
    idea = getattr(a, "idea", None)
    if not idea:
        return None
    try:
        text = _read_text_or_none(Path(idea))
    except ValueError:
        return None
    fields, _ = route.parse_frontmatter(text) if text is not None else ({}, "")
    repo = fields.get("repo")
    return repo if isinstance(repo, str) and repo else None


def _place_or_refuse(
    a: argparse.Namespace, profile: Mapping, forced_local: bool,
) -> tuple[lane_hosts.LaneHost | None, int | None]:
    """Edge. Place the lane by the hosts table and print `placed on <host> (<in use>/<cap>)`; a remote pick comes back
    as its LaneHost. Epic and decompose go remote, each probed for the toolchain of its repo (an epic's initiative or an
    intake's `repo:`); the other graphs have no remote launch, so they place on this machine. A forced local launch
    places on this machine, refused at capacity without `--force`. Under `policy.dispatch.local_lanes: decompose` an
    epic is not placed locally without an explicit local flag: with no lane host free it is refused, not run here."""
    graph = getattr(a, "graph", "epic")
    if not forced_local and graph not in ("epic", "decompose"):
        return None, None
    runs_dir = Path(profile.get("workspace_dir", "")).expanduser() / "runs"
    rows = run_store.hosts(runs_dir)
    if not rows and not forced_local:
        return None, None
    local = socket.gethostname()
    live = _live_by_host_name(run_store.live_lanes(runs_dir, _now_iso()), local)
    cap = _local_max_in_flight(runs_dir, dict(profile), local)
    ceiling = host_load.resolve_load_ceiling((profile.get("chair") or {}).get("host_load_ceiling"))
    force = bool(getattr(a, "force", False))
    profile_text = _read_text_or_none(_profile_path(a)) or ""
    parsed = _profile_lane_hosts(profile_text)
    if forced_local:
        placed = launch_place.place_local(local, rows, live, force, cap)
    else:
        profile_hosts = () if isinstance(parsed, lane_hosts.LaneHostError) else parsed
        candidates = _lane_host_candidates(profile_text, rows, local)
        epic_repo = _intake_repo(a) if graph == "decompose" else _epic_repo(a, profile)
        tools = _repo_check_tools(epic_repo).found if epic_repo else ()
        local_excluded = graph == "epic" and chair_capacity.chair_local_lanes(dict(profile)) == "decompose"

        def refused(name: str) -> str | None:
            host = None if isinstance(candidates, lane_hosts.LaneHostError) else lane_hosts.find_lane_host(candidates, name)
            if name != local and host is None:
                return None  # the unknown host is refused below
            line = _toolchain_refusal(name, None if host is None else host.ssh, tools, repo=epic_repo)
            if line is not None:
                print(f"routing: {line}")
            return line

        placed = launch_place.place_checked(
            lambda barred: launch_place.place(
                rows, live, datetime.datetime.now(datetime.UTC), local, cap, profile_hosts, barred,
                local_excluded=local_excluded, load_ceiling=ceiling,
            ),
            refused,
        )
    if isinstance(placed, launch_place.Refusal):
        print("routing: no host has room; pass --force to launch on this machine anyway" if forced_local else "routing: no host has room")
        for line in launch_place.format_refusal(placed):
            print(line)
        return None, 2
    print(f"placed on {placed.host} ({placed.in_use}/{placed.capacity})")
    if placed.host == local:
        return None, None
    dropped = _dropped_ceilings(a)
    if dropped:
        print(f"routing: placed on {placed.host}, which does not carry {', '.join(dropped)}; launch without them or pass --local")
        return None, 2
    hosts = _lane_host_candidates(profile_text, rows, local)
    host = None if isinstance(hosts, lane_hosts.LaneHostError) else lane_hosts.find_lane_host(hosts, placed.host)
    if host is None:
        print(f"routing: unknown lane host: {placed.host}")
        return None, 2
    return host, None


def _lane_host_or_refuse(a: argparse.Namespace, profile: Mapping) -> tuple[lane_hosts.LaneHost | None, int | None]:
    """Exit code 2 after printing the refusal. No host means this machine.

    `--on local` or `--local` forces this machine, refused at capacity without `--force`. `--on NAME` names a table host.
    With neither, epic and decompose are placed by the hosts table; an empty table places nothing. Under
    `local_lanes: decompose` an epic is not placed locally without `--local` or `--on local`."""
    name = getattr(a, "on", None)
    if name == "local" or getattr(a, "local", False):
        return _place_or_refuse(a, profile, forced_local=True)
    if name is None:
        return _place_or_refuse(a, profile, forced_local=False)
    dropped = _dropped_ceilings(a)
    if dropped:
        print(f"routing: --on does not carry {', '.join(dropped)} to the host; launch without them")
        return None, 2
    rows = run_store.hosts(Path(profile.get("workspace_dir", "")).expanduser() / "runs")
    profile_text = _read_text_or_none(_profile_path(a)) or ""
    hosts = _lane_host_candidates(profile_text, rows)
    if isinstance(hosts, lane_hosts.LaneHostError):
        print(f"routing: {hosts.message}")
        return None, 2
    host = lane_hosts.find_lane_host(hosts, name)
    if host is None:
        print(f"routing: unknown lane host: {name}")
        print(f"configured: {', '.join(h.name for h in hosts) or 'none'}")
        note = _shadowed_note(profile_text, [str(r["name"]) for r in rows])
        if note:
            print(note)
        return None, 2
    if not host.workspace_dir:
        print(f"routing: host {name} has no workspace_dir: run `cox host beat {name}` on it, or list it under lane_hosts in the profile")
        return None, 2
    return host, None


def _host_auth_output(argv: list[str]) -> str:
    """Edge: stdout and stderr of `claude auth status` on the host; an ssh that cannot run gives its error text."""
    try:
        done = bounded_run.completed(argv, capture_output=True, text=True)
    except OSError as exc:
        return f"{argv[0]}: {exc}"
    return done.stdout + done.stderr


def _host_env_probe(argv: list[str], env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Edge: (exit code, text) of a `printenv` over ssh; text is stdout on 0, else stderr. An ssh that cannot run is (127, error).

    `env` is the probe's environment for a local lookup; None inherits this process's, as ssh probes do.
    Not `_host_auth_output`: ssh stderr (a host-key warning) is never the variable's value."""
    try:
        done = bounded_run.completed(argv, capture_output=True, text=True, env=env)
    except OSError as exc:
        return 127, f"{argv[0]}: {exc}"
    return done.returncode, done.stdout if done.returncode == 0 else done.stderr


def _pid_probe_ssh(argv: list[str]) -> tuple[int, str] | None:
    """Edge: (exit code, stdout) of a pid check run from `ssh_argv`; None when ssh cannot run or takes over 15s,
    so a probe that failed is left out of the facts rather than read as a dead pid.

    Probes run one per remote lane, in series, every tick. BatchMode makes a host that would prompt for a password
    fail at once instead of hanging, and ConnectTimeout bounds a host slow to accept, so a tick waits at most 15s a lane."""
    batch = [argv[0], "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *argv[1:]] if argv[:1] == ["ssh"] else argv
    try:
        done = subprocess.run(batch, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.returncode, done.stdout


def _lane_sync_ssh(argv: list[str]) -> tuple[int, str]:
    """Edge: (exit code, stdout and stderr) of one lane checkout sync call; ssh never prompts and no call outlasts 60s.

    The sync runs in series on every live tick before the gather, so a host that hangs must cost a bounded wait.
    A timeout or an ssh that cannot run is a nonzero code, which the sync reads as an unreachable host."""
    batch = [argv[0], "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *argv[1:]] if argv[:1] == ["ssh"] else argv
    try:
        done = subprocess.run(batch, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: no answer within 60s"
    except OSError as exc:
        return 127, f"{argv[0]}: {exc}"
    return done.returncode, done.stdout


def _host_checkouts(rows: Sequence[Mapping[str, object]], name: str) -> tuple[str, ...]:
    """The checkouts the named host's own beat recorded; () for a host with no row."""
    return next((tuple(host_cmd.recorded_repos(r)) for r in rows if r.get("name") == name), ())


def _workspace_cartridges(runs_dir: Path) -> str | None:
    """Edge. "cartridges" when the chair's workspace (`runs_dir.parent`, the rsync cwd of `_remote_edge`) holds one, else None."""
    return "cartridges" if (runs_dir.parent / "cartridges").is_dir() else None


def _routing_profile_or_empty(a: argparse.Namespace) -> dict:
    """Edge. The parsed routing profile; {} when it is missing or unreadable, which reads as every default."""
    text = _read_text_or_none(_profile_path(a))
    try:
        return route.parse_profile(text) if text else {}
    except route.ProfileError:
        return {}


def _published_cartridges(runs_dir: Path, profile: Mapping) -> str | None:
    """Edge. The sha of the chair's cartridges, published to the store, when lanes read them from there: the
    workspace is out of git, the store is shared, and the workspace holds a cartridges directory. None keeps the copy
    by rsync (`profile` is the routing profile). A publish that fails also returns None, after saying so: the launch then copies them as before."""
    from agent_tools import cartridge_snapshot, courier_bus

    folder = _workspace_cartridges(runs_dir)  # the same directory the rsync copies
    if workspace_git_on(profile) or folder is None or not courier_bus.uses_store(runs_dir.parent / "courier.jsonl"):
        return None
    try:
        return cartridge_snapshot.publish(runs_dir, runs_dir.parent / folder)
    except Exception as exc:
        print(f"cartridges: could not publish to the store, copying them instead: {type(exc).__name__}: {exc}")
        return None


def _workspace_probe(cwd: Path) -> Callable[[list[str]], tuple[int, str]]:
    """Edge. `remote_launch`'s probe (exit code, stdout then stderr) run from `cwd`: the cartridges check names its source relative to the workspace."""

    def probe(argv: list[str]) -> tuple[int, str]:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=300, cwd=cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, f"sync: {argv[0]}: {exc}"
        return done.returncode, done.stdout + done.stderr

    return probe


def _route_launch_on_host(
    a: argparse.Namespace, host: lane_hosts.LaneHost, runs_dir: Path, run_id: str, repo: str | None = None,
    harness_dir: str | None = None, *, home: str | None = None, tools: Sequence[str] = (),
    graph: str = "epic", intake: str | None = None, phase: str | None = None,
) -> int:
    """Copies the initiative to `host`, and the workspace's cartridges directory with it when there is one, and
    starts the lane there; writes only `<run>.remote.json`, and only on success.
    `tools` are probed on the host's lane PATH before the copy. A decompose names its initiative by `a.initiative_id`
    and copies the one `intake` file; its record carries `graph: decompose`."""
    run, locate = _remote_edge(runs_dir.parent)
    initiative = a.initiative_id if graph == "decompose" else _initiative_path(runs_dir, a.initiative).name
    launched_at = datetime.datetime.now(datetime.UTC).isoformat()
    provider, _ = _lake_provider(a)  # an unreadable profile is `{}`: runner unset, so the claude-code check as today
    # The one read of runner/auth_env/endpoint_env, shared with the chair's check_login. Move to the
    # `setup doctor --host` helper once remote-doctor-runner-check lands one; do not write a second read.
    runner, env_names = chair_exec._login_runner_and_env(dict(provider))
    if runner == "claude-code":
        preflight = remote_doctor.auth_status_verdict(_host_auth_output(remote_argv.ssh_argv(host.ssh, remote_argv.auth_status_argv())))
    else:
        preflight = remote_launch.env_preflight(
            env_names, {name: _host_env_probe(remote_argv.ssh_argv(host.ssh, remote_argv.env_check_argv(name))) for name in env_names}
        )
    result = remote_launch.launch_on_host(
        host, initiative, run_id, _holder_label(a), launched_at, run, locate, repo, harness_dir,
        preflight=preflight, home=home, host_checkouts=_host_checkouts(run_store.hosts(runs_dir), host.name), tools=tools,
        graph=graph, intake=intake, cartridges=_workspace_cartridges(runs_dir), probe=_workspace_probe(runs_dir.parent),
        profile=provider, phase=phase, cartridge_sha=_published_cartridges(runs_dir, _routing_profile_or_empty(a)),
    )
    if isinstance(result, remote_launch.LaunchError):
        print(f"routing: launch on {host.name} failed at {result.step}: {result.message}")
        return 2
    # `route remove` reads the initiative back from here, as it does from `.launched.json` for a local run.
    record = {**result, "initiative": initiative, **({"graph": "decompose"} if graph == "decompose" else {})}
    remote_lane.remote_record_path(runs_dir, run_id).write_text(json.dumps(record), encoding="utf-8")
    print(f"run {run_id}")
    print(f"host {host.name}")
    return 0


def _live_by_host(lanes: Sequence[run_store.Lane], local: str) -> dict[str, int]:
    """Live lanes per host name; a lane with no host, or on `local`, counts under the empty name."""
    names = [("" if lane.host in (None, local) else lane.host) for lane in lanes]
    return {name: names.count(name) for name in dict.fromkeys(names)}


def _live_by_host_name(lanes: Sequence[run_store.Lane], local: str) -> dict[str, int]:
    """Live lanes per hosts-table row name. `_live_by_host` files this machine's lanes under "", but `cox host beat` names
    this machine's row `local`, so the "" count is re-keyed to `local`. Every live lane counts: the pidfile filter
    `_dispatch_counts` applies exists only because it adds the pidfile runs back, and a host list adds nothing back."""
    return {(local if name == "" else name): n for name, n in _live_by_host(lanes, local).items()}
