"""`cox runs`: usage, trace, top, stop, move, recover, wait and the other run commands."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

from agent_tools.cli_common import _initiative_texts, _profile_path, _read_text_or_none, _work_items
from agent_tools.cli_lake import _lake_provider
from agent_tools.cli_land_facts import _land_record, _recover_branches
from agent_tools.cli_land_steps import _git_out
from agent_tools.cli_lazy import (
    base_branch,
    bounded_run,
    chair,
    chair_exec,
    chair_facts,
    chair_read_branch_merged,
    cleanup,
    epic,
    install_root,
    land,
    land_manual,
    lane_hosts,
    notify,
    queue_export,
    records,
    remote_lane,
    repo_path,
    route,
    run_store,
    runs_bar,
    runs_detail,
    runs_detail_screen,
    runs_herdr,
    runs_module,
    runs_move,
    runs_stop,
    runs_stranded,
    runs_top,
    runs_top_screen,
    runs_triage,
    stats_chair,
    stats_ingest,
    store_cli,
    store_url,
    towpath_launch,
    work_state,
)
from agent_tools.cli_runs_fetch import (
    _branch_exists,
    _fetch_hosts,
    _fetch_one,
    _holder_label,
    _initiative_of,
    _lane_host_candidates,
    _runs_dir_for_land,
)


def _runs_usage(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    header = None
    usage = run_store.usage(runs_dir, a.run_id)
    if usage is not None:
        s = records.usage_summary(usage)
    else:
        pid_text = _read_text_or_none(runs_dir / f"{a.run_id}.pid")
        pid = route.parse_pid(pid_text) if pid_text is not None else None
        live = epic.run_live(pid, runs_dir / f"{a.run_id}.pid")
        trace_dir = runs_dir / f"{a.run_id}-trace"
        traces = stats_ingest._read_traces(trace_dir, [])
        if not live and not traces:
            print(f"no usage record and no trace for {a.run_id} in {runs_dir}")
            return 2
        rows = stats_ingest.recovered_call_rows(a.run_id, traces)
        s = records.usage_summary({"run_id": a.run_id, "calls": rows})
        header = f"live (pid {pid}) — from the trace so far" if live else "not live — from the trace so far"
    if a.json:
        print(json.dumps({**s, "note": header} if header else s, indent=2)); return 0
    if header:
        print(header)
    print(f"{s['run_id']}: {s['calls']} calls, {s['turns']} turns, ${s['cost_usd']:.2f}, cache-read share {s['cache_read_share']}")
    print(records.format_table([{"role": k, **v} for k, v in s["by_role"].items()], ["role", "calls", "cost_usd", "turns"]))
    print(); print(records.format_table([{"model": k, **v} for k, v in s["by_model"].items()], ["model", "calls", "cost_usd", "turns"]))
    return 0


_SERIES_COLUMNS = ["run", "date", "cartridge_sha", "provider_profile", "calls", "turns", "cost_usd",
                   "cache_share", "completions", "quarantined", "review_rounds", "cost_per_completion",
                   "tier_ceiling", "effort_ceiling", "launched_by"]


def _runs_series(a: argparse.Namespace) -> int:
    d = Path(a.runs_dir)
    files = {f.name: f.read_text(encoding="utf-8")
             for pattern in ("*.usage.json", "*:*.json", "*.ceiling.json", "*.launched.json") for f in d.glob(pattern)}
    stored = {f"{run}.usage.json": json.dumps(usage) for run, usage in run_store.usages(d).items()}
    stored |= {f"{m['run_id']}.json": json.dumps(m)
               for ms in run_store.all_phase_manifests(d).values() for m in ms if m.get("run_id")}
    rows = records.series({**stored, **files})
    totals = records.series_totals(rows)
    if a.json:
        print(json.dumps({"rows": rows, "totals": totals}, indent=2))
    else:
        print(records.format_table(rows, _SERIES_COLUMNS))
        print(f"totals: {totals['runs']} runs, ${totals['cost_usd']:.2f}, {totals['completions']} completions, "
              f"{totals['quarantined']} quarantined, cost/completion {totals['cost_per_completion']}, "
              f"{totals['runs_landing_nothing']} landed nothing")
    if a.append:
        p = Path(a.append)
        new_lines = records.series_new_lines(p.read_text(encoding="utf-8") if p.exists() else "", rows)
        if new_lines:
            with p.open("a", encoding="utf-8") as fh:
                fh.write("".join(line + "\n" for line in new_lines))
    return 0


def _runs_events(a: argparse.Namespace) -> int:
    for ev in runs_module.events(Path(a.runs_dir), follow=a.follow):
        if a.json:
            print(json.dumps(dataclasses.asdict(ev)), flush=True)
        else:
            detail = " ".join(f"{k}={v}" for k, v in ev.detail.items())
            print(f"{ev.run} {ev.kind} {detail}".rstrip(), flush=True)
    return 0


def _run_slug(run_key: str) -> str:
    """The initiative slug of a run key: the key with one trailing `-<digits>` removed."""
    return re.sub(r"-\d+$", "", run_key)


def _with_short_ids(rows: Sequence[runs_top.Row], short_ids: Mapping[str, str]) -> list[runs_top.Row]:
    """Each row with `short_id` set from its slug; a slug with no entry keeps the row's own."""
    return [dataclasses.replace(r, short_id=short_ids.get(_run_slug(r.run), r.short_id)) for r in rows]


def _run_short_ids(run_ids: Sequence[str], short_ids: Mapping[str, str]) -> dict[str, str]:
    """Run id to short id, from a slug-keyed `short_ids`; a run whose slug has no entry is absent."""
    return {run_id: short_ids[_run_slug(run_id)] for run_id in run_ids if _run_slug(run_id) in short_ids}


def _runs_top(a: argparse.Namespace) -> int:
    """On two terminals with towpath installed, exec towpath; otherwise, and always with --once, print the table."""
    # The seams are passed at call time so a test can stub them; launch's own defaults bind at import.
    if not a.once and towpath_launch.launch(shutil.which, os.execv, sys.stdout, sys.stdin):
        return 0
    heartbeat_minutes = _leader_heartbeat_minutes()
    chair_state = runs_top_screen.chair_now(a.runs_dir, heartbeat_minutes)
    rows = _with_short_ids(runs_top_screen.rows_now(a.runs_dir, heartbeat_minutes), run_store.initiative_short_ids(a.runs_dir))
    print("\n".join(runs_top.render(rows, 120, chair_state)))
    return 0


def _bar_rows(runs_dir: str) -> list[dict]:
    """The rows `cox runs top` draws, local and remote, plus lanes whose lease lapsed unreleased, as dicts."""
    rows = [*runs_top_screen.rows_now(runs_dir, _leader_heartbeat_minutes()), *runs_top_screen.lost_rows(runs_dir)]
    return [dataclasses.asdict(r) for r in rows]


def _runs_bar(a: argparse.Namespace) -> int:
    rows = _bar_rows(a.runs_dir)
    print(json.dumps(runs_bar.bar(rows, runs_bar.attention(rows, _leader_heartbeat_minutes()))))
    return 0


def _stranded_count(runs_dir: str) -> int:
    """How many approved task records are not landed, the same set `cox runs stranded` lists. A record or
    workspace that cannot be read counts as none, so a bad file never stops `cox runs herdr`."""
    try:
        task_records, items = _stranded_inputs(Path(runs_dir).expanduser())
        return len(runs_stranded.stranded(task_records, items, os.path.isdir,
                                          lambda repo, branch: _branch_exists(Path(repo), branch)))
    except (OSError, ValueError, KeyError):
        return 0


def _runs_herdr(a: argparse.Namespace) -> int:
    if not a.once:
        runs_herdr.install_exit_handlers()
    return runs_herdr.run_loop(lambda: _bar_rows(a.runs_dir), lambda: _stranded_count(a.runs_dir), os.environ,
                               heartbeat_minutes=_leader_heartbeat_minutes(), once=a.once, interval=a.interval,
                               now=time.monotonic, sleep=time.sleep, seq_start=int(time.time()))


def _resolved_notify_policy(runs_dir: Path) -> dict:
    """`policy.notify` from `<runs_dir>/policy.notify.json` when present, else DEFAULT_POLICY, field by field."""
    default = notify.DEFAULT_POLICY
    text = _read_text_or_none(runs_dir / "policy.notify.json")
    if text is None:
        return default
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return default
    if not isinstance(raw, dict):
        return default
    return {"kinds": raw.get("kinds", default["kinds"]), "min_cost_usd": raw.get("min_cost_usd", default["min_cost_usd"])}


def _runs_notify(a: argparse.Namespace) -> int:
    policy = _resolved_notify_policy(Path(a.runs_dir))
    return notify.run_loop(a.runs_dir, once=a.once, interval=a.interval, policy=policy,
                            heartbeat_minutes=_leader_heartbeat_minutes(), replay=a.replay)


def _resolved_run_id(runs_dir: Path, token: str) -> str:
    """A run command's token, resolved: `token` may already be a short run id or the old run key, and
    `run_store.resolve_run_id` maps either to the same run id. Falls back to `token` unchanged when the
    store or the harness cannot resolve it, so an unresolvable token still reaches the command as before."""
    return run_store.resolve_run_id(runs_dir, token) or token


def _runs_detail(a: argparse.Namespace) -> int:
    d = runs_detail.detail(**runs_detail_screen.facts_for(a.runs_dir, _resolved_run_id(Path(a.runs_dir), a.run_id)))
    if a.json:
        print(json.dumps(dataclasses.asdict(d)))
    else:
        print("\n".join(runs_detail.render(d, 120)))
    return 0


def _stranded_inputs(runs_dir: Path) -> tuple[list[dict], list[dict]]:
    """Edge. The task records under `runs_dir` and the workspace's work items with their repo, as `runs_stranded` takes them."""
    task_records = []
    for path in sorted(runs_dir.glob("*/tasks/*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        record.setdefault("run", path.parent.parent.parent.name)
        record.setdefault("task", path.stem)
        record.setdefault("phase", path.parent.name)
        task_records.append(record)
    ws = runs_dir.parent
    initiative_texts = _initiative_texts(ws)
    items = [
        {**item, "repo": route.parse_frontmatter(initiative_texts.get(item["initiative"], ""))[0].get("repo")}
        for item in _work_items(ws)
    ]
    return task_records, items


def _runs_stranded(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"stranded: {reason}")
        return 2
    task_records, items = _stranded_inputs(runs_dir)
    rows = runs_stranded.stranded(task_records, items, os.path.isdir,
                                   lambda repo, branch: _branch_exists(Path(repo), branch), base_branch.base_name,
                                   land_manual.is_manual)
    missing = runs_stranded.missing_repos(task_records, items, os.path.isdir)

    def report_missing() -> None:
        if missing:
            print(f"skipped missing repo: {', '.join(missing)}", file=sys.stderr)

    if a.json:
        print(json.dumps(rows))
        report_missing()
        return 0
    if not rows:
        print("no stranded work")
        report_missing()
        return 0
    print(records.format_table(rows, ["run", "task", "phase", "branch", "remedy"]))
    report_missing()
    return 0


def _waiting_lines(runs_dir: Path, initiative: str, items: list, land_at: str = "task") -> list[str]:  # "task" is route.DEFAULT_LAND_AT; `route` loads lazily, so not named in a default
    """Edge. `route.waiting_lines` for `initiative` under `land_at`, with the remedy `cox runs stranded` prints for each
    task. A record or workspace that cannot be read leaves the remedy out; it never stops a launch."""
    try:
        task_records, stranded_items = _stranded_inputs(runs_dir)
        rows = runs_stranded.stranded(
            [r for r in task_records if r.get("initiative") in (None, initiative)], stranded_items, os.path.isdir,
            lambda repo, branch: _branch_exists(Path(repo), branch), base_branch.base_name, land_manual.is_manual)
    except (OSError, ValueError, KeyError):
        rows = []
    return route.waiting_lines(items, {row["task"]: row["remedy"] for row in rows if row["remedy"]}, initiative, land_at=land_at)


def _pick_work_item(paths: list[str], task: str, initiative: str | None) -> tuple[str | None, int]:
    """The one `<initiative>/<phase>/<task>.md` among work-relative `paths`, with the match count.
    `initiative` None considers every initiative. The path is None unless exactly one matches."""
    hits = [p for p in paths
            if p.count("/") == 2 and p.rsplit("/", 1)[1] == f"{task}.md" and (initiative is None or p.split("/")[0] == initiative)]
    return (hits[0] if len(hits) == 1 else None), len(hits)


def _runs_cause(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"cause: {reason}")
        return 2
    record, where, found = _land_record(runs_dir, a.run_id, a.task_id)
    if record is None:
        print(f"cause: expected one task record for {a.task_id} under {where}, found {found}")
        return 2
    work_root = runs_dir.parent / "work"
    own = chair_facts.run_initiative(a.run_id)
    initiative = record.get("initiative") or (own if (work_root / own).is_dir() else None)
    items = sorted(work_root.glob(f"{initiative or '*'}/{record['phase']}/{a.task_id}.md"))
    if not items:
        listing = [str(p.relative_to(work_root)) for p in sorted(work_root.glob("*/*/*.md"))]
        picked, count = _pick_work_item(listing, a.task_id, initiative)
        if picked is None:
            print(f"cause: expected one work item {a.task_id} in initiative {initiative or '(any)'}, found {count}")
            return 2
        items = [work_root / picked]
    if len(items) != 1:
        print(f"cause: expected one work item {a.task_id} in phase {record['phase']}, found {len(items)}")
        return 2
    updated = stats_chair.set_attempt_cause(items[0].read_text(encoding="utf-8"), a.run_id, a.cause, a.note)
    if updated is None:
        print(f"cause: {items[0]} has no attempts entry for run {a.run_id}")
        return 2
    items[0].write_text(updated, encoding="utf-8")
    print(f"{a.task_id}: attempt {a.run_id} cause = {a.cause}")
    return 0


def _runs_stop_run_remote(argv: list[str]) -> bool:
    """Edge: the one real subprocess call for a remote `runs stop`; ok only when it exits 0."""
    return bounded_run.completed(argv).returncode == 0


def _runs_stop_is_ended(runs_dir: Path) -> Callable[[str], bool]:
    """Closes over `runs_dir`. Reads the run's local pidfile when present, passing pid 0
    when it is not, so a remote run with no local pidfile still falls through to
    `epic.run_live`'s lease-first check."""
    def is_ended(run_id: str) -> bool:
        pidfile = runs_dir / f"{run_id}.pid"
        pid_text = _read_text_or_none(pidfile)
        pid = int(pid_text.strip()) if pid_text is not None else 0
        return not epic.run_live(pid, pidfile)
    return is_ended


def _runs_stop_hosts(a: argparse.Namespace) -> tuple[lane_hosts.LaneHost, ...]:
    """The hosts `route launch --on` picks from: the hosts table, else the profile's `lane_hosts`. Drained rows
    count, since a run launched before its host was drained still has to be stopped (as `_fetch_hosts`). A missing
    profile or an unreadable table just leaves a local-only stop with nothing to look up."""
    rows = run_store.hosts(Path(a.runs_dir))
    found = _lane_host_candidates(_read_text_or_none(_profile_path(a)) or "", [{**row, "state": "active"} for row in rows])
    return () if isinstance(found, lane_hosts.LaneHostError) else found


def _move_target_refusal(name: str, hosts: Sequence[lane_hosts.LaneHost], rows: Sequence[Mapping]) -> str | None:
    """Why a relaunch on `name` would refuse, else None: the rule `_lane_host_or_refuse` applies, so a move
    never pauses and stops a run that the relaunch then turns away."""
    host = lane_hosts.find_lane_host(hosts, name)
    if host is None:
        return f"no such host {name}"
    if any(r.get("name") == name and r.get("state") != "active" for r in rows):
        return f"host {name} is not active"
    if not host.workspace_dir:
        return f"host {name} has no workspace_dir: run `cox host beat {name}` on it, or list it under lane_hosts in the profile"
    return None


def _runs_stop(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    run_id = _resolved_run_id(runs_dir, a.run_id)
    result = runs_stop.stop_run(
        run_id, runs_dir, kill_local=os.kill, run_remote=_runs_stop_run_remote,
        is_ended=_runs_stop_is_ended(runs_dir), sleep=time.sleep, hosts=_runs_stop_hosts(a),
    )
    if a.json:
        print(json.dumps(result))
    elif result["ok"]:
        print(f"stopped {run_id}")
    else:
        print(f"refused {run_id}: {result['reason']}")
    return 0 if result["ok"] else 1


def _runs_triage(a: argparse.Namespace) -> int:
    runs_dir = None
    if a.profile or _profile_path(a).exists():
        _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
        if refuse_rc is not None:
            return refuse_rc
    code, out = runs_triage.run(a.task_id, a.json, runs_dir=runs_dir, repo=Path(a.repo).expanduser())
    print(out)
    return code


def _runs_pause_resume_outcome(verbed: str, run_id: str, result, as_json: bool) -> int:
    """Shared print/exit mapping for `runs pause`/`runs resume`, over every store_cli result
    type: `Paused`/`Resumed` prints `<verbed> <run_id>` and exits 0 (with a `reason:` line
    when `Paused.reason` is set); `PauseRefused`/`ResumeRefused` -- the run is not in the
    store -- prints `refused <run_id>: not in store` and exits 3; `Failed` -- the store ran
    but refused or errored -- and `NotAvailable` -- the store could not be opened -- both print
    `failed <run_id>: ...` to stderr and exit 1, neither is "not in store"."""
    if isinstance(result, store_cli.Paused | store_cli.Resumed):
        outcome, code, stream, message = verbed, 0, sys.stdout, f"{verbed} {run_id}"
    elif isinstance(result, store_cli.PauseRefused | store_cli.ResumeRefused):
        outcome, code, stream, message = "refused", 3, sys.stdout, f"refused {run_id}: not in store"
    elif isinstance(result, store_cli.Failed):
        outcome, code, stream = "failed", 1, sys.stderr
        message = f"failed {run_id}: {result.detail} (exit {result.code})"
    else:
        outcome, code, stream = "failed", 1, sys.stderr
        message = f"failed {run_id}: the store is not available: {result.reason}"
    if as_json:
        print(json.dumps({"outcome": outcome, "run": run_id, **dataclasses.asdict(result)}))
        return code
    print(message, file=stream)
    if outcome == verbed and isinstance(result, store_cli.Paused) and result.reason:
        print(f"reason: {result.reason}")
    return code


def _runs_pause(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    run_id = _resolved_run_id(runs_dir, a.run_id)
    store_url = run_store.store_url_for(runs_dir)
    return _runs_pause_resume_outcome("paused", run_id, store_cli.pause(store_url, run_id, reason=a.reason), a.json)


def _runs_resume(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    run_id = _resolved_run_id(runs_dir, a.run_id)
    store_url = run_store.store_url_for(runs_dir)
    return _runs_pause_resume_outcome("resumed", run_id, store_cli.resume(store_url, run_id), a.json)


def _runs_move_pause(store_url: str, run_id: str, reason: str | None) -> tuple[bool, str]:
    """Edge: `cox runs move`'s pause step, over the same `store_cli.pause` result types
    `_runs_pause_resume_outcome` already maps."""
    result = store_cli.pause(store_url, run_id, reason=reason)
    if isinstance(result, store_cli.Paused):
        return True, ""
    if isinstance(result, store_cli.PauseRefused):
        return False, "not in store"
    if isinstance(result, store_cli.Failed):
        return False, result.detail
    return False, f"the store is not available: {result.reason}"


def _runs_move_stop(a: argparse.Namespace, run_id: str) -> tuple[bool, str]:
    """Edge: `cox runs move`'s stop step, wired exactly as `_runs_stop` wires `runs_stop.stop_run`.
    Resolves the run's current host from its own launch record, independent of `--to`."""
    runs_dir = Path(a.runs_dir)
    result = runs_stop.stop_run(
        run_id, runs_dir, kill_local=os.kill, run_remote=_runs_stop_run_remote,
        is_ended=_runs_stop_is_ended(runs_dir), sleep=time.sleep, hosts=_runs_stop_hosts(a),
    )
    return result["ok"], result.get("reason", "")


def _repo_for(ws: Path, home: str) -> Callable[[dict], str]:
    """The `chair_exec.Deps.repo_for`: the action's own repo, else `repo:` from the initiative's
    `initiative.md`, either expanded under `home`; "" when either is missing."""
    def repo_for(action: dict) -> str:
        if action.get("repo"):
            return repo_path.expand(action["repo"], home)
        initiative = action.get("initiative", "")
        if not initiative:
            return ""
        try:
            text = (ws / "work" / initiative / "initiative.md").read_text(encoding="utf-8")
        except OSError:
            return ""
        recorded = route.parse_frontmatter(text)[0].get("repo")
        return repo_path.expand(recorded, home) if recorded else ""
    return repo_for


def _approved_branch_merged(
    runner: Callable[[list[str]], tuple[int, str]],
    repo_for: Callable[[dict], str],
    runs_dir: Path,
) -> Callable[[Mapping], bool]:
    """`FactsDeps.approved_branch_merged`: whether the approved task's commit is on its epic phase branch or the pr branch.

    The commit is the tip of the `branch` the task record names. Only a row whose `land_at` is ticket or level asks;
    task landing, a missing run, branch, commit or repository is False. A task branch deleted after landing cannot be
    resolved, so it reads False and the hold stays."""
    def merged(row: Mapping) -> bool:
        initiative = str(row["initiative"])
        if row.get("land_at", "initiative") == "task":
            return False
        root = repo_for({"initiative": initiative})
        branch = land_manual.record_branch(runs_dir, str(row.get("run") or ""), str(row["id"]))
        if not root or not branch:
            return False
        code, out = runner(["git", "-C", root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"])
        return code == 0 and chair_read_branch_merged.branch_merged(
            lambda args: runner(["git", *args]), root, out.strip(),
            [f"epic/{initiative}/{row['phase']}", land.pr_branch_name(initiative)],
        )
    return merged


def _wrongly_released_merged(
    approved_merged: Callable[[Mapping], bool],
    runs_dir: Path,
    live: Callable[[], Collection[str]],
    land_at: Mapping[str, str],
) -> Callable[[Mapping], bool]:
    """`chair_facts.wrongly_released_facts`' `branch_merged`: `approved_branch_merged` asked of the task's newest run record.

    The commit is the tip of the `branch` that record names, as for an approval. A task with no run record, or any
    live run, is False: a live run is rebuilding it."""
    def merged(item: Mapping) -> bool:
        initiative, task = str(item["initiative"]), str(item["id"])
        runs = {
            run for p in runs_dir.glob(f"*/tasks/*/{task}.json")
            if chair_facts.run_initiative(run := p.parent.parent.parent.name) == initiative
        }
        return bool(runs) and not runs & set(live()) and approved_merged(
            {**item, "run": max(runs, key=chair_facts.run_ordinal), "land_at": land_at.get(initiative, "initiative")}
        )
    return merged


def _runs_move_repo_for(ws: Path, home: str) -> Callable[[dict], str]:
    """`cox runs move`'s clear_branches and relaunch steps resolve a repo as the chair does."""
    return _repo_for(ws, home)


def _runs_move_exec_deps(ws: Path, runs_dir: Path, harness_python: str) -> chair_exec.Deps:
    """The `chair_exec.Deps` bundle `cox runs move`'s clear_branches and relaunch steps run through.
    Records nothing: a needs_chair a step raises is refused by `_runs_move_step` instead."""
    return chair_exec.edge_deps(
        runs_dir, ws, "cox-runs-move", os.getpid(),
        run_id=lambda action: "", repo_for=_runs_move_repo_for(ws, os.path.expanduser("~")), record=lambda action: None,
        host=socket.gethostname(), harness_python=harness_python,
    )


def _runs_move_step(step: dict, a: argparse.Namespace, exec_deps: chair_exec.Deps) -> tuple[bool, str]:
    """One `runs_move.move_plan` step through the edge its kind reaches; epoch 0 stamped and matched."""
    kind = step["kind"]
    if kind == "pause":
        return _runs_move_pause(run_store.store_url_for(Path(a.runs_dir)), step["run"], step.get("reason"))
    if kind == "stop":
        return _runs_move_stop(a, step["run"])
    if kind in ("clear_branches", "relaunch"):
        return _runs_move_exec_outcome(
            kind, chair_exec.perform([{**step, "epoch": 0}], exec_deps, current_epoch=lambda: 0, dry_run=False)[0],
        )
    return False, f"unsupported move step {kind!r}"


def _runs_move_exec_outcome(kind: str, result: dict) -> tuple[bool, str]:
    """A done result carrying a needs_chair, such as a clear_branches carry conflict, is a refusal naming it:
    the loop records that escalation for the chair, and a move must not report success over it."""
    if result["status"] != "done":
        return False, result["reason"]
    if "needs_chair" in result:
        raised = result["needs_chair"]
        return False, f"{kind} needs the chair: {raised.get('reason') or raised.get('cause', '')}"
    return True, result["reason"]


def _runs_move_report(a: argparse.Namespace, executed: list[dict], ok: bool, reason: str) -> int:
    """Edge: one output shape for every outcome of `cox runs move`; exit 0 only when every step was ok."""
    if a.json:
        print(json.dumps({"run": a.run_id, "to": a.to, "ok": ok, "reason": reason, "move_plan": executed}))
    else:
        print(f"moved {a.run_id} -> {a.to}" if ok else f"refused {a.run_id}: {reason}")
    return 0 if ok else 1


def _runs_move_harness_python(a: argparse.Namespace) -> str:
    """The harness python `cox runs move`'s clear_branches and relaunch steps run through, read
    from the profile the same way `_chair_run_deps` reads it; a missing or unreadable profile
    leaves `python` on the caller's own PATH to resolve."""
    profile_text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(profile_text) if profile_text is not None else {}
    except route.ProfileError:
        profile = {}
    harness_dir = profile.get("harness_dir", "")
    return str(install_root.harness_python(harness_dir)) if harness_dir else "python"


def _runs_move(a: argparse.Namespace) -> int:
    """`cox runs move <run_id> --to <host>`: pause, stop, clear_branches, relaunch, in order,
    stopping at the first step whose own result is not ok."""
    hosts = _runs_stop_hosts(a)
    refusal = _move_target_refusal(a.to, hosts, run_store.hosts(Path(a.runs_dir)))
    if refusal is not None:
        if lane_hosts.find_lane_host(hosts, a.to) is None:
            print(f"configured: {', '.join(h.name for h in hosts) or 'none'}", file=sys.stderr)
        return _runs_move_report(a, [], False, refusal)
    initiative = chair_facts.run_initiative(a.run_id)
    runs_dir = Path(a.runs_dir)
    ws = runs_dir.parent
    exec_deps = _runs_move_exec_deps(ws, runs_dir, _runs_move_harness_python(a))
    executed: list[dict] = []
    for step in runs_move.move_plan(a.run_id, initiative, a.to, reason=a.reason):
        ok, reason = _runs_move_step(step, a, exec_deps)
        executed.append({"step": step, "ok": ok, "reason": reason})
        if not ok:
            return _runs_move_report(a, executed, False, reason)
    return _runs_move_report(a, executed, True, "")


def _stored_trace_calls(calls: list[dict], role: str | None) -> list[tuple[str, dict]]:
    """Usage calls as (`<role>-<n>`, call), numbered per role in usage order before `role` filters, sorted like the loose files."""
    named = [
        (f"{c.get('role')}-{1 + sum(1 for p in calls[:i] if p.get('role') == c.get('role'))}", c)
        for i, c in enumerate(calls)
    ]
    kept = [(node, c) for node, c in named if role is None or c.get("role") == role]
    return sorted(kept, key=lambda t: (t[0].rsplit("-", 1)[0], int(t[0].rsplit("-", 1)[1])))


def _print_trace(a: argparse.Namespace, nodes: list[tuple[str, dict]]) -> int:
    """Print the -v detail per node, then the table. `nodes` is (node name, `records.trace_summary` result)."""
    rows = []
    for node, s in nodes:
        rows.append({"node": node, "turns": s["turns"] or 0, "cost_usd": float(s["cost_usd"] or 0), "result": s["subtype"] or "?",
                     "bash": s["tools"].get("Bash", 0), "reads": sum(s["reads"].values()), "whole": s["whole_file_reads"]})
        if a.verbose:
            print(f"== {node}: tools={s['tools']} reads={s['reads']} whole_file_reads={s['whole_file_reads']}")
            for c in s["commands"][:12]:
                print("   $", c)
    print(records.format_table(rows, ["node", "turns", "cost_usd", "result", "bash", "reads", "whole"]))
    return 0


def _runs_trace(a: argparse.Namespace) -> int:
    d = Path(a.runs_dir) / f"{a.run_id}-trace"
    files = sorted(d.glob(f"{a.role}-*.jsonl" if a.role else "*.jsonl"), key=lambda p: (p.stem.rsplit("-", 1)[0], int(p.stem.rsplit("-", 1)[1])))
    if files:
        return _print_trace(a, [(f.stem, records.trace_summary(records.load_trace(f))) for f in files])
    # The loose files are gone (or the dir is empty) once a finished run is compacted: read its calls from the usage record and the trace store.
    usage = run_store.usage(Path(a.runs_dir), a.run_id)
    if not usage or not usage.get("calls"):
        print(f"no trace for {a.run_id} in {a.runs_dir}")
        return 2
    nodes: list[tuple[str, dict]] = []
    for node, call in _stored_trace_calls(list(usage["calls"]), a.role):
        stored = records.summary_from_call(call)
        if stored is not None:
            nodes.append((node, stored))
            continue
        try:
            ev = run_store.call_events(Path(a.runs_dir), a.run_id, call)
        except run_store.TracesUnavailable as exc:
            print(exc)
            return 2
        if ev is not None:
            nodes.append((node, records.trace_summary(ev)))
    return _print_trace(a, nodes)


def _runs_clean(a: argparse.Namespace) -> int:
    repo = Path(a.repo).expanduser()
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"clean: {reason}")
        return 2
    records = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in (runs_dir / a.run_id / "tasks").glob("*/*.json")}
    landed = {task for task, r in records.items() if r.get("landed")}
    dropped = {task for task, r in records.items() if r.get("status") == "dropped"}
    reasons = {task: r.get("reason", "unlanded") for task, r in records.items()}
    p = cleanup.plan_cleanup(run_id=a.run_id, worktrees=cleanup.git_worktrees(repo), branches=cleanup.git_branches(repo), worktree_root=a.worktree_root,
                             live=cleanup.live_owner(runs_dir, a.run_id))
    for line in cleanup.apply_cleanup(repo, p, dry_run=not a.apply, landed=landed, dropped=dropped, reasons=reasons, force=a.force):
        print(line)
    if not a.apply:
        print("(dry run — pass --apply to do it)")
    return 0


def _runs_recover(a: argparse.Namespace) -> int:
    """Merge one approved task's commit into its phase branch, the remedy for
    a merge the harness escalated to `self_modification` and so never
    applied. Never runs the repo's checks, never pushes: this only lands one
    commit on a local phase branch, and the gate to `main` stays a person's.

    The task is then recorded as the epic records a merge it made itself: `approved`, with `merged` and the merge
    commit, never `done` (it is on a phase branch, not landed). Whether it may be is decided before any merge
    (`land.recover_gate`), from the store row and from whether the work is already on the default branch."""
    repo = Path(a.repo).expanduser()
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"recover: {reason}")
        return 2
    record, searched, count = _land_record(runs_dir, a.run_id, a.task_id)
    if record is None:
        print(f"recover: looked in {searched}, found {count} task records, expected 1")
        return 2
    work_root = runs_dir.parent / "work"
    initiative = _initiative_of(work_root, a.task_id)
    if initiative is None:
        print(f"recover: {a.task_id}: no single initiative under {work_root} holds this task")
        return 2
    record_path = searched
    record = land.recover_record(searched, record.get("ticket"), initiative)
    if record.get("kind") == "refuse":
        print(f"recover: {record['reason']}")
        return 2
    phase = record["phase"]
    phase_branch = f"epic/{initiative}/{phase}"
    if not _branch_exists(repo, phase_branch):
        print(f"recover: phase branch {phase_branch} does not exist in {repo}")
        return 2
    item_path = str(work_root / initiative / phase / f"{a.task_id}.md")
    reopen = bool(getattr(a, "reopen", False))
    step = land.recover_plan(record, _recover_branches(repo, record, phase_branch))[0]
    if step["kind"] == "refuse":
        print(f"refused: {step['reason']}")
        return 2
    source = step["source"]
    default, why = _recover_default_ref(repo)
    if default is None:
        print(f"refused: {why}")
        return 2
    if step["kind"] == "already_recovered":
        listed = bounded_run.completed(["git", "-C", str(repo), "rev-list", f"{default}..{source}"], capture_output=True, text=True)
        if listed.returncode != 0:
            print(f"refused: cannot count the commits of {source} off {default}: {listed.stderr.strip()}")
            return 2
        if (refusal := land.recover_contained_refusal(len(listed.stdout.split()), source, default)) is not None:
            print(f"refused: {refusal}")
            return 2
    row = run_store.task_merge_row(runs_dir, initiative, a.task_id)
    landed = False
    if reopen and row is not None and row[0] == "done" and not row[1]:
        # Only a done row may have landed: an approved one is what an escalated merge leaves, and its work may well
        # conflict with a default branch that moved on, which the phase merge below does not care about.
        landed, why, conflicted = _recover_landed(repo, default, source)
        if landed is None:
            print(f"refused: {a.task_id}: {why}")
            if conflicted:
                print(land.reopen_conflict_next_step(default, source, phase_branch, _recover_set_state_argv(runs_dir, initiative, a), _recover_argv(a)))
            return 2
    if (refusal := land.recover_gate(row, reopen=reopen, landed=bool(landed))) is not None:
        print(f"refused: {a.task_id}: {refusal}")
        return 2
    mark_step = {"kind": "mark_merged", "item": item_path, "state": "approved", **({"reopen": "done -> approved"} if reopen else {})}
    if step["kind"] == "already_recovered":
        print(f"recover: {step['branch']} already contains the commit ({step['reason']}); nothing to merge")
        commit = _git_out(repo, "rev-parse", "--verify", source) or ""
        if a.dry_run:
            print(json.dumps({**mark_step, "commit": commit}))
            return 0
        return _record_recovered(a, runs_dir, initiative, item_path, record_path, commit, row)
    target, subject = step["target"], step["commit_subject"]
    message = f"Merge branch '{source}' into {target}"
    if a.dry_run:
        print(f"recover: would merge {source} into {target} --no-ff -m {message!r} ({subject!r})")
        print(json.dumps({**mark_step, "commit": f"the new tip of {target}"}))
        return 0
    before = bounded_run.completed(["git", "-C", str(repo), "rev-parse", target], capture_output=True, text=True).stdout.strip()
    wt = Path(tempfile.mkdtemp(prefix="recover-"))
    add = bounded_run.completed(["git", "-C", str(repo), "worktree", "add", str(wt), target], capture_output=True, text=True)
    if add.returncode != 0:
        print(f"refused: could not check out {target}: {add.stderr.strip() or add.stdout.strip()}")
        return 2
    try:
        merge = bounded_run.completed(["git", "-C", str(wt), "merge", "--no-ff", source, "-m", message], capture_output=True, text=True)
        if merge.returncode != 0:
            bounded_run.completed(["git", "-C", str(wt), "merge", "--abort"], capture_output=True, text=True)
            print(f"refused: merge of {source} into {target} was not clean: {merge.stderr.strip() or merge.stdout.strip()}")
            return 2
    finally:
        bounded_run.completed(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
    after = bounded_run.completed(["git", "-C", str(repo), "rev-parse", target], capture_output=True, text=True).stdout.strip()
    diff = bounded_run.completed(["git", "-C", str(repo), "diff", "--stat", before, after], capture_output=True, text=True)
    print(f"recover: {target} {before[:8]} -> {after[:8]}")
    print(diff.stdout.strip())
    return _record_recovered(a, runs_dir, initiative, item_path, record_path, after, row)


def _recover_default_ref(repo: Path) -> tuple[str | None, str]:
    """Edge. (the default branch's ref, "") or (None, why not). With an `origin`, its HEAD's branch, fetched first as
    `_phase_adds_nothing` fetches, so a landing made elsewhere is seen; a fetch that fails refuses. With no `origin`,
    a local `main` or `master`; with neither, refuses."""
    if _git_out(repo, "remote", "get-url", "origin") is None:
        local = next((b for b in ("main", "master") if _branch_exists(repo, f"refs/heads/{b}")), None)
        return (local, "") if local else (None, f"cannot tell the default branch of {repo}: no origin, main or master")
    head = _git_out(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if not head or not head.startswith("origin/"):
        return None, f"cannot tell origin's default branch in {repo}: origin/HEAD is not set (git remote set-head origin --auto)"
    name = head.removeprefix("origin/")
    ref = f"refs/remotes/origin/{name}"
    fetched = bounded_run.completed(["git", "-C", str(repo), "fetch", "--quiet", "origin", f"+refs/heads/{name}:{ref}"], capture_output=True, text=True)
    if fetched.returncode != 0:
        return None, f"cannot fetch origin's {name} to tell whether the task landed: {fetched.stderr.strip()}"
    return ref, ""


def _recover_landed(repo: Path, default: str, source: str) -> tuple[bool | None, str, bool]:
    """Edge. Whether `source`'s work is on `default` already, by the tree test `_phase_adds_nothing` uses: merging it
    changes nothing. That holds for a merge, a cherry-pick and a squash alike. (None, why, conflicted) when git cannot
    tell; `conflicted` is true when the reason is that the work conflicts with `default`."""
    merged = bounded_run.completed(["git", "-C", str(repo), "merge-tree", "--write-tree", default, source], capture_output=True, text=True)
    tree = bounded_run.completed(["git", "-C", str(repo), "rev-parse", f"{default}^{{tree}}"], capture_output=True, text=True)
    landed, why = land.landed_by_tree(merged.returncode, merged.stdout, tree.returncode, tree.stdout, default, source)
    return landed, why, merged.returncode == 1


def _recover_set_state_argv(runs_dir: Path, initiative: str, a: argparse.Namespace) -> list[str]:
    """Edge. The harness store command that moves the task's row from done back to approved: the store the routing
    profile's provider profile names when it has a storage_url (so no store URL is printed), else cox.db in `runs_dir`."""
    named = store_url.read_provider_profile(_profile_path(a)).get("provider_profile")
    # Both: the provider profile's storage_url when it names one, else cox.db in the runs dir.
    where = [*(["--provider-profile", str(Path(named).expanduser())] if isinstance(named, str) and named else []), "--runs-dir", str(runs_dir)]
    return [sys.executable, *store_cli._MODULE, "set-state", initiative, a.task_id, "approved",
            "--by", _holder_label(a), "--expect", "done", *where]


def _recover_argv(a: argparse.Namespace) -> list[str]:
    """The same recover without --reopen or --dry-run."""
    return ["cox", "runs", "recover", a.run_id, a.task_id, "--repo", str(a.repo),
            *(["--runs-dir", str(a.runs_dir)] if a.runs_dir else []), *(["--profile", str(a.profile)] if a.profile else [])]


def _record_recovered(
    a: argparse.Namespace, runs_dir: Path, initiative: str, item_path: str, record_path: str, commit: str,
    row: tuple[str, str | None] | None,
) -> int:
    """Edge. Record the recovered task as merged into its phase branch at `commit`, its state `approved`. The store
    moves first (a `done` row `land.recover_gate` let through goes back to approved, then the merge commit is
    stamped), and is read back: only a row that reads (approved, commit) lets the task record and the work item follow.
    Anything else stops with exit 1 and both left as they were."""
    task, by = a.task_id, _holder_label(a)
    if not commit:
        print(f"recover: {task}: no commit to record")
        return 1
    if row is not None and row[0] == "done":
        moved = store_cli.set_state(runs_dir, initiative, task, "approved", by, expected="done")
        if not isinstance(moved, store_cli.StateSet):
            print(f"recover: store did not move {task} back to approved: {moved}")
            return 1
    stored = store_cli.mark_merged(runs_dir, initiative, task, commit, by)
    now = run_store.task_merge_row(runs_dir, initiative, task)
    if now != ("approved", commit):
        print(f"recover: store did not record {task} as merged at {commit[:12]} ({stored}; the row reads {now}); "
              "the task record and the work item are left as they were")
        return 1
    path = Path(record_path)
    if path.exists():
        found = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**found, "merged": True, "merged_commit": commit}, indent=2), encoding="utf-8")
    item = Path(item_path)
    if item.exists() and (new_text := land.recovered_text(item.read_text(encoding="utf-8"), commit)) is not None:
        item.write_text(new_text, encoding="utf-8")
        queue_export.record_written_paths(runs_dir.parent, [item.absolute()])
    print(f"recover: {task} recorded merged into its phase branch at {commit[:12]}, state approved")
    return 0


def _lane_pid(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None


def _lane_live(runs_dir: Path, run: str) -> bool:
    """Live, or fenced: a process that answers without a held lease still occupies its lane."""
    pidfile = runs_dir / f"{run}.pid"
    return epic.run_live(_lane_pid(pidfile), pidfile) or epic.run_state(_lane_pid(pidfile), pidfile) == "fenced"


def _fenced_lanes(runs_dir: Path) -> set[str]:
    """Edge. Local runs whose process answers but whose lease is not held; `runs_top_screen` rows read them as exited."""
    return {pf.stem for pf in runs_dir.glob("*.pid") if epic.run_state(_lane_pid(pf), pf) == "fenced"}


def _lane_outcome(runs_dir: Path, run: str, at: str) -> dict:
    try:
        log = (runs_dir / f"{run}.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    s = epic.summarize_log(log)
    return {"run": run, "at": at, "approved": s["approved"], "quarantined": s["quarantined"], "summary": s["summary"]}


def _wait_fetch(a: argparse.Namespace, runs_dir: Path, run_id: str) -> tuple[str, list[str], str]:
    """Edge. `runs fetch <run>` for a remote run that exited while `runs wait` watched it."""
    ns = argparse.Namespace(**{"profile": None, **vars(a)})  # `runs wait` takes no --profile; `_lake_provider` reads one
    hosts = _fetch_hosts(_read_text_or_none(_profile_path(ns)) or "", run_store.hosts(runs_dir))
    mode = work_state.work_state_mode(_lake_provider(ns)[0])
    return _fetch_one(runs_dir, hosts, run_id, mode, _holder_label(ns))


def _poll_live(rows_fn, runs_dir: Path, busy: list[str]):
    """A liveness probe over `rows_fn`. `epic.wait_lanes` asks about `busy` in order once per poll, so the rows
    are read once per poll, when it asks about the first run, and not once per run."""
    live: set[str] = set()

    def is_live(run: str) -> bool:
        nonlocal live
        if run == busy[0]:
            live = set(runs_top_screen.waitable(rows_fn(runs_dir))) | _fenced_lanes(runs_dir)
        return run in live

    return is_live


def _runs_wait(a: argparse.Namespace, *, rows_fn=None, clock=None, sleep=None, fetch=None) -> int:
    """`rows_fn`, `clock`, `sleep` and `fetch` are test seams; each defaults to the real one at call time."""
    d = Path(a.runs_dir)
    rows_fn = rows_fn or runs_top_screen.rows_now
    rows = rows_fn(d)
    busy = sorted({*runs_top_screen.waitable(rows), *_fenced_lanes(d)})
    if not busy:
        print("all lanes clear")
        return 2
    exited, still = epic.wait_lanes(busy, _poll_live(rows_fn, d, busy), clock or time.monotonic, sleep or time.sleep, a.max_seconds, a.interval)
    at = datetime.datetime.now(datetime.UTC).astimezone().strftime("%H:%M")  # when the exit was noticed, not when it happened
    for run in sorted(set(exited) & runs_top_screen.remote_run_ids(rows)):
        if remote_lane.fetched_record_path(d, run).exists():
            continue
        outcome, lines, _host = (fetch or (lambda r_dir, r: _wait_fetch(a, r_dir, r)))(d, run)
        if outcome != "fetched":
            print("\n".join([f"{run}: {lines[0]}", *lines[1:]]), file=sys.stderr if a.json else sys.stdout)
    outcomes = [_lane_outcome(d, r, at) for r in exited]
    if a.json:
        print(json.dumps({"exited": outcomes, "busy": still}, indent=2))
    elif not outcomes:
        print(f"still busy: {' '.join(still)}")
    else:
        for o in outcomes:
            print(f"exited {o['run']} at {o['at']}")
            print("\n".join([*o["approved"], *o["quarantined"], *([o["summary"]] if o["summary"] else [])]))
    return 0 if outcomes else 3


def _leader_runs_dir_or_refuse(a: argparse.Namespace):
    """spec: the leader lock lives under the profile's workspace, same as
    `route status`'s runs dir. Returns (profile, runs_dir, None) or
    (None, None, 2) after printing the refusal."""
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return None, None, 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return None, None, 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return None, None, 2
    return profile, Path(workspace).expanduser() / "runs", None


def _leader_heartbeat_minutes() -> int:
    """`policy.leader.heartbeat_minutes` from the resolved cartridge dict, default 10.
    Resolving a cartridge's own policy is another repository's item, and
    `route.parse_profile`'s flat `key: scalar` schema (spec §1) has no `policy` key to
    read in the meantime, so this always answers the default until that lands."""
    return chair.DEFAULT_HEARTBEAT_MINUTES
