"""cox — the coxswain's operator tools (alias: agent-tools, removed next release)."""

from __future__ import annotations

import argparse
import ast
import collections
import contextlib
import contextvars
import dataclasses
import datetime
import getpass
import importlib
import importlib.metadata
import importlib.util
import inspect
import io
import json
import math
import os
import platform
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import types
import urllib.parse
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

import yaml

from agent_tools import commands


# Importing `agent_tools.cli` does not import the command modules: a name like `chair` below is a
# `_LazyModule`, not the module. Writes reach the real module, so `monkeypatch.setattr(cli.chair, ...)` holds.
class _LazyModule:
    """A module imported with importlib on its first attribute read; writes and deletes go to the real module."""

    def __init__(self, path: str) -> None:
        object.__setattr__(self, "_lazy_path", path)
        _LAZY_MODULES.append(self)

    def _lazy_load(self) -> types.ModuleType:
        return importlib.import_module(self._lazy_path)

    def __getattr__(self, name: str):
        return getattr(self._lazy_load(), name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._lazy_load(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._lazy_load(), name)

    def __repr__(self) -> str:
        return f"<lazy module {self._lazy_path}>"


_LAZY_MODULES: list[_LazyModule] = []


@dataclasses.dataclass(frozen=True)
class _LazyHandler:
    """A `"dotted.module:function"` table entry; calling it imports the module with importlib, not before."""

    target: str

    def __call__(self, *args, **kwargs):
        module_path, _, func = self.target.partition(":")
        return getattr(importlib.import_module(module_path), func)(*args, **kwargs)


card_keys = _LazyModule("agent_tools.card_keys")
chair = _LazyModule("agent_tools.chair")
chair_capacity = _LazyModule("agent_tools.chair_capacity")
chair_chat = _LazyModule("agent_tools.chair_chat")
chair_dash_sync = _LazyModule("agent_tools.chair_dash_sync")
chair_decompose_runs = _LazyModule("agent_tools.chair_decompose_runs")
chair_exec = _LazyModule("agent_tools.chair_exec")
chair_facts = _LazyModule("agent_tools.chair_facts")
chair_holder = _LazyModule("agent_tools.chair_holder")
chair_resolve = _LazyModule("agent_tools.chair_resolve")
chair_land_freeze = _LazyModule("agent_tools.chair_land_freeze")
chair_load_warn = _LazyModule("agent_tools.chair_load_warn")
chair_login_check = _LazyModule("agent_tools.chair_login_check")
chair_orchestrate = _LazyModule("agent_tools.chair_orchestrate")
chair_pid_probe = _LazyModule("agent_tools.chair_pid_probe")
chair_read_approved = _LazyModule("agent_tools.chair_read_approved")
chair_read_attempts = _LazyModule("agent_tools.chair_read_attempts")
chair_read_branch_merged = _LazyModule("agent_tools.chair_read_branch_merged")
chair_read_cache = _LazyModule("agent_tools.chair_read_cache")
chair_read_docket = _LazyModule("agent_tools.chair_read_docket")
chair_read_exits = _LazyModule("agent_tools.chair_read_exits")
chair_read_housekeeping = _LazyModule("agent_tools.chair_read_housekeeping")
chair_read_idle_backlog = _LazyModule("agent_tools.chair_read_idle_backlog")
chair_read_idle_hosts = _LazyModule("agent_tools.chair_read_idle_hosts")
chair_read_idle_lands = _LazyModule("agent_tools.chair_read_idle_lands")
chair_read_idle_open = _LazyModule("agent_tools.chair_read_idle_open")
chair_read_intake = _LazyModule("agent_tools.chair_read_intake")
chair_read_lease = _LazyModule("agent_tools.chair_read_lease")
chair_read_live = _LazyModule("agent_tools.chair_read_live")
chair_read_lost = _LazyModule("agent_tools.chair_read_lost")
chair_read_patch = _LazyModule("agent_tools.chair_read_patch")
chair_read_phase_records = _LazyModule("agent_tools.chair_read_phase_records")
chair_read_quarantined = _LazyModule("agent_tools.chair_read_quarantined")
chair_read_record = _LazyModule("agent_tools.chair_read_record")
chair_read_remote_unfetched = _LazyModule("agent_tools.chair_read_remote_unfetched")
chair_read_run_id = _LazyModule("agent_tools.chair_read_run_id")
chair_read_stale = _LazyModule("agent_tools.chair_read_stale")
chair_read_stall = _LazyModule("agent_tools.chair_read_stall")
chair_read_stranded = _LazyModule("agent_tools.chair_read_stranded")
chair_report = _LazyModule("agent_tools.chair_report")
chair_restart = _LazyModule("agent_tools.chair_restart")
chair_restart_log = _LazyModule("agent_tools.chair_restart_log")
chair_revert_green = _LazyModule("agent_tools.chair_revert_green")
chair_revert_ports = _LazyModule("agent_tools.chair_revert_ports")
chair_revert_watch = _LazyModule("agent_tools.chair_revert_watch")
chair_run = _LazyModule("agent_tools.chair_run")
chair_service = _LazyModule("agent_tools.chair_service")
chair_service_host = _LazyModule("agent_tools.chair_service_host")
chair_smoke = _LazyModule("agent_tools.chair_smoke")
chair_unit = _LazyModule("agent_tools.chair_unit")
chair_wake = _LazyModule("agent_tools.chair_wake")
chair_watchdog = _LazyModule("agent_tools.chair_watchdog")
checkout = _LazyModule("agent_tools.checkout")
checkout_sync = _LazyModule("agent_tools.checkout_sync")
cleanup = _LazyModule("agent_tools.cleanup")
console_screen = _LazyModule("agent_tools.console_screen")
courier = _LazyModule("agent_tools.courier")
dash_binary = _LazyModule("agent_tools.dash_binary")
dash_detail_health = _LazyModule("agent_tools.dash_detail_health")
dash_detail_initiative = _LazyModule("agent_tools.dash_detail_initiative")
dash_detail_machine = _LazyModule("agent_tools.dash_detail_machine")
dash_detail_release = _LazyModule("agent_tools.dash_detail_release")
dash_detail_run = _LazyModule("agent_tools.dash_detail_run")
dash_detail_spend = _LazyModule("agent_tools.dash_detail_spend")
dash_feed = _LazyModule("agent_tools.dash_feed")
dash_feed_share = _LazyModule("agent_tools.dash_feed_share")
decisions = _LazyModule("agent_tools.decisions")
doctor = _LazyModule("agent_tools.doctor")
draft_apply = _LazyModule("agent_tools.draft_apply")
draft_list = _LazyModule("agent_tools.draft_list")
epic = _LazyModule("agent_tools.epic")
forge = _LazyModule("agent_tools.forge")
forge_github = _LazyModule("agent_tools.forge_github")
generate = _LazyModule("agent_tools.generate")
home_paths = _LazyModule("agent_tools.home")
host_cmd = _LazyModule("agent_tools.host_cmd")
host_load = _LazyModule("agent_tools.host_load")
install = _LazyModule("agent_tools.install")
install_exec = _LazyModule("agent_tools.install_exec")
install_root = _LazyModule("agent_tools.install_root")
intake_fields = _LazyModule("agent_tools.intake_fields")
intake_queue = _LazyModule("agent_tools.intake_queue")
intake_state = _LazyModule("agent_tools.intake_state")
jsonl_log = _LazyModule("agent_tools.jsonl_log")
lake_config = _LazyModule("agent_tools.lake_config")
lake_lease = _LazyModule("agent_tools.lake_lease")
land = _LazyModule("agent_tools.land")
land_batch_effects = _LazyModule("agent_tools.land_batch_effects")
land_batch_group = _LazyModule("agent_tools.land_batch_group")
land_batch_run = _LazyModule("agent_tools.land_batch_run")
land_batch_settings = _LazyModule("agent_tools.land_batch_settings")
land_batch_squash = _LazyModule("agent_tools.land_batch_squash")
land_lease = _LazyModule("agent_tools.land_lease")
land_limit = _LazyModule("agent_tools.land_limit")
land_merge_lock = _LazyModule("agent_tools.land_merge_lock")
land_repo_lease = _LazyModule("agent_tools.land_repo_lease")
lane_checkout_sync = _LazyModule("agent_tools.lane_checkout_sync")
lane_hosts = _LazyModule("agent_tools.lane_hosts")
lane_python = _LazyModule("agent_tools.lane_python")
lane_toolchain = _LazyModule("agent_tools.lane_toolchain")
launch_place = _LazyModule("agent_tools.launch_place")
leader_chat = _LazyModule("agent_tools.leader_chat")
notify = _LazyModule("agent_tools.notify")
notify_core = _LazyModule("agent_tools.notify_core")
pacing = _LazyModule("agent_tools.pacing")
plan = _LazyModule("agent_tools.plan")
pr_land = _LazyModule("agent_tools.pr_land")
provenance = _LazyModule("agent_tools.provenance")
queue_export = _LazyModule("agent_tools.queue_export")
queue_rows = _LazyModule("agent_tools.queue_rows")
records = _LazyModule("agent_tools.records")
remote_argv = _LazyModule("agent_tools.remote_argv")
remote_doctor = _LazyModule("agent_tools.remote_doctor")
remote_fetch = _LazyModule("agent_tools.remote_fetch")
remote_lane = _LazyModule("agent_tools.remote_lane")
remote_launch = _LazyModule("agent_tools.remote_launch")
release_gate = _LazyModule("agent_tools.release_gate")
repo_clone = _LazyModule("agent_tools.repo_clone")
repo_path = _LazyModule("agent_tools.repo_path")
repo_path_migrate = _LazyModule("agent_tools.repo_path_migrate")
review_pr = _LazyModule("agent_tools.review_pr")
review_pr_close = _LazyModule("agent_tools.review_pr_close")
route = _LazyModule("agent_tools.route")
route_drift = _LazyModule("agent_tools.route_drift")
route_edit = _LazyModule("agent_tools.route_edit")
route_epic = _LazyModule("agent_tools.route_epic")
route_guard = _LazyModule("agent_tools.route_guard")
route_hold = _LazyModule("agent_tools.route_hold")
route_import = _LazyModule("agent_tools.route_import")
route_reach = _LazyModule("agent_tools.route_reach")
route_remove = _LazyModule("agent_tools.route_remove")
route_requeue = _LazyModule("agent_tools.route_requeue")
route_schedule = _LazyModule("agent_tools.route_schedule")
route_sync = _LazyModule("agent_tools.route_sync")
route_sync_gh = _LazyModule("agent_tools.route_sync_gh")
router = _LazyModule("agent_tools.router")
run_logs = _LazyModule("agent_tools.run_logs")
run_store = _LazyModule("agent_tools.run_store")
runs_bar = _LazyModule("agent_tools.runs_bar")
runs_detail = _LazyModule("agent_tools.runs_detail")
runs_detail_screen = _LazyModule("agent_tools.runs_detail_screen")
runs_herdr = _LazyModule("agent_tools.runs_herdr")
runs_module = _LazyModule("agent_tools.runs")
runs_move = _LazyModule("agent_tools.runs_move")
runs_stop = _LazyModule("agent_tools.runs_stop")
runs_stranded = _LazyModule("agent_tools.runs_stranded")
base_branch = _LazyModule("agent_tools.base_branch")
land_checkout_guard = _LazyModule("agent_tools.land_checkout_guard")
phase_status = _LazyModule("agent_tools.phase_status")
repo_clone = _LazyModule("agent_tools.repo_clone")
land_manual = _LazyModule("agent_tools.land_manual")
runs_top = _LazyModule("agent_tools.runs_top")
runs_top_screen = _LazyModule("agent_tools.runs_top_screen")
runs_triage = _LazyModule("agent_tools.runs_triage")
schema = _LazyModule("agent_tools.schema")
settings_model = _LazyModule("agent_tools.settings_model")
setup_install = _LazyModule("agent_tools.setup_install")
setup_migrate = _LazyModule("agent_tools.setup_migrate")
setup_migrate_hosts = _LazyModule("agent_tools.setup_migrate_hosts")
setup_receipts = _LazyModule("agent_tools.setup_receipts")
setup_store = _LazyModule("agent_tools.setup_store")
setup_store_io = _LazyModule("agent_tools.setup_store_io")
setup_token = _LazyModule("agent_tools.setup_token")
claude_token = _LazyModule("agent_tools.claude_token")
sources = _LazyModule("agent_tools.sources")
stats_causes = _LazyModule("agent_tools.stats_causes")
stats_chair = _LazyModule("agent_tools.stats_chair")
stats_efficiency = _LazyModule("agent_tools.stats_efficiency")
stats_examples = _LazyModule("agent_tools.stats_examples")
stats_flaky = _LazyModule("agent_tools.stats_flaky")
stats_gates = _LazyModule("agent_tools.stats_gates")
stats_ingest = _LazyModule("agent_tools.stats_ingest")
stats_lanes = _LazyModule("agent_tools.stats_lanes")
stats_models_cmd = _LazyModule("agent_tools.stats_models_cmd")
stats_query = _LazyModule("agent_tools.stats_query")
stats_schema = _LazyModule("agent_tools.stats_schema")
stats_shadow_tier = _LazyModule("agent_tools.stats_shadow_tier")
stats_system_one = _LazyModule("agent_tools.stats_system_one")
stats_tiers_cmd = _LazyModule("agent_tools.stats_tiers_cmd")
steward = _LazyModule("agent_tools.steward")
steward_draft = _LazyModule("agent_tools.steward_draft")
store_cli = _LazyModule("agent_tools.store_cli")
store_dialect = _LazyModule("agent_tools.store_dialect")
store_probe = _LazyModule("agent_tools.store_probe")
store_switch = _LazyModule("agent_tools.store_switch")
store_url = _LazyModule("agent_tools.store_url")
towpath_launch = _LazyModule("agent_tools.towpath_launch")
tracker = _LazyModule("agent_tools.tracker")
upgrade_exec = _LazyModule("agent_tools.upgrade_exec")
upgrade_plan = _LazyModule("agent_tools.upgrade_plan")
usage_meter = _LazyModule("agent_tools.usage_meter")
usage_window = _LazyModule("agent_tools.usage_window")
work_state = _LazyModule("agent_tools.work_state")
workspace_sync = _LazyModule("agent_tools.workspace_sync")

# Argument defaults the parser needs at build time, copied so `--help` imports no command module.
# tests/test_cli_lazy_dispatch.py pins each to the module constant it copies.
_STATS_CAUSES = ("ticket", "code", "harness", "unknown")  # stats_chair.CAUSES
_GATES_MIN_SAMPLE = 50  # stats_gates.MIN_SAMPLE
_CHAIR_RUN_INTERVAL = 60.0  # chair_run.DEFAULT_INTERVAL


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


def _stats_ingest(a: argparse.Namespace) -> int:
    try:
        report = stats_ingest.ingest(
            a.runs_dir, a.db, work_store_root=a.work_store_root, cartridges_repo=a.cartridges_repo,
        )
    except FileNotFoundError as exc:
        print(exc)
        return 1
    profile_summary = (
        f"provider_profile: {report.provider_profile_from_ledger} from ledger, "
        f"{report.provider_profile_from_node} from node record, "
        f"{report.provider_profile_unresolved} unresolved; "
        f"provider_profile_sha: {report.provider_profile_sha_resolved} resolved, "
        f"{report.provider_profile_sha_unresolved} unresolved"
    )
    if report.unparsed_count:
        print(f"{report.runs_ingested} run(s) ingested; {report.unparsed_count} file(s) failed to parse:")
        for path in report.unparsed_sample:
            print(f"  {path}")
        return 1
    print(f"{report.runs_ingested} run(s) ingested from {a.runs_dir} into {a.db} ({profile_summary})")
    return 0


def _stats_fetch(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def _stats_roles(a: argparse.Namespace) -> int:
    lands = run_store.landed_tasks(Path(a.runs_dir), "", "9999")
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.roles_report(
            _stats_fetch(conn, "calls"), _stats_fetch(conn, "tasks"), _stats_fetch(conn, "runs"),
            cartridge_sha=a.cartridge_sha, provider_profile=a.provider_profile,
            landed={row["task_id"] for row in lands.rows} if lands.rows else None,
        )
    finally:
        conn.close()
    print(f"lands from: {lands.source}", file=sys.stderr)
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _canonical_date(text: str) -> bool:
    """True only for YYYY-MM-DD. fromisoformat also takes `20260901` and `2026-W36-1`, which compare wrong as text in SQL."""
    try:
        return datetime.date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def _stats_tiers(a: argparse.Namespace) -> int:
    if a.since is not None and not _canonical_date(a.since):
        print(f"cox stats tiers: --since must be a date as YYYY-MM-DD, got {a.since!r}", file=sys.stderr)
        return 2
    return stats_tiers_cmd.run(a.db, a.since, a.min_samples, a.json)


def _stats_models(a: argparse.Namespace) -> int:
    if a.since is not None and not _canonical_date(a.since):
        print(f"cox stats models: --since must be a date as YYYY-MM-DD, got {a.since!r}", file=sys.stderr)
        return 2
    return stats_models_cmd.run(a.db, a.role, a.since, a.json)


def _stats_causes(a: argparse.Namespace) -> int:
    if a.since is not None and not _canonical_date(a.since):
        print(f"cox stats causes: --since must be a date as YYYY-MM-DD, got {a.since!r}", file=sys.stderr)
        return 2
    summaries = stats_causes.summarise(run_store.attempt_causes(Path(a.runs_dir), a.since or ""))
    print(json.dumps(stats_causes.to_json(summaries), indent=2) if a.json else "\n".join(stats_causes.render_lines(summaries)))
    return 0


def _stats_efficiency(a: argparse.Namespace) -> int:
    if a.days < 1:
        print(f"cox stats efficiency: --days must be at least 1, got {a.days}", file=sys.stderr)
        return 2
    now = datetime.datetime.now(datetime.UTC)
    runs_dir = Path(a.runs_dir)
    since = (now - datetime.timedelta(days=a.days - 1)).date().isoformat()
    stored = run_store.efficiency_rows(runs_dir, since)
    landed = run_store.landed_tasks(runs_dir, "", "9999")
    lands = stats_efficiency.lands_of(landed.rows)
    rows, total = stats_efficiency.efficiency(stored["calls"], stored["tasks"], lands, since, now, stats_efficiency.coverage_start(lands))
    if a.json:
        print(json.dumps(stats_efficiency.to_json(rows, total, landed.source), indent=2))
    else:
        print("\n".join(stats_efficiency.render_lines(rows, total, landed.source)))
    return 0


def _print_gates(rows: list[dict], as_json: bool, min_sample: int) -> None:
    verdicts = [stats_gates.verdict_line(row, min_sample) for row in rows]
    print(json.dumps({"rows": rows, "verdicts": verdicts}, indent=2) if as_json else stats_query.render_gates(rows, verdicts))


def _stats_gates_from_store(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    rows = stats_gates.gate_rows(run_store.task_verdict_rows(runs_dir, a.since), run_store.gate_call_rows(runs_dir, a.since))
    if not rows:
        window = f" on or after {a.since}" if a.since is not None else ""
        print(f"no gate rows{window} in the run store under {a.runs_dir}", file=sys.stderr)
        return 1
    _print_gates(rows, a.json, a.min_sample)
    return 0


def _stats_gates(a: argparse.Namespace) -> int:
    if a.since is not None and not _canonical_date(a.since):
        print(f"cox stats gates: --since must be a date as YYYY-MM-DD, got {a.since!r}", file=sys.stderr)
        return 2
    if a.store:
        return _stats_gates_from_store(a)
    hint = f"no gate rows in {a.db}: run cox stats ingest"
    # stats_schema.connect creates a missing db, so a missing file is checked before the open.
    if not Path(a.db).exists():
        print(hint, file=sys.stderr)
        return 1
    conn = stats_schema.connect(a.db)
    try:
        rows = stats_gates.gate_rows(*stats_query.gates_inputs(conn, a.since))
        in_db = a.since is not None and not rows and bool(stats_gates.gate_rows(*stats_query.gates_inputs(conn, None)))
    finally:
        conn.close()
    if not rows:
        print(f"no gate rows on or after {a.since} in {a.db}: widen or drop --since" if in_db else hint, file=sys.stderr)
        return 1
    _print_gates(rows, a.json, a.min_sample)
    return 0


def _stats_coverage(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.coverage_report(
            _stats_fetch(conn, "calls"), _stats_fetch(conn, "tasks"), _stats_fetch(conn, "runs"),
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _stats_explain(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.explain_report(_stats_fetch(conn, "calls"), _stats_fetch(conn, "tasks"), a.role)
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped([report]))
    return 0


def _stats_series(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.series_report(
            _stats_fetch(conn, "runs"), _stats_fetch(conn, "tasks"),
            cartridge_sha=a.cartridge_sha, provider_profile=a.provider_profile,
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _stats_spend_mix(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.spend_mix_report(_stats_fetch(conn, "calls"))
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _stats_lanes(a: argparse.Namespace) -> int:
    if a.hours < 1:
        print("--hours must be at least 1", file=sys.stderr)
        return 2
    now = datetime.datetime.now(datetime.UTC)
    spans = run_store.run_spans(Path(a.runs_dir), stats_lanes.window_start(now, a.hours).isoformat())
    rows = stats_lanes.lanes_by_hour(spans, now, a.hours)
    if a.json:
        print(json.dumps({"rows": rows, "totals": stats_lanes.totals(rows)}, indent=2))
    else:
        print(stats_lanes.render_table(rows, None))
        print(stats_lanes.render_totals(rows))
    return 0


def _file_date(p: Path) -> str:
    return datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.UTC).date().isoformat()


def _in_window(p: Path, since: str | None) -> bool:
    return since is None or _file_date(p) >= since


def _run_in_window(runs_dir: Path, run_id: str, since: str | None) -> bool:
    """A file run by its file's mtime date; a store-only run by the date of its `launched_at`, absent counting as outside."""
    p = runs_dir / f"{run_id}.usage.json"
    if p.exists():
        return _in_window(p, since)
    started = run_store.run_started(runs_dir, run_id)
    return since is None or (started is not None and started[:10] >= since)


def _json_or(text: str | None, default):
    try:
        return json.loads(text) if text is not None else default
    except ValueError:
        return default


def _chair_catalog_prices(a: argparse.Namespace) -> dict:
    """model -> price from `catalog.yaml` beside the profile's `provider_profile`, else `<cartridges_dir>/providers/catalog.yaml`, read as data; {} when the profile or both catalogs are unreadable."""
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        profile = {}
    provider_profile, cartridges_dir = profile.get("provider_profile"), profile.get("cartridges_dir")
    candidates = [
        *([Path(provider_profile).expanduser().parent / "catalog.yaml"] if provider_profile else []),
        *([Path(cartridges_dir).expanduser() / "providers" / "catalog.yaml"] if cartridges_dir else []),
    ]
    catalog_text = next((t for p in candidates if (t := _read_text_or_none(p)) is not None), None)
    try:
        return stats_chair.catalog_prices(yaml.safe_load(catalog_text) if catalog_text is not None else None)
    except yaml.YAMLError:
        return {}


def _chair_transcript_lines(projects_dir: Path, session: str) -> list[str]:
    """Every line of the session transcript and of its subagent transcripts."""
    paths = [*projects_dir.glob(f"*/{session}.jsonl"), *projects_dir.glob(f"*/{session}/subagents/agent-*.jsonl")]
    return [line for p in sorted(paths) for line in (_read_text_or_none(p) or "").splitlines()]


def _stats_chair(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    landed_tasks = [
        (p, run_dir.name, p.stem)
        for run_dir in sorted(d for d in runs_dir.glob("*") if d.is_dir())
        for p in sorted(run_dir.glob("tasks/*/*.json"))
        if _json_or(_read_text_or_none(p), {}).get("landed") is True
    ]
    landed = [(run, task) for p, run, task in landed_tasks if _in_window(p, a.since)]
    usage_calls = [
        {**call, "run": run_id}
        for run_id, u in run_store.usages(runs_dir).items() if _run_in_window(runs_dir, run_id, a.since)
        for call in (u.get("calls") or [])
    ]
    land_rows = list(jsonl_log.read_log(runs_dir / "land.jsonl").rows)
    prices = _chair_catalog_prices(a)
    sessions = stats_chair.session_ids(chair.read(runs_dir) if (runs_dir / chair.CHAIR_FILENAME).exists() else None, a.session or [])
    lines = [line for s in sessions for line in _chair_transcript_lines(Path(a.projects_dir).expanduser(), s)]
    chair_usd = stats_chair.chair_cost(stats_chair.lines_since(lines, a.since), prices) if prices else None
    items = [
        stats_chair.frontmatter_item(text, p.stem)
        for p in sorted(Path(a.work_store_root).glob("*/*/*.md")) if p.name != "initiative.md"
        if (text := _read_text_or_none(p)) is not None
    ]
    report = stats_chair.chair_report(a.since, landed, usage_calls, land_rows, chair_usd, items)
    print(json.dumps(report, indent=2) if a.json else stats_chair.render_chair(report))
    return 0


def _stats_examples(a: argparse.Namespace) -> int:
    roles = list(dict.fromkeys(a.role))
    for role in roles:
        if role not in stats_examples.BUILDERS:
            print(f"unknown role {role!r}: choose one of {', '.join(stats_examples.BUILDERS)}", file=sys.stderr)
            return 2
    texts = [_read_text_or_none(p) for p in sorted(Path(a.runs_dir).glob("*/tasks/*/*.json"))]
    records = [stats_examples.parse_record(t) if t is not None else None for t in texts]
    built = [(role, stats_examples.build_examples(records, role, a.since)) for role in roles]
    out = "".join(stats_examples.format_lines(examples) for _, examples in built)
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
    else:
        sys.stdout.write(out)
    counts = [f"written {len(ex)}, skipped {len(records) - len(ex)}" for _, ex in built]
    if len(built) == 1:
        print(f"read {len(records)}, {counts[0]}", file=sys.stderr)
    else:
        print(f"read {len(records)}; " + "; ".join(f"{r}: {c}" for (r, _), c in zip(built, counts)), file=sys.stderr)
    return 0


def _stats_flaky(a: argparse.Namespace) -> int:
    if a.days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return 2
    dated = []
    for p in sorted(Path(a.runs_dir).glob("*/tasks/*/*.json")):
        text = _read_text_or_none(p)
        try:
            record = json.loads(text) if text is not None else None
        except ValueError:
            record = None
        if isinstance(record, dict):
            dated.append((_file_date(p), record))
    today = datetime.datetime.now(datetime.UTC).date()
    flaky = stats_flaky.flaky_tests(dated, today, a.days)
    print(json.dumps(flaky, indent=2) if a.json else "\n".join(stats_flaky.render_lines(flaky, a.days)))
    return 0


def _stats_system_one(a: argparse.Namespace) -> int:
    usages = list(run_store.usages(Path(a.runs_dir)).values())
    found = stats_system_one.summaries(stats_system_one.rows_from_usage(usages), a.since, a.role)
    print(json.dumps(stats_system_one.to_json(found), indent=2) if a.json else stats_system_one.render_report(found))
    if a.propose:
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        for s in found:
            if stats_system_one.verdict(s)[0] != "READY":
                print(f"skipped {s.role}: not ready", file=sys.stderr)
                continue
            out = Path(a.plans_dir) / f"system-one-graduation-{s.role}-{today}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(stats_system_one.render_proposal(s, today), encoding="utf-8")
            print(f"wrote {out}", file=sys.stderr)
    return 0


def _stats_shadow_tier(a: argparse.Namespace) -> int:
    usages = list(run_store.usages(Path(a.runs_dir)).values())
    found = stats_shadow_tier.summaries(stats_shadow_tier.rows_from_usage(usages), a.since, a.role)
    print(json.dumps(stats_shadow_tier.to_json(found), indent=2) if a.json else stats_shadow_tier.render_report(found))
    return 0


def _bounds_ceiling_for(a: argparse.Namespace):
    """role -> declared ceiling from the resolved provider profile's
    `role_budget_usd` (per-role) falling back to its `budget_usd` (default);
    `None` for both when the routing profile at `_profile_path(a)` (spec §1:
    `--profile`, `$AGENT_TOOLS_PROFILE` or `DEFAULT_PROFILE`, same as `route
    context`/`setup doctor`) or its named `provider_profile` is missing or
    unreadable — reported, never refused, matching `_gather_doctor_facts`'s
    style rather than `_resolve_profile_or_refuse`'s (docs/design/
    cost-bounds.md §2: 'the declared ceiling from the resolved provider
    profile')."""
    text = _read_text_or_none(_profile_path(a))
    try:
        routing_profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        routing_profile = {}
    provider_path = routing_profile.get("provider_profile")
    provider_text = _read_text_or_none(Path(provider_path).expanduser()) if provider_path else None
    try:
        provider_profile = yaml.safe_load(provider_text) if provider_text is not None else None
    except yaml.YAMLError:
        provider_profile = None
    provider_profile = provider_profile if isinstance(provider_profile, dict) else {}
    role_budget_usd = provider_profile.get("role_budget_usd") or {}
    budget_usd = provider_profile.get("budget_usd")
    budget_usd = budget_usd if isinstance(budget_usd, dict) else {}
    tier_overrides = provider_profile.get("tier_overrides") or {}
    defaults = provider_profile.get("defaults") or {}

    def _ceiling(role, _model):
        tier = _resolved_tier(tier_overrides, defaults, role)
        value = role_budget_usd[role] if role in role_budget_usd else budget_usd.get(tier)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return _ceiling


# A profile may name a capability class or a tier; tools does not import cartridges.
_CLASS_TIER = {"extract": "cheap", "reason": "standard", "judge": "deep", "frontier": "deep"}


def _resolved_tier(tier_overrides: dict, defaults: dict, role: str) -> str:
    """A role's tier: its own `tier_overrides` entry, else the profile's
    `defaults` entry, else `"standard"` — the chain `_bounds_ceiling_for`
    and `_router_policy_for` both resolve a role's tier through. An entry
    may name a tier or a capability class; the result is always a tier."""
    raw = tier_overrides.get(role, defaults.get(role, "standard"))
    tier = _CLASS_TIER.get(raw, raw) if isinstance(raw, str) else None
    return tier if tier in _CLASS_TIER.values() else "standard"


def _stats_bounds(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.bounds_report(_stats_fetch(conn, "calls"), _bounds_ceiling_for(a))
    finally:
        conn.close()
    if a.level:
        drop = {"strict", "moderate", "liberal"} - {a.level}
        report = [{k: v for k, v in row.items() if k not in drop} for row in report]
    if a.write:
        write_path = Path(a.write).resolve()
        repo_root = Path.cwd().resolve()
        if write_path != repo_root and repo_root not in write_path.parents:
            print(f"stats bounds: --write path {write_path} is outside the repo checkout {repo_root}")
            return 2
        generated = datetime.datetime.now(datetime.UTC).isoformat()
        write_path.write_text(json.dumps({"generated": generated, "db": a.db, "rows": report}, indent=2))
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _router_policy_for(a: argparse.Namespace, role: str) -> dict | None:
    """Routing inputs for `role`: `policy["mode"]` is the routing profile's
    own `router` key (off|shadow|on), defaulting to `"off"` when absent
    since router-steward.md §1 names the three values but not a default.
    `policy["default_tier"]` resolves the same chain `_bounds_ceiling_for`
    resolves a role's tier through: `tier_overrides`, then `defaults`, then
    `"standard"`. `None` when the routing profile at `_profile_path(a)` or
    its `provider_profile` is missing or unparseable, so the caller can
    refuse rather than invent a policy."""
    text = _read_text_or_none(_profile_path(a))
    try:
        routing_profile = route.parse_profile(text) if text is not None else None
    except route.ProfileError:
        routing_profile = None
    if routing_profile is None:
        return None
    provider_path = routing_profile.get("provider_profile")
    provider_text = _read_text_or_none(Path(provider_path).expanduser()) if provider_path else None
    try:
        provider_profile = yaml.safe_load(provider_text) if provider_text is not None else None
    except yaml.YAMLError:
        provider_profile = None
    if not isinstance(provider_profile, dict):
        return None
    tier_overrides = provider_profile.get("tier_overrides") or {}
    defaults = provider_profile.get("defaults") or {}
    return {
        "mode": routing_profile.get("router", "off"),
        "default_tier": _resolved_tier(tier_overrides, defaults, role),
        "min_n": 20,
        "deviation_floor": 0.70,
        "challenger_n": 10,
    }


def _stats_window_from_report(report: list[dict], role: str) -> dict:
    """`stats_window` for `role`, sliced from `stats_query.roles_report`'s
    existing output rather than a new aggregation: `n` sums `n_calls`
    across every row for the role, challenger rows included
    (router-steward.md §1: "n counts every call of that role in the
    window, challenger calls included, so the section 2 schedule lands on
    every Nth call"). `landed_rate` is the role's non-challenger rows'
    `landed_rate` weighted by `n_calls`, 0.0 when none carry one
    (router-steward.md §3 excludes challenger rows from that aggregate).
    `start`/`end` are `None`: no `roles_report` row carries a window
    timestamp, and `select_tier` never reads either field."""
    rows = [row for row in report if row["role"] == role]
    n = sum(row["n_calls"] for row in rows)
    floor_rows = [row for row in rows if not row["challenger"]]
    rated = [row for row in floor_rows if row["landed_rate"] is not None]
    weight = sum(row["n_calls"] for row in rated)
    landed_rate = sum(row["landed_rate"] * row["n_calls"] for row in rated) / weight if weight else 0.0
    return {"landed_rate": landed_rate, "n": n, "start": None, "end": None}


def _router_stats_window_for(a: argparse.Namespace, role: str) -> dict:
    """The edge for `_stats_window_from_report`: opens `a.db` and fetches
    the same `roles_report` inputs `cox stats roles` reads, no new
    aggregation added to `stats_query.py`."""
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.roles_report(_stats_fetch(conn, "calls"), _stats_fetch(conn, "tasks"), _stats_fetch(conn, "runs"))
    finally:
        conn.close()
    return _stats_window_from_report(report, role)


def _router_select(a: argparse.Namespace) -> int:
    """`cox router select` — the CLI edge of docs/design/router-steward.md:
    `router.select_tier` stays pure; every read happens here. `off` returns
    the floor without calling `select_tier`. `shadow` calls it, prints the
    would-be decision marked as not authoritative, and still returns the
    floor as the effective answer. `on` calls it and returns its tier as
    the effective, authoritative answer."""
    policy = _router_policy_for(a, a.role)
    if policy is None:
        print(f"router select: no usable profile at {_profile_path(a)}")
        return 2
    mode = policy["mode"]
    floor = policy["default_tier"]
    if mode == "off":
        effective, reason = floor, "off"
    else:
        stats_window = _router_stats_window_for(a, a.role)
        tier, tier_reason = router.select_tier(a.role, stats_window, policy)
        if mode == "shadow":
            print(f"router select: shadow tier={tier} reason={tier_reason} (not authoritative; effective stays {floor})")
            effective, reason = floor, "shadow"
        else:
            effective, reason = tier, tier_reason
    if a.json:
        print(json.dumps({"role": a.role, "mode": mode, "effective_tier": effective, "reason": reason}))
    else:
        print(f"router select: role={a.role} mode={mode} effective_tier={effective} reason={reason}")
    return 0


def _steward_recent_run_ids(pair_calls: list[dict], run_span: dict, *, day_cap: int, run_cap: int) -> set:
    """Run ids for `pair_calls`, newest-first by `ended_at` (falling back to
    `started_at`), kept up to `run_cap` runs or until a run's `started_at`
    falls more than `day_cap` days before the newest kept run's own end —
    whichever limit is hit first (router-steward.md §5: "a window capped at
    14 days or 20 runs, whichever completes first"). Measured from the
    pair's own latest run, never the wall clock, so a pair that has simply
    existed a long time is not disqualified by history it no longer needs."""
    ordered = sorted(
        {c["run_id"] for c in pair_calls if c.get("run_id") in run_span},
        key=lambda rid: run_span[rid][1] or run_span[rid][0] or "",
        reverse=True,
    )
    if not ordered:
        return set()
    newest_end = run_span[ordered[0]][1] or run_span[ordered[0]][0]
    cutoff = datetime.datetime.fromisoformat(newest_end) - datetime.timedelta(days=day_cap) if newest_end else None
    kept = []
    for rid in ordered:
        if len(kept) >= run_cap:
            break
        started_at = run_span[rid][0]
        if cutoff is not None and started_at is not None and datetime.datetime.fromisoformat(started_at) < cutoff:
            break
        kept.append(rid)
    return set(kept)


def _steward_window_days_for(run_ids: set, run_span: dict) -> int:
    """Days between the earliest `started_at` and the latest `ended_at`
    among `run_ids`; `0` when neither parses."""
    starts = [run_span[rid][0] for rid in run_ids if run_span.get(rid, (None, None))[0]]
    ends = [run_span[rid][1] for rid in run_ids if run_span.get(rid, (None, None))[1]]
    if not starts or not ends:
        return 0
    start = datetime.datetime.fromisoformat(min(starts))
    end = datetime.datetime.fromisoformat(max(ends))
    return (end - start).days


def _steward_candidate_rows(bounds: list[dict], calls: list[dict], tasks: list[dict], runs: list[dict]) -> list[dict]:
    """Per (role, model) in `bounds`: `stats_query.roles_report`'s
    challenger/floor split and `n_calls`, recomputed over only that pair's
    own most recent 20 runs or 14 days (`_steward_recent_run_ids`) rather
    than its whole history, joined onto `bound`'s ceiling/censored/strict/
    moderate/liberal. A pair with no runs in that window, no challenger
    row, no floor row, or either row's `landed_rate` is dropped: the
    evidence bar cannot score a delta it cannot compute. Pure: no file, no
    db connection, no clock read — `calls`/`tasks`/`runs` arrive already
    fetched."""
    run_span = {r["run_id"]: (r.get("started_at"), r.get("ended_at")) for r in runs}
    rows = []
    for bound in bounds:
        role, model = bound["role"], bound["model"]
        pair_calls = [c for c in calls if c.get("role") == role and c.get("model") == model]
        recent_run_ids = _steward_recent_run_ids(pair_calls, run_span, day_cap=14, run_cap=20)
        if not recent_run_ids:
            continue
        recent_calls = [c for c in pair_calls if c.get("run_id") in recent_run_ids]
        recent_tasks = [t for t in tasks if t.get("run_id") in recent_run_ids]
        windowed = stats_query.roles_report(recent_calls, recent_tasks, runs)
        challenger_row = next((r for r in windowed if r["role"] == role and r["model"] == model and r["challenger"]), None)
        floor_row = next((r for r in windowed if r["role"] == role and r["model"] == model and not r["challenger"]), None)
        if challenger_row is None or floor_row is None:
            continue
        if challenger_row["landed_rate"] is None or floor_row["landed_rate"] is None:
            continue
        rows.append({
            **bound,
            "n_challenger": challenger_row["n_calls"],
            "landed_rate_challenger": challenger_row["landed_rate"],
            "landed_rate_floor": floor_row["landed_rate"],
            "window_days": _steward_window_days_for(recent_run_ids, run_span),
        })
    return rows


def _steward_bounds_rows_for(a: argparse.Namespace) -> list[dict]:
    """Edge for `_steward_candidate_rows`: opens `a.db` once, fetches
    `calls`/`tasks`/`runs` and the read-only ceiling from
    `_bounds_ceiling_for`, then hands everything to the pure join. Writes
    nothing."""
    conn = stats_schema.connect(a.db)
    try:
        calls = _stats_fetch(conn, "calls")
        tasks = _stats_fetch(conn, "tasks")
        runs = _stats_fetch(conn, "runs")
    finally:
        conn.close()
    bounds = stats_query.bounds_report(calls, _bounds_ceiling_for(a))
    return _steward_candidate_rows(bounds, calls, tasks, runs)


def _steward_propose(a: argparse.Namespace) -> int:
    """`cox steward propose` — the CLI edge of docs/design/router-steward.md
    §4: reads `a.db` and the routing/provider profile read-only to build
    candidate rows, then writes each candidate that clears §5's bar as a
    new intake file via the same writer `route file --intake` uses. It
    never opens a provider profile to write it; its only writes are new
    files under `workspace_dir/intake`."""
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    ws = Path(profile["workspace_dir"]).expanduser()
    policy = {"min_n": 20, "window_days_cap": 14, "landed_rate_delta_floor": 0.05}
    candidates = steward.ceiling_candidates(_steward_bounds_rows_for(a), policy)
    if not candidates:
        if a.json:
            print(json.dumps([]))
        else:
            print("steward propose: [] (no candidate cleared the evidence bar)")
        return 0
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    ids = profile.get("ids", "slug")
    mapping: dict[str, str] = {}
    for candidate in candidates:
        title = f"steward: {candidate['direction']} {candidate['role']}'s ceiling for {candidate['model']}"
        mapping.update(route.intake_file(title, steward.render_proposal(candidate), "coxswain", date, ids=ids))
    targets = {rel: ws / rel for rel in mapping}
    existing = [str(path) for path in targets.values() if path.exists()]
    if existing:
        print(f"routing: refusing to overwrite existing path(s): {', '.join(existing)}")
        return 2
    for rel, path in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mapping[rel], encoding="utf-8")
    queue_export.record_written(ws, mapping)
    written = [str(targets[rel]) for rel in sorted(mapping)]
    print(json.dumps(written) if a.json else "\n".join(written))
    return 0


def _steward_draft(a: argparse.Namespace) -> int:
    """`cox steward draft` — writes grounded steward proposals as draft initiatives; see steward_draft."""
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    return steward_draft.run_draft(Path(profile["workspace_dir"]).expanduser(), as_json=a.json)


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
    `run_store.resolve_id` maps either to the same run record. Falls back to `token` unchanged when the
    store or the harness cannot resolve it, so an unresolvable token still reaches the command as before."""
    record = run_store.resolve_id(runs_dir, token)
    return token if record is None else record.get("run_id", token)


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
    return subprocess.run(argv).returncode == 0


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
    but refused or errored -- and `NotAvailable` -- no harness to run it -- both print
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
        message = f"failed {run_id}: the harness is not installed"
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
    store_url = run_store._store_url(runs_dir)
    return _runs_pause_resume_outcome("paused", run_id, store_cli.pause(run_id, reason=a.reason, store_url=store_url), a.json)


def _runs_resume(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    run_id = _resolved_run_id(runs_dir, a.run_id)
    store_url = run_store._store_url(runs_dir)
    return _runs_pause_resume_outcome("resumed", run_id, store_cli.resume(run_id, store_url=store_url), a.json)


def _runs_move_pause(run_id: str, reason: str | None) -> tuple[bool, str]:
    """Edge: `cox runs move`'s pause step, over the same `store_cli.pause` result types
    `_runs_pause_resume_outcome` already maps."""
    result = store_cli.pause(run_id, reason=reason)
    if isinstance(result, store_cli.Paused):
        return True, ""
    if isinstance(result, store_cli.PauseRefused):
        return False, "not in store"
    if isinstance(result, store_cli.Failed):
        return False, result.detail
    return False, "the harness is not installed"


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
            {**item, "run": max(runs, key=chair_facts._run_number), "land_at": land_at.get(initiative, "initiative")}
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
        return _runs_move_pause(step["run"], step.get("reason"))
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


def _runs_dir_for_land(a: argparse.Namespace) -> tuple[Path | None, str | None]:
    """`--runs-dir` overrides; otherwise `runs/` under the profile's `workspace_dir`."""
    if a.runs_dir:
        return Path(a.runs_dir).expanduser(), None
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        return None, f"no profile at {profile_path}"
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return None, f"profile unreadable: {exc}"
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        return None, f"workspace_dir not set in profile {profile_path}"
    return Path(workspace).expanduser() / "runs", None


def _land_record(runs_dir: Path, run_id: str, task: str | None) -> tuple[dict | None, str, int]:
    """The one record file for `task` (any task when None). A named task with no file under the land's own run takes the
    newest earlier run's, the order `_land_phase_record` uses; several files in one run stay ambiguous."""
    tasks_root = runs_dir / run_id / "tasks"
    matches = sorted(tasks_root.glob(f"*/{task}.json" if task else "*/*.json"))
    if not matches and task:
        matches = next((m for r in _earlier_runs(run_id) if (m := sorted((runs_dir / r / "tasks").glob(f"*/{task}.json")))), [])
    if len(matches) != 1:
        return None, str(tasks_root.resolve()), len(matches)
    path = matches[0]
    record = json.loads(path.read_text(encoding="utf-8"))
    record.setdefault("run", path.parents[2].name)
    record.setdefault("task", path.stem)
    record.setdefault("phase", path.parent.name)
    return record, str(path), 1


def _choose_record(stored: dict | None, filed: dict | None) -> tuple[dict | None, str | None]:
    """The store record wins over the file record; the label is `store` or `file`, None when neither exists."""
    if stored is not None:
        return stored, "store"
    if filed is not None:
        return filed, "file"
    return None, None


def _land_load(runs_dir: Path, run_id: str, task: str | None) -> tuple[dict | None, str, int, str | None]:
    """Edge: `(record, where, count, source)`, the store first and the file when the store says None.

    The store is asked by phase when exactly one file names the task, whose parent dir is the phase and stem the task.
    With no file and a task given, it is asked which phases hold the task; exactly one phase names a record
    that has no file yet, and `where` is the path that file would have.
    """
    filed, where, count = _land_record(runs_dir, run_id, task)
    if count == 0 and task:
        phases = run_store.task_record_phases(runs_dir, run_id, task)
        held = run_store.task_record(runs_dir, run_id, phases[0], task) if len(phases) == 1 else None
        if held is not None:
            path = runs_dir / run_id / "tasks" / phases[0] / f"{task}.json"
            return {"run": run_id, "task": task, "phase": phases[0], **held}, str(path), 1, "store"
        return None, where, count, None
    path = Path(where)
    filed_run = path.parents[2].name if count == 1 else run_id  # an earlier run's record is read from that run
    stored = run_store.task_record(runs_dir, filed_run, path.parent.name, path.stem) if count == 1 else None
    if stored is not None:
        stored = {"run": filed_run, "task": path.stem, "phase": path.parent.name, **stored}
    record, source = _choose_record(stored, filed)
    return record, where, count, source


def _initiative_of(work_root: Path, task_id: str) -> str | None:
    """The one initiative whose `work/<initiative>/<phase>/*.md` holds an item
    with this id (`id` falls back to the file stem, as `route.work_item` does),
    or `None` when none or several do. The task record never names it."""
    found = set()
    for p in work_root.glob("*/*/*.md"):
        text = _read_text_or_none(p)
        if text is None:
            continue
        item = route.work_item(route.parse_frontmatter(text)[0], initiative=p.parent.parent.name, phase_dir=p.parent.name, stem=p.stem)
        if item["id"] == task_id:
            found.add(item["initiative"])
    return found.pop() if len(found) == 1 else None


def _initiative_of_phase(work_root: Path, phase: str, run_id: str | None = None) -> str | None:
    """The initiative whose `work/<initiative>/<phase>/` holds a ticket: the run's own (`x-3` is `x`) when it does,
    else the one initiative that does, else `None`. The phase record may not name it, and a phase name such as `fix`
    repeats across initiatives."""
    own = chair_facts.run_initiative(run_id) if run_id else None
    if own and any(p.name != "initiative.md" for p in (work_root / own / phase).glob("*.md")):
        return own
    found = {p.parent.parent.name for p in work_root.glob(f"*/{phase}/*.md") if p.name != "initiative.md"}
    return found.pop() if len(found) == 1 else None


def _initiative_title(work_root: Path, initiative: str) -> str:
    """Edge. `work_root/<initiative>/initiative.md`'s own `title:`; "" when the file is missing, unreadable,
    or carries no title -- the phase land PR title then falls back to its id-and-phase-only form."""
    try:
        text = (work_root / initiative / "initiative.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    value = route.parse_frontmatter(text)[0].get("title", "")
    return value if isinstance(value, str) else ""


def _land_branches(repo: Path, record: dict, default_branch: str) -> dict[str, list[str]]:
    """Commit subjects ahead of `default_branch`, per candidate branch, with
    merge commits excluded by `git` (`--no-merges`) and patch-equivalent
    commits already on `default_branch` dropped (`--cherry-pick`)."""
    candidates = [f"agents/{record['run']}/{record['task']}", f"epic/{record.get('initiative')}/{record['phase']}"]
    branches: dict[str, list[str]] = {}
    for b in candidates:
        out = subprocess.run(["git", "-C", str(repo), "log", "--no-merges", "--cherry-pick", "--right-only", "--format=%s", f"{default_branch}...{b}"],
                             capture_output=True, text=True)
        if out.returncode == 0:
            branches[b] = [line for line in out.stdout.splitlines() if line]
    return branches


def _land_branch_counts(repo: Path, record: dict, default_branch: str, branches: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """Edge. Per candidate branch git resolves, `ahead` and `behind` from `rev-list --left-right --count
    <base>...<branch>` (raw, patch-equivalent commits included) and `unique`, the commits `_land_branches` kept."""
    counts: dict[str, dict[str, int]] = {}
    for b in branches:
        out = subprocess.run(["git", "-C", str(repo), "rev-list", "--left-right", "--count", f"{default_branch}...{b}"],
                             capture_output=True, text=True)
        behind, _, ahead = out.stdout.partition("\t")
        if out.returncode == 0 and behind.strip().isdigit() and ahead.strip().isdigit():
            counts[b] = {"ahead": int(ahead), "behind": int(behind), "unique": len(branches[b])}
    return counts


def _phase_done_fact(statuses: Sequence[str], validated: bool) -> bool:
    return phase_status.phase_status(statuses, validated) == "done"


def _land_phase_done(runs_dir: Path, run_id: str, record: dict) -> bool:
    """Edge. Is the task's phase done: its tickets all settled and its manifest's `phase_verdict.goal_met` true
    (the key `phase_validate` writes). A missing initiative, unreadable items or no manifest give False.
    Items come from the files, never the store: the fact only orders the branch choice, and a store read here
    would add a `task_state` call ahead of the lease's own re-read."""
    initiative, phase = record.get("initiative"), record.get("phase")
    if not initiative or not phase:
        return False
    items, _ = _phase_items(runs_dir.parent / "work", initiative, phase, runs_dir=runs_dir, land_mode="files")
    if items is None:
        return False
    try:
        manifest = _land_phase_record(runs_dir, run_id, phase)[0] or {}
    except (OSError, ValueError):
        return False
    validated = (manifest.get("phase_verdict") or {}).get("goal_met") is True
    return _phase_done_fact([str(i.get("status")) for i in items], validated)


def _phase_task_facts(repo: Path, phase_branch: str, items: list[dict], task_records: list[dict]) -> list[dict] | None:
    """Edge. Per approved task, the facts `land.task_gather_status` reads: its `agents/<run>/<task>` commit is an
    ancestor of `phase_branch`, that branch exists, the record carries a non-blank `build.patch`. A task with none
    of those but a `build.files_touched` counts as reachable. None when
    `phase_branch` does not resolve, so a land with no phase branch to compare against plans as it always did."""
    def git_ok(*args: str) -> bool:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).returncode == 0
    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{phase_branch}"):
        return None
    approved = {i["id"] for i in items if i.get("status") == "approved"}
    facts = []
    for record in task_records:
        task = record.get("task")
        if task not in approved:
            continue
        branch = f"agents/{record.get('run')}/{task}"
        exists = git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        build = record.get("build") or {}
        has_patch = isinstance(build.get("patch"), str) and bool(build["patch"].strip())
        # No branch and no patch but recorded files: the squash's files check, not this plan, says if they landed.
        deferred = not exists and not has_patch and bool(build.get("files_touched"))
        facts.append({"task": task, "branch_exists": exists, "has_patch": has_patch,
                      "reachable": deferred or (exists and git_ok("merge-base", "--is-ancestor", branch, phase_branch))})
    return facts


def _recover_branches(repo: Path, record: dict, phase_branch: str) -> dict[str, list[str]]:
    """Commit subjects ahead of the phase branch, per recover candidate
    branch — the same `git log --no-merges` shape `_land_branches` uses, but
    against the phase branch instead of the default branch, since recover
    merges INTO the phase branch rather than cherry-picking off it."""
    candidates = [f"agents/{record['run']}/{record['task']}", f"epic/{record.get('initiative')}/{record['phase']}--{record['task']}"]
    branches: dict[str, list[str]] = {}
    for b in candidates:
        out = subprocess.run(["git", "-C", str(repo), "log", "--no-merges", "--format=%s", f"{phase_branch}..{b}"], capture_output=True, text=True)
        if out.returncode == 0:
            branches[b] = [line for line in out.stdout.splitlines() if line]
    return branches


def _branch_exists(repo: Path, branch: str) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch], capture_output=True, text=True)
    return r.returncode == 0


def _git_out(repo: Path, *args: str) -> str | None:
    """Stripped stdout of `git -C repo <args>`, or `None` when git exits non-zero."""
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _open_prs_for(repo: Path, branch: str, forge_module=forge_github) -> list[int] | str:
    return forge_module.find_open_prs(repo, branch)


def _phase_adds_nothing(repo: Path, phase_branch: str, default_branch: str) -> bool:
    """Edge. True when merging `phase_branch` into origin's `default_branch` changes nothing, so its work is already there."""
    ref = f"refs/remotes/origin/{default_branch}"
    fetched = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", f"+refs/heads/{default_branch}:{ref}"], capture_output=True, text=True)
    tree = _git_out(repo, "merge-tree", "--write-tree", ref, phase_branch) if fetched.returncode == 0 else None
    return tree is not None and tree.splitlines()[0] == _git_out(repo, "rev-parse", f"{ref}^{{tree}}")


def _phase_already_merged(prs: list[dict] | str, branch_exists: bool, adds_nothing: bool) -> bool:
    """Pure. A PR of the phase's PR branch merged, none is open, and the surviving phase branch adds nothing over main; `prs` is a reason string when unread."""
    return branch_exists and adds_nothing and not isinstance(prs, str) and any(p.get("state") == "merged" for p in prs) and not any(p.get("state") == "open" for p in prs)


ALREADY_MERGED_PREFIX = "merge: already merged"


def already_merged_line(pr: dict) -> str:
    """Pure. The one line a phase land prints when it skips the merge because the PR is already merged. `chair_exec.landed` reads the prefix."""
    return f"{ALREADY_MERGED_PREFIX} (PR #{pr.get('number')})"


def _carried_files(items: list[dict], task_records: list[dict]) -> list[str]:
    """Pure. Sorted files of the approved tasks, from each record's `build.files_touched`; the items carry no files."""
    approved = {i["id"] for i in items if i.get("status") == "approved"}
    return sorted({f for r in task_records if r.get("task") in approved for f in (r.get("build") or {}).get("files_touched") or []})


def _main_subjects_touching(repo: Path, default_branch: str, branch: str, files: list[str]) -> list[str]:
    """Edge. Subjects, newest first, of commits on the fetched default branch that touched `files` and left them as `branch` holds them.
    A later commit that changed those files again does not match, so it cannot lend its PR number to this branch."""
    log = _git_out(repo, "log", "--max-count=20", "--format=%H %s", f"refs/remotes/origin/{default_branch}", "--", *files) if files else None
    same = lambda sha: subprocess.run(["git", "-C", str(repo), "diff", "--quiet", sha, branch, "--", *files], capture_output=True).returncode == 0  # noqa: E731
    return [line.split(" ", 1)[1] for line in (log or "").splitlines() if " " in line and same(line.split(" ", 1)[0])]


def _branch_has_own_commits(repo: Path, branch: str, default_branch: str) -> bool:
    """Edge. True when `branch` holds a commit the fetched default branch does not reach; a branch level with main holds none."""
    count = _git_out(repo, "rev-list", "--count", f"refs/remotes/origin/{default_branch}..{branch}")
    return count is not None and int(count) > 0


def _parent_merged_pr(repo: Path, initiative: str, parent: str | None, forge_module) -> int | None:
    """Edge. Number of the first merged PR of the parent phase's PR branch, None without a parent or a merged PR."""
    prs = forge.branch_prs(forge_module, repo, f"pr/{initiative}--{parent}") if parent else []
    return None if isinstance(prs, str) else next((p["number"] for p in prs if p.get("state") == "merged"), None)


def _phase_merged_pr(repo: Path, initiative: str, phase: str, default_branch: str, forge_module,
                     files: list[str], parent: str | None) -> dict | None:
    """Edge. The first merged PR of `pr/<initiative>--<phase>` when `_phase_already_merged` holds, else a `carried` dict
    for the PR that put an unclaimed, non-empty branch onto main (see `_main_subjects_touching`, `_parent_merged_pr`); None without a number.
    With no `files` and no `parent` there is nothing to find a carrier by, so only the own-PR case can hold."""
    branch = f"epic/{initiative}/{phase}"
    exists = _branch_exists(repo, f"refs/heads/{branch}")
    prs = forge.branch_prs(forge_module, repo, f"pr/{initiative}--{phase}")
    adds_nothing = exists and _phase_adds_nothing(repo, branch, default_branch)
    if _phase_already_merged(prs, exists, adds_nothing):
        return next(p for p in prs if p.get("state") == "merged")
    unclaimed = not isinstance(prs, str) and not any(p.get("state") in ("merged", "open") for p in prs)
    if not (unclaimed and adds_nothing and (files or parent) and _branch_has_own_commits(repo, branch, default_branch)):
        return None
    subjects = _main_subjects_touching(repo, default_branch, branch, files)
    named = any(land.pr_number_of_subject(s) is not None for s in subjects)
    number = land.carrying_pr(subjects, None if named else _parent_merged_pr(repo, initiative, parent, forge_module))
    return None if number is None else {"number": number, "state": "merged", "carried": True}


def _task_already_on_main(repo: Path, step: dict, record: dict | None) -> int | None:
    """Edge. The PR number that carried a task branch onto the default branch, or None to run the step as planned.
    Only the task form of `cherry_pick` (no `task` key) with files on its record qualifies; the branch must add
    nothing over `from` yet hold a commit the fetched default branch lacks, and a subject must name the PR."""
    files = ((record or {}).get("build") or {}).get("files_touched") or []
    if step.get("kind") != "cherry_pick" or "task" in step or not files:
        return None
    if not (_phase_adds_nothing(repo, step["branch"], step["from"]) and _branch_has_own_commits(repo, step["branch"], step["from"])):
        return None
    return land.carrying_pr(_main_subjects_touching(repo, step["from"], step["branch"], files), None)


def _carried_walk(steps: list[dict], planned: list[dict], number: int) -> tuple[list[dict], list[dict], str] | None:
    """Pure. The closing steps of `steps` and of `planned`, and the running `pr`, for a task main already carries.
    None when the gated steps keep no closing step, so the walk runs as planned."""
    tail = land.landed_tail(steps)
    return (tail, land.landed_tail(planned), f"#{number}") if tail else None


def _carried_pr(merged_pr: dict | None) -> str:
    """Pure. The running `pr` a walk starts from: `#<n>` for a carried phase, `""` otherwise."""
    return f"#{merged_pr['number']}" if merged_pr is not None and merged_pr.get("carried") else ""


def _merged_pr_ref(merged_pr: dict | None) -> str:
    """Pure. The `pr` a mark_done records for a merged PR: its url when the forge returned one, else `#<n>`, else `""`."""
    if merged_pr is None:
        return ""
    return str(merged_pr.get("url") or (f"#{merged_pr['number']}" if merged_pr.get("number") is not None else ""))


def _merged_line(merged_pr: dict) -> str:
    """Pure. The report line for a phase land that skips the merge: `landed via PR #n` when main carried it, else the already-merged line."""
    return land.landed_via_line(_carried_pr(merged_pr)) if merged_pr.get("carried") else already_merged_line(merged_pr)


def _phase_already_landed(repo: Path, initiative: str, phase: str, default_branch: str, forge_module) -> bool:
    """Edge. True when `_phase_merged_pr` finds a merged PR."""
    return _phase_merged_pr(repo, initiative, phase, default_branch, forge_module, [], None) is not None


def _phase_pr_facts(repo: Path, pr_branch: str, forge_module=forge_github) -> dict | str:
    """Edge. The plain facts `land.phase_resume` decides on, or the reason the open PRs could not be listed.
    The PR head is read as the fetched `origin/<pr_branch>` tip: the forge call returns PR numbers only,
    and a PR from this repo's own branch has exactly that head."""
    local = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{pr_branch}")
    fetched = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", f"+refs/heads/{pr_branch}:refs/remotes/origin/{pr_branch}"],
                             capture_output=True, text=True).returncode == 0
    remote = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{pr_branch}") if fetched else None
    prs = _open_prs_for(repo, pr_branch, forge_module)
    if isinstance(prs, str):
        return prs
    return {"local_tip": local, "remote_tip": remote, "pr": prs[0] if prs else None, "pr_head": remote if prs else None}


def _land_resume(repo: Path, cherry_pick: dict, forge_module=forge_github) -> dict:
    """`land.resume_decision` for the `pr/<task>` branch a `cherry_pick` step
    would create. The expected tree is what cherry-picking the one commit onto
    `from` yields, computed by `git merge-tree` without touching the checkout.
    Open PRs are asked for whenever a branch exists; one is adopted whatever the trees."""
    branch, base = cherry_pick["onto"], cherry_pick["from"]
    local = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{tree}}")
    fetched = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                             capture_output=True, text=True).returncode == 0
    remote = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{tree}}") if fetched else None
    if local is None and remote is None:
        return land.resume_decision("", None, None, [])
    prs = _open_prs_for(repo, branch, forge_module)
    if isinstance(prs, str):
        return {"kind": "refuse", "reason": prs}
    if prs:
        return land.resume_decision("", local, remote, prs)
    shas = (_git_out(repo, "rev-list", f"{base}..{cherry_pick['branch']}") or "").split()
    merged = _git_out(repo, "merge-tree", "--write-tree", f"--merge-base={shas[0]}^", base, shas[0]) if len(shas) == 1 else None
    expected = merged.splitlines()[0] if merged else None
    if expected is None:
        # Marked so `_land_leftover` can tell this refusal apart: only here does `land.leftover_gate` decide.
        return {"kind": "refuse", "reason": f"cannot compute the cherry-picked tree of {cherry_pick['branch']} onto {base}",
                "uncomputable_tree": True}
    if _git_out(repo, "cat-file", "-e", f"{expected}:.agent-generate") is not None:
        # The pushed tree is the one after generation and the amend, not the bare cherry-pick.
        expected, why = _generated_tree(repo, branch, base, shas[0], cherry_pick.get("umbrella"))
        if expected is None:
            return {"kind": "refuse", "reason": f"cannot compute the generated tree of {cherry_pick['branch']} onto {base}: {why}"}
    existing = [t for t in (local, remote) if t is not None]
    decision = land.resume_decision(expected, local, remote, [])
    if decision["kind"] != "refuse":
        return decision
    files = sorted({f for t in existing if t != expected
                    for f in (_git_out(repo, "diff-tree", "-r", "--name-only", expected, t) or "").split()})
    refused = {**decision, "reason": decision["reason"] + (f"; files differing: {', '.join(files)}" if files else "")}
    # Carried for `_land_leftover`, so it need not run the generator a second time.
    return {**refused, "local_tree": local, "remote_tree": remote, "expected_tree": expected} if any(t != expected for t in existing) else refused


def _remote_branch_exists(repo: Path, branch: str) -> bool:
    """Edge. Whether `origin` has `branch`. Fails closed: only a repo with no `origin`, or `ls-remote` exiting 2
    (reached the remote, no such ref), says absent. A failed fetch is not absence, and absence leads to a delete."""
    if _git_out(repo, "config", "--get", "remote.origin.url") is None:
        return False
    listed = subprocess.run(["git", "-C", str(repo), "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
                            capture_output=True, text=True)
    return listed.returncode != 2


def _branch_checked_out(repo: Path, branch: str) -> bool:
    """Edge. Whether any worktree has `branch` checked out. Its uncommitted state is not the land's to discard."""
    listing = _git_out(repo, "worktree", "list", "--porcelain")
    return listing is None or f"branch refs/heads/{branch}" in listing.splitlines()


def _open_pr_exists(repo: Path, branch: str, forge_module=forge_github) -> bool | str:
    """Edge. Whether an open pull request has `branch` as its head, or the reason the forge could not say."""
    prs = _open_prs_for(repo, branch, forge_module)
    return prs if isinstance(prs, str) else bool(prs)


def _leftover_gate_facts(repo: Path, branch: str, base: str, forge_module=forge_github) -> dict | None:
    """Edge, read-only. The keyword facts `land.leftover_gate` decides on for `branch` against `base`, or None when
    they cannot be read: the forge could not list open PRs, or a worktree has the branch checked out."""
    prs = _open_prs_for(repo, branch, forge_module)
    if isinstance(prs, str) or _branch_checked_out(repo, branch):
        return None
    local_tip = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    ancestor = (subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", local_tip, base],
                               capture_output=True, text=True).returncode == 0) if local_tip else None
    count = _git_out(repo, "rev-list", "--count", f"{base}..{local_tip}") if local_tip else None
    return {"local_tip": local_tip, "is_ancestor_of_main": ancestor, "unique_commits": int(count) if count else None,
            "remote_exists": _remote_branch_exists(repo, branch), "open_pr_number": prs[0] if prs else None}


def _land_uncomputable(repo: Path, cherry_pick: dict, decision: dict, forge_module=forge_github) -> dict:
    """Edge, read-only. `land.leftover_gate` over the `_land_resume` refusal that could not compute the cherry-picked
    tree. A `recreate` becomes a `fresh` carrying `old_tip` and the refusal's reason, for `_land_recreate` to act on
    under the repo lease; a `refuse` carries the gate's reason; no decision, or unreadable facts, leave it unchanged."""
    branch = cherry_pick["onto"]
    facts = _leftover_gate_facts(repo, branch, cherry_pick["from"], forge_module)
    gated = land.leftover_gate(branch, **facts) if facts is not None else None
    if gated is None:
        return decision
    if gated["kind"] == "refuse":
        return {"kind": "refuse", "reason": gated["reason"]}
    return {"kind": "fresh", "old_tip": gated["old_tip"], "reason": decision["reason"]}


def _land_leftover(repo: Path, cherry_pick: dict, decision: dict, forge_module=forge_github) -> dict:
    """Edge, read-only. A `_land_resume` refusal over a `pr/<initiative>--<phase>` whose local or pushed tree differs,
    that has no open PR and that no worktree has checked out, is a leftover. `land.leftover_branch_decision` then
    turns it into a `fresh` carrying its `backup_refs`, both tips and the refusal's reason, for `_land_rebuild` to
    act on under the repo lease. The refusal that could not compute the tree goes to `_land_uncomputable` instead.
    Any other decision, or a remote the fetch and `ls-remote` disagree about, comes back unchanged."""
    if decision["kind"] == "refuse" and decision.get("uncomputable_tree"):
        return _land_uncomputable(repo, cherry_pick, decision, forge_module)
    if decision["kind"] != "refuse" or "expected_tree" not in decision:
        return decision
    branch = cherry_pick["onto"]
    open_pr = _open_pr_exists(repo, branch, forge_module)
    local_tip = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    remote_tip = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}") if decision["remote_tree"] else None
    remote_exists = _remote_branch_exists(repo, branch)
    if (isinstance(open_pr, str) or (local_tip is None) != (decision["local_tree"] is None)
            or remote_exists != (remote_tip is not None) or _branch_checked_out(repo, branch)):
        return decision
    leftover = land.leftover_branch_decision(branch, remote_exists, open_pr, decision["local_tree"], decision["expected_tree"],
                                             local_commit=local_tip, remote_commit=remote_tip, remote_tree=decision["remote_tree"])
    if leftover["kind"] != "back_up_and_rebuild":
        return decision
    return {**leftover, "kind": "fresh", "reason": decision["reason"]}


def _git_failure(repo: Path, *args: str) -> str | None:
    """Edge. Run a git command that writes; its stderr when it exits non-zero, else None."""
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return None if done.returncode == 0 else done.stderr.strip() or f"git {args[0]} exited {done.returncode}"


def _land_push(repo: Path, branch: str, forge_module=forge_github) -> tuple[bool, str]:
    """Edge. Push `branch`. A rebuilt task pushes the name its closed PR left on origin with unrelated history, which
    a plain push rejects; when every PR on that remote branch is closed, replace exactly the tip seen. A branch with
    an open or merged PR, or whose PRs cannot be listed, gets the plain push and git's own refusal."""
    listing = _git_out(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    tip = listing.split()[0] if listing else None
    prs = forge.branch_prs(forge_module, repo, branch) if tip else []
    decision = land.push_lease(tip, [] if isinstance(prs, str) else prs)
    if decision["kind"] != "lease":
        return forge_module.push(repo, branch)
    ok, detail = forge_module.push(repo, branch, expected_tip=decision["tip"])
    closed = ", ".join(f"#{n}" for n in decision["prs"])
    return (True, f"{detail} (replaced {decision['tip'][:8]} left by closed {closed})") if ok else (False, detail)


def _land_rebuild(repo: Path, branch: str, rebuild: dict) -> str | None:
    """Edge. Point each `backup_refs` ref at its tip and push it to origin, a rerun finding the same ref already
    there. Only once every backup is safe, delete the remote `branch` if it still sits at `remote_tip`, then the
    local one if it still sits at `local_tip`. Runs under the repo lease. The reason it stopped, or None."""
    if _branch_checked_out(repo, branch):
        return f"{branch} is now checked out in a worktree"
    has_origin = _git_out(repo, "config", "--get", "remote.origin.url") is not None
    for ref, tip in rebuild["backup_refs"].items():
        failure = _git_failure(repo, "update-ref", ref, tip) or (_git_failure(repo, "push", "--quiet", "origin", f"{ref}:{ref}") if has_origin else None)
        if failure:
            return f"could not back up to {ref}: {failure}"
    local_tip, remote_tip = rebuild["local_tip"], rebuild["remote_tip"]
    if local_tip and _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}") != local_tip:
        return f"{branch} moved off {local_tip[:8]}"
    if remote_tip:
        gone = _git_failure(repo, "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{remote_tip}", "origin", f":refs/heads/{branch}")
        if gone:
            return f"could not delete origin/{branch} at {remote_tip[:8]}: {gone}"
    dropped = _git_failure(repo, "update-ref", "-d", f"refs/heads/{branch}", local_tip) if local_tip else None
    return f"{branch} moved off {local_tip[:8]}: {dropped}" if dropped else None


def _land_recreate(repo: Path, branch: str, old_tip: str) -> str | None:
    """Edge. Delete the local `branch` if it still sits at `old_tip`; one already gone is fine. Runs under the repo
    lease. Touches no remote and writes no backup ref. The reason it stopped, or None."""
    if _branch_checked_out(repo, branch):
        return f"{branch} is now checked out in a worktree"
    tip = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if tip is None:
        return None
    if tip != old_tip:
        return f"{branch} moved off {old_tip[:8]}"
    dropped = _git_failure(repo, "update-ref", "-d", f"refs/heads/{branch}", old_tip)
    return f"{branch} moved off {old_tip[:8]}: {dropped}" if dropped else None


def _land_enrich(steps: list[dict], *, path: str, worktree_root: str, task_paths: dict[str, str] | None = None,
                  item_path: str | None = None, workspace: str | None = None,
                  item_id: str | None = None, profile: str | None = None,
                  runs_dir: str | None = None, umbrella: str | None = None,
                  task_items: dict[str, str] | None = None, store_runs_dir: str | None = None,
                  check_paths: bool = False) -> list[dict]:
    """Steps enriched with what only the edge knows: each `mark_done`'s own
    record file path (`task_paths` maps task name to path in phase mode), and
    the configured worktree root for `clean`/`clean_phase`. `item_path` (task
    mode only) is the work item `mark_done` will also try to close out;
    `task_items` (phase mode) is the same, keyed by task name, since a phase
    closes one item per task rather than a single item. A
    `checks` step that names a `branch` (phase mode) gets no filesystem write
    here — a dry run must stay read-only, so the actual worktree is only ever
    created at execution time, inside `_execute_land_step`, under `--apply`.
    A `route_sync` step gets the `workspace` to sync, the item's own `id`
    (`item_id`, else the task name the plan used), and the land's `profile`
    and `runs_dir`, so the step reads the tracker from where the plan did.
    A `cherry_pick` or `squash_phase` step gets the profile's `umbrella_dir`
    as `umbrella` when set, for `.agent-generate`. A `mark_done` with `recorded` False has no record file: it
    gets its store item and `store_runs_dir` instead of a `path`. With `check_paths`, a `mark_done` whose path is
    not a file becomes that store-only step when it has an item, else a `refuse` step naming the task and the path."""
    def enrich(step: dict) -> dict:
        if step["kind"] in ("cherry_pick", "squash_phase") and umbrella is not None:
            return {**step, "umbrella": umbrella}
        if step["kind"] == "route_sync":
            return {**step, "item": item_id or step["item"], "workspace": workspace,
                    "profile": profile, "runs_dir": runs_dir}
        if step["kind"] == "fetch":
            return {**step, "profile": profile, "runs_dir": runs_dir}
        if step["kind"] == "mark_done" and step.get("recorded") is False:
            item = (task_items or {}).get(step["task"])
            return {**step, "item": item, "from": "approved", "to": "done", "runs_dir": store_runs_dir}
        if step["kind"] == "mark_done":
            resolved = (task_paths or {}).get(step["task"], path)
            item = item_path or (task_items or {}).get(step["task"])
            if check_paths and not Path(resolved).is_file():  # a phase record that lives only in the store
                if item is None:
                    return {"kind": "refuse", "reason": f"{step['task']}: record {resolved} does not exist and the task has no approved item; nothing merged"}
                return {**step, "recorded": False, "item": item, "from": "approved", "to": "done", "runs_dir": store_runs_dir}
            marked = {**step, "path": resolved}
            return {**marked, "item": item, "from": "approved", "to": "done"} if item else marked
        if step["kind"] == "mark_merged":
            return {**step, "path": (task_paths or {}).get(step["task"], path), "runs_dir": store_runs_dir,
                    "item": item_path or (task_items or {}).get(step["task"])}
        if step["kind"] == "phase_done":
            return {**step, "runs_dir": runs_dir or store_runs_dir}
        if step["kind"] in ("clean", "clean_phase"):
            return {**step, "worktree_root": worktree_root}
        return step
    return [enrich(s) for s in steps]


def _with_check_cap(steps: list[dict], cores: int, capacity: int | None) -> list[dict]:
    """Each `checks` step carrying the host's `cores` and the lane `capacity`, for `_execute_land_step` to run its checks with."""
    return [{**s, "cores": cores, "capacity": capacity} if s["kind"] == "checks" else s for s in steps]


def _backfill_phase_task_files(runs_dir: Path, run_id: str, phase: str) -> None:
    """Store mode only: write any task record the store holds for this run and phase, then for each earlier run of
    the same initiative (newest first), that has no file yet under `runs/<run>/tasks/<phase>/`, so
    `_land_phase_record`'s own directory read finds it. Never overwrites a file already on disk."""
    phase_dir = runs_dir / run_id / "tasks" / phase
    for source_run in (run_id, *_earlier_runs(run_id)):
        for task_id in run_store.run_task_ids(runs_dir, source_run):
            path = phase_dir / f"{task_id}.json"
            if path.exists():
                continue
            record = run_store.task_record(runs_dir, source_run, phase, task_id)
            if record is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({**record, "run": source_run}), encoding="utf-8")


def _earlier_runs(run_id: str) -> list[str]:
    """`<initiative>-<n>`'s earlier runs, newest first: a task approved in run 2 and still unlanded when run 3 lands
    the phase has its record only under run 2 (2026-10-06, the-store-holds-every-initiative-and-ticket). Tagging the
    written record with its own run keeps the gather on `agents/<that run>/<task>`. Empty for an unnumbered id."""
    prefix, _, number = run_id.rpartition("-")
    return [f"{prefix}-{n}" for n in range(int(number) - 1, 0, -1)] if prefix and number.isdigit() else []


def _land_phase_record(runs_dir: Path, run_id: str, phase: str) -> tuple[dict | None, list[dict], dict[str, str], str]:
    """The phase record at `runs/<run>:<phase>.json` and every task record
    filed under it, keyed by task name for `mark_done`'s own file path."""
    phase_path = runs_dir / f"{run_id}:{phase}.json"
    phase_record = next((m for m in run_store.phase_manifests(runs_dir, run_id) if m.get("run_id") == f"{run_id}:{phase}"), None)
    if phase_record is None and phase_path.exists():  # a file whose body carries no run_id
        phase_record = json.loads(phase_path.read_text(encoding="utf-8"))
    if phase_record is None:
        return None, [], {}, str(phase_path.resolve())
    phase_record.setdefault("run", run_id)
    phase_record.setdefault("phase", phase)
    task_records, task_paths = [], {}
    for p in sorted((runs_dir / run_id / "tasks" / phase).glob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        r.setdefault("run", run_id); r.setdefault("task", p.stem); r.setdefault("phase", phase)
        task_records.append(r)
        task_paths[r["task"]] = str(p)
    for p in _land_phase_fallback_paths(runs_dir, run_id, phase):
        if p.stem in task_paths:
            continue
        r = json.loads(p.read_text(encoding="utf-8"))
        r.setdefault("task", p.stem); r.setdefault("phase", phase); r.setdefault("run", p.parent.parent.parent.name)
        task_records.append(r)
        task_paths[p.stem] = str(p)
    return phase_record, task_records, task_paths, str(phase_path)


def _land_phase_fallback_paths(runs_dir: Path, run_id: str, phase: str) -> list[Path]:
    """Every earlier run of `run_id`'s own initiative that carries a `tasks/<phase>/` directory,
    newest first, so the caller's own-run-wins, newest-earlier-run-wins search order falls out of
    a plain skip-if-already-seen loop over this list."""
    m = re.search(r"-(\d+)$", run_id)
    if m is None:
        return []
    initiative, own_n = chair_facts.run_initiative(run_id), int(m.group(1))
    sibling_re = re.compile(rf"{re.escape(initiative)}-(\d+)$")
    matches = (sibling_re.fullmatch(d.name) for d in runs_dir.iterdir() if d.is_dir()) if runs_dir.is_dir() else []
    earlier_ns = sorted({int(sm.group(1)) for sm in matches if sm and int(sm.group(1)) < own_n}, reverse=True)
    paths = []
    for n in earlier_ns:
        paths.extend(sorted((runs_dir / f"{initiative}-{n}" / "tasks" / phase).glob("*.json")))
    return paths


def _phase_items(work_root: Path, initiative: str, phase: str, runs_dir: Path | None = None,
                  land_mode: str = "files") -> tuple[list[dict] | None, str]:
    """The phase's own tickets from the work store (`work/<initiative>/<phase>/*.md`), each
    carrying its own `file` path for `mark_done` to close out, or `None` with the directory
    searched when there is nothing there to read — a phase can never be waved through by a
    missing ticket file. `status` comes from `route.work_item`'s parsed `state` in files mode;
    in store mode it comes from the store's own `work_items` row instead (`run_store.task_state_of`),
    `None` when the store holds no row for that task id, which `land.phase_landable` already
    refuses by name."""
    phase_dir = work_root / initiative / phase
    paths = sorted(phase_dir.glob("*.md")) if phase_dir.is_dir() else []
    if not paths:
        return None, str(phase_dir)
    items = []
    for p in paths:
        text = _read_text_or_none(p)
        if text is None:
            continue
        fields = route.parse_frontmatter(text)[0]
        item = route.work_item(fields, initiative=initiative, phase_dir=phase, stem=p.stem)
        status = run_store.task_state_of(runs_dir, initiative, item["id"]) if land_mode == "store" else item["state"]
        items.append({"id": item["id"], "status": status, "file": str(p)})
    return items, str(phase_dir)


def _natural_key(name: str) -> list[int | str]:
    """Pure. Sort key that orders `phase-2` before `phase-10`."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _sole_parent(phase_of: dict[str, str], needs: dict[str, list[str]], phase: str) -> str | None:
    """Pure. The one other phase that tasks of `phase` need tasks of, whatever those tasks' state; None for none or several.
    `waves.phase_parents` drops done needs, and a parent that already landed has all of its tasks done."""
    parents = {phase_of[n] for t, ns in needs.items() if phase_of.get(t) == phase for n in ns if n in phase_of} - {phase}
    return next(iter(parents)) if len(parents) == 1 else None


def _parent_phase(work_root: Path, initiative: str, phase: str) -> str | None:
    """Edge. The phase `phase` stacks on, read from the `needs` of its tickets; see `_sole_parent`."""
    root = work_root / initiative
    paths = sorted(root.glob("*/*.md")) if root.is_dir() else []
    items = [route.work_item(route.parse_frontmatter(text)[0], initiative=initiative, phase_dir=p.parent.name, stem=p.stem)
             for p in paths if (text := _read_text_or_none(p)) is not None]
    return _sole_parent({str(i["id"]): str(i["phase"]) for i in items}, {str(i["id"]): [str(n) for n in i["needs"] or []] for i in items}, phase)


def _initiative_land_facts(work_root: Path, initiative: str, runs_dir: Path, land_mode: str) -> tuple[dict[str, str], dict[str, list[str]], set[str]]:
    """Edge. Every ticket's phase and needs across the initiative, as `_parent_phase` reads them, and the ids whose
    state is done or dropped: from the store's `work_items` rows in store mode, the ticket files otherwise."""
    root = work_root / initiative
    paths = sorted(root.glob("*/*.md")) if root.is_dir() else []
    items = [route.work_item(route.parse_frontmatter(text)[0], initiative=initiative, phase_dir=p.parent.name, stem=p.stem)
             for p in paths if (text := _read_text_or_none(p)) is not None]
    rows = run_store.work_items(runs_dir, initiative) if land_mode == "store" else []
    state = {str(i["id"]): (run_store.task_state(rows, initiative, str(i["id"])) if land_mode == "store" else i["state"]) for i in items}
    return ({str(i["id"]): str(i["phase"]) for i in items}, {str(i["id"]): [str(n) for n in i["needs"] or []] for i in items},
            {t for t, s in state.items() if s in ("done", "dropped")})


def _parent_land_refusal(tasks: Sequence[str], facts: tuple[dict[str, str], dict[str, list[str]], set[str]],
                         carried: Collection[str] = ()) -> list[dict] | None:
    """Pure. The one `refuse` step for a land of `tasks` whose parent phase has not landed, else None; see `land.parent_land_wait`."""
    phase_of, needs, landed = facts
    wait = land.parent_land_wait(tasks, phase_of, needs, landed, carried)
    return None if wait is None else [{"kind": "refuse", "reason": wait}]


def _land_parent_refused(a: argparse.Namespace, steps: list[dict]) -> int:
    """Edge. Report a parent-land refusal the way the land reports its others: the step listing on a dry run, a
    `land: refusing` line on apply. Exit 2."""
    print(f"land: refusing, {steps[0]['reason']}" if a.apply else json.dumps(steps, indent=2))
    return 2


def _ticket_items(work_root: Path, initiative: str, runs_dir: Path, land_mode: str, current: tuple[str, list[dict]]) -> tuple[dict[str, str], list[str]]:
    """Approved task id to ticket path across every phase of the initiative (`current` is already read), and the phases in natural order."""
    root = work_root / initiative
    phases = sorted((d.name for d in root.iterdir() if d.is_dir()), key=_natural_key) if root.is_dir() else []
    found = [i for p in phases for i in (current[1] if p == current[0] else _phase_items(work_root, initiative, p, runs_dir, land_mode)[0] or [])]
    return {i["id"]: i["file"] for i in found if i["status"] == "approved"}, phases


def _ticket_present(repo: Path, head: str, records: list[dict]) -> set[str]:
    """Edge. Ids of `records` whose work is in `head`: a `Task: <id>` trailer on it, else the task's agent branch an ancestor of it."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    log = git("log", "--format=%B", head)
    trailers = set(re.findall(r"^Task: (.+)$", log.stdout, re.M)) if log.returncode == 0 else set()
    return {r["task"] for r in records if r["task"] in trailers or git("merge-base", "--is-ancestor", f"agents/{r.get('run')}/{r['task']}", head).returncode == 0}


def _phase_task_items(items: list[dict]) -> dict[str, str]:
    """Task id to ticket path, `approved` items only: `mark_done` moves an item approved to done, and a
    `done` item that `phase_landable` also admits would be refused by the store's `--expect approved`."""
    return {i["id"]: i["file"] for i in items if i["status"] == "approved"}


def _phase_needing_land(runs_dir: Path, run_id: str) -> str | None:
    """The one phase under `runs/<run_id>/tasks/*/` holding more than one
    task record — phase mode's default trigger when neither `--phase` nor
    `--task` is given. `None` when no single phase qualifies, leaving task
    mode's own record count to speak (including its "found 0" refusal)."""
    tasks_root = runs_dir / run_id / "tasks"
    if not tasks_root.is_dir():
        return None
    candidates = [d.name for d in tasks_root.iterdir() if d.is_dir() and len(list(d.glob("*.json"))) > 1]
    return candidates[0] if len(candidates) == 1 else None


def _last_phase_with_manifest(runs_dir: Path, run_id: str) -> str | None:
    """Edge. The last phase, by name, that holds task records under `run_id` and has a phase record; None when none does."""
    tasks_root = runs_dir / run_id / "tasks"
    held = sorted(d.name for d in tasks_root.iterdir() if d.is_dir() and any(d.glob("*.json"))) if tasks_root.is_dir() else []
    return next((p for p in reversed(held) if _land_phase_record(runs_dir, run_id, p)[0] is not None), None)


def _checks_env(cwd: Path, cores: int = 1, capacity: int | None = None) -> dict[str, str] | None:
    """Edge. The environment a check runs in: `cwd/.venv/bin` first on PATH when the worktree has one (the venv
    `_link_venv` links), so a bare `pytest` is the project's, not a standalone tool's. With a `capacity` of 1 or more
    the lane's xdist worker count rides on top (`harness.lane_workers.check_env`). None, the inherited environment,
    when there is neither a venv nor a capacity."""
    from harness.lane_workers import check_env
    venv_bin = Path(cwd) / ".venv" / "bin"
    base = {**os.environ, "PATH": os.pathsep.join([str(venv_bin), os.environ.get("PATH", "")])} if venv_bin.is_dir() else None
    capped = check_env(base if base is not None else os.environ, cores, capacity)
    return capped if capped is not None else base


def _git_common_dir(cwd: Path) -> Path | None:
    """Edge. The repository's git common directory, shared by every worktree, or None when git cannot say."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"], capture_output=True, text=True,
                             check=True, timeout=30).stdout.strip()
    except subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError:
        return None
    return (Path(cwd) / out).resolve() if out else None


def _run_checks(checks: list[tuple[str, str]], cwd: Path, cores: int = 1, capacity: int | None = None) -> tuple[bool, str]:
    """Each `(name, shell line)` pair in order, run by a shell in `cwd` as the builders run it (`harness.checks`),
    stopping at the first launch error or failure and naming it by `name`, never by its command.

    A check that already passed on this exact tree and command is not run again (`harness.check_reuse`); the detail
    names it. `cores` and `capacity` are the land's, read by the caller. With a `capacity` the passing detail names
    the xdist worker count the checks ran with."""
    from harness.check_reuse import run_checks_reusing
    from harness.checks import run_checks
    from harness.lane_workers import XDIST_VAR
    if not checks:
        return True, "none declared"
    env = _checks_env(cwd, cores, capacity)
    workers = env.get(XDIST_VAR) if env is not None and capacity is not None and capacity >= 1 else None
    note = f" (xdist workers: {workers})" if workers else ""
    common = _git_common_dir(Path(cwd))
    skipped: list[str] = []
    reused: list[str] = []
    flaky: list[str] = []
    for name, line in checks:
        one = [{"name": name, "cmd": line}]
        [result] = (run_checks_reusing(Path(cwd), one, ledger_dir=common, env=env) if common is not None
                    else run_checks(Path(cwd), one, env=env))
        if result.get("reused"):
            reused.append(name)
        if result.get("note"):
            flaky.append(result["note"])
        if result["outcome"] == "unrunnable":
            return False, land.checks_detail(f"{land.LAUNCH_ERROR}{name}: {result['error']}", skipped)
        if result["outcome"] == "timed_out":
            return False, land.checks_detail(f"{name}: {result['error']}", skipped)
        verdict = land.check_exit(line, result["exit_code"])
        if verdict == "fail":
            return False, land.checks_detail(f"{name}: {result['output_tail'].strip()}", skipped, flaky)
        if verdict == "skip":
            skipped.append(name)
    return True, land.checks_detail(f"{len(checks)} checks passed{note}", skipped, flaky, reused)


def _land_worktree(repo: Path, branch: str) -> Path:
    """Carries this process's pid: on 2026-09-27 two concurrent lands of one branch, the chair's and the loop's, deleted each other's worktree mid-check."""
    return Path(tempfile.gettempdir()) / f"cox-land-{Path(repo).resolve().name}-{branch.replace('/', '-')}-{os.getpid()}"


def _link_venv(repo: Path, wt: Path) -> None:
    """Point `wt/.venv` at the checkout's, so a check naming `.venv/bin/python` runs the worktree's code in the repo's environment."""
    venv = Path(repo).resolve() / ".venv"
    if venv.is_dir() and not (wt / ".venv").exists():
        (wt / ".venv").symlink_to(venv, target_is_directory=True)


_GENERATE_FAILED = "generate failed: "


def _generate_and_amend(wt: Path, umbrella: str | None, echo: Callable[[str], None] = print) -> tuple[bool, str]:
    """Run the worktree's `.agent-generate`, then fold any change it made into the one commit.

    `(True, "")` when the file is absent or nothing changed; `(False, output)` on the first non-zero exit."""
    spec = wt / ".agent-generate"
    if not spec.is_file():
        return True, ""
    plan = generate.plan_commands(generate.parse_generate_file(spec.read_text(encoding="utf-8")), umbrella)
    for _, note in plan:
        if note is not None:
            echo(f"land: {note}")
    env = {**os.environ, **generate.build_env(str(wt), umbrella)}
    for command in (c for c, note in plan if note is None):
        r = subprocess.run(command, shell=True, cwd=wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode != 0:
            return False, f"{_GENERATE_FAILED}{command!r} exited {r.returncode}: {r.stdout.strip()}"
    # `_link_venv`'s `.venv` is a symlink, which a `.venv/` ignore rule does not match: keep it out of the commit.
    outside_venv = ["--", ".", ":(exclude).venv"]
    if not _git_out(wt, "status", "--porcelain", *outside_venv):
        return True, ""
    # Not `add -A` with the exclude: git 2.55 refuses an exclude pathspec that names an ignored path. Stage all,
    # then take the linked `.venv` back out, which covers a git that ignores the symlink and one that does not.
    staged = subprocess.run(["git", "-C", str(wt), "add", "-A", "--", "."], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(wt), "reset", "-q", "--", ".venv"], capture_output=True, text=True)
    changed = (_git_out(wt, "diff", "--cached", "--name-only") or "").split()
    amend = subprocess.run(["git", "-C", str(wt), "commit", "--amend", "--no-edit"], capture_output=True, text=True)
    failed = next((r for r in (staged, amend) if r.returncode != 0), None)
    if failed is not None:
        return False, f"{_GENERATE_FAILED}{failed.stderr.strip() or failed.stdout.strip()}"
    echo(f"land: regenerated {' '.join(changed)}")
    return True, ""


def _skip_empty_pick(wt: Path, sha: str, cp: subprocess.CompletedProcess, echo: Callable[[str], None] = print) -> bool:
    """Edge. True when the stopped pick `cp` of `sha` in `wt` was empty, not a conflict and not a commit authored empty:
    git's skip cleared it and the skip line was echoed. False leaves the stopped pick for the caller's handling."""
    conflicted = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True).stdout
    if not land.is_empty_pick(cp.stderr, cp.stdout, bool(conflicted.split())):
        return False
    if not _git_out(wt, "diff-tree", "--no-commit-id", "--name-only", "-r", sha):
        return False  # a commit authored empty is not already on the base: it fails as it always did
    subject = _git_out(wt, "log", "-1", "--format=%s", sha) or ""
    if subprocess.run(["git", "-C", str(wt), "cherry-pick", "--skip"], capture_output=True, text=True).returncode != 0:
        return False
    echo(land.skip_note(sha, subject, land.SKIP_EMPTY))
    return True


def _patch_id_skipped(repo: Path, step: dict) -> str | None:
    """Edge. For a `cherry_pick` step whose range is empty: the all-skipped detail when the base holds the branch's
    commits by patch-id, after echoing each with its reason. None for a branch with no commits ahead of its base."""
    raw = _git_out(repo, "rev-list", "--reverse", "--no-merges", "--right-only", f"{step['from']}...{step['branch']}")
    dropped = (raw or "").split()
    for sha in dropped:
        print(land.skip_note(sha, _git_out(repo, "log", "-1", "--format=%s", sha) or "", land.SKIP_PATCH_ID))
    return land.skipped_all_detail(len(dropped), step["from"]) if dropped else None


def _drop_pr_branch(repo: Path, onto: str) -> None:
    """Edge. An all-skipped pick built nothing to keep: drop its land worktree and the local pr branch, so a rerun can cut it again."""
    _remove_land_worktree(repo, onto)
    subprocess.run(["git", "-C", str(repo), "branch", "-D", onto], capture_output=True, text=True)


def _range_detail(step: dict, shas: list[str], skipped: list[str]) -> str:
    """Pure. The success detail of a `cherry_pick` step: the all-skipped detail when every commit was skipped."""
    if shas and len(skipped) == len(shas):
        return land.skipped_all_detail(len(skipped), step["from"])
    return f"cherry-picked {len(shas) - len(skipped)} commits onto {step['onto']}"


def _cherry_pick_generated(wt: Path, sha: str, umbrella: str | None, echo: Callable[[str], None] = print) -> tuple[bool, str]:
    """Cherry-pick `sha` into `wt`, then regenerate and amend. The real land and the resume check both
    build the pushed tree here, so the tree the resume check expects cannot drift from the tree pushed.
    A pick that stops empty without a conflict is skipped, not failed: it returns `(True, land.SKIP_EMPTY)`."""
    cp = subprocess.run(
        ["git", "-C", str(wt), "-c", "merge.conflictStyle=diff3", "cherry-pick", sha], capture_output=True, text=True
    )
    regenerated: list[str] = []
    if cp.returncode != 0 and _skip_empty_pick(wt, sha, cp, echo):
        return True, land.SKIP_EMPTY
    if cp.returncode != 0:
        conflicted = subprocess.run(
            ["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True
        ).stdout.splitlines()

        def resolve(name: str) -> str | None:
            # A binary add/add leaves no markers, and a rename or directory/file conflict can leave no file:
            # neither is resolvable, so both take the abort path rather than escaping mid-cherry-pick.
            try:
                return land.resolve_add_add_conflicts((wt / name).read_text())
            except (OSError, UnicodeDecodeError):
                return None

        generated_only = land.generated_conflict_only(conflicted)
        # A generated-only conflict is regenerated or it stops the land: it never reaches the add/add resolver.
        failure = _regenerate_generated_conflicts(wt, conflicted) if generated_only else None
        resolved = {} if generated_only else {name: resolve(name) for name in conflicted}
        unresolvable = not generated_only and (not conflicted or any(text is None for text in resolved.values()))
        if failure is not None or unresolvable:
            subprocess.run(["git", "-C", str(wt), "cherry-pick", "--abort"], capture_output=True, text=True)
            # The conflicts line leads, so the land prints it as `cherry_pick: ... conflicts in: ...` (what
            # `partial_land.conflicting_paths` reads); git's own "could not apply" line follows for `land_refusal`.
            named = f"cherry-pick of {sha[:8]} conflicts in: {', '.join(sorted(conflicted))}" if conflicted else ""
            return False, "\n".join(filter(None, [named, failure, cp.stderr.strip() or cp.stdout.strip()]))
        for name, text in resolved.items():
            (wt / name).write_text(text)
        staged = sorted(conflicted) if generated_only else sorted(resolved)
        subprocess.run(["git", "-C", str(wt), "add", *staged], capture_output=True, text=True)
        cont = subprocess.run(
            ["git", "-C", str(wt), "-c", "core.editor=true", "cherry-pick", "--continue"],
            capture_output=True, text=True,
        )
        if cont.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "cherry-pick", "--abort"], capture_output=True, text=True)
            return False, cont.stderr.strip() or cont.stdout.strip()
        if generated_only:
            regenerated = staged
        else:
            echo(f"resolved add/add: {', '.join(staged)}")
    ok, detail = _generate_and_amend(wt, umbrella, echo)
    # Named once the land has got past the step that could still stop it, so the line is never printed for a failed land.
    if ok:
        for name in regenerated:
            echo(f"cherry_pick: regenerated {name} after a conflict in generated files only")
    return ok, detail


def _regenerate_generated_conflicts(wt: Path, conflicted: Sequence[str]) -> str | None:
    """Edge. Resolve each of `conflicted`, all declared in `land.GENERATED_FILES`, to ours inside its generated
    span, then rewrite the file with its generator run in `wt` so it sees the cherry-picked code.

    None when every file was regenerated; otherwise why it was not, for the caller to abort the cherry-pick
    with: a conflict outside the generated span, an unreadable file, or the generator's exit code and output."""
    for name in conflicted:
        spec = land.GENERATED_FILES[name]
        try:
            text = land.resolve_block_conflicts((wt / name).read_text(encoding="utf-8"), spec.begin, spec.end)
        except (OSError, UnicodeDecodeError):
            return f"{name} cannot be read as text"
        if text is None:
            return f"{name} conflicts outside its generated block"
        (wt / name).write_text(text, encoding="utf-8")
        # Run from the worktree's own tree: `python -m` puts the cwd first on sys.path, ahead of any install.
        run = subprocess.run([sys.executable, *spec.command, str(wt / name)], cwd=wt / spec.cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if run.returncode != 0:
            return f"generator {' '.join(spec.command)} for {name} exited {run.returncode}: {run.stdout.strip()}"
    return None


def _generated_tree(repo: Path, branch: str, base: str, sha: str, umbrella: str | None) -> tuple[str | None, str]:
    """The tree `_cherry_pick_generated` of `sha` onto `base` yields, or `(None, why)` when it cannot be built.

    Built at `branch`'s own land worktree path, so `COX_WORKTREE` matches the real land's. The generator runs
    again on a rerun, as it does on any land that is not resumed: it must be deterministic and idempotent,
    and its writes outside the worktree (the umbrella) repeat."""
    scratch = _land_worktree(repo, branch)
    _remove_land_worktree(repo, branch)
    try:
        add = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(scratch), base], capture_output=True, text=True)
        if add.returncode != 0:
            return None, add.stderr.strip() or add.stdout.strip()
        _link_venv(repo, scratch)
        ok, detail = _cherry_pick_generated(scratch, sha, umbrella, echo=lambda _: None)
        if ok and detail == land.SKIP_EMPTY:
            return None, f"cherry-pick of {sha[:8]} is empty on {base}"
        return (_git_out(scratch, "rev-parse", "HEAD^{tree}"), "") if ok else (None, detail)
    finally:
        _remove_land_worktree(repo, branch)


def _remove_land_worktree(repo: Path, branch: str) -> None:
    """Drop `branch`'s land worktree and its registration; the branch itself stays."""
    wt = _land_worktree(repo, branch)
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
    # A leftover directory git no longer knows (a crashed land, another repo) would block `worktree add`.
    shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)


def _scratch_merge_branch(branch: str) -> str:
    """A throwaway local branch name for merging `from` into a scratch copy of `branch`; never `branch`
    itself, so the real phase branch is never checked out into a second worktree."""
    return f"{branch}--land-scratch"


def _remove_scratch_worktree(repo: Path, scratch_branch: str) -> None:
    """Like `_remove_land_worktree`, but the scratch branch is throwaway, so it goes too."""
    _remove_land_worktree(repo, scratch_branch)
    subprocess.run(["git", "-C", str(repo), "branch", "-D", scratch_branch], capture_output=True, text=True)


def _resolve_landed_conflicts(repo: Path, scratch_wt: Path, from_ref: str, branch_tip: str,
                               conflicted: list[str]) -> list[str]:
    """Resolve each of `conflicted`'s paths to `scratch_wt`'s side (the phase branch) when `from_ref`'s
    content there matches that path's content at some commit already reachable from `branch_tip` -
    already-landed content the branch is reintroducing, not a real conflict. Return the paths left
    unresolved."""
    unresolved = []
    for path in conflicted:
        main_show = subprocess.run(["git", "-C", str(repo), "show", f"{from_ref}:{path}"], capture_output=True, text=True)
        log = subprocess.run(["git", "-C", str(repo), "log", "--format=%H", branch_tip, "--", path], capture_output=True, text=True)
        shas = [s for s in log.stdout.split() if s]
        landed = main_show.returncode == 0 and any(
            subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{path}"], capture_output=True, text=True).stdout == main_show.stdout
            for sha in shas)
        if landed:
            subprocess.run(["git", "-C", str(scratch_wt), "checkout", "--ours", "--", path], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(scratch_wt), "add", path], capture_output=True, text=True)
        else:
            unresolved.append(path)
    return unresolved


def _appended_python(name: str, text: str) -> str | None:
    """`text` resolved to ours-then-theirs, only for a `.py` file that held a real conflict hunk and still
    parses with no top-level name defined twice; None otherwise (no markers is a modify/delete, not an append)."""
    resolved = land.resolve_add_add_conflicts(text) if name.endswith(".py") else None
    if resolved is None or resolved == text:
        return None
    try:
        tops = [n.name for n in ast.parse(resolved).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    except (SyntaxError, ValueError):
        return None
    return resolved if len(tops) == len(set(tops)) else None


def _resolve_append_only(wt: Path, paths: list[str]) -> bool:
    """Edge. Resolve each conflicted `paths` file in `wt` to ours-then-theirs and stage it; all or nothing."""
    if not land.append_only_paths(paths):
        return False

    def resolve(name: str) -> str | None:
        try:
            return _appended_python(name, (wt / name).read_text())
        except (OSError, UnicodeDecodeError):
            return None

    resolved = {name: resolve(name) for name in paths}
    if any(text is None for text in resolved.values()):
        return False
    for name, text in resolved.items():
        (wt / name).write_text(text)
    staged = subprocess.run(["git", "-C", str(wt), "add", *sorted(resolved)], capture_output=True, text=True)
    if staged.returncode != 0:
        return False
    print(f"resolved: append-only in {', '.join(sorted(resolved))}")
    return True


def _gather_worktree(repo: Path, step: dict) -> tuple[Path | None, str]:
    """The pr branch's land worktree for a gather step: made from `from` by the first step, reused by the rest."""
    wt = _land_worktree(repo, step["onto"])
    if wt.exists():
        return wt, ""
    co = subprocess.run(["git", "-C", str(repo), "worktree", "add", step.get("worktree_flag", "-b"), step["onto"], str(wt), step["from"]],
                        capture_output=True, text=True)
    if co.returncode != 0:
        return None, co.stderr.strip() or co.stdout.strip()
    _link_venv(repo, wt)
    return wt, ""


def _commit_count_refusal(step: dict, shas: list[str]) -> str | None:
    """Why a `cherry_pick` step's `shas` are refused, or None. A step with no `commits` key accepts any positive count."""
    ahead = f"ahead of {step['from']} on {step['branch']}, found {len(shas)}"
    if "commits" in step and step["commits"] != len(shas):
        return f"expected {step['commits']} commits {ahead}"
    return None if shas else f"expected at least one commit {ahead}"


def _gather_failed(wt: Path, what: str, abort: list[str], result: subprocess.CompletedProcess) -> tuple[bool, str]:
    """Name the unmerged files, run `abort` in `wt`, and report `what conflicts in: ...` like a squash does."""
    conflicted = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                                capture_output=True, text=True).stdout.split()
    subprocess.run(["git", "-C", str(wt), *abort], capture_output=True, text=True)
    return False, f"{what} conflicts in: {', '.join(conflicted)}" if conflicted else result.stderr.strip() or result.stdout.strip()


def _execute_gather_step(repo: Path, step: dict) -> tuple[bool, str]:
    """Edge. A gather `cherry_pick` or `patch_apply` step, run on the pr branch's worktree."""
    wt, why = _gather_worktree(repo, step)
    if wt is None:
        return False, why
    if step["kind"] == "cherry_pick":
        rev = subprocess.run(["git", "-C", str(repo), "rev-list", "--reverse", "--no-merges", "--cherry-pick", "--right-only",
                              f"{step['from']}...{step['branch']}"], capture_output=True, text=True)
        shas = rev.stdout.split()
        dropped = _patch_id_skipped(repo, step) if rev.returncode == 0 and not shas else None
        if dropped:
            return True, dropped
        refusal = _commit_count_refusal(step, shas)
        if rev.returncode != 0 or refusal:
            return False, refusal or rev.stderr.strip()
        skipped: list[str] = []
        for sha in shas:
            cp = subprocess.run(["git", "-C", str(wt), "cherry-pick", sha], capture_output=True, text=True)
            if cp.returncode == 0:
                continue
            if _skip_empty_pick(wt, sha, cp):
                skipped.append(sha)
                continue
            _, detail = _gather_failed(wt, f"cherry-pick of {sha[:8]}", ["cherry-pick", "--abort"], cp)
            return False, f"{detail}\nstopped at {sha[:8]} on {step['branch']}"
        return True, _range_detail(step, shas, skipped)
    applied = subprocess.run(["git", "-C", str(wt), "apply", "--3way", "--index", "-"], input=step["patch"],
                             capture_output=True, text=True)
    if applied.returncode != 0:
        return _gather_failed(wt, f"patch of {step['task']}", ["reset", "--hard"], applied)
    commit = subprocess.run(["git", "-C", str(wt), "commit", "-qm", step["task"]], capture_output=True, text=True)
    if commit.returncode != 0:
        subprocess.run(["git", "-C", str(wt), "reset", "--hard"], capture_output=True, text=True)
        return False, commit.stderr.strip() or commit.stdout.strip()
    return True, f"applied patch of {step['task']} onto {step['onto']}"


def _execute_land_step(repo: Path, step: dict, forge_module=forge_github) -> tuple[bool, str]:
    kind = step["kind"]
    if kind == "pick_branch":
        return True, f"{step['branch']} ({step['commit_subject']})"
    if kind == "fetch":
        # The one fetch path: `cox runs fetch <run>`, read from the profile and runs dir the land enriched in.
        fetch = argparse.Namespace(run_id=step["run"], all=False, runs_dir=step.get("runs_dir"), profile=step.get("profile"))
        return (True, f"fetched {step['run']}") if _runs_fetch(fetch) == 0 else (False, f"fetch of {step['run']} failed")
    if kind == "patch_apply" or (kind == "cherry_pick" and "task" in step):
        return _execute_gather_step(repo, step)
    if kind == "cherry_pick":
        # The pr branch is built in its own worktree, so `repo`'s HEAD never moves.
        wt = _land_worktree(repo, step["onto"])
        _remove_land_worktree(repo, step["onto"])
        co = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", step["onto"], str(wt), step["from"]], capture_output=True, text=True)
        if co.returncode != 0:
            return False, co.stderr.strip() or co.stdout.strip()
        _link_venv(repo, wt)
        # The branch is `commits` ahead of `from`, counted as `_land_branches` counts it: merges and commits
        # whose patch `from` already has (a stacked parent that merged as a squash) are left out. The range
        # names them without matching on subject text, and `--reverse` picks them oldest first. A step from
        # an older plan has no `commits` key and accepts any positive count.
        rev = subprocess.run(["git", "-C", str(repo), "rev-list", "--reverse", "--no-merges", "--cherry-pick", "--right-only",
                              f"{step['from']}...{step['branch']}"], capture_output=True, text=True)
        shas = [s for s in rev.stdout.split() if s]
        dropped = _patch_id_skipped(repo, step) if rev.returncode == 0 and not shas else None
        if dropped:
            _drop_pr_branch(repo, step["onto"])
            return True, dropped
        refusal = _commit_count_refusal(step, shas)
        if rev.returncode != 0 or refusal:
            return False, refusal or rev.stderr.strip()
        skipped: list[str] = []
        for sha in shas:
            ok, detail = _cherry_pick_generated(wt, sha, step.get("umbrella"))
            if not ok:
                break
            if detail == land.SKIP_EMPTY:
                skipped.append(sha)
        if not ok and detail.startswith(_GENERATE_FAILED):
            # Keep the worktree, detached, for inspection; free the branch so a rerun starts fresh
            # instead of finding a pr branch that holds the un-generated tree and refusing on it.
            subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--detach"], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "branch", "-D", step["onto"]], capture_output=True, text=True)
        # The sha and branch follow the detail: `land_conflict_reason` reads a detail that leads with `cherry-pick of`.
        if not ok:
            return False, f"{detail}\nstopped at {sha[:8]} on {step['branch']}"
        picked = _range_detail(step, shas, skipped)
        if land.is_skipped_all(picked):
            _drop_pr_branch(repo, step["onto"])
        return True, picked
    if kind == "squash_phase":
        # Built in its own worktree from `from`, exactly like `cherry_pick`, so `repo`'s HEAD never moves.
        wt = _land_worktree(repo, step["onto"])
        _remove_land_worktree(repo, step["onto"])
        # `-B` (set by a plan that found a stale local pr branch) resets that branch instead of refusing on it.
        co = subprocess.run(["git", "-C", str(repo), "worktree", "add", step.get("worktree_flag", "-b"), step["onto"], str(wt), step["from"]],
                            capture_output=True, text=True)
        if co.returncode != 0:
            return False, co.stderr.strip() or co.stdout.strip()
        _link_venv(repo, wt)
        def drop() -> None:
            # A refused squash leaves neither worktree nor pr branch, so a rerun starts fresh.
            subprocess.run(["git", "-C", str(wt), "reset", "--hard"], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "branch", "-D", step["onto"]], capture_output=True, text=True)
        # When a parent phase already squash-landed, `from` (main) and `branch` both carry the same new
        # content and a plain squash conflicts on it for no reason. Unless `from` is already an ancestor
        # of `branch`, merge `from` into a scratch copy of `branch` first, auto-resolving paths where
        # `from`'s content matches content already reachable from `branch`'s tip, and squash that merge
        # commit instead - `branch` itself is never moved.
        squash_source = step["branch"]
        appended: set[str] = set()
        ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", step["from"], step["branch"]],
                                  capture_output=True, text=True)
        if ancestor.returncode != 0:
            scratch_branch = _scratch_merge_branch(step["branch"])
            _remove_scratch_worktree(repo, scratch_branch)
            scratch_wt = _land_worktree(repo, scratch_branch)
            branch_tip = _git_out(repo, "rev-parse", step["branch"])
            sco = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", scratch_branch, str(scratch_wt), step["branch"]],
                                 capture_output=True, text=True)
            if sco.returncode != 0:
                drop()
                return False, sco.stderr.strip() or sco.stdout.strip()
            smerge = subprocess.run(["git", "-C", str(scratch_wt), "-c", "merge.conflictStyle=diff3", "merge", "--no-edit", step["from"]],
                                    capture_output=True, text=True)
            if smerge.returncode != 0:
                conflicted = subprocess.run(["git", "-C", str(scratch_wt), "diff", "--name-only", "--diff-filter=U"],
                                            capture_output=True, text=True).stdout.split()
                unresolved = _resolve_landed_conflicts(repo, scratch_wt, step["from"], branch_tip, conflicted)
                if unresolved and _resolve_append_only(scratch_wt, unresolved):
                    appended |= set(unresolved)
                    unresolved = []
                if unresolved:
                    subprocess.run(["git", "-C", str(scratch_wt), "merge", "--abort"], capture_output=True, text=True)
                    _remove_scratch_worktree(repo, scratch_branch)
                    drop()
                    return False, f"squash of {step['branch']} conflicts in: {', '.join(unresolved)}"
                commit = subprocess.run(["git", "-C", str(scratch_wt), "commit", "-qm",
                                         f"merge {step['from']} into {step['branch']} (land scratch)"], capture_output=True, text=True)
                if commit.returncode != 0:
                    _remove_scratch_worktree(repo, scratch_branch)
                    drop()
                    return False, commit.stderr.strip() or commit.stdout.strip()
            squash_source = scratch_branch
        merge = subprocess.run(["git", "-C", str(wt), "-c", "merge.conflictStyle=diff3", "merge", "--squash", squash_source],
                               capture_output=True, text=True)
        if squash_source != step["branch"]:
            _remove_scratch_worktree(repo, squash_source)
        if merge.returncode != 0:
            conflicted = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                                        capture_output=True, text=True).stdout.split()
            if _resolve_append_only(wt, conflicted):
                appended |= set(conflicted)
            else:
                drop()
                return False, f"squash of {step['branch']} conflicts in: {', '.join(conflicted)}"
        if not _git_out(wt, "diff", "--cached", "--name-only"):
            drop()
            return False, f"{step['branch']} adds nothing over {step['from']}"
        commit = subprocess.run(["git", "-C", str(wt), "commit", "-qm", step["subject"]], capture_output=True, text=True)
        if commit.returncode != 0:
            return False, commit.stderr.strip() or commit.stdout.strip()
        squashed_files = (_git_out(wt, "diff", "--name-only", step["from"], "HEAD") or "").splitlines()
        refusal = land.squash_missing_files(step.get("approved_files", []), squashed_files)
        if refusal is not None:
            # Leave the worktree, branch and squash commit local for inspection: nothing is
            # pushed and no task is marked done until the missing files show up in a rerun.
            return False, refusal
        ok, detail = _generate_and_amend(wt, step.get("umbrella"), print)
        if not ok and detail.startswith(_GENERATE_FAILED):
            # Keep the worktree, detached, for inspection; free the branch so a rerun starts fresh
            # instead of finding a pr branch that holds the un-generated tree and refusing on it.
            subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--detach"], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "branch", "-D", step["onto"]], capture_output=True, text=True)
        suffix = f"; resolved: append-only in {', '.join(sorted(appended))}" if appended else ""
        return (True, f"squashed {step['branch']} onto {step['onto']}{suffix}") if ok else (False, detail)
    if kind == "reuse_branch":
        wt = _land_worktree(repo, step["branch"])
        _remove_land_worktree(repo, step["branch"])
        args = ["worktree", "add", str(wt), step["branch"]] if step["local"] else ["worktree", "add", "-b", step["branch"], str(wt), f"origin/{step['branch']}"]
        co = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if co.returncode == 0:
            _link_venv(repo, wt)
        return co.returncode == 0, (f"reusing {step['branch']}" if co.returncode == 0 else co.stderr.strip() or co.stdout.strip())
    if kind == "checks":
        cap = (step.get("cores", 1), step.get("capacity"))  # set by `_with_check_cap` on an applied land
        if step.get("gate") == "ticket":
            # The ticket gate shares the repo lock with level check runs; task checks below never take it.
            from harness import check_gate
            with check_gate.repo_gate_lock(repo):
                return _run_checks(step["checks"], _land_worktree(repo, step["worktree_of"]), *cap)
        if "skipped" in step:
            return True, f"checks: skipped ({step['skipped']})"
        if "kept" in step:
            print(step["kept"])
        if "branch" in step:
            # Phase mode: never check out the phase branch in the working
            # repo (that races anything else using it) — build it fresh in
            # its own worktree instead, and remove it whatever happens. The
            # directory is allocated here, at execution, not in the plan
            # (`land.py`/`_land_enrich`), so a dry run never touches disk.
            wt = Path(tempfile.mkdtemp(prefix="land-checks-"))
            add = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), step["branch"]], capture_output=True, text=True)
            if add.returncode != 0:
                return False, add.stderr.strip() or add.stdout.strip()
            _link_venv(repo, wt)
            try:
                return _run_checks(step["checks"], wt, *cap)
            finally:
                subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        if "worktree_of" in step:
            return _run_checks(step["checks"], _land_worktree(repo, step["worktree_of"]), *cap)
        return _run_checks(step["checks"], repo, *cap)
    if kind == "push":
        return _land_push(repo, step["branch"], forge_module)
    if kind == "pr_branch_cut":
        return _cut_pr_branch(repo, step["branch"], step["from"])
    if kind == "merge_phase":
        return _merge_phase(repo, step["branch"], step["onto"], step["message"])
    if kind == "pr_create":
        draft = {"draft": True} if step.get("draft") else {}
        return forge_module.open_pr(repo, step["title"], step["body"], head=step.get("head"), base=step.get("base"), **draft)
    if kind == "pr_update":
        return forge_module.update_pr(repo, step["title"], step["body"], head=step["head"])
    if kind == "pr_ready":
        return forge_module.mark_ready(repo, head=step["head"])
    if kind == "mark_merged":
        return _mark_merged(repo, step)
    if kind == "phase_done":
        return _phase_done_record(step)
    if kind == "wait_checks":
        return forge_module.wait_checks(repo, float(step.get("timeout_s", 180)), ref=step.get("branch", "HEAD"))
    if kind == "batch_squash":
        return land_batch_squash.squash_onto_batch(repo, step)
    if kind == "merge":
        return forge_module.merge(repo, step)
    if kind == "clean":
        wt = Path(step["worktree_root"]).expanduser() / step["run"] / step["task"]
        if wt.exists():
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "branch", "-D", step["branch"]], capture_output=True)
        return True, f"deleted branch {step['branch']}"
    if kind == "clean_phase":
        doomed = [b for b in cleanup.git_branches(repo) if b == step["phase_branch"] or b.startswith(step["phase_branch"] + "--")]
        doomed += [f"agents/{step['run']}/{t}" for t in step["tasks"]]
        if step.get("pr_branch"):
            # git refuses `branch -D` on a branch a worktree holds, and squash_phase's worktree sits under the
            # temp dir, not `worktree_root`, so drop it here rather than trust an earlier step to have done so.
            _remove_land_worktree(repo, step["pr_branch"])
            doomed.append(step["pr_branch"])
        for t in step["tasks"]:
            wt = Path(step["worktree_root"]).expanduser() / step["run"] / t
            if wt.exists():
                subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        for b in doomed:
            subprocess.run(["git", "-C", str(repo), "branch", "-D", b], capture_output=True)
        # The phase branch is named too, so one that lives only on origin is still deleted there.
        parts = step["phase_branch"].split("/")
        prefix = "/".join(parts[:2]) + "/" if parts[0] == "epic" and len(parts) >= 3 else ""
        gone, failed = _delete_remote_branches(repo, _remote_doomed([step["phase_branch"], *doomed], prefix))
        # A failed remote delete is reported, never a failed land: the merge has already happened.
        remote = (f"; deleted on origin: {', '.join(gone)}" if gone else "") \
            + (f"; remote delete failed: {'; '.join(failed)}" if failed else "")
        return True, "deleted " + ", ".join(doomed) + remote
    if kind == "mark_done" and step.get("recorded") is False:
        return _mark_done_unrecorded(step)
    if kind == "mark_done":
        record_path = Path(step["path"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record_path.write_text(json.dumps({**record, "landed": True}, indent=2), encoding="utf-8")
        line = _mirror_landed(step)
        if line:
            print(line)
        stop = _close_approved_item(step.get("item"), runs_dir=record_path.resolve().parents[3], by=step.get("by", _UNLABELED),
                                    merged=True, mode=step.get("mode", "files"))
        if stop is not None:
            return False, stop
        return True, f"{step['task']} marked landed at {record_path}"
    if kind == "route_sync":
        # A failed sync is reported, never a failed land: after the merge nothing is undone,
        # before the PR it opens without `Closes`.
        when = "before the PR" if step.get("before") else "after the merge"
        tail = "the PR opens without Closes" if step.get("before") else "land not undone"
        sync = argparse.Namespace(item=step["item"], workspace=step["workspace"], dry_run=False, project=None,
                                  profile=step.get("profile"), runs_dir=step.get("runs_dir"))
        if _sync_skipped(sync):
            return True, f"sync of {step['item']} skipped {when}: tracker is none"
        try:
            rc = _route_sync(sync)
        except Exception as exc:
            return True, f"sync of {step['item']} failed {when} ({type(exc).__name__}: {exc}); {tail}"
        return True, f"synced {step['item']}" if rc == 0 else f"sync of {step['item']} failed {when} (exit {rc}); {tail}"
    return False, f"unknown step {kind!r}"


def _git_run(repo: Path | str, *args: str) -> tuple[bool, str]:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.returncode == 0, (done.stderr.strip() or done.stdout.strip())


def _remote_doomed(branches: list[str], prefix: str) -> list[str]:
    """Names under `prefix` (`epic/<initiative>/`), first occurrence kept, in order. `agents/` scratch branches were never pushed."""
    return [b for b in dict.fromkeys(branches) if prefix and b.startswith(prefix)]


def _delete_remote_branches(repo: Path, names: list[str]) -> tuple[list[str], list[str]]:
    """Edge. Delete `names` on origin; returns (deleted, failed as 'name: reason'). No origin, or a branch already gone,
    is neither."""
    if not names or not _git_run(repo, "remote", "get-url", "origin")[0]:
        return [], []
    results = [(n, *_git_run(repo, "push", "origin", f":refs/heads/{n}")) for n in names]
    gone = [n for n, ok, _ in results if ok]
    return gone, [f"{n}: {why}" for n, ok, why in results if not ok and "remote ref does not exist" not in why]


def _cut_pr_branch(repo: Path, branch: str, base: str) -> tuple[bool, str]:
    """Edge. Cut `branch` from `base` in its land worktree when no local branch of that name exists. An existing branch
    is never reset or moved: a later phase's tip is the work already merged into it."""
    if _git_run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")[0]:
        return True, f"reusing {branch}"
    wt = _land_worktree(repo, branch)
    _remove_land_worktree(repo, branch)
    ok, detail = _git_run(repo, "worktree", "add", "-b", branch, str(wt), base)
    if ok:
        _link_venv(repo, wt)
    return ok, f"cut {branch} from {base}" if ok else detail


def _merge_phase(repo: Path, phase_branch: str, onto: str, message: str) -> tuple[bool, str]:
    """Edge. Merge `phase_branch` into `onto` with a merge commit, in `onto`'s land worktree, which stays for the checks
    that follow. A conflict is aborted and the worktree dropped, so `onto` keeps its tip and no half merge is left."""
    wt = _land_worktree(repo, onto)
    _remove_land_worktree(repo, onto)
    ok, detail = _git_run(repo, "worktree", "add", str(wt), onto)
    if not ok:
        return False, detail
    _link_venv(repo, wt)
    ok, detail = _git_run(wt, "-c", "merge.conflictStyle=diff3", "merge", "--no-ff", "-m", message, phase_branch)
    if ok:
        return True, f"merged {phase_branch} into {onto}"
    _git_run(wt, "merge", "--abort")
    _remove_land_worktree(repo, onto)
    return False, detail


def _mark_merged(repo: Path, step: dict) -> tuple[bool, str]:
    """Edge. The task's work is on the pr branch: `merged` and the branch tip are set on the task record, the work item
    and the store row, and the state stays `approved`. The store takes it first under `store` mode, as `mark_done` does."""
    task, mode = step["task"], step.get("mode", "files")
    ok, tip = _git_run(repo, "rev-parse", "--verify", step["pr_branch"])
    if not ok:
        return False, f"{task}: no pr branch {step['pr_branch']}: {tip}"
    item = step.get("item")
    if item and step.get("runs_dir"):
        result = store_cli.mark_merged(Path(step["runs_dir"]), Path(item).absolute().parents[1].name, task, tip, step.get("by", _UNLABELED))
        problem = None if isinstance(result, store_cli.MergeRecorded) else f"store did not record {task} as merged: {result}"
        if problem and mode == "store":
            return False, problem
        if problem:
            print(f"warning: {problem}")
    path = step.get("path")
    if path and Path(path).exists():
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        Path(path).write_text(json.dumps({**record, "merged": True, "merged_commit": tip}, indent=2), encoding="utf-8")
    if item and Path(item).exists():
        from core import workstore
        found = workstore.read_item(item)
        if found["state"] == "approved":
            workstore.write_item({**found, "merged": True, "merged_commit": tip}, item)
    return True, f"{task} marked merged at {tip[:7]}, state approved"


def _phase_done_record(step: dict) -> tuple[bool, str]:
    """Edge. The phase record is written done and merged into the pr branch, never landed: the manifest file
    `<runs_dir>/<run_id>.json` when one exists, and the store's phase row. A phase recorded in neither fails the step."""
    run_id, branch = step.get("phase_record_id"), step["pr_branch"]
    if not run_id or not step.get("runs_dir"):
        return False, f"phase {step['phase']} not recorded as done: no phase record id or runs dir"
    runs_dir, wrote = Path(step["runs_dir"]), []
    path = runs_dir / f"{run_id}.json"
    if path.exists():
        record = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "landed"}
        path.write_text(json.dumps({**record, "status": "done", "merged_into": branch}, indent=2), encoding="utf-8")
        wrote.append("file")
    run, _, phase = run_id.partition(":")
    stored = store_cli.mark_phase_done(runs_dir, run, phase, branch)
    if isinstance(stored, store_cli.MergeRecorded):
        wrote.append("store")
    elif not wrote:
        return False, f"phase {run_id} not recorded as done: no manifest file and the store gave {stored}"
    return True, f"phase {phase} done, merged into {branch} ({' and '.join(wrote)})"


def _mark_done_unrecorded(step: dict) -> tuple[bool, str]:
    """Edge. A `mark_done` with no task record here: the store row moves approved to done carrying the land's `pr`, with
    no record file and no landed mirror. The failure line is the route edit's own."""
    item, task = step.get("item"), step["task"]
    if not item or not step.get("runs_dir"):
        return False, f"{task} has no work item or runs dir to mark done"
    runs_dir, initiative = Path(step["runs_dir"]), Path(item).absolute().parents[1].name
    row = next((r for r in run_store.read_queue(runs_dir, initiative, kind="task") if r.get("task_id") == task), None)
    if row is None:
        return False, f"no store row for {task}"
    result = route_edit.mark_done_with_pr(runs_dir, row, step.get("pr", ""), step.get("at"))
    if result is None:
        return True, f"{task} marked done with {step.get('pr') or 'no PR'}"
    return result.code == route_edit.DONE, result.text


def _mark_remaining_done(steps: list[dict], pr: str, at: str, mark: Callable[[dict], tuple[bool, str]]) -> list[tuple[str, str]]:
    """Edge. Mark done with `pr` every task of the `mark_done` steps in `steps`; returns `(task, reason)` for each it could
    not. A task whose record file is missing, or that never had one, goes by its store item as `_mark_done_unrecorded` does.
    A record `mark` fails on (False or an exception) falls back to the store item. Never raises."""
    unmarked: list[tuple[str, str]] = []
    for step in {s["task"]: s for s in steps if s["kind"] == "mark_done"}.values():
        path = step.get("path")
        has_record = step.get("recorded") is not False and path is not None and Path(path).is_file()
        reason = ""
        if has_record:
            try:
                ok, detail = mark(_mark_done_facts(step, pr, at))
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            if ok:
                continue
            reason = detail
        try:
            runs_dir = step.get("runs_dir") or (str(Path(path).resolve().parents[3]) if path else None)
            ok, detail = _mark_done_unrecorded({**step, "runs_dir": runs_dir, "pr": pr, "at": at})
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            unmarked.append((step["task"], f"{reason}; then {detail}" if reason else detail))
    return unmarked


def _mark_done_facts(step: dict, pr: str, at: str) -> dict:
    """`step` with the store facts `mark_done` mirrors: run and phase from the
    `runs/<run>/tasks/<phase>/<task>.json` path, the PR url, and the caller's ISO time."""
    path = Path(step["path"])
    return {**step, "run": path.parents[2].name, "phase": path.parent.name, "pr": pr, "at": at}


def _mirror_landed(step: dict) -> str | None:
    """The line to print for the store mirror of a landed record, None when it took.
    A step with no `at` was not given the store facts and is not mirrored. Never raises."""
    if "at" not in step:
        return None
    try:
        # The record path is <runs_dir>/<run>/tasks/<phase>/<task>.json.
        result = store_cli.mark_landed(Path(step["path"]).resolve().parents[3], step["run"], step["phase"], step["task"], step["pr"], step["at"])
    except Exception as exc:
        return f"warning: store mirror of {step['task']} failed ({type(exc).__name__}: {exc}); land not undone"
    if isinstance(result, store_cli.Landed):
        return None
    if isinstance(result, store_cli.NotInStore):
        return f"store mirror of {step['task']} skipped: the record predates the mirror"
    if isinstance(result, store_cli.Failed):
        return f"warning: store mirror of {step['task']} failed (exit {result.code}: {result.detail}); land not undone"
    return f"warning: store mirror of {step['task']} skipped: harness not available; land not undone"


def _land_item_facts(item_path: str | None) -> tuple[str | None, str | None]:
    """`(id, issue)` from the work item's frontmatter, each `None` when the
    item is absent or does not carry it. `issue` is whatever the sync wrote
    back, normally a bare number."""
    text = _read_text_or_none(Path(item_path)) if item_path else None
    fields = route.parse_frontmatter(text)[0] if text is not None else {}
    item_id, issue = fields.get("id"), fields.get("issue")
    return (str(item_id) if item_id else None), (str(issue) if issue else None)


def _land_reference(profile: dict, workspace: str, record: dict, item_id: str | None,
                    issue: str | None, repo: Path | None = None,
                    sync: argparse.Namespace | None = None) -> tuple[str, str | None]:
    """Edge. `(scope, reference)` for a task land: the resolved `tracker_scope` and the PR's issue line under it.
    `all` has no reference; the store is read only for a narrowed scope, and unreadable means no initiative issue.
    A one-task fix with a target release says `Part of` its `<version> fixes` issue; `sync`, when given, creates
    that issue first if it is not on file yet."""
    scope, threshold, error = _tracker_scope(profile, repo)
    if error:
        print(f"land: {error}; landing under tracker_scope {scope}", file=sys.stderr)
        return scope, land.pr_reference(issue, None, False)
    if scope == "all":
        return scope, None
    initiative = record.get("initiative")
    try:
        items = route_sync_gh.items_from_store(workspace)
    except (OSError, ValueError) as exc:
        print(f"land: work store unreadable ({type(exc).__name__}: {exc}); the PR carries no issue reference",
              file=sys.stderr)
        items = []
    rollup = route_sync.rollup_for(items, scope, threshold, _initiative_item_id(initiative) or "")
    if rollup is not None:
        number = route_sync_gh.rollup_issue_number(subprocess.run, rollup)
        if number is None and sync is not None:
            _route_sync(argparse.Namespace(**{**vars(sync), "item": item_id or record.get("task", "")}))
            number = route_sync_gh.rollup_issue_number(subprocess.run, rollup)
        return scope, land.pr_reference(issue, number, False)
    parent_issue, last = route_sync.initiative_facts(items, scope, threshold,
                                                     _initiative_item_id(initiative), item_id or record.get("task", ""))
    return scope, land.pr_reference(issue, parent_issue, last)


def _initiative_item_id(initiative: str | None) -> str | None:
    return f"initiative:{initiative}" if initiative else None


def _pr_url(detail: str) -> str | None:
    """The `pr_create` detail as the PR, only when it is a URL; the local forge opens none."""
    url = detail.strip()
    return url if url.startswith(("http://", "https://")) else None


def _landed_pr(kind: str, ok: bool, detail: str, prior: str, step_pr: object = None) -> str:
    """The land's `pr` after a step: `""`, never None, is "no PR", because the store's `--pr` argv takes a str.
    `pr_update` and `pr_ready` act on a PR a phase-two land never opened: they carry its url when the forge answers one,
    else the number the plan pinned (`#<n>`), else the PR already carried."""
    if not ok:
        return prior
    if kind == "pr_create":
        return _pr_url(detail) or ""
    if kind in ("pr_update", "pr_ready"):
        return _pr_url(detail) or prior or (f"#{step_pr}" if step_pr is not None else "")
    return prior


def _open_pr_ref(repo: Path, steps: Sequence[dict], forge_module) -> str:
    """Edge. `#<n>` of the open PR for the pr branch, for a plan that starts after the PR was opened and so has no
    `pr_create`; `""` when the plan opens one, names no pr branch, or the forge finds none."""
    head = next((s["pr_branch"] for s in steps if s["kind"] == "phase_done"), None)
    if head is None or any(s["kind"] == "pr_create" for s in steps) or not hasattr(forge_module, "find_open_prs"):
        return ""
    try:
        found = forge_module.find_open_prs(repo, head)
    except Exception:
        return ""
    return f"#{found[0]}" if isinstance(found, list) and found else ""


# A conflict, a missing review or a draft can never merge; UNKNOWN is GitHub still computing, so it is re-read, and
# merges if it stays UNKNOWN (gh's own merge then refuses what cannot merge). UNSTABLE (only optional checks red) and
# HAS_HOOKS merge, as every land did before this gate (2026-10-06: #1426 and #1430 stopped on UNKNOWN with green checks).
_MERGE_STOPS = frozenset({"DIRTY", "BLOCKED", "DRAFT"})
_UNKNOWN_READS = 6
_UNKNOWN_WAIT_S = 5.0


_LOCAL_MERGE_SHA = re.compile(r"^merged \S+ into \S+ at ([0-9a-f]{7,40})$")


def _landing_ref(detail: str, pr: str, number: int | None, default_branch: str) -> str:
    """What a review PR's close comment names as the landing, never "": the sha the local forge's merge detail reports
    (its exact `merged <branch> into <default> at <sha>` first line), else the merged PR's url, else the PR number the
    land pinned (an adopted or resumed land opens no PR of its own), else the merge into `default_branch`.
    `gh pr merge` reports no sha, so a github land is named by its PR."""
    first = detail.splitlines()[0] if detail else ""
    found = _LOCAL_MERGE_SHA.match(first)
    if found:
        return found.group(1)
    if pr:
        return pr
    return f"PR #{number}" if number is not None else f"the merge into {default_branch}"


def review_close_targets(initiative: str, task: str, prs: Sequence[Mapping]) -> tuple[list[tuple[int, str]], list[str]]:
    """`(number, head)` of each of `prs` whose head is exactly the task's review branch, and one line per matching
    row too malformed to close (no integer `number`); pure, never raises on a forge row."""
    rows = [p for p in prs if isinstance(p, Mapping)]
    targets, bad = [], []
    for p in review_pr_close.review_prs_for_task(initiative, task, rows):
        try:
            targets.append((int(p["number"]), str(p["head"])))
        except (KeyError, TypeError, ValueError):
            bad.append(f"warning: review PR row {dict(p)!r} of {task} unreadable; not closed")
    return targets, bad


def _close_review_prs(repo: Path, initiative: str, task: str, ref: str, forge_module) -> list[str]:
    """Close the landed task's open review PRs and delete their branches; the lines to print. Best effort: a forge
    failure or a malformed forge row becomes a line, never an exception, and never stops the next PR. A PR the forge
    reports already closed counts as closed, so a rerun is quiet."""
    prefix = review_pr_close.review_branch(initiative, task)
    try:
        listed = forge.list_open_prs_by_prefix(forge_module, repo, prefix)
    except Exception as exc:
        listed = f"{type(exc).__name__}: {exc}"
    if isinstance(listed, str):
        return [f"warning: review PRs of {task} not closed: {listed}; land not undone"]
    try:
        targets, lines = review_close_targets(initiative, task, listed)
    except Exception as exc:
        return [f"warning: review PRs of {task} not closed ({type(exc).__name__}: {exc}); land not undone"]
    for number, head in targets:
        try:
            ok, detail = forge.close_pr(forge_module, repo, number, review_pr_close.close_comment(ref, task))
            if not ok:
                lines.append(f"warning: review PR #{number} of {task} not closed: {detail}; land not undone")
                continue
            ok, detail = forge.delete_branch(forge_module, repo, head)
            lines.append(f"closed review PR #{number} and deleted {head}" if ok else
                         f"warning: closed review PR #{number}, branch {head} not deleted: {detail}")
        except Exception as exc:
            lines.append(f"warning: review PR #{number} of {task} failed ({type(exc).__name__}: {exc}); land not undone")
    return lines


def _step_initiative(step: dict, record: dict | None) -> str | None:
    """The initiative a mark_done step's task belongs to: its record file's, else the land's own record's.
    A step with no record file has its work item's directory."""
    if step.get("recorded") is False and step.get("item"):
        return Path(step["item"]).absolute().parents[1].name
    try:
        found = json.loads(Path(step["path"]).read_text(encoding="utf-8")).get("initiative")
    except (KeyError, TypeError, OSError, ValueError, AttributeError):
        found = None
    return found or (record or {}).get("initiative")


def merge_gate(state: str) -> tuple[str, str]:
    """`(decision, state)` from a PR's mergeStateStatus: BEHIND is updated first, DIRTY, BLOCKED and DRAFT stop, and
    anything else merges."""
    if state == "BEHIND":
        return "update_then_merge", ""
    if state in _MERGE_STOPS:
        return "stop", state
    return "merge", ""


def _settled_merge_state(read: Callable[[], str], sleep: Callable[[float], None] | None = None) -> str:
    """The first state other than UNKNOWN in up to `_UNKNOWN_READS` reads, `_UNKNOWN_WAIT_S` apart; UNKNOWN if it never settles."""
    state = read()
    for _ in range(_UNKNOWN_READS - 1):
        if state != "UNKNOWN":
            return state
        (sleep or time.sleep)(_UNKNOWN_WAIT_S)
        state = read()
    return state


def _pr_number(steps: Sequence[dict], i: int, pr_url: str) -> int | None:
    """The PR a `merge` at `steps[i]` lands: the `pr` of the last earlier `wait_checks`, `pr_update` or `pr_ready` that has one, else the number ending the carried PR url."""
    pinned = [s["pr"] for s in steps[:i] if s["kind"] in ("wait_checks", "pr_update", "pr_ready") and s.get("pr") is not None]
    found = re.search(r"(\d+)/?$", str(pinned[-1] if pinned else pr_url))
    return int(found.group(1)) if found else None


def _fetch_branch(repo: Path, branch: str) -> str | None:
    """Edge. Fetch `origin/<branch>`; the failure text, or None when it landed."""
    r = subprocess.run(["git", "-C", str(repo), "fetch", "origin", branch], capture_output=True, text=True)
    return None if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip() or f"git fetch origin {branch} failed")


def _merge_updating(repo: Path, steps: Sequence[dict], i: int, pr_url: str, forge_module,
                    guard: Callable[[str], str | None] | None = None,
                    attempts: int | None = None,
                    lock: Callable[[], contextlib.AbstractContextManager] | None = None) -> tuple[bool, str, str | None]:
    """Edge. Run the `merge` at `steps[i]`, updating the PR branch first when it is BEHIND and again after each merge refused as behind.

    Each update is one pass: the repo lease is renewed through `guard`, the branch updated, its checks awaited again, the merge tried.
    At most `attempts` passes run, `land.MERGE_UPDATE_ATTEMPTS` when None (read at call time: `land` loads lazily). Returns `(ok, detail, halt)`: `halt` is the reason to stop at the merge, else the merge's own result.
    No PR number, or a forge with no PRs, merges as before.
    `lock`, when given, makes a `land_merge_lock.hold` for the repo. It is held for the first state read and each merge try only,
    and released before every update and wait, which take minutes. A lock not taken in time stops the land naming its holder."""
    step = steps[i]
    attempts = land.MERGE_UPDATE_ATTEMPTS if attempts is None else attempts
    number = _pr_number(steps, i, pr_url)
    # Imported here: `agent_tools.cli` must not import a command module before one is dispatched.
    from agent_tools.forge import ForgeError, ForgeNotSupported

    wait = next((s for s in reversed(steps[:i]) if s["kind"] == "wait_checks"), {"kind": "wait_checks"})
    behind = False
    reading = number is not None  # the state is read once, on the first lock; a later merge refused as behind says the base moved
    refusal = ""
    used = 0
    while True:
        if behind:
            if used >= attempts:
                return False, "", land.behind_stop(attempts, refusal)
            used += 1
            lost = guard("update_branch") if guard is not None else None
            if lost is not None:
                return False, "", lost
            try:
                forge_module.update_branch(number, repo=repo)
            except ForgeError as exc:
                if land.is_conflict_error(str(exc)):
                    return False, "", land.conflict_stop(number, str(exc))
                return False, "", f"merge: cannot update pull request #{number}: {exc}"
            print(f"land: pull request #{number} was BEHIND; updated its branch (update {used} of {attempts}), waiting for checks again")
            failed = _fetch_branch(repo, step["branch"])
            if failed is not None:
                return False, "", f"merge: cannot fetch the updated {step['branch']}: {failed}"
            ok, detail = _execute_land_step(repo, {**wait, "branch": f"origin/{step['branch']}"}, forge_module)
            if not ok:
                return False, "", f"wait_checks: {detail}"
            behind = False
        with (lock() if lock is not None else contextlib.nullcontext()) as held:
            if isinstance(held, land_merge_lock.NotHeld):
                return False, "", land.lock_stop(held.holder, held.reason)
            if reading:
                reading = False
                try:
                    decision, state = merge_gate(_settled_merge_state(lambda: forge_module.merge_state(number, repo=repo)))
                except ForgeNotSupported:
                    return (*_execute_land_step(repo, step, forge_module), None)
                except ForgeError as exc:
                    return False, "", f"merge: cannot read the state of pull request #{number}: {exc}"
                if decision == "stop":
                    return False, "", f"merge: pull request #{number} is {state}; not merging"
                if decision == "update_then_merge":
                    behind = True
                    continue  # leaves the `with`, releasing the lock before the update
            ok, refusal = _execute_land_step(repo, step, forge_module)
            if ok or not land.is_behind_refusal(refusal) or number is None:
                return ok, refusal, None
            behind = True


def _resolved_tracker(profile: dict, runs_dir: Path) -> str:
    """The policy file wins, then the profile's `tracker`, then `none`."""
    return tracker.tracker_name(profile, runs_dir)


_UNLABELED = "unlabeled"


def _close_approved_item(item_path: str | None, *, runs_dir: Path, by: str, merged: bool = False, mode: str = "files") -> str | None:
    """Moves the item to `done` and mirrors any item left `done`, rerun or not, to the store; `merged` only after a merge step succeeded.

    Under `store` the store moves first, as `set-state done --expect approved`, whatever the local file says or even
    when it is missing; a refusal comes back as the reason, with the item untouched. Else None."""
    if item_path is None:
        return None
    item = Path(item_path)
    if mode == "store":
        stop = land.set_state_stop(store_cli.set_state(runs_dir, item.absolute().parents[1].name, item.stem, "done", by, expected="approved"))
        if stop is not None:
            return stop
    if not item.exists():
        return None
    text = item.read_text(encoding="utf-8")
    new_text, message = land.approve_to_done(text, merged=merged)
    if new_text is not None:
        item.write_text(new_text, encoding="utf-8")
        queue_export.record_written_paths(runs_dir.parent, [item.absolute()])
    if message:
        print(message)
    if mode == "store" or (new_text is None and message is not None):
        return None
    task = str(route.parse_frontmatter(new_text or text)[0].get("id") or item.stem)
    line = store_cli.mirror_state(runs_dir, item.absolute().parents[1].name, task, "done", by)
    if line is not None:
        print(line)
    return None


_await_checks = _LazyHandler("agent_tools.land:await_checks")
_wait_checks = _LazyHandler("agent_tools.forge_github:wait_checks")


def _repo_is_dirty(repo: Path) -> bool:
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return bool(status.stdout.strip())


def sync_decision(has_origin: bool, behind: int, ahead: int) -> str:
    """Pure. What to do with the local default branch: skip, current, fast_forward or refuse."""
    if not has_origin:
        return "skip"
    if behind > 0 and ahead > 0:
        return "refuse"
    if behind > 0:
        return "fast_forward"
    return "current"


def _sync_default_branch(repo: Path, default_branch: str) -> tuple[str, str]:
    """Edge. Fast-forward the local default branch to origin's; HEAD and the tree stay put."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    remotes = git("remote").stdout.split()
    if "origin" not in remotes:
        return sync_decision(False, 0, 0), ""
    fetched = git("fetch", "origin", default_branch)
    if fetched.returncode != 0:
        return "refuse", fetched.stderr.strip()
    remote_ref = f"origin/{default_branch}"
    counted = git("rev-list", "--left-right", "--count", f"{default_branch}...{remote_ref}")
    if counted.returncode != 0:
        return "refuse", counted.stderr.strip()
    ahead, behind = (int(n) for n in counted.stdout.split())
    decision = sync_decision(True, behind, ahead)
    if decision == "refuse":
        return decision, f"{default_branch} has diverged from {remote_ref} ({ahead} ahead, {behind} behind); reconcile it first"
    if decision != "fast_forward":
        return decision, ""
    if git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() == default_branch:
        moved = git("merge", "--ff-only", remote_ref)
    else:
        old = git("rev-parse", f"refs/heads/{default_branch}").stdout.strip()
        moved = git("update-ref", f"refs/heads/{default_branch}", remote_ref, old)
    if moved.returncode != 0:
        return "refuse", (moved.stderr or moved.stdout).strip()
    return decision, f"{default_branch} fast-forwarded to {remote_ref} ({behind} commits)"


_RUN_FILE_SUFFIXES = re.compile(
    r"\.(?:log|pid|json|jsonl|result\.json|remote\.json|launched\.json|fetched\.json|usage\.json|progress\.json"
    r"|ceiling\.json|calls\.jsonl|jsonl\.zst|parquet|provider-profile\.yaml)$"
)


def _run_id_of(name: str) -> str:
    """`<id>.log`, `<id>.remote.json`, `<id>:<task>.json` and `<id>-trace` all belong to `<id>`.

    Wrong belief: "the id ends at the first dot". A phase run id is `<initiative>.<phase>-<N>`, so only a known
    run-file suffix is cut, from the right; a name with none keeps its dots up to the first colon."""
    return _RUN_FILE_SUFFIXES.sub("", name).split(":", 1)[0].removesuffix("-trace")


def _taken_run_names(runs_dir: Path) -> list[str]:
    """Edge. Names in `runs_dir`, the bare run id each belongs to, and every run id in the store.

    A remote lane leaves only `<id>.remote.json`, which `next_run_id` does not read as an id; the bare ids let
    both it and an exact `--run-id` check see that run as taken."""
    names = [p.name for p in runs_dir.iterdir()] if runs_dir.is_dir() else []
    return [*names, *sorted({_run_id_of(n) for n in names}), *sorted(run_store.run_ids(runs_dir))]


def _runs_review(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    runs_dir = Path(profile["workspace_dir"]).expanduser() / "runs"
    run_id = route.next_run_id(_taken_run_names(runs_dir), "review")
    return review_pr.run_review(a.pr, profile, run_id, lambda argv: subprocess.run(argv, capture_output=True, text=True))


_LAND_LEASE_TTL_SECONDS = 3600
_GUARDED_STEPS = ("merge", "mark_done", "pr_branch_cut", "merge_phase", "pr_update", "pr_ready", "mark_merged", "phase_done")
_PR_REPORT_STEPS = ("pr_create", "pr_update", "pr_ready")  # a failure after the push is reported; mark_merged still runs
_AFTER_PR_FAILURE = (*_PR_REPORT_STEPS, "mark_merged", "phase_done")


def _land_approval_stop(mode: str, store_state: str | None, file_state: str | None) -> str | None:
    """Pure. None when the state the approved check reads is `approved`, else the reason to stop, naming that state."""
    state = land.approved_state(mode, store_state, file_state)
    if state == "approved":
        return None
    if state == "done":
        return "land: task is already done; another machine landed it, so nothing is merged"
    return f"land: task state is {state!r}, not approved; nothing is merged"


def _land_store_stop(runs_dir: Path, record: dict, item_path: str | None) -> str | None:
    """Edge. The store's state and the item's, through the approved check; prints which of the two decided.

    `task_state_of` reads a missing row and a failed read alike as None, so the file line names both causes."""
    if item_path is None:
        return "land: refusing, the record names no initiative, so work_state store has no task to check or lease"
    initiative, task = record["initiative"], record["task"]
    store_state = run_store.task_state_of(runs_dir, initiative, task)
    text = _read_text_or_none(Path(item_path))
    file_state = route.parse_frontmatter(text)[0].get("state") if text else None
    source = "store" if store_state is not None else f"file (the store returned no row for {initiative}/{task}: none recorded, or the read failed)"
    print(f"land: state from {source}", file=sys.stderr)
    return _land_approval_stop("store", store_state, file_state)


def _task_lease_stop(task: str, kind: str, result: store_cli.LeaseResult) -> str | None:
    """None when the `land:<task>` lease renewed, else the reason to stop before `kind`."""
    if isinstance(result, store_cli.LeaseGranted):
        return None
    lost = f"held by {result.holder} at epoch {result.epoch}" if isinstance(result, store_cli.LeaseRefused) else (
        result.detail if isinstance(result, store_cli.LeaseError) else "the store is not available")
    return f"land: refusing {kind}, the land lease for {task} could not be renewed: {lost}"


# The ttl `_land` passes to land_repo_lease.acquire. A renew restarts it, but only when one runs: `_LeaseGuard.renewing`
# renews every third of it for as long as the land runs, because a step such as wait_checks blocks for longer than it.
_REPO_LEASE_TTL_SECONDS = 1200
_LAND_LOCK_POLL_SECONDS = 5  # `runs land --wait-lock` retries the repo lease this often
# The merge lock is held for one state read and one merge, not the waits between them, so its ttl is short and a land
# that cannot take it within the timeout stops for the chair to retry.
_MERGE_LOCK_TTL_SECONDS = 120
_MERGE_LOCK_TIMEOUT_SECONDS = 60


def _merge_lock_factory(runs_dir: Path, repo: str, holder: str, limit: int, leased: bool) -> Callable[[], contextlib.AbstractContextManager] | None:
    """The merge lock for `repo`, or None. With a limit of 1 the repo lease already admits one land, so no lock is taken;
    without a repo lease there is no store to take it in."""
    if limit <= 1 or not leased:
        return None
    return lambda: land_merge_lock.hold(runs_dir, repo, holder, _MERGE_LOCK_TTL_SECONDS, _MERGE_LOCK_TIMEOUT_SECONDS)


def _lease_standing(result: store_cli.LeaseResult) -> str:
    """Pure. `kept` on a grant, `refused` on any refusal, else `unrenewable`.

    A refused renew does not say whether the lease lapsed or another holder took it. The store answers both with no
    holder and no epoch: harness/store_cli_lease.lease_renew returns `_result(<bool>, None, None)`, and `parse_lease`
    turns exit 3 into `LeaseRefused(None, None)`, the `held by None at epoch None` of the 2026-10-30 land that waited on CI.
    Only an acquire names a holder, and it refuses while another live holder has the lease. So a refused renew is settled
    by acquiring: a grant means the lease had lapsed with no other holder, a refusal names who holds it."""
    if isinstance(result, store_cli.LeaseGranted):
        return "kept"
    if isinstance(result, store_cli.LeaseRefused):
        return "refused"
    return "unrenewable"


def _renew_loop(beat: Callable[[], None], interval: float, stop: threading.Event) -> None:
    """Calls `beat` every `interval` seconds until `stop` is set. A beat that raises is dropped: the guard's own renew before the next step reports it."""
    while not stop.wait(interval):
        with contextlib.suppress(Exception):
            beat()


def _repo_lease_stop(repo: str, kind: str, result: store_cli.LeaseResult, renewed_at: float, now: float, ttl: int) -> str | None:
    """None when the repo lease renewed, else the reason to stop before `kind`. Clock values are seconds on one clock."""
    if isinstance(result, store_cli.LeaseGranted):
        return None
    if isinstance(result, store_cli.LeaseRefused):
        late = f", {int(now - renewed_at)}s since the last renewal against a {ttl}s ttl" if now - renewed_at > ttl else ""
        return f"land: refusing {kind}, the repo lease for {repo} was lost: held by {result.holder} at epoch {result.epoch}{late}"
    if isinstance(result, store_cli.LeaseError):
        why = result.detail
    elif isinstance(result, store_cli.NotAvailable):
        why = result.reason or "the store is not available"
    else:
        why = "it was released"
    return f"land: refusing {kind}, the repo lease for {repo} could not be renewed: {why}"


class _LeaseGuard:
    """Edge. Keeps one lease renewed: before each step it guards, and every third of `ttl` while `renewing` is entered.

    A refused renew is settled by `reacquire`, which only succeeds when no other live holder has the lease: a grant means it
    lapsed untaken, and `say` notes it. A refusal from the acquire names the holder; with no `reacquire` the renew's own refusal stands.
    Either refusal is final: it is remembered, appended to `lost` once, and every later call returns the stop.
    A transient failure on the timer is not remembered; the next guarded step renews again and reports it."""

    def __init__(self, label: str, renew: Callable[[], store_cli.LeaseResult], reacquire: Callable[[], store_cli.LeaseResult] | None,
                 clock: Callable[[], float], ttl: int, stop_for: Callable[[str, store_cli.LeaseResult, float, float], str | None],
                 lost: list[str] | None = None, only: Collection[str] | None = None,
                 say: Callable[[str], None] = print, new_stop: Callable[[], threading.Event] = threading.Event) -> None:
        self.interval = max(1, ttl // 3)
        self._label, self._renew, self._reacquire, self._clock = label, renew, reacquire, clock
        self._stop_for, self._lost, self._only, self._say, self._new_stop = stop_for, lost, only, say, new_stop
        self._renewed = clock()
        self._failed: tuple[store_cli.LeaseResult, float, float] | None = None  # the result, the last renewal, the time of failure
        self._lock = threading.Lock()

    def _attempt(self) -> store_cli.LeaseResult | None:
        result = self._renew()
        if _lease_standing(result) == "refused" and self._reacquire is not None:
            result = self._reacquire()
            if isinstance(result, store_cli.LeaseGranted):
                self._say(f"land: the {self._label} expired with no holder; re-acquired it at epoch {result.epoch}, continuing")
        return None if isinstance(result, store_cli.LeaseGranted) else result

    def _check(self, kind: str, background: bool) -> str | None:
        with self._lock:
            if self._failed is None:
                now = self._clock()
                result = self._attempt()
                if result is None:
                    self._renewed = now
                elif not background or isinstance(result, store_cli.LeaseRefused):
                    self._failed = (result, self._renewed, now)
                    if isinstance(result, store_cli.LeaseRefused) and self._lost is not None:
                        self._lost.append(self._stop_for(kind, result, self._renewed, now) or "")
            if self._failed is None:
                return None
            result, renewed, now = self._failed
        return self._stop_for(kind, result, renewed, now)

    def __call__(self, kind: str) -> str | None:
        return None if self._only is not None and kind not in self._only else self._check(kind, False)

    def beat(self) -> str | None:
        """One timer renewal. Only a refusal is remembered; a store error is left for the next guarded step to renew again."""
        return self._check("the wait", True)

    @contextlib.contextmanager
    def renewing(self):
        """Renews on a background thread for the length of the `with`, so a step that blocks past the ttl still keeps the lease."""
        stop = self._new_stop()
        thread = threading.Thread(target=_renew_loop, args=(self.beat, self.interval, stop), daemon=True)
        thread.start()
        try:
            yield self
        finally:
            stop.set()
            thread.join()


def _repo_lease_guard(repo: str, renew: Callable[[], store_cli.LeaseResult], clock: Callable[[], float], ttl: int,
                      lost: list[str], reacquire: Callable[[], store_cli.LeaseResult] | None = None) -> _LeaseGuard:
    """Edge. A walk guard that renews the repo lease before every step, not only the guarded ones.

    A refusal means another holder owns the lease: it is appended to `lost` so the caller does not release it."""
    return _LeaseGuard(f"repo lease for {repo}", renew, reacquire, clock, ttl,
                       lambda kind, result, renewed, now: _repo_lease_stop(repo, kind, result, renewed, now, ttl), lost)


def _first_stop(first: Callable[[str], str | None], then: Callable[[str], str | None] | None) -> Callable[[str], str | None]:
    """A guard that asks `first`, and asks `then` only when `first` lets the step run."""
    return lambda kind: first(kind) or (then(kind) if then is not None else None)


_LandWalk = Callable[[Callable[[str], str | None] | None], tuple[int, list[str], str]]


def _land_walked(a: argparse.Namespace, runs_dir: Path, mode: str, task: str | None, recheck: Callable[[], str | None],
                 walk: _LandWalk) -> tuple[int, list[str], str] | None:
    """`walk(None)` under `files`. Under `store` `recheck` runs again inside the `land:<task>` lease, and
    the walk renews the lease before `merge` and `mark_done`; the lease is released on every exit.

    None when the lease was refused or could not be taken, or the check under it stopped: the reason is printed and no step ran."""
    if mode != "store" or task is None:
        return walk(None)
    holder = chair.lease_holder(_holder_label(a), os.getpid(), socket.gethostname())

    held = [0]  # the epoch the lease is held at now: a re-acquire after an expiry moves it, and the release must follow

    def renew() -> store_cli.LeaseResult:
        return store_cli.lease_renew(runs_dir, store_cli.land_lease_name(task), holder, held[0], _LAND_LEASE_TTL_SECONDS)

    def reacquire() -> store_cli.LeaseResult:
        again = store_cli.lease_acquire(runs_dir, store_cli.land_lease_name(task), holder, _LAND_LEASE_TTL_SECONDS)
        if isinstance(again, store_cli.LeaseGranted):
            held[0] = again.epoch
        return again

    refused_by: list[str | None] = []  # holders the first take was refused by; a body that runs after one ran on a takeover

    def acquire(runs_dir: Path, name: str, holder: str, ttl: int, steal: bool = False) -> store_cli.LeaseResult:
        got = store_cli.lease_acquire(runs_dir, name, holder, ttl, steal=True) if steal else store_cli.lease_acquire(runs_dir, name, holder, ttl)
        if isinstance(got, store_cli.LeaseRefused) and not steal:
            refused_by.append(got.holder)
        return got

    def body(epoch: int) -> tuple[int, list[str], str] | str:
        held[0] = epoch
        if refused_by:  # printed here so it lands before the walk's own output and survives a walk that raises
            print(f"land: taking over the land lease for {task} from {refused_by[0]} (pid gone)")
        stop = recheck()
        guard = _LeaseGuard(f"land lease for {task}", renew, reacquire, time.monotonic, _LAND_LEASE_TTL_SECONDS,
                            lambda kind, result, _renewed, _now: _task_lease_stop(task, kind, result), only=_GUARDED_STEPS)
        return stop if stop is not None else walk(guard)

    ran = land_lease.run_under_land_lease(runs_dir, task, holder, _LAND_LEASE_TTL_SECONDS, body, acquire=acquire,
                                          epoch_now=lambda: held[0], this_host=socket.gethostname(), alive=land_repo_lease.pid_alive)
    if isinstance(ran, land_lease.Refused):
        if ran.holder is None:  # a steal over a dead holder was granted at an unexpected epoch: a live land had taken it first
            print(f"land: refusing, {task} was taken by a live land while taking over a dead holder's lease (refused at epoch {ran.epoch})")
        else:
            print(f"land: refusing, {task} is being landed by {ran.holder} (lease epoch {ran.epoch})")
        return None
    if isinstance(ran, land_lease.Unavailable):
        print(f"land: refusing, could not take the land lease for {task}: {ran.detail}")
        return None
    if ran.warning:
        print(ran.warning)
    if isinstance(ran.value, str):
        print(ran.value)
        return None
    return ran.value


def _land_done_before_fetch(runs_dir: Path, task: str | None, mode: str) -> bool:
    """Edge. True when work_state is store and the store already says `task` is done, so a fetch is pointless."""
    if task is None or mode != "store":
        return False
    initiative = _initiative_of(runs_dir.parent / "work", task)
    return initiative is not None and run_store.task_state_of(runs_dir, initiative, task) == "done"


def _cartridge_checks(repo: Path, profile: dict) -> tuple[list[dict], str | None]:
    """Edge. The profile's team cartridge `landing_areas.checks`, resolved as the chair resolves it, and the error text
    when it cannot be: a profile naming no team and no cartridges_dir has no cartridge, which is `([], None)`."""
    landing, error = _cartridge_landing(repo, profile)
    return list(landing.get("checks") or []), error


def _cartridge_landing(repo: Path, profile: dict) -> tuple[dict, str | None]:
    """Edge. The resolved team cartridge's `landing_areas` and the error text when it cannot be resolved; `({}, None)` when the profile names no cartridge."""
    team, cartridges_dir = profile.get("team"), profile.get("cartridges_dir")
    if not team or not cartridges_dir:
        return {}, None
    try:
        from harness.resolve import overlay_path, resolve_cartridge
        overlay_file = Path(overlay_path(str(repo)))
        overlay = yaml.safe_load(overlay_file.read_text(encoding="utf-8")) if overlay_file.is_file() else None
        cartridge, _ = resolve_cartridge(team, cartridges_dir=Path(cartridges_dir).expanduser(),
                                         skills_root=[Path(r).expanduser() for r in profile.get("skills_roots") or []], overlay=overlay)
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return dict(cartridge.get("landing_areas") or {}), None


def _required_checks(repo: Path, default_branch: str | None, forge_module) -> tuple[str, ...] | None:
    """Edge. The default branch's required check names, or None when the forge is unnamed, has no such read (the local
    forge), no branch is given, or the read fails; a failure's cause goes to stderr, since the plan only says unreadable."""
    read = getattr(forge_module, "read_required_for_base", None)
    if not default_branch or read is None:
        return None
    try:
        return read(repo, default_branch)
    except Exception as exc:
        print(f"land: required checks unreadable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def _land_repo_facts(repo: Path, profile: dict, default_branch: str | None = None, forge_module=None) -> dict:
    """Edge. What `land.checks_argv` needs: the cartridge's checks plus the repo's `.agent-checks` (parsed by
    `harness.checks.repo_checks`, as the chair does), the files that say whether pytest belongs here, and
    `cartridge_error`, set when the cartridge could not be resolved and the checks are therefore short.
    `land_checks` is the cartridge's; only `ci` reads the forge, for `required_checks`."""
    from harness.check_gate import gate_checks
    from harness.checks import repo_checks
    try:
        agent_checks = (repo / ".agent-checks").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        agent_checks = ""
    landing, cartridge_error = _cartridge_landing(repo, profile)
    cartridge_checks = list(landing.get("checks") or [])
    facts = {"venv_python": (repo / ".venv" / "bin" / "python").exists(), "uv_lock": (repo / "uv.lock").exists(),
             "python_project": any((repo / name).exists() for name in land.PYTHON_PROJECT_FILES),
             "checks": land.resolved_checks(cartridge_checks, repo_checks(agent_checks)), "cartridge_error": cartridge_error,
             "ticket_checks": gate_checks(landing, "ticket")}
    land_checks = landing.get("land_checks")
    if land_checks is None:
        return facts
    ci = {"required_checks": _required_checks(repo, default_branch, forge_module)} if land_checks == "ci" else {}
    return {**facts, "land_checks": land_checks, **ci}


def _land_checks_lines(steps: list[dict]) -> list[str]:
    """The dry run's lines, one per checks step and labelled by what it checks, each check as its command (or
    `name = command` when the name is neither the command nor its first word); `none declared` for a step holding none.
    A skipped step says `skipped (<reason>)`; a kept step's line is followed by its reason."""
    def show(name: str, line: str) -> str:
        return line if name == line or line.split(None, 1)[:1] == [name] else f"{name} = {line}"
    lines = []
    for s in (s for s in steps if s["kind"] == "checks"):
        gate = "ticket gate" if s.get("gate") == "ticket" else "checks"
        label = f"{gate} ({s.get('worktree_of') or s.get('branch') or 'repo'}): "
        if "skipped" in s:
            lines.append(f"{label}skipped ({s['skipped']})")
            continue
        lines.append(label + ("; ".join(show(n, line) for n, line in s["checks"]) or "none declared"))
        if "kept" in s:
            lines.append(f"  {s['kept']}")
    return lines


def _base_behind_warning(runs_dir: Path, run_id: str, repo: Path, default_branch: str, limit: int) -> str | None:
    """Edge. `land.behind_warning` for the base commit the run's remote record holds; a local run or an older record has none."""
    path = remote_lane.remote_record_path(runs_dir, run_id)
    record = remote_lane.parse_remote_record(path.read_text()) if path.exists() else None
    base_sha = None if record is None else record.get("base_sha")
    if base_sha is None:
        return None
    count = _git_out(repo, "rev-list", "--count", f"{base_sha}..{default_branch}")
    return land.behind_warning(base_sha, int(count) if count and count.isdigit() else None, limit, default_branch)


def _print_behind_warning(a: argparse.Namespace, runs_dir: Path, run_id: str, repo: Path, default_branch: str) -> None:
    behind = _base_behind_warning(runs_dir, run_id, repo, default_branch, getattr(a, "behind_limit", 20))
    if behind is not None:
        print(behind, file=sys.stderr)


def _runs_land(a: argparse.Namespace) -> int:
    repo = Path(a.repo).expanduser()
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"land: {reason}")
        return 2
    run_id = _resolved_run_id(runs_dir, a.run_id)
    if a.task and _land_done_before_fetch(runs_dir, a.task, work_state.work_state_mode(_lake_provider(a)[0])):
        print(_land_approval_stop("store", "done", None))
        return 3
    # A remote run fetched before the marker existed has none, so land refuses with the fetch hint; a re-fetch is
    # idempotent (rsync -a and git fetch of the same refs).
    fetched = remote_lane.fetched_record_path(runs_dir, run_id).exists()
    if remote_lane.land_needs_fetch(remote_lane.remote_record_path(runs_dir, run_id).exists(), fetched):
        print(f"land: {run_id} is a remote run; run cox runs fetch {run_id}, then cox runs land {run_id} again")
        return 2
    if a.apply:
        guard_rc = _leader_guard_or_refuse(runs_dir, _holder_label(a), a.force, claim=not a.no_claim, explicit_label=getattr(a, "label", None))
        if guard_rc is not None:
            return guard_rc
    profile_text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(profile_text) if profile_text is not None else {}
    except route.ProfileError as exc:
        print(f"land: profile unreadable: {exc}")
        return 2
    checked = land_checkout_guard.verdict(
        _repo_is_detached(repo),
        land_checkout_guard.is_install_managed(str(repo), _install_roots(profile)),
        repo_clone.working_clone(profile),
    )
    if checked.refusal:
        print(f"land: refusing, {checked.refusal}")
        return 2
    if checked.notice:
        print(f"land: {checked.notice}")
    base_flag = getattr(a, "base", None)
    if not base_flag:
        phase_of_land = getattr(a, "phase", None) or (None if a.task else _phase_needing_land(runs_dir, run_id))
        base_flag, refusal = _initiative_target_branch(profile, repo, runs_dir.parent, _land_initiative(runs_dir, run_id, phase_of_land, a.task))
        if refusal:
            print(f"land: refusing, {refusal}")
            return 2
    try:
        default_branch = base_branch.base_ref(str(repo), base_flag).removeprefix("origin/")
    except base_branch.BaseError as exc:
        print(f"land: refusing, {exc}")
        return 2
    repo_facts = _land_repo_facts(repo, profile, default_branch, forge.forge_for(forge.forge_name(profile)))
    if repo_facts["cartridge_error"]:
        # The chair fails a run whose cartridge will not resolve; a land that went on would push after fewer checks than the build ran.
        shortfall = f"cartridge checks unavailable ({repo_facts['cartridge_error']}); the land would run fewer checks than the build did"
        if a.apply:
            print(f"land: refusing, {shortfall}")
            return 2
        print(f"land: {shortfall}; the list below is short", file=sys.stderr)
    phase = getattr(a, "phase", None) or (None if a.task else _phase_needing_land(runs_dir, run_id))
    # No phase named and records in several phases: the ticket's last phase, but only to finish an already merged PR (checked below).
    inferred = phase is None and not a.task and _land_record(runs_dir, run_id, None)[2] > 1
    phase = _last_phase_with_manifest(runs_dir, run_id) if inferred else phase
    inferred = inferred and phase is not None
    land_mode = work_state.work_state_mode(_lake_provider(a)[0]) if a.apply else "files"
    record, item_path, lease_task = None, None, None
    land_at, ticket_tasks = None, []
    merged_pr: dict | None = None
    if phase:
        if land_mode == "store":
            _backfill_phase_task_files(runs_dir, run_id, phase)
        phase_record, task_records, task_paths, searched = _land_phase_record(runs_dir, run_id, phase)
        if phase_record is None:
            print(f"land: no phase record at {searched}")
            return 2
        initiative = phase_record.get("initiative") or _initiative_of_phase(runs_dir.parent / "work", phase, run_id)
        items, items_path = (_phase_items(runs_dir.parent / "work", initiative, phase, runs_dir=runs_dir, land_mode=land_mode)
                              if initiative else (None, f"{runs_dir.parent / 'work'} (no single initiative holds phase {phase})"))
        if items is None:
            print(f"land: no work items at {items_path}, expected the phase's tickets")
            return 2
        initiative_title = _initiative_title(runs_dir.parent / "work", initiative)
        # An all-done phase is landed and its branch was deleted on purpose; only an unfinished phase needs the branch.
        missing_on = (None if all(i["status"] == "done" for i in items)
                      else _land_refetch_phase_branch(a, runs_dir, run_id, repo, f"epic/{initiative}/{phase}"))
        if missing_on is not None:
            print(f"land: stopped at fetch_phase_branch: phase branch not fetched from {missing_on}")
            return 2
        phase_pr, pr_forge = None, forge.forge_for(forge.forge_name(profile)) if a.apply else None
        if pr_forge is not None:
            phase_pr = _phase_pr_facts(repo, f"pr/{initiative}--{phase}", pr_forge)
            if isinstance(phase_pr, str):
                print(f"land: refusing, {phase_pr}")
                return 2
        read_forge = pr_forge or forge.forge_for(forge.forge_name(profile))
        merged_pr = (_phase_merged_pr(repo, initiative, phase, default_branch, read_forge, _carried_files(items, task_records),
                                      _parent_phase(runs_dir.parent / "work", initiative, phase))
                     if read_forge is not None else None)
        already_merged = merged_pr is not None
        if inferred and not already_merged:
            _, searched, count = _land_record(runs_dir, run_id, None)
            print(f"land: looked in {searched}, found {count} task records, expected 1")
            return 2
        land_at = _phase_land_at(phase_record, profile, runs_dir.parent / "work", initiative)
        ticket_items, phases = _ticket_items(runs_dir.parent / "work", initiative, runs_dir, land_mode, (phase, items)) if land.is_initiative(land_at) else ({}, [])
        ticket_tasks = list(ticket_items)
        # An already merged phase puts nothing new on the target branch; a ticket land carries the earlier phases in its one PR.
        parent_wait = (None if already_merged else _parent_land_refusal(
            [i["id"] for i in items], _initiative_land_facts(runs_dir.parent / "work", initiative, runs_dir, land_mode), ticket_tasks))
        if parent_wait:
            return _land_parent_refused(a, parent_wait)
        for earlier_phase in (phases if land_mode == "store" else []):
            if earlier_phase != phase:
                _backfill_phase_task_files(runs_dir, run_id, earlier_phase)
        earlier = [r for p in phases if p != phase for r in _land_phase_record(runs_dir, run_id, p)[1]
                   if r["task"] in ticket_items and r.get("status", "approved") == "approved"]
        ticket_present = _ticket_present(repo, phase_record.get("card_branch") or f"epic/{initiative}/{phase}", earlier)
        # Approved in the store with no task record in any phase, even after the earlier-run fallback: built elsewhere.
        recorded = {r["task"] for p in phases for r in _land_phase_record(runs_dir, run_id, p)[1]} | {r["task"] for r in task_records}
        ticket_unrecorded = sorted(t for t in (ticket_items if land.is_initiative(land_at) else _phase_task_items(items)) if t not in recorded)
        from core.landing import phase_land_deferred  # lazy: core sits beside agent_tools in one venv
        plan_steps = land.land_plan(
            {**phase_record, "initiative": initiative, "initiative_title": initiative_title,
             "last_phase": not phase_land_deferred(land_at, phase, phases) if phases else bool(phase_record.get("last_phase"))},
            {}, default_branch,
            repo_facts, items=items, task_records=task_records, phase_pr=phase_pr,
            task_facts=_phase_task_facts(repo, f"epic/{initiative}/{phase}", items, task_records),
            already_merged=already_merged, ticket_tasks=ticket_tasks, ticket_present=ticket_present,
            ticket_records=[r for r in earlier if r["task"] in ticket_present], ticket_unrecorded=ticket_unrecorded,
        )
        if inferred:  # a re-run named by no phase, after the PR merged, leaves a task already done (by hand or by a first run) alone
            finished = {i["id"] for i in items if i["status"] == "done"}
            plan_steps = [s for s in plan_steps if not (s["kind"] == "mark_done" and s["task"] in finished)]
        earlier_paths = {t: p for ph in phases if ph != phase for t, p in _land_phase_record(runs_dir, run_id, ph)[2].items()}
        steps = _land_enrich(plan_steps, path=searched, worktree_root=a.worktree_root, task_paths={**earlier_paths, **task_paths},
                              umbrella=profile.get("umbrella_dir"), store_runs_dir=str(runs_dir),
                              task_items={**_phase_task_items(items), **ticket_items}, check_paths=True)
        lease_task = f"phase:{initiative}/{phase}"
    else:
        record, searched, count, source = _land_load(runs_dir, run_id, a.task)
        if record is None:
            print(f"land: looked in {searched}, found {count} task records, expected 1")
            return 2
        print(f"land: record from {source}", file=sys.stderr)
        parent_wait = (_parent_land_refusal([record["task"]], _initiative_land_facts(runs_dir.parent / "work", record["initiative"], runs_dir, land_mode))
                       if record.get("initiative") and record.get("phase") else None)
        if parent_wait:
            return _land_parent_refused(a, parent_wait)
        lease_task = record["task"]
        branches = _land_branches(repo, record, default_branch)
        item_path = (str(runs_dir.parent / "work" / record["initiative"] / record["phase"] / f"{record['task']}.md")
                     if record.get("initiative") else None)
        item_id, issue = _land_item_facts(item_path)
        sync = (
            argparse.Namespace(
                item=None,
                workspace=str(runs_dir.parent),
                dry_run=False,
                project=None,
                profile=str(_profile_path(a)),
                runs_dir=str(runs_dir),
            )
            if a.apply
            else None
        )
        scope, reference = _land_reference(profile, str(runs_dir.parent), record, item_id, issue, repo, sync)
        plan_steps = land.land_plan(record, branches, default_branch, repo_facts,
                                    tracker=_resolved_tracker(profile, runs_dir), issue=issue,
                                    scope=scope, reference=reference,
                                    phase_done=_land_phase_done(runs_dir, run_id, record),
                                    compared=_land_branch_counts(repo, record, default_branch, branches))
        steps = _land_enrich(plan_steps, path=searched, worktree_root=a.worktree_root, item_path=item_path,
                              workspace=str(runs_dir.parent), item_id=item_id,
                              profile=str(_profile_path(a)), runs_dir=str(runs_dir), umbrella=profile.get("umbrella_dir"))
    level = a.gate or _resolved_gate_level(runs_dir)
    if a.gate:
        print(f"land: --gate {level} overrides the resolved level")
    planned, steps = steps, (land.gate_steps(steps, level, land_at, ticket_tasks) if land_at else land.gate_steps(steps, level))
    level = land.gate_level(level, land_at)
    if not a.apply:
        _print_behind_warning(a, runs_dir, run_id, repo, default_branch)
        for line in _land_checks_lines(steps):
            print(line, file=sys.stderr)
        print(json.dumps(steps, indent=2))
        return 2 if any(s["kind"] == "refuse" for s in steps) else 0
    steps = _with_check_cap(steps, os.cpu_count() or 1, _check_capacity(runs_dir, profile, socket.gethostname()))
    forge_choice = forge.forge_name(profile)
    forge_module = forge.forge_for(forge_choice)
    if forge_module is None:
        print(f"land: no forge named {forge_choice} (built in: local, github, auto)")
        return 2
    stale = forge.missing_refs(forge_module)
    if stale:
        print(f"land: refusing, forge {forge_choice} does not accept {', '.join(stale)}; see agent_tools/forge.py")
        return 2
    print(f"forge: {forge_choice}")
    if land_mode == "store" and record is not None:
        stop = _land_store_stop(runs_dir, record, item_path)
        if stop is not None:
            print(stop)
            return 2
    if _repo_is_dirty(repo):
        print(f"land: refusing, {repo} is dirty")
        return 2
    sync, sync_detail = _sync_default_branch(repo, default_branch)
    if sync == "fast_forward":
        print(f"land: {sync_detail}")
    elif sync == "refuse":
        print(f"land: refusing, {sync_detail}")
        return 2
    cherry_pick, rebuild, recreate = next((s for s in steps if s["kind"] == "cherry_pick"), None), None, None
    _print_behind_warning(a, runs_dir, run_id, repo, default_branch)
    if cherry_pick is not None:
        # An existing pr/<task> is not necessarily stale or foreign: a retried
        # push can leave a same-tree branch behind, and the rerun should reuse it.
        decision = _land_leftover(repo, cherry_pick, _land_resume(repo, cherry_pick, forge_module), forge_module)
        rebuild = decision if "backup_refs" in decision else None
        recreate = decision if "old_tip" in decision else None
        if decision["kind"] == "refuse":
            print(f"land: refusing, branch {cherry_pick['onto']} already exists in {repo}: {decision['reason']}")
            return 2
        if decision["kind"] == "resume":
            print(f"land: resuming on existing branch {cherry_pick['onto']}")
        if decision["kind"] == "adopt":
            print(f"land: adopting open pull request #{decision['pr']} on {cherry_pick['onto']}")
        steps = land.resume_steps(steps, decision, cherry_pick["onto"])
        planned = land.resume_steps(planned, decision, cherry_pick["onto"])
    if merged_pr is not None:
        print(_merged_line(merged_pr))
    # Steps start here: every return from now on is logged. Earlier returns are not.
    repo_lost: list[str] = []
    stops: list[dict] = []

    repo_epoch = [0]  # the epoch the repo lease is held at now: a re-acquire after an expiry moves it, and the release must follow

    repo_slot = [0]  # the slot of the repo lease this land holds: it keeps the same one through a re-acquire

    def reacquire_repo() -> store_cli.LeaseResult:
        again = land_repo_lease.acquire_slot(runs_dir, str(repo), repo_slot[0], repo_holder, _REPO_LEASE_TTL_SECONDS)
        if isinstance(again, store_cli.LeaseGranted):
            repo_epoch[0] = again.epoch
        return again

    def walk(task_guard):
        repo_guard = _repo_lease_guard(
            str(repo),
            lambda: land_repo_lease.renew_slot(runs_dir, str(repo), repo_slot[0], repo_holder, repo_epoch[0], _REPO_LEASE_TTL_SECONDS),
            time.monotonic, _REPO_LEASE_TTL_SECONDS, repo_lost, reacquire_repo) if isinstance(got, store_cli.LeaseGranted) else None
        keep = [g for g in (repo_guard, task_guard) if isinstance(g, _LeaseGuard)]
        # The repo lease is checked first: a lost repo lease stops the walk before the task lease is renewed.
        guard = task_guard if repo_guard is None else _first_stop(repo_guard, task_guard)
        # The leftover rebuild deletes a branch, so it runs only once the repo lease is held: a concurrent
        # land mid-walk owns that branch, and the lease is what refuses this one before it touches it.
        failed = ("no repo lease to rebuild under" if not leased else _land_rebuild(repo, cherry_pick["onto"], rebuild)) if rebuild else None
        if failed:
            print(f"land: refusing, branch {cherry_pick['onto']} already exists in {repo}: {rebuild['reason']}")
            print(f"land: not rebuilt: {failed}")
            return 2, [], ""
        if rebuild:
            saved = " and ".join(f"{tip[:8]} to {ref}" for ref, tip in rebuild["backup_refs"].items())
            print(f"land: backed up {saved}, rebuilding {cherry_pick['onto']}")
        # An empty leftover holds nothing to back up; its local branch is deleted under the same lease.
        dropped = ("no repo lease to recreate under" if not leased
                   else _land_recreate(repo, cherry_pick["onto"], recreate["old_tip"])) if recreate else None
        if dropped:
            print(f"land: refusing, branch {cherry_pick['onto']} already exists in {repo}: {recreate['reason']}")
            print(f"land: not recreated: {dropped}")
            return 2, [], ""
        if recreate:
            print(f"land: {cherry_pick['onto']} at {recreate['old_tip'][:8]} holds nothing beyond {cherry_pick['from']}, "
                  f"recreating it")
        return _land_execute(repo, steps, planned, record, item_path, level, a.no_merge, forge_module, by=_holder_label(a),
                             mode=land_mode, guard=guard, stops=stops, keep=keep, pr=_merged_pr_ref(merged_pr),
                             lock=_merge_lock_factory(runs_dir, str(repo), repo_holder, repo_limit, leased))

    repo_holder = f"{_holder_label(a)}@{socket.gethostname()}:{os.getpid()}"
    repo_limit = land_limit.repo_land_limit(str(repo))
    repo_slot[0], got = land_repo_lease.acquire_in_slots(
        runs_dir, str(repo), repo_holder, _REPO_LEASE_TTL_SECONDS, socket.gethostname(), repo_limit)
    if getattr(a, "wait_lock", False) and isinstance(got, store_cli.LeaseRefused):
        print(land_repo_lease.waiting_message(str(repo), got.holder))
        repo_slot[0], got = land_repo_lease.acquire_in_slots_waiting(
            runs_dir, str(repo), repo_holder, _REPO_LEASE_TTL_SECONDS, socket.gethostname(), repo_limit,
            getattr(a, "wait_lock_timeout", 600), _LAND_LOCK_POLL_SECONDS)
    leased = isinstance(got, store_cli.LeaseGranted)
    if isinstance(got, store_cli.LeaseGranted):
        repo_epoch[0] = got.epoch
    if isinstance(got, store_cli.LeaseRefused):
        print(land_repo_lease.refusal_message(str(repo), got.holder or "another land"))
        walked = None
    elif isinstance(got, store_cli.LeaseGranted):
        try:
            walked = _land_walked(a, runs_dir, land_mode, lease_task,
                                   lambda: _land_store_stop(runs_dir, record, item_path) if record else None, walk)
        finally:
            if not repo_lost:  # a refused renew means another land holds it now; releasing would not be ours to do
                # Release goes through the store, so a refused store can fail it too; that is warned, never raised
                # over the walk's own error, and the TTL plus the dead-pid takeover clear the lease for the next land.
                name = land_repo_lease.slot_lease_name(str(repo), repo_slot[0])
                try:
                    freed = land_repo_lease.release_slot(runs_dir, str(repo), repo_slot[0], repo_holder, repo_epoch[0])
                    why = freed.detail if isinstance(freed, store_cli.LeaseError) else (
                        "the store is not available" if isinstance(freed, store_cli.NotAvailable) else None)
                except Exception as exc:
                    why = f"{type(exc).__name__}: {exc}"
                if why is not None:
                    print(f"warning: repo lease {name} not released: {why}", file=sys.stderr)
    else:
        # A store outage (NotAvailable/LeaseError) must not block a land: walk without the repo lease.
        walked = _land_walked(a, runs_dir, land_mode, lease_task,
                               lambda: _land_store_stop(runs_dir, record, item_path) if record else None, walk)
    rc, reached, pr = walked if walked is not None else (2, [], "")  # a refused lease stops before any step, and is logged too
    task = record["task"] if record else None
    _mark_stopped_land(repo, runs_dir, rc, record, item_path, stops, cherry_pick, _holder_label(a), land_mode)
    _append_land_log(runs_dir, land.land_log_row(datetime.datetime.now(datetime.UTC).isoformat(), run_id, task, reached, rc, pr,
                                                   stops[0] if stops else None))
    _lake_after_land(a, runs_dir, rc, reached)
    return rc


_BATCH_POLL_S = 30.0  # how often a queue pass re-reads the sources while it waits for a full batch


class _BatchLease:
    """Edge. The repo land lease for one queue pass, through the same acquire, renew and release calls `_runs_land` makes.

    Taken on the first batch to open, never while sources are collected, so a normal land is not blocked meanwhile."""

    def __init__(self, runs_dir: Path, repo: Path, holder: str):
        self.runs_dir, self.repo, self.holder = runs_dir, repo, holder
        self.slot, self.epoch, self.held, self.busy = 0, 0, False, None
        self.lost: list[str] = []
        self.limit = land_limit.repo_land_limit(str(repo))

    def take(self) -> str | None:
        """The refusal text when another land holds the repo, else None. A store outage lands without the lease."""
        if self.busy is not None or self.held:
            return self.busy
        self.slot, got = land_repo_lease.acquire_in_slots(
            self.runs_dir, str(self.repo), self.holder, _REPO_LEASE_TTL_SECONDS, socket.gethostname(), self.limit)
        if isinstance(got, store_cli.LeaseRefused):
            self.busy = land_repo_lease.refusal_message(str(self.repo), got.holder or "another land")
        elif isinstance(got, store_cli.LeaseGranted):
            self.held, self.epoch = True, got.epoch
        return self.busy

    def _reacquire(self) -> store_cli.LeaseResult:
        again = land_repo_lease.acquire_slot(self.runs_dir, str(self.repo), self.slot, self.holder, _REPO_LEASE_TTL_SECONDS)
        if isinstance(again, store_cli.LeaseGranted):
            self.epoch = again.epoch
        return again

    def guard(self) -> _LeaseGuard | None:
        if not self.held:
            return None
        return _repo_lease_guard(
            str(self.repo),
            lambda: land_repo_lease.renew_slot(self.runs_dir, str(self.repo), self.slot, self.holder, self.epoch, _REPO_LEASE_TTL_SECONDS),
            time.monotonic, _REPO_LEASE_TTL_SECONDS, self.lost, self._reacquire)

    def merge_lock(self) -> Callable[[], contextlib.AbstractContextManager] | None:
        return _merge_lock_factory(self.runs_dir, str(self.repo), self.holder, self.limit, self.held)

    def release(self) -> None:
        if not self.held or self.lost:  # a refused renew means another land holds it now; releasing is not ours to do
            return
        name = land_repo_lease.slot_lease_name(str(self.repo), self.slot)
        try:
            freed = land_repo_lease.release_slot(self.runs_dir, str(self.repo), self.slot, self.holder, self.epoch)
            why = freed.detail if isinstance(freed, store_cli.LeaseError) else (
                "the store is not available" if isinstance(freed, store_cli.NotAvailable) else None)
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"
        if why is not None:
            print(f"warning: repo lease {name} not released: {why}", file=sys.stderr)


def _ready_phase_ids(runs_dir: Path, work_root: Path) -> list[tuple[str, str, str]]:
    """Edge. `(initiative, phase, run)` for every phase directory under `runs/<run>/tasks/` that a work initiative owns."""
    found = [(_initiative_of_phase(work_root, p.name, p.parent.parent.name), p.name, p.parent.parent.name)
             for p in sorted(runs_dir.glob("*/tasks/*")) if p.is_dir()]
    return [(i, ph, r) for i, ph, r in found if i]


def _batch_sources(runs_dir: Path, repo: Path, land_mode: str, wanted: list[tuple[str, str, str]] | None) -> list[dict]:
    """Edge. The source mappings of the ready phases among `wanted`, or among every phase when None.

    Reads the records `_runs_land` reads for a phase. A phase with no tickets, no record, or a task still unapproved is skipped."""
    work_root = runs_dir.parent / "work"
    sources = []
    for initiative, phase, run in wanted if wanted is not None else _ready_phase_ids(runs_dir, work_root):
        items, _ = _phase_items(work_root, initiative, phase, runs_dir=runs_dir, land_mode=land_mode)
        if not items or not land_batch_effects.phase_ready(items):
            continue
        if land_mode == "store":
            _backfill_phase_task_files(runs_dir, run, phase)
        record, task_records, task_paths, _ = _land_phase_record(runs_dir, run, phase)
        if record is not None:
            sources.append(land_batch_effects.build_source(initiative, phase, run, str(repo), items, task_records, task_paths))
    return sources


def _batch_effects(a: argparse.Namespace, repo: Path, runs_dir: Path, profile: dict, lease: _BatchLease,
                   wanted: list[tuple[str, str, str]] | None):
    """Edge. The queue's `Effects`, or the reason they cannot be built. Its steps run through `_land_execute`, as a land's do."""
    try:
        default_branch = base_branch.base_ref(str(repo), getattr(a, "base", None)).removeprefix("origin/")
    except base_branch.BaseError as exc:
        return f"land: refusing, {exc}"
    forge_choice = forge.forge_name(profile)
    forge_module = forge.forge_for(forge_choice)
    if forge_module is None:
        return f"land: no forge named {forge_choice} (built in: local, github, auto)"
    stale = forge.missing_refs(forge_module)
    if stale:
        return f"land: refusing, forge {forge_choice} does not accept {', '.join(stale)}; see agent_tools/forge.py"
    repo_facts = _land_repo_facts(repo, profile, default_branch, forge_module)
    if repo_facts["cartridge_error"] and a.apply:
        return f"land: refusing, cartridge checks unavailable ({repo_facts['cartridge_error']}); the land would run fewer checks than the build did"
    land_mode = work_state.work_state_mode(_lake_provider(a)[0]) if a.apply else "files"
    level = a.gate or _resolved_gate_level(runs_dir)
    capacity = _check_capacity(runs_dir, profile, socket.gethostname())

    def run_steps(steps: list[dict], batch: list[dict]) -> tuple[int, list[str], str]:
        enriched = _with_check_cap(
            _land_enrich(steps, path="", worktree_root=a.worktree_root, workspace=str(runs_dir.parent),
                         profile=str(_profile_path(a)), runs_dir=str(runs_dir), umbrella=profile.get("umbrella_dir"),
                         task_paths={t: p for s in batch for t, p in s["task_paths"].items()},
                         task_items={t: f for s in batch for t, f in s["task_items"].items()}),
            os.cpu_count() or 1, capacity)
        repo_guard = lease.guard()
        return _land_execute(repo, enriched, steps, None, None, level, False, forge_module, by=_holder_label(a), mode=land_mode,
                             guard=_first_stop(repo_guard, None) if repo_guard else None, stops=[],
                             keep=[repo_guard] if repo_guard else [], lock=lease.merge_lock())

    return land_batch_effects.make_effects(
        lambda: _batch_sources(runs_dir, repo, land_mode, wanted), run_steps, repo_facts, default_branch,
        lambda: datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S"))


def _runs_land_batch(a: argparse.Namespace) -> int:
    """Edge. One merge-queue pass: collect, take the repo lease, open the first batch, print the result.

    Exits 2 when the lease is refused, 1 when every opened source was returned, else 0. A returned source keeps its
    tasks approved and its phase branch untouched."""
    repo = Path(a.repo).expanduser()
    wanted, bad = land_batch_effects.parse_source_ids(a.source or [])
    if bad:
        print(f"land-batch: {bad}")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"land-batch: {reason}")
        return 2
    if a.apply:
        guard_rc = _leader_guard_or_refuse(runs_dir, _holder_label(a), a.force, claim=not a.no_claim, explicit_label=getattr(a, "label", None))
        if guard_rc is not None:
            return guard_rc
    profile_text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(profile_text) if profile_text is not None else {}
    except route.ProfileError as exc:
        print(f"land-batch: profile unreadable: {exc}")
        return 2
    configured = land_batch_settings.merge_queue_setting(profile)
    settings = land_batch_settings.MergeQueue(a.batch_size or configured.batch_size, configured.wait_s if a.wait_s is None else a.wait_s)
    lease = _BatchLease(runs_dir, repo, f"{_holder_label(a)}@{socket.gethostname()}:{os.getpid()}")
    effects = _batch_effects(a, repo, runs_dir, profile, lease, wanted)
    if isinstance(effects, str):
        print(effects)
        return 2
    if not a.apply:
        sources = effects.read_sources()
        batch = land_batch_group.disjoint_batches(sources, settings.batch_size)[0] if sources else []
        print(json.dumps({"would_batch": [f"{s['initiative']}/{s['phase']}" for s in batch],
                          "ready": [f"{s['initiative']}/{s['phase']}" for s in sources]}, indent=2))
        return 0

    def land(group: list[dict]):
        busy = lease.take()
        return land_batch_run.Outcome(busy, None) if busy is not None else effects.land(group)

    try:
        result = land_batch_run.run_queue(settings, land_batch_run.Effects(effects.read_sources, land, effects.sleep, effects.now), _BATCH_POLL_S)
    finally:
        lease.release()
    if lease.busy is not None:
        print(lease.busy)
        return 2
    print(land_batch_run.render_lines(result), end="")
    return 1 if result.returned and not result.landed else 0


def _mark_land_conflict(runs_dir: Path, record: dict, item_path: str | None, reason: str, by: str, mode: str) -> None:
    """Edge. A land whose cherry-pick conflicted leaves the task `ready`, not `approved`, with `reason` as a `land_conflict`
    marker and a body note, on the store row and the item file. The row is written first, so a crash before the state
    move leaves an approved task with the marker, which `route edit --rebuild` accepts."""
    initiative, task = record.get("initiative"), record.get("task")
    if not initiative or not task:
        return
    stored = next((r for r in run_store.read_queue(runs_dir, initiative, kind="task") if r.get("task_id") == task), None)
    key = {"initiative": initiative, "task_id": task}
    failed = "no store row"
    if stored is not None:
        marked = {"extra": {**(stored.get("extra") or {}), land.LAND_CONFLICT_KEY: reason},
                  "body": route_edit.append_note(stored.get("body") or "", land.conflict_note(reason))}
        failed = run_store.upsert_row_detail(runs_dir, {**key, **marked})
        if failed:
            print(f"warning: the store did not take the land conflict of {task}: {failed}")
    if mode == "store":
        stop = land.set_state_stop(store_cli.set_state(runs_dir, initiative, task, "ready", by, expected="approved"))
        if stop is not None:
            print(stop)
            if stored is not None and not failed:  # the task is not ours to mark: put the row back as it was
                run_store.upsert_row_detail(runs_dir, {**key, "extra": stored.get("extra") or {}, "body": stored.get("body") or ""})
            return
    item = Path(item_path) if item_path else None
    if item is not None and item.exists():
        new_text = land.conflict_to_ready(item.read_text(encoding="utf-8"), reason)
        if new_text is not None:
            item.write_text(new_text, encoding="utf-8")
            queue_export.record_written_paths(runs_dir.parent, [item.absolute()])
    if mode != "store":
        line = store_cli.mirror_state(runs_dir, initiative, task, "ready", by)
        if line is not None:
            print(line)
    print(f"land: {task} is ready again, to be rebuilt: {reason}")


def _mark_stopped_land(repo: Path, runs_dir: Path, rc: int, record: dict | None, item_path: str | None, stops: list[dict],
                       cherry_pick: dict | None, by: str, mode: str) -> None:
    """Edge. After a land that stopped (exit 1) on its cherry-pick conflicting, mark the task for a rebuild."""
    if rc != 1 or record is None or not stops or cherry_pick is None:
        return
    base = f"{cherry_pick['from']}@{(_git_out(repo, 'rev-parse', cherry_pick['from']) or '')[:8]}"
    reason = land.land_conflict_reason(stops[0]["stopped_step"], stops[0]["stop_detail"], base)
    if reason is not None:
        _mark_land_conflict(runs_dir, record, item_path, reason, by, mode)


def _append_land_log(runs_dir: Path, row: dict) -> None:
    jsonl_log.append_line(runs_dir / "land.jsonl", row, sync=True)


def should_sync_lake(rc: int, reached: Sequence[str], extra_imports: bool, config_resolved: bool,
                     catalog_path: Path | None, catalog_exists: bool) -> bool:
    """A land syncs the lake when it exited 0 having reached `mark_done`, the extra imports and the config resolved.

    `catalog_path` is the SQLite catalog file, None for any other catalog; that file must already exist."""
    return (rc == 0 and "mark_done" in reached and extra_imports and config_resolved
            and (catalog_path is None or catalog_exists))


def _lake_after_land(a: argparse.Namespace, runs_dir: Path, rc: int, reached: Sequence[str]) -> None:
    """Edge. Sync a lake that already exists once a land has closed its item. Never changes the land's exit code."""
    try:
        # Imported here, as `_lake_doctor` does: the lake modules stay off cli.py's import path.
        from agent_tools import lake_doctor

        runs = runs_dir.resolve()
        provider, problem = _lake_provider(a)
        config = lake_config.resolve_lake(provider, runs)
        catalog_path = lake_doctor.sqlite_catalog_path(config.catalog_uri)
        extra = all(importlib.util.find_spec(name) is not None for name in ("pyiceberg", "pyarrow"))
        exists = catalog_path is not None and catalog_path.exists()
        if not should_sync_lake(rc, reached, extra, problem is None, catalog_path, exists):
            return
        root = store_url.profile_traces_root(provider, runs).url
        root = root if "://" in root else str(Path(root).resolve())
        sync = _lake_real_run(config, store_url.profile_store_url(provider, runs), root)
        outcome = lake_lease.sync_under_lease(sync, runs)
        if isinstance(outcome, lake_lease.Synced):
            report = outcome.value
            rows = sum(t["rows_appended"] for t in report["tables"])
            print(f"lake: +{rows} rows, +{report['traces']['registered']} trace files")
        elif isinstance(outcome, lake_lease.SkippedHeld):
            print(f"lake: sync skipped (held by {outcome.holder})")
        else:
            print(f"lake: sync skipped ({outcome.name})")
    except Exception as exc:  # the land has already happened; a lake problem must not fail it
        print(f"lake: sync skipped ({type(exc).__name__})")


def _land_execute(repo: Path, steps: list[dict], planned: list[dict], record: dict | None, item_path: str | None,
                  level: str, no_merge: bool, forge_module=forge_github, by: str = _UNLABELED,
                  mode: str = "files", guard: Callable[[str], str | None] | None = None,
                  stops: list[dict] | None = None, keep: Sequence[_LeaseGuard] = (),
                  lock: Callable[[], contextlib.AbstractContextManager] | None = None, pr: str = "") -> tuple[int, list[str], str]:
    """Walk `steps`; return the exit code, the step kinds run in order, and the PR url ("" when none opened).

    `pr` seeds the running PR, so a land that opens none still records the PR that carried its work.

    A pr branch's land worktree is dropped before `merge` and on every stop; the branch itself stays.
    `guard(kind)`, when given, runs before each step; a reason back stops the land with exit 2 before that step.
    Each lease guard in `keep` also renews on a timer for the whole walk, so a step that blocks past the ttl keeps its lease.
    `stops`, when given, receives the land log keys of the step that stopped the land."""
    built: list[str] = []
    try:
        with contextlib.ExitStack() as renewing:
            for kept in keep:
                renewing.enter_context(kept.renewing())
            return _land_walk(repo, steps, planned, record, item_path, level, no_merge, forge_module, built, by=by, mode=mode,
                              guard=guard, stops=stops, lock=lock, pr=pr)
    finally:
        for branch in built:
            _remove_land_worktree(repo, branch)


def _note_stop(repo: Path, step: dict, detail: str, stops: list[dict] | None) -> None:
    """Edge. Print the closing `land: stopped at` line and hand the log keys to `stops`.

    A squash conflict resolves its branch and from refs here; neither moves when the squash is refused."""
    squash = step["kind"] == "squash_phase"
    fields = land.stop_fields(step["kind"], detail, _git_out(repo, "rev-parse", step["branch"]) if squash else None,
                              _git_out(repo, "rev-parse", step["from"]) if squash else None)
    print(f"land: stopped at {step['kind']}: {detail}")
    if stops is not None:
        stops.append(fields)


# The note land.py plans for an earlier phase; the chair reads it from this output to defer, not fail, that land. Change them together.
_TICKET_GRANULARITY_NOTE = "ticket granularity, the one pull request opens at the last phase"


def _land_walk(repo: Path, steps: list[dict], planned: list[dict], record: dict | None, item_path: str | None,
               level: str, no_merge: bool, forge_module, built: list[str], by: str = _UNLABELED,
               mode: str = "files", guard: Callable[[str], str | None] | None = None,
               stops: list[dict] | None = None,
               lock: Callable[[], contextlib.AbstractContextManager] | None = None, pr: str = "") -> tuple[int, list[str], str]:
    landing = ""  # what a landed task's review PRs are closed against; "" until a merge succeeds
    reached: list[str] = []
    looped = False
    pr = pr or _open_pr_ref(repo, steps, forge_module)
    pr_failed: tuple[dict, str] | None = None  # the pr step that failed after the push, and why
    merge_ran = False  # once a merge step succeeded, a later step's exception is a failed result with the tasks marked, not a traceback
    into_pr_branch = any(s["kind"] == "phase_done" for s in steps)  # only such a plan reports a pr failure instead of stopping
    i = 0
    while i < len(steps):
        step = steps[i]
        if step["kind"] == "refuse":
            print(f"refused: {step['reason']}")
            return 2, reached, pr
        if step["kind"] == "note":
            if step.get("unmerged") or _TICKET_GRANULARITY_NOTE in step["reason"]:
                print(step["reason"])
            i += 1
            continue
        if pr_failed is not None and step["kind"] not in _AFTER_PR_FAILURE:
            # The tasks are marked merged and the phase recorded; the final merge must not run on a PR that is not ready.
            print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i:]))
            _note_stop(repo, pr_failed[0], pr_failed[1], stops)
            return 1, reached, pr
        carried = _task_already_on_main(repo, step, record) if step["kind"] == "cherry_pick" else None
        walk = None if carried is None else _carried_walk(steps[i:], planned, carried)
        if walk is not None:
            steps, planned, pr = walk
            print(land.landed_via_line(pr))
            i = 0
            continue
        held = guard(step["kind"]) if guard is not None else None
        if held is not None:
            print(held)
            print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i:]))
            return 2, reached, pr
        reached.append(step["kind"])
        if step["kind"] in ("cherry_pick", "squash_phase", "reuse_branch", "patch_apply", "pr_branch_cut", "merge_phase", "batch_squash"):
            built.append(step.get("onto") or step["branch"])
        if step["kind"] == "mark_merged":
            pr_branch = next((s["pr_branch"] for s in steps if s["kind"] == "phase_done"), "")
            step = {**step, "pr_branch": pr_branch, "by": by, "mode": mode}
        if step["kind"] == "phase_done":
            rec = record or {}
            step = {**step, "phase_record_id": rec.get("run_id") or (f"{rec['run']}:{rec['phase']}" if rec.get("run") and rec.get("phase") else None)}
        if step["kind"] == "merge":
            # gh and git cannot delete a branch a worktree still holds.
            for branch in built:
                _remove_land_worktree(repo, branch)
            merged, merge_detail, halt = _merge_updating(repo, steps, i, pr, forge_module, guard, lock=lock)
            if halt is not None:
                # `_note_stop` prints the reason; printing it here too would say it twice.
                print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i:]))
                _note_stop(repo, step, halt, stops)
                return 1, reached, pr
        if step["kind"] == "mark_done" and step.get("recorded") is False:
            step = {**step, "pr": pr, "at": datetime.datetime.now(datetime.UTC).isoformat(), "by": by, "mode": mode}
        elif step["kind"] == "mark_done":
            if record is not None and not Path(step["path"]).exists():
                # The store copy is written out as the record file, so the path-derived run, phase and runs_dir below keep working.
                Path(step["path"]).parent.mkdir(parents=True, exist_ok=True)
                Path(step["path"]).write_text(json.dumps(record, indent=2), encoding="utf-8")
            step = {**_mark_done_facts(step, pr, datetime.datetime.now(datetime.UTC).isoformat()), "by": by, "mode": mode}
        raised = False  # the step raised after the merge; a returned failure keeps today's stop
        if step["kind"] == "merge":
            ok, detail = merged, merge_detail
        elif not merge_ran:
            ok, detail = _execute_land_step(repo, step, forge_module)
        else:
            try:
                ok, detail = _execute_land_step(repo, step, forge_module)
            except Exception as exc:
                ok, detail, raised = False, f"{type(exc).__name__}: {exc}", True
        if step.get("before") == "pr_create":
            # The sync may have just written `issue:`; the PR opened next must carry its `Closes`.
            steps = land.with_issue(steps, record, _land_item_facts(item_path)[1])
        pr = _landed_pr(step["kind"], ok, detail, pr, step.get("pr"))
        if not ok and into_pr_branch and step["kind"] in _PR_REPORT_STEPS:
            print(f"{step['kind']}: {detail}")
            print(f"warning: {step['kind']} failed after the push; the phase is merged into the pr branch")
            pr_failed = (step, detail)
            i += 1
            continue
        if step["kind"] == "checks" and not ok and detail.startswith(land.LAUNCH_ERROR):
            # A check whose executable `subprocess` can't find is a refusal,
            # not an ordinary failure, and it fires before `push` so a check
            # that never ran leaves no pushed branch behind.
            print(detail)
            _note_stop(repo, step, detail, stops)
            return 2, reached, pr
        print(f"{step['kind']}: {detail}")
        if ok and step["kind"] == "merge":
            merge_ran = True
            landing = _landing_ref(detail, pr, _pr_number(steps, i, pr), step.get("default_branch", "main"))
        if ok and step["kind"] == "mark_done":
            initiative = _step_initiative(step, record)
            if not landing:
                print(f"review PRs of {step['task']} not closed: no merge ran in this land")
            elif not initiative:
                print(f"warning: review PRs of {step['task']} not closed: no initiative on its record")
            else:
                for line in _close_review_prs(repo, initiative, step["task"], landing, forge_module):
                    print(line)
        if not ok and step["kind"] in ("cherry_pick", "squash_phase") and detail.startswith(_GENERATE_FAILED):
            # `_execute_land_step` detached it and deleted its branch; the next land's same step removes it.
            built.remove(step["onto"])
            print(f"land: worktree left at {_land_worktree(repo, step['onto'])} for inspection")
        if not ok and step["kind"] == "merge" and not looped and land.merge_expects_check(detail):
            # A required check had not reported when the merge ran; wait once more, then retry the merge.
            waits = [j for j in range(i) if steps[j]["kind"] == "wait_checks"]
            if waits:
                looped = True
                print("land: merge refused for an expected required check; waiting for checks once more")
                i = waits[-1]
                continue
        if raised or (not ok and step["kind"] == "mark_done" and pr):
            # The PR is merged: record it and mark every task whose mark_done has not run, so none keeps holding its files.
            number = _pr_number(steps, i, pr)
            pr = pr or (f"#{number}" if number is not None else "")
            at = datetime.datetime.now(datetime.UTC).isoformat()
            for task, why in _mark_remaining_done(steps[i:], pr, at, lambda s: _execute_land_step(repo, {**s, "by": by, "mode": mode}, forge_module)):
                print(f"{task} not marked done: {why}")
        if not ok:
            remaining = [s["kind"] for s in steps[i + 1:]]
            print("stopped; remaining: " + ", ".join(remaining))
            _note_stop(repo, step, detail, stops)
            return 1, reached, pr
        if step["kind"] == "cherry_pick" and "task" not in step and land.is_skipped_all(detail):
            # Every commit is on the base already: close through the PR that carried them, with no push, checks or merge.
            number = _task_already_on_main(repo, step, record)
            walk = None if number is None else _carried_walk(steps[i + 1:], planned, number)
            if walk is None:
                why = f"{detail}, but no pull request number was found on {step['from']} for {step['branch']}"
                print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i + 1:]))
                _note_stop(repo, step, why, stops)
                return 1, reached, pr
            steps, planned, pr = walk
            print(land.landed_via_line(pr))
            i = 0
            continue
        if step["kind"] == "wait_checks" and no_merge:
            print("stopping after wait_checks (--no-merge)")
            return 0, reached, pr
        i += 1
    if pr_failed is not None:
        _note_stop(repo, pr_failed[0], pr_failed[1], stops)
        return 1, reached, pr
    stop = land.gate_stop(planned, steps, level, pr)
    if stop:
        print(stop)
    return (3 if stop else 0), reached, pr


def _runs_recover(a: argparse.Namespace) -> int:
    """Merge one approved task's commit into its phase branch, the remedy for
    a merge the harness escalated to `self_modification` and so never
    applied. Never runs the repo's checks, never pushes: this only lands one
    commit on a local phase branch, and the gate to `main` stays a person's."""
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
    mark_done_step = {"kind": "mark_done", "item": item_path, "from": "approved", "to": "done"}
    branches = _recover_branches(repo, record, phase_branch)
    step = land.recover_plan(record, branches)[0]
    if step["kind"] == "already_recovered":
        print(f"recover: {step['branch']} already contains the commit ({step['reason']}); nothing to do")
        if a.dry_run:
            print(json.dumps(mark_done_step))
            return 0
        _close_approved_item(item_path, runs_dir=runs_dir, by=_holder_label(a))
        return 0
    if step["kind"] == "refuse":
        print(f"refused: {step['reason']}")
        return 2
    source, target, subject = step["source"], step["target"], step["commit_subject"]
    message = f"Merge branch '{source}' into {target}"
    if a.dry_run:
        print(f"recover: would merge {source} into {target} --no-ff -m {message!r} ({subject!r})")
        print(json.dumps(mark_done_step))
        return 0
    before = subprocess.run(["git", "-C", str(repo), "rev-parse", target], capture_output=True, text=True).stdout.strip()
    wt = Path(tempfile.mkdtemp(prefix="recover-"))
    add = subprocess.run(["git", "-C", str(repo), "worktree", "add", str(wt), target], capture_output=True, text=True)
    if add.returncode != 0:
        print(f"refused: could not check out {target}: {add.stderr.strip() or add.stdout.strip()}")
        return 2
    try:
        merge = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", source, "-m", message], capture_output=True, text=True)
        if merge.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True, text=True)
            print(f"refused: merge of {source} into {target} was not clean: {merge.stderr.strip() or merge.stdout.strip()}")
            return 2
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
    after = subprocess.run(["git", "-C", str(repo), "rev-parse", target], capture_output=True, text=True).stdout.strip()
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--stat", before, after], capture_output=True, text=True)
    print(f"recover: {target} {before[:8]} -> {after[:8]}")
    print(diff.stdout.strip())
    _close_approved_item(item_path, runs_dir=runs_dir, by=_holder_label(a))
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


def _pr_land(a: argparse.Namespace) -> int:
    """Wait for the PR's required checks, squash-merge it and mark its tasks done; exit 2 when it was not merged."""
    runs_dir, reason = _runs_dir_for_land(argparse.Namespace(runs_dir=None, profile=None))
    deps = pr_land.real_deps(runs_dir or Path("."), a.timeout, _LAND_LOCK_POLL_SECONDS)
    if runs_dir is None:  # the merge still goes ahead; the carried tasks are named as not marked
        deps = dataclasses.replace(deps, mark_done=lambda task, url: (False, f"no runs dir: {reason}"))
    result = pr_land.land_pr(Path(a.repo).expanduser(), a.number, deps)
    print(f"{result.event}: {result.text}")
    return 0 if result.event == pr_land.LEVEL_MERGED else 2


def _epic_watch(a: argparse.Namespace) -> int:
    out = epic.watch(a.pidfile, log=a.log, max_seconds=a.max_seconds, interval=a.interval)
    print(json.dumps(out, indent=2) if a.json else "\n".join(
        [f"pid {out['pid']}: {'fenced' if out['state'] == 'fenced' else 'finished' if out['finished'] else 'still running'}",
         *out.get("quarantined", []), *out.get("reused", []), *(x for x in (out.get("summary"), out.get("usage")) if x)]))
    return 0 if out["finished"] else 3






def _plan_serve(a: argparse.Namespace) -> int:
    if a.check:
        print(plan.check(a.dir))
    print(plan.serve(a.dir, kind=a.kind, open_browser=not a.no_open)); return 0


DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"


def _profile_path(a: argparse.Namespace) -> Path:
    return Path(getattr(a, "profile", None) or os.environ.get("AGENT_TOOLS_PROFILE") or DEFAULT_PROFILE).expanduser()


def _read_text_or_none(p: Path):
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _mtime_iso(p: Path):
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC).isoformat()


def _heartbeats(runs_dir: Path, run_ids) -> dict:
    """Edge. `run id -> heartbeat_at` for each run that holds its own store lease."""
    leases = {run_id: run_store.lease(runs_dir, run_id) for run_id in run_ids}
    return {run_id: row[2] for run_id, row in leases.items() if row is not None and row[0] == run_id}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _with_remote_lanes(runs_dir: Path, local_entries: list) -> list:
    """Edge. The local run entries, then an entry for each live store lease no local pidfile names.

    `now` must be `_now_iso()`'s form: `live_lanes` compares lease times as text."""
    lanes = run_store.live_lanes(runs_dir, _now_iso())
    remote = run_store.remote_lanes(lanes, {entry["id"] for entry in local_entries})
    return [*local_entries, *route.remote_lane_entries(remote)]


def _with_lane_fields(rows: list, runs: list, now: str) -> list:
    """`status_rows` output with `host` and `remote` on every row. A remote row reads `alive` and carries its lane text as `summary`, because `status_rows` has no place for either."""
    by_id = {run["id"]: run for run in runs}

    def one(row: dict) -> dict:
        run = by_id.get(row["id"], {})
        if not run.get("remote"):
            return {**row, "host": run.get("host"), "remote": False}
        lane = route._lane(run, now)
        return {**row, "state": "alive", "host": run["host"], "remote": True, "summary": lane.removeprefix(run["id"] + " ")}

    return [one(row) for row in rows]


def _efficiency_figures(runs_dir: Path, now: str) -> dict | None:
    """Edge. Today's (UTC day of `now`) cost, landed count, per-landed cost and first-try share; None with no store or one that fails to answer.

    The one place both the docket's `efficiency:` line and the dash feed's `spend.today` read from."""
    today = now[:10]
    tomorrow = (datetime.date.fromisoformat(today) + datetime.timedelta(days=1)).isoformat()
    try:
        spend = run_store.cost_since(runs_dir, today, tomorrow)
        if spend is None:
            return None
        landed = route.landed_today(_read_text_or_none(runs_dir / "land.jsonl") or "", today)
        counts = run_store.build_counts(runs_dir, [t for _, t in landed if t is not None], [r for r, _ in landed if r is not None])
    except (*run_store._DB_ERRORS, OSError, RuntimeError, ValueError, TypeError):
        return None
    rate, measured = route.first_try(landed, counts)
    return {
        "date": today,
        "cost": spend,
        "landed": len(landed),
        "per_landed": spend / len(landed) if landed else None,  # deprecated alias of cost_per_completion
        "completions": len(landed),
        "cost_per_completion": spend / len(landed) if landed else None,
        "first_try_rate": rate,
        "measured": measured,
    }


def _efficiency(runs_dir: Path, now: str) -> str | None:
    """Edge. The `efficiency:` line for the UTC day of `now`; None with no store or one that fails to answer."""
    figures = _efficiency_figures(runs_dir, now)
    if figures is None:
        return None
    return route.efficiency_line(figures["cost"], figures["completions"], figures["first_try_rate"], figures["measured"])


def _gather_context(profile_path: Path, mode: str = "files"):
    """Read the profile and the workspace; return (profile_or_none, reason, intake, runs, initiatives, problems, efficiency)."""
    text = _read_text_or_none(profile_path)
    if text is None:
        return None, f"no profile at {profile_path}", [], [], [], [], None
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return None, f"profile unreadable: {exc}", [], [], [], [], None
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        return profile, "workspace_dir not set in profile", [], [], [], [], None
    ws = Path(workspace).expanduser()
    pid_paths = sorted((ws / "runs").glob("*.pid"))
    pids = {p.stem: t for p in pid_paths if (t := _read_text_or_none(p)) is not None}
    alive = {rid: epic.run_live(route.parse_pid(t), ws / "runs" / f"{rid}.pid") for rid, t in pids.items()}
    started = {rid: _mtime_iso(ws / "runs" / f"{rid}.pid") for rid in pids}
    # the initiative id is the DIRECTORY name: work/<initiative>/<phase>/<task>.md;
    # the edge only reads and names the path parts — route.work_item normalises
    items = _stored_work_items(ws, mode)
    return (profile, "",
            _intake_groups(ws, items),
            _with_remote_lanes(ws / "runs", route.run_entries(pids, alive, started, _heartbeats(ws / "runs", pids))),
            route.initiative_summaries(items),
            route.state_problems(items),
            _efficiency(ws / "runs", _now_iso()))


def _route_context(a: argparse.Namespace) -> int:
    # The edge is allowed to catch everything _gather_context can raise: this
    # command must never take a session down with it (spec §2). That
    # tolerance is for profile/workspace file reads only — it stops at the
    # gatherer, per charter A6, so a bug in the usage assessment surfaces
    # instead of erasing an otherwise-good docket (run tools-pacing-7).
    try:
        # Inside the guard on purpose: the provider profile is a file read like the rest.
        provider_profile = _lake_provider(a)[0]
        work_mode, work_line = work_state.resolve(provider_profile)
        profile, reason, intake, runs, initiatives, problems, efficiency = _gather_context(_profile_path(a), work_mode)
    except Exception as exc:
        print(f"routing: context unavailable ({type(exc).__name__}: {exc})")
        return 0
    # Computed once via the gatherer, same reason `route launch` gates on;
    # only when there is a workspace to gather usage files from.
    usage_reason = (
        _usage_assessment(
            Path(profile["workspace_dir"]).expanduser() / "runs",
            profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd"), profile.get("weekly_reset"),
            profile.get("monthly_budget_usd"), profile.get("budget_timezone"),
        ).reason
        if not reason and profile is not None else None
    )
    if a.json:
        doc = route.context_document(profile, intake, runs, initiatives, problems)
        if reason:
            doc["reason"] = reason
        if usage_reason is not None:
            doc["usage"] = usage_reason
        print(json.dumps(doc, indent=2))
    elif reason:
        # nothing was read, so print no counts: an unread workspace must not
        # look like an empty one
        empty_groups = {"queued": [], "decomposed": [], "landed": []}
        first_line = route.render_context(profile, empty_groups, [], []).partition("\n")[0]
        print(f"{first_line} ({reason})")
    else:
        gate_level = _resolved_gate_level(Path(profile["workspace_dir"]).expanduser() / "runs")
        rendered = route.render_context(profile, intake, runs, initiatives, problems, gate_level=gate_level, now=_now_iso(), efficiency=efficiency)
        # Printed whenever a provider profile was read. With none (`_lake_provider` gives `{}`)
        # there is no work-state source to name, and the output stays as it was before this line.
        if provider_profile:
            print(work_line)
        print(f"{rendered}\nusage: {usage_reason}")
    return 0


def _refuse_if_already_running(runs_dir: Path, prefix: str):
    """spec: at most one live run per `prefix` — the single-writer rule
    applied to a run-id prefix. Returns the refusal line to print, or
    None when no run under `prefix` has a live process. A fenced run
    answers to its pid but lost its lease, so it refuses too."""
    host = socket.gethostname()
    for run in epic.live_runs(runs_dir, prefix):
        if run["state"] == "fenced":
            return (
                f"routing: {run['run']} is fenced (pid {run['pid']} on {host}): its lease heartbeat stopped, "
                f"so the epoch fence refuses its writes. Stop pid {run['pid']} on {host} before relaunching"
            )
        return f"routing: {run['run']} is already running (pid {run['pid']}) on {host}"
    return None


def _work_items(ws: Path) -> list:
    return [
        route.work_item(fields, initiative=task_path.parent.parent.name, phase_dir=task_path.parent.name, stem=task_path.stem)
        for task_path in sorted((ws / "work").glob("*/*/*.md"))
        if task_path.name != "initiative.md"
        for text in [_read_text_or_none(task_path)] if text is not None
        for fields in [route.parse_frontmatter(text)[0]]
    ]


def _stored_work_items(ws: Path, mode: str) -> list:
    """Edge. `_work_items` with each state from the store under mode "store"; the store is not read under "files"."""
    items = _work_items(ws)
    return route.with_store_states(items, run_store.work_items(ws / "runs") if mode == "store" else [], mode)


def _initiative_texts(ws: Path) -> dict:
    return {
        p.name: _read_text_or_none(p / "initiative.md") or ""
        for p in sorted((ws / "work").glob("*"))
        if (p / "initiative.md").is_file()
    }


def _intake_groups(ws: Path, items: list) -> dict:
    intake_root = ws / "intake"
    paths = sorted(intake_root.glob("*.md")) + sorted((intake_root / "done").glob("*.md"))
    files = {str(p.relative_to(intake_root)): t for p in paths if (t := _read_text_or_none(p)) is not None}
    texts = _initiative_texts(ws)
    states = route.initiative_states(sorted(texts), items)
    initiatives = [{"id": iid, "done": states[iid], "text": texts[iid]} for iid in texts]
    return route.intake_groups(route.intake_entries(files), initiatives)


def _intake_groups_for(ws: Path, items: list):
    """`_intake_groups` from disk, or `None` with no `intake/` dir."""
    return _intake_groups(ws, items) if (ws / "intake").is_dir() else None


def _status_rows_for(runs_dir: Path, short_ids: Mapping[str, str] | None = None) -> list:
    pids = {p.stem: t for p in sorted(runs_dir.glob("*.pid")) if (t := _read_text_or_none(p)) is not None}
    alive = {run_id: epic.run_live(route.parse_pid(t), runs_dir / f"{run_id}.pid") for run_id, t in pids.items()}
    fenced = {run_id: epic.run_state(route.parse_pid(t), runs_dir / f"{run_id}.pid") == "fenced" for run_id, t in pids.items()}
    started = {run_id: _mtime_iso(runs_dir / f"{run_id}.pid") for run_id in pids}
    runs = _with_remote_lanes(runs_dir, route.run_entries(pids, alive, started, _heartbeats(runs_dir, pids), fenced))
    summaries = {p.stem: epic.summarize_log(_read_text_or_none(p) or "") for p in runs_dir.glob("*.log")}
    entries = route.status_entries(runs, summaries)
    by_run = _run_short_ids([e["id"] for e in entries], short_ids or {})
    return _with_lane_fields(route.status_rows(entries, by_run), runs, _now_iso())


def _route_export(a: argparse.Namespace) -> int:
    """Edge. Refresh this machine's copy of the board from the store. No git, no lease."""
    profile = _courier_profile(a)
    if profile is None:
        return 2
    if work_state.work_state_mode(_lake_provider(a)[0]) != "store":
        print("export: work_state is files; the files are the board, so there is nothing to write")
        return 0
    workspace = Path(profile["workspace_dir"]).expanduser()
    try:
        skipped = _export_board(workspace / "runs", workspace)
    except Exception as exc:
        print(f"export: {type(exc).__name__}: {exc}")
        return 1
    print(f"export: {workspace} matches the store" + (f"; skipped {len(skipped)} contentless row(s)" if skipped else ""))
    return 0


def _route_drift(a: argparse.Namespace) -> int:
    """Report only: exits 0 whether or not the store and the files disagree."""
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    ws = Path(workspace).expanduser()
    rows = run_store.read_queue(ws / "runs")
    if not rows:
        print("store has no work_items")
        return 0
    items = _work_items(ws)
    files = [(item["initiative"], item["id"], item["state"]) for item in items]
    groups = _intake_groups_for(ws, items) or {}
    intake_files = [(entry["path"], name) for name, entries in groups.items() for entry in entries]
    task_rows = [r for r in rows if r.get("kind") == "task"]
    intake_rows = [r for r in rows if r.get("kind") == "intake"]
    found = route_drift.drift(files, task_rows, intake_files, intake_rows)
    print(route_drift.format_json(found) if a.json else route_drift.format_text(found))
    return 0


def _route_import_files(ws: Path) -> list[tuple[tuple[str, ...], str]]:
    """Edge: `(path_parts, text)` for every intake file, task file, and `initiative.md` under `ws`,
    `path_parts` relative to `ws`. `plan_import` still writes no row for an `initiative.md`; it reads
    the file's text only to derive an intake row's `queued`/`decomposed`/`landed` state."""
    paths = (
        sorted(ws.glob("intake/*.md"))
        + sorted(ws.glob("intake/done/*.md"))
        + sorted(ws.glob("work/*/*/*.md"))
        + sorted(ws.glob("work/*/initiative.md"))
    )
    return [(p.relative_to(ws).parts, t) for p in paths if (t := _read_text_or_none(p)) is not None]


def _route_import_workspace(a: argparse.Namespace) -> Path | str:
    """`--workspace`, else the profile's `workspace_dir`, else `.`, as `_sync_context` resolves it.
    A str is why the profile is unreadable."""
    if a.workspace:
        return Path(a.workspace).expanduser()
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError as exc:
        return f"profile unreadable: {exc}"
    return Path(profile.get("workspace_dir") or ".").expanduser()


def _route_import(a: argparse.Namespace) -> int:
    """Loads intake/ and work/<initiative>/<phase>/*.md files into the store's work_items table.
    Idempotent: importing the same files again reports zero written. Exits 2, after printing the
    failing row's key and the store's error, then the count written so far, the moment a row fails
    to upsert."""
    ws = _route_import_workspace(a)
    if isinstance(ws, str):
        print(f"route import: {ws}")
        return 2
    # A workspace with neither dir is a wrong path, not an empty queue: refuse rather than report "written 0".
    if not (ws / "work").is_dir() and not (ws / "intake").is_dir():
        print(f"route import: no work/ or intake/ under {ws}; pass --workspace or set workspace_dir in the profile")
        return 2
    runs_dir = ws / "runs"
    plan = route_import.plan_import(_route_import_files(ws), run_store.read_queue(runs_dir))
    written = 0
    for row in plan.to_write:
        detail = run_store.upsert_row_detail(runs_dir, row)
        if detail:
            print(f"route import: store refused {row['initiative']}/{row['task_id']}: {detail}")
            print(route_import.format_summary(written, plan))
            return 2
        written += 1
    print(route_import.format_summary(written, plan))
    return 0


def _paths_lines(changes: Sequence[Mapping[str, str]]) -> list[str]:
    """One `key: old -> new` line per change, then the count."""
    return [f"{c['key']}: {c['old']} -> {c['new']}" for c in changes] + [f"{len(changes)} change(s)"]


def _open_files(files: Mapping[str, str]) -> dict[str, str]:
    """Pure. `files` without done or dropped records. An `initiative.md` is done when it says so or when route's rule
    does: it has task files and all are done or dropped. The file carries no state of its own, so its tasks decide."""
    parts = {rel: rel.split("/") for rel in files}
    states = {rel: route.parse_frontmatter(files[rel])[0].get("state") for rel, p in parts.items() if len(p) == 4 and p[0] == "work"}
    tasks = [{"initiative": parts[rel][1], "state": s} for rel, s in states.items() if s is not None]  # a stateless file is no task
    initiative_files = {rel: p[1] for rel, p in parts.items() if p[0] == "work" and p[2:] == ["initiative.md"]}
    finished = route.initiative_states(sorted(set(initiative_files.values())), tasks)
    return {
        rel: text
        for rel, text in files.items()
        if repo_path_migrate._file_is_open(text) and not finished.get(initiative_files.get(rel), False)
    }


def _open_rows(rows: Sequence[Mapping]) -> list[Mapping]:
    """Pure. `rows` without done or dropped records. An initiative row holds the default state, so it is done
    when route's rule says so of its task rows: it has tasks and all are done or dropped."""
    tasks = [{"initiative": r["initiative"], "state": r.get("state")} for r in rows if r.get("kind") == "task"]
    finished = route.initiative_states(sorted({r["initiative"] for r in rows if r.get("kind") == "initiative"}), tasks)
    return [
        r
        for r in rows
        if repo_path_migrate._is_open(r.get("state")) and not (r.get("kind") == "initiative" and finished[r["initiative"]])
    ]


def _paths_apply_rows(
    runs_dir: Path, rows: Sequence[Mapping], home: str, move: tuple[str, str] | None = None, open_only: bool = False
) -> int:
    """Edge. Upsert only `extra` and `surfaces` of each changed row, then export once; exit 1 on a refusal or failed export.
    The store owns state, needs and the claim columns, so a stale snapshot of them is never written back.
    With `open_only`, done and dropped rows are skipped."""
    refused = 0
    taken = 0
    for row in _open_rows(rows) if open_only else rows:
        migrated = repo_path_migrate.migrate_row(row, home, move=move)
        if migrated is None:
            continue
        partial = {k: migrated[k] for k in ("initiative", "task_id", "extra", "surfaces") if k in migrated}
        if run_store.upsert_row(runs_dir, partial):
            taken += 1
        else:
            refused += 1
            print(f"route paths: store refused {row.get('initiative')}/{row.get('task_id')}")
    if taken:
        try:
            _export_board(runs_dir, runs_dir.parent)
        except RuntimeError as exc:
            print(f"route paths: rows are migrated but the file export failed: {exc}")
            return 1
    return 1 if refused else 0


def _paths_apply_files(
    ws: Path, files: Mapping[str, str], home: str, move: tuple[str, str] | None = None, open_only: bool = False
) -> int:
    """Edge. Write each changed work file in place; with `open_only`, files of done and dropped records are left alone."""
    for rel, text in (_open_files(files) if open_only else files).items():
        migrated = repo_path_migrate.migrate_text(text, home, move=move)
        if migrated is not None:
            (ws / rel).write_text(migrated, encoding="utf-8")
    return 0


def _route_paths(a: argparse.Namespace) -> int:
    """Dry run unless `--apply`: lists each recorded repo path that would move to the tilde form.
    Store mode reads and upserts queue rows, then re-exports the files; file mode edits the files."""
    ws = _route_import_workspace(a)
    if isinstance(ws, str):
        print(f"route paths: {ws}")
        return 2
    if not (ws / "work").is_dir() and not (ws / "intake").is_dir():
        print(f"route paths: no work/ or intake/ under {ws}; pass --workspace or set workspace_dir in the profile")
        return 2
    store_mode = work_state.work_state_mode(_lake_provider(a)[0]) == "store"
    runs_dir, home = ws / "runs", os.path.expanduser("~")
    from agent_tools import store_fill  # imported here: cli loads a command module only when it runs

    rows = run_store.read_queue(runs_dir) if store_mode else []
    if store_mode and not rows and store_fill._read_store(runs_dir) is None:
        print(f"route paths: the store under {runs_dir} cannot be read; nothing was checked")
        return 2
    files = {} if store_mode else {"/".join(parts): text for parts, text in store_fill._workspace_files(ws)}
    move = (repo_path.contract(a.move[0], home), repo_path.contract(a.move[1], home)) if a.move else None
    open_only = move is not None
    if open_only:
        print("route paths: only open records are moved; done and dropped records are left alone")
    listed_rows, listed = (_open_rows(rows), _open_files(files)) if open_only else (rows, files)
    print("\n".join(_paths_lines(repo_path_migrate.plan(listed_rows, listed, home, move=move, open_only=open_only))))
    if not a.apply:
        return 0
    code = (
        _paths_apply_rows(runs_dir, rows, home, move, open_only)
        if store_mode
        else _paths_apply_files(ws, files, home, move, open_only)
    )
    if move is None:
        return code
    rows_after = run_store.read_queue(runs_dir) if store_mode else []
    files_after = {} if store_mode else {"/".join(parts): text for parts, text in store_fill._workspace_files(ws)}
    left_rows, left_files = (_open_rows(rows_after), _open_files(files_after)) if open_only else (rows_after, files_after)
    left = len(repo_path_migrate.plan(left_rows, left_files, home, move=move, open_only=open_only))
    print(f"route paths: {left} value(s) still under {a.move[0]}")
    return 1 if left else code


def _initiative_token(runs_dir: Path, token: str) -> str:
    """Edge. The initiative slug for a token that may be its short id; a slug comes back unchanged."""
    return run_store.resolve_initiative(runs_dir, token)


def _initiative_path(runs_dir: Path, token: str) -> Path:
    """Edge. A launch argument as a directory: a path stays as given, a bare short id becomes `<workspace>/work/<slug>`."""
    path = Path(token).expanduser()
    if len(path.parts) != 1 or path.exists():
        return path
    slug = _initiative_token(runs_dir, token)
    return path if slug == token else runs_dir.parent / "work" / slug


def _route_draft(a: argparse.Namespace, act: Callable[[Path, draft_apply.Store, str], int]) -> int:
    """Resolve the workspace and the work-state mode, then hand both and the initiative slug to `act`."""
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    store = draft_apply.Store(
        work_state.work_state_mode(_lake_provider(a)[0]),
        lambda initiative: run_store.work_items(runs_dir, initiative),
        lambda initiative, task, state, by, expected: store_cli.set_state(runs_dir, initiative, task, state, by, expected),
    )
    return act(runs_dir.parent / "work", store, _initiative_token(runs_dir, a.initiative))


def _route_approve(a: argparse.Namespace) -> int:
    return _route_draft(a, lambda work, store, slug: draft_apply.approve(work, slug, a.task, a.by, store))


def _route_decline(a: argparse.Namespace) -> int:
    return _route_draft(a, lambda work, store, slug: draft_apply.decline(work, slug, a.reason, a.by, store))


def _live_runs(runs_dir: Path, now: str) -> set[str]:
    """Edge. Every live run on any host: the store's live leases, which remote lanes hold, plus local pidfile lanes.

    A lane carries no initiative, so `route edit` and `route remove` both narrow the set with `_live_runs_on` to the
    runs recorded on the target initiative. A run id says nothing reliable about its initiative (`--run-id` takes any name).
    `now` is `_now_iso()`'s form, which `live_lanes` compares as text."""
    leased = {lane.run for lane in run_store.live_lanes(runs_dir, now)}
    return leased | {pf.stem for pf in runs_dir.glob("*.pid") if _lane_live(runs_dir, pf.stem)}


def _recorded_initiatives(runs_dir: Path, runs: Collection[str]) -> dict[str, str | None]:
    """Edge. Run to the initiative its `<run>.launched.json` (local) or `<run>.remote.json` (`--on`) names, None for none.

    A run with no such file, an unreadable one, or one without an `initiative` key is left out: its initiative is unknown.
    Runs launched before the key existed are in that case."""
    def read(run: str) -> dict:
        for path in (runs_dir / f"{run}.launched.json", remote_lane.remote_record_path(runs_dir, run)):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and "initiative" in doc and isinstance(doc["initiative"], str | None):
                return doc
        return {}
    docs = {run: read(run) for run in runs}
    return {run: doc["initiative"] for run, doc in docs.items() if "initiative" in doc}


def _live_runs_on(live: Collection[str], initiative: str, recorded: Mapping[str, str | None]) -> set[str]:
    """Live runs that may be on `initiative`: every one except a run whose record names another initiative or none.

    A run absent from `recorded` is unknown and stays, so removal refuses until it is stopped."""
    return {run for run in live if run not in recorded or recorded[run] == initiative}


def _after_ids(values: Sequence[str] | None) -> list[str] | None:
    """`--after` values as ids: None when the flag was absent, [] when every value was empty (clear the list)."""
    return None if values is None else [part.strip() for value in values for part in value.split(",") if part.strip()]


def _held_entries(rows: Sequence[Mapping]) -> list[dict]:
    """`render_status`'s `held` rows from queue rows: the intake id and its `hold_reason` when it has one."""
    return [{"id": row["task_id"], "reason": str(row["extra"].get("hold_reason") or "")} for row in rows]


def _waiting_by_entry_id(queue: Sequence[Mapping], waiting: Mapping[str, Sequence[str]]) -> dict[str, Sequence[str]]:
    """`waiting` (keyed by file stem) re-keyed by the `id:` the intake group lines print, which differs from the stem under slug ids."""
    ids = {row["task_id"]: str(row["extra"].get("id") or row["task_id"]) for row in queue if row["kind"] == "intake"}
    return {ids.get(task_id, task_id): names for task_id, names in waiting.items()}


def _route_edit(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    now = _now_iso()
    live = _live_runs(runs_dir, now)
    target_id = _initiative_token(runs_dir, a.id)
    on_target = _live_runs_on(live, target_id, _recorded_initiatives(runs_dir, live))
    result = route_edit.apply_edit(
        runs_dir, runs_dir.parent, target_id, title=a.title, body=a.body, body_file=a.body_file, repo=a.repo,
        surfaces=a.surfaces, state=a.state, dry_run=a.dry_run, live_runs=on_target, now=now,
        rebuild=getattr(a, "rebuild", False), force=getattr(a, "force", False), pr=getattr(a, "pr", None),
        link=getattr(a, "link", None), after=_after_ids(getattr(a, "after", None)), epic=getattr(a, "epic", None),
        note=getattr(a, "note", None),
    )
    if result.code != route_edit.DONE:
        print(f"edit: {'refused' if result.code == route_edit.REFUSED else 'failed'}: {result.text}")
    else:
        print(result.text.rstrip("\n") or "edit: no change")
    return result.code


def _route_epic(a: argparse.Namespace) -> int:
    if a.action != "new":
        print(f"epic: unknown action {a.action!r}; usage: route epic new <id> [--title T] [--target-branch B] [--dry-run]")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    code, text = route_epic.create_epic(runs_dir.parent, a.id, a.title, a.target_branch, a.dry_run)
    print(text.rstrip("\n"))
    return code


def _route_remove(a: argparse.Namespace) -> int:
    if not a.id and a.match is None:
        print("remove: give an id or --match")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    store = draft_apply.Store(
        work_state.work_state_mode(_lake_provider(a)[0]),
        lambda initiative: run_store.work_items(runs_dir, initiative),
        lambda initiative, task, state, by, expected: store_cli.set_state(runs_dir, initiative, task, state, by, expected),
    )
    now, rows = datetime.datetime.now(datetime.UTC), run_store.read_queue(runs_dir)
    live = _live_runs(runs_dir, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    recorded = _recorded_initiatives(runs_dir, live)
    by = a.by or getpass.getuser()
    if len(a.id) == 1 and a.match is None:
        target_id = _initiative_token(runs_dir, a.id[0])
        rc = route_remove.remove(runs_dir.parent, target_id, a.reason, by, now, rows, _live_runs_on(live, target_id, recorded), store, dry_run=a.dry_run)
        target = route_guard.resolve_target(rows, target_id)
        if rc == route_guard.DONE and not a.dry_run and isinstance(target, route_guard.QueuedIntake):
            _record_intake_done(runs_dir, runs_dir.parent / "intake" / "done" / f"{target.task_id}.md")
        return rc
    ids = [_initiative_token(runs_dir, token) for token in a.id]
    targets, _why = route_remove.expand_targets(rows, ids, a.match)
    live_on = {target: _live_runs_on(live, target, recorded) for target in targets}
    resolved = [route_guard.resolve_target(rows, target) for target in targets]
    intakes = [t.task_id for t in resolved if isinstance(t, route_guard.QueuedIntake)]
    ws = runs_dir.parent
    rc = route_remove.remove_many(ws, ids, a.reason, by, now, rows, live_on, store, match=a.match, dry_run=a.dry_run)
    if not a.dry_run:
        for task_id in intakes:
            done = ws / "intake" / "done" / f"{task_id}.md"
            if done.is_file() and not (ws / "intake" / f"{task_id}.md").exists():
                _record_intake_done(runs_dir, done)
    return rc


def _route_hold(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    return route_hold.hold(runs_dir.parent, runs_dir, a.id, a.reason, a.by or getpass.getuser(), datetime.datetime.now(datetime.UTC), run_store.read_queue(runs_dir), dry_run=a.dry_run)


def _route_release(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    return route_hold.release(runs_dir.parent, runs_dir, a.id, run_store.read_queue(runs_dir), dry_run=a.dry_run)


def _route_set_stage(a: argparse.Namespace, stage: str) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    return route_hold.set_stage(runs_dir.parent, runs_dir, a.id, stage, run_store.read_queue(runs_dir), dry_run=a.dry_run)


def _route_stage_ready(a: argparse.Namespace) -> int:
    return _route_set_stage(a, "ready")


def _route_stage_draft(a: argparse.Namespace) -> int:
    return _route_set_stage(a, "draft")


def _route_requeue(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    code, message = route_requeue.requeue(runs_dir.parent, runs_dir, a.id, dry_run=a.dry_run)
    print(message)
    return code


def _record_intake_done(runs_dir: Path, done_path: Path) -> None:
    """Edge. Move a removed intake's queue row from `queued` to `done`, so `route status` and the guard stop seeing it.

    The file has already moved; a store that cannot take the row is one warning, as in `route.write_filed_item`."""
    row = queue_rows.parse_item("intake", ("done", done_path.name), done_path.read_text(encoding="utf-8"))
    if row is None or not run_store.upsert_row(runs_dir, row):
        print(f"remove: warning: the store still lists {done_path.stem} as queued; run cox route import to reconcile")


def _chair_machine_line(profile: Mapping, runs_dir: Path) -> str:
    """Edge. The status line naming this machine and its effective orchestration mode; empty when either read fails
    or the hosts table has no rows (`run_store.hosts` answers `[]` for no store, so there is nothing to report)."""
    try:
        rows = run_store.hosts(runs_dir)
        if not rows:
            return ""
        configured = chair_capacity.chair_local_lanes(dict(profile))
        return route.chair_machine_line(chair_orchestrate.mode_line(configured, rows, socket.gethostname(), _now_iso()))
    except Exception:
        return ""


def _initiative_events(queue: Sequence[Mapping], run_ids: Sequence[str]) -> dict[str, list]:
    """Run id to the `events` its initiative's own row carries. A run id is a slug with an optional
    `-<digits>` suffix, so it is looked up whole, then by `_run_slug`. A run with no events, or with
    anything but a list of them, is absent."""
    found = {
        str(r.get("initiative") or r.get("task_id") or ""): (r["extra"].get("events") if isinstance(r.get("extra"), Mapping) else None)
        for r in queue
        if r.get("kind") == "initiative"
    }
    by_run = {run_id: found.get(run_id) or found.get(_run_slug(run_id)) for run_id in run_ids}
    return {run_id: list(evs) for run_id, evs in by_run.items() if isinstance(evs, list) and evs}


def _route_status(a: argparse.Namespace) -> int:
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    # Past this point a stray pidfile — hand-edited, absurdly large — must
    # not take the session down with it (spec §2); charter A6 puts
    # exceptions at the edge, matching _route_context's guard above.
    try:
        ws = Path(workspace).expanduser()
        provider_profile = _lake_provider(a)[0]
        work_mode, work_line = work_state.resolve(provider_profile)
        short_ids = run_store.initiative_short_ids(ws / "runs")
        rows = _status_rows_for(ws / "runs", short_ids)
        items = _stored_work_items(ws, work_mode)
        groups = _intake_groups_for(ws, items)
        problems = route.state_problems(items)
        queue = run_store.read_queue(ws / "runs")
        from agent_tools.version_info import package_version  # local: keeps the import off every other command's path

        held_intakes = intake_queue.held_rows(queue)
        waiting = intake_queue.waiting_on(queue)
        held = [
            *_held_entries(held_intakes),
            *route.held_initiative_entries(queue, release_gate.held_initiatives(queue), held_intakes),
        ]
        deferred = route.deferred_by_target(queue, package_version())
        if a.json:
            # The bare rows list is the shape older callers read; it stays when there
            # is nothing else to say. A problem is never dropped for lack of an intake dir.
            queue_facts = {
                **({"waiting": waiting, "held": held} if waiting or held else {}),
                **({"deferred": deferred} if deferred else {}),
            }
            if groups is None and not problems and not queue_facts:
                doc = rows
            elif groups is None:
                doc = {"runs": rows, "problems": problems, **queue_facts}
            else:
                doc = {"runs": rows, "intake": groups, "problems": problems, **queue_facts}
            print(json.dumps(doc, indent=2))
        else:
            shown, hidden = (rows, 0) if a.all else route.recent_rows(rows, datetime.datetime.now(datetime.UTC))
            status = route.render_status(
                shown, groups, problems, gate_level=_resolved_gate_level(ws / "runs"), hidden=hidden,
                short_ids=_run_short_ids([r["id"] for r in rows], short_ids),
                waiting=_waiting_by_entry_id(queue, waiting), held=held, deferred=deferred,
                chair_line=_chair_machine_line(profile, ws / "runs"),
                events=_initiative_events(queue, [r["id"] for r in rows]),
            )
            drafts = route.render_drafts(
                draft_list.read_drafts(ws / "work", datetime.datetime.now(datetime.UTC).isoformat()), draft_list.format_age
            )
            # Printed whenever a provider profile was read. With none (`_lake_provider` gives `{}`)
            # there is no work-state source to name, and the output stays as it was before this line.
            if provider_profile:
                print(work_line)
            print("\n".join(part for part in (status, drafts) if part))
    except Exception as exc:
        print(f"routing: status unavailable ({type(exc).__name__}: {exc})")
    return 0


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


def _leader_takeover_hours() -> float:
    """`policy.leader.takeover_hours`, default 3; answers the default for the same reason `_leader_heartbeat_minutes` does."""
    return 3.0


def _leader_refuse_hours(hours: float | None) -> int | None:
    if hours is None or hours > 0:
        return None
    print(f"chair: --hours must be positive, got {hours}")
    return 2


def _leader_read_or_refuse(runs_dir: Path):
    """A file present but unreadable or not valid JSON is a refusal, not a free lock
    (charter A6: `chair.read` raises at the edge; here is where that becomes a value).
    Returns `(record_or_none, None)` or `(None, 2)` after printing the reason."""
    try:
        return chair.read(runs_dir), None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"chair: lock file unreadable ({type(exc).__name__}: {exc})")
        return None, 2


def _recorded_chair_model(runs_dir: Path) -> str:
    """The `model` the chair record names, else "". Silent on an unreadable record: `_leader_guard_or_refuse` reports that once."""
    try:
        record = chair.read(runs_dir)
    except (OSError, json.JSONDecodeError):
        return ""
    model = record.get("model") if isinstance(record, dict) else None
    return model if isinstance(model, str) else ""


def _holder_label(a: argparse.Namespace) -> str:
    return getattr(a, "label", None) or os.environ.get("COX_SESSION_LABEL") or _UNLABELED


def _leader_guard_or_refuse(runs_dir: Path, holder: str, force: bool, claim: bool = False, explicit_label: str | None = None) -> int | None:
    """Exit code 2 with the refusal printed when another live session holds the loop;
    None to proceed. `claim` takes an unheld, stale or crashed lock for `holder` before
    proceeding, so launching or landing is what makes a session the leader."""
    record, rc = _leader_read_or_refuse(runs_dir)
    if rc is not None:
        return None
    state = chair.liveness(record, _leader_pid_alive(record), datetime.datetime.now(datetime.UTC), socket.gethostname(), _leader_heartbeat_minutes())
    holder = chair.resolve_holder_label(record, state, os.environ, socket.gethostname(), explicit_label) or holder
    line = chair.guard(record, holder, state)
    if line is not None:
        print(f"override: {line}" if force else line)
        return None if force else 2
    if claim and state != "live":
        pid, host = _leader_identity(None, os.environ)
        new_record, _ = chair.take(record, holder, pid, host, datetime.datetime.now(datetime.UTC), _leader_heartbeat_minutes(), _leader_pid_alive(record), steal=True, claude_session=chair.claude_session_from_env(os.environ))
        chair.write(runs_dir, new_record)
        print(f"taking the loop: {holder}" if record is None else f"taking the loop from {record.get('session')} ({state})")
    return None


def _leader_pid_choice(explicit_pid: int | None, env: Mapping[str, str], ppid: int, alive: Callable[[int], bool]) -> tuple[int, bool]:
    """Pure. The pid to record and whether it is the parent-pid fallback.
    An explicit pid wins, then an all-digit, non-zero, live `CLAUDE_PID`, then `ppid`."""
    if explicit_pid:
        return explicit_pid, False
    raw = env.get("CLAUDE_PID", "")
    if raw.isascii() and raw.isdigit() and int(raw) > 0 and alive(int(raw)):
        return int(raw), False
    return ppid, True


def _leader_identity(explicit_pid: int | None = None, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """The pid and host that name *this session* to the lock, for `take`/`beat`/`release`.
    `os.getpid()` names this one `cox` invocation, which exits the moment the command
    returns. `os.getppid()` names the process that invoked `cox`, which is the invoking
    shell and not the loop: a caller that runs each command in a fresh shell records a
    pid that is dead before the next command, so its lock reads as stale at once.
    `--pid`, or a live `CLAUDE_PID` in the passed `env`, names the durable process instead.
    Every verb that writes or matches a lease passes `os.environ`, so they all name the same pid;
    no `env` means no `CLAUDE_PID` lookup."""
    return _leader_pid_choice(explicit_pid, env or {}, os.getppid(), chair.pid_alive)[0], socket.gethostname()


def _leader_take_warning(pid: int, explicit_pid: int | None, env: Mapping[str, str], ppid: int, alive: Callable[[int], bool]) -> str | None:
    """Pure. The warning for a take that records the invoking shell's pid, else None.
    `pid` is the pid actually recorded, so a pid read back off a held lease counts as the shell's when it equals `ppid`."""
    if explicit_pid or pid != ppid or _leader_pid_choice(None, env, ppid, alive) == (ppid, False):
        return None
    return f"warning: recorded pid {pid} is the invoking shell's; the lease goes stale when it exits. Pass --pid with a long-lived loop's pid."


def _leader_pid_alive(record: dict | None) -> bool:
    """A pid is judged dead only on the host that recorded it: this process has no way
    to check a pid number on another machine, so a foreign host's lock is never read as
    dead here — only its own host, or its heartbeat aging out, can make it stale."""
    if record is None:
        return False
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    if record.get("host") != socket.gethostname():
        return True
    return chair.pid_alive(pid)


def _leader_refuse_dead_pid(explicit_pid: int | None) -> int | None:
    """Refuses only an explicit `--pid`; the `os.getppid()` default is alive by construction."""
    if explicit_pid is None or chair.pid_alive(explicit_pid):
        return None
    print(f"chair pid not alive: {explicit_pid}")
    return 2


def _leader_launched_by(record: dict | None, pid_alive_: bool, now: datetime.datetime, host: str, heartbeat_minutes: int) -> str | None:
    """The label to stamp on a run launched right now: the lock's own holder when it
    reads live, `None` when it reads stale, crashed, or none — never a guess."""
    if chair.liveness(record, pid_alive_, now, host, heartbeat_minutes) != "live":
        return None
    return record.get("session")


def _print_if_stale(record: dict | None, state: str) -> None:
    if record is not None and state in ("stale", "crashed"):
        print(f"chair {state}: {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')}")


def _retaken_chair(record: dict, now_iso: str, model: str | None, session: str, pid: int, host: str, hours: float | None, now: datetime.datetime) -> tuple[dict, str]:
    """Pure. The record to write and the line to print when a take retakes a lease its own holder holds.
    A given `model` replaces the recorded one, none keeps it; given `hours` set `until` to `now` plus hours, none keeps the recorded `until`."""
    refreshed = {**record, "heartbeat_at": now_iso, **({"model": model} if model else {})}
    new_record = refreshed if hours is None else chair.extend(refreshed, now, hours)
    recorded = new_record.get("model")
    suffix = f" (model {recorded})" if isinstance(recorded, str) and recorded else ""
    until = "" if hours is None else f" until {new_record['until']}"
    return new_record, f"chair lease retaken: {session} (pid {pid}) on {host}{suffix}{until}"


def _read_key() -> str:
    """Edge. One keypress, no Enter. Without a terminal nothing confirms an attach or a takeover, so the answer is b, a non-chair session."""
    if not sys.stdin.isatty():
        return "b"
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _caller_holder(runs_dir: Path, explicit_label: str | None) -> tuple[str, int] | None:
    """Edge. The (label, pid) of the live lease this caller holds, or None; an explicit label opts out so a mismatch is still refused."""
    if explicit_label:
        return None
    try:
        record = chair.read(runs_dir)
    except (OSError, ValueError):
        return None
    host = socket.gethostname()
    state = chair.liveness(record, _leader_pid_alive(record), datetime.datetime.now(datetime.UTC), host, _leader_heartbeat_minutes())
    label = chair.resolve_holder_label(record, state, os.environ, host, None)
    return None if label is None else (label, record["pid"])


def _route_chair_take(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    held = _caller_holder(runs_dir, a.label)
    session = a.label or (held[0] if held else "chair")
    pid, host = _leader_identity(a.pid or (held[1] if held else None), os.environ)
    warning = _leader_take_warning(pid, a.pid, os.environ, os.getppid(), chair.pid_alive)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    hours = getattr(a, "hours", None)  # the bare launcher's namespace has none: its record carries no `until`
    refuse_rc = _leader_refuse_hours(hours)
    if refuse_rc is not None:
        return refuse_rc
    heartbeat_minutes = _leader_heartbeat_minutes()
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        now = datetime.datetime.now(datetime.UTC)
        alive = _leader_pid_alive(record)
        prior_state = chair.liveness(record, alive, now, host, heartbeat_minutes)
        _print_if_stale(record, prior_state)
        new_record, reason = chair.take(record, session, pid, host, now, heartbeat_minutes, alive, steal=a.steal, claude_session=getattr(a, "claude_session", None) or chair.claude_session_from_env(os.environ), hours=hours, model=getattr(a, "model", None), cwd=getattr(a, "cwd", None) or os.getcwd())
        if new_record is None and prior_state == "live" and not chair.expired(record, now) and chair.same_holder(record, session, pid, host):
            retaken, line = _retaken_chair(record, now.isoformat(), getattr(a, "model", None), session, pid, host, hours, now)
            refusal = chair.acquire_lease(runs_dir, session, pid, host, ttl=chair.lease_ttl_for(retaken, now), steal=a.steal)
            if refusal:
                print(refusal)
                return 2
            chair.write(runs_dir, retaken)
            print(line)
            if warning:
                print(warning)
            return 0
        if new_record is None:
            print(reason)
            return 2
        refusal = chair.acquire_lease(runs_dir, session, pid, host, ttl=chair.lease_ttl_for(new_record, now), steal=a.steal)
        if refusal:
            print(refusal)
            return 2
        chair.write(runs_dir, new_record)
        displaced = a.steal and prior_state == "live" and not chair.expired(record, now)
    if displaced:
        print(chair.displaced_line(record))
    print(f"chair taken: {new_record['session']} (pid {new_record['pid']}) on {new_record['host']}")
    if warning:
        print(warning)
    return 0


def _route_chair_extend(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    session = a.label or "chair"
    pid, host = _leader_identity(a.pid, os.environ)
    refuse_rc = _leader_refuse_dead_pid(a.pid) or _leader_refuse_hours(a.hours)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        if not chair.same_holder(record, session, pid, host):
            print(f"chair: not held by {session} (pid {pid}) on {host}")
            return 2
        new_record = chair.extend(record, datetime.datetime.now(datetime.UTC), a.hours)
        chair.write(runs_dir, new_record)
    print(f"chair extended until {new_record['until']}")
    return 0


def _route_chair_beat(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    held = _caller_holder(runs_dir, a.label)
    session = a.label or (held[0] if held else "chair")
    pid, host = _leader_identity(a.pid or (held[1] if held else None), os.environ)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        now = datetime.datetime.now(datetime.UTC)
        new_record, reason = chair.beat(record, session, pid, host, now, run_id=a.run, claude_session=chair.claude_session_from_env(os.environ))
        if new_record is None:
            print(reason)
            return 2
        lost = chair.renew_lease(runs_dir, session, pid, host, ttl=chair.lease_ttl_for(new_record, now))
        if lost:
            print(lost)
            return 2
        chair.write(runs_dir, new_record)
    print(f"chair heartbeat: {new_record['session']}")
    return 0


def _route_chair_release(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    held = _caller_holder(runs_dir, a.label)
    session = a.label or (held[0] if held else "chair")
    pid, host = _leader_identity(a.pid or (held[1] if held else None), os.environ)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        _new_record, reason = chair.release(record, session, pid, host)
        if reason:
            print(reason)
            return 2
        chair.release_lease(runs_dir, session, pid, host)
        chair.write(runs_dir, None)
    print(f"chair released: {record['session']}")
    return 0


def _route_chair_status(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    record, read_rc = _leader_read_or_refuse(runs_dir)
    if read_rc is not None:
        return read_rc
    now = datetime.datetime.now(datetime.UTC)
    alive = _leader_pid_alive(record)
    state = chair.liveness(record, alive, now, socket.gethostname(), _leader_heartbeat_minutes())
    if not a.json:
        _print_if_stale(record, state)
    if a.json:
        print(json.dumps({**(record or {}), "state": state}, indent=2))
    elif record is None:
        print("chair: none")
    else:
        left = chair.hours_left(record, now)
        window = "" if left is None else f" until {record['until']} ({'expired' if chair.expired(record, now) else f'{left:.1f}h left'})"
        print(f"chair: {record['session']} (pid {record['pid']}) on {record['host']} — {state}{window}")
    return 0


def _route_leader_status(a: argparse.Namespace) -> int:
    """The SessionStart line: one `chair:` line saying whether this session is the chair."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    record, read_rc = _leader_read_or_refuse(runs_dir)
    if read_rc is not None:
        return read_rc
    host = socket.gethostname()
    live = _live_loop_holder(runs_dir)
    if live is not None and not chair.is_caller_holder(record, os.environ, host):
        print(chair_resolve.held_line(live.label or "?", live.is_service))  # a live chair loop holds the chair: read only, the lease is not touched
        return 0
    state = chair.liveness(record, _leader_pid_alive(record), datetime.datetime.now(datetime.UTC), host, _leader_heartbeat_minutes())
    with chair.locked(runs_dir):  # re-read under the lock: a beat or a takeover's release request may have landed since the read above
        fresh = chair.read(runs_dir)
        record = chair.adopt_session(fresh, state, os.environ, host)
        line = chair.session_line(record, state, os.environ, host)
        settled = chair.settle_resumed(record, state, os.environ, host)
        if settled is not fresh:
            chair.write(runs_dir, settled)
    print(line)
    return 0


def _route_chair_clear(a: argparse.Namespace) -> int:
    """Holds `chair.locked` across the whole read-decide-unlink sequence, same as
    take/beat/release, so a `take` landing mid-clear cannot be discarded silently."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        result = chair.clear(runs_dir, force=a.force)
    if result is None:
        print("chair: nothing cleared")
        return 0
    path, record = result
    print(f"chair cleared: {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')} [{path.name}]")
    return 0


def _route_chair_chat(a: argparse.Namespace) -> int:
    """`--read` prints the thread (or, with `--since`, only what's unread); otherwise
    appends one line, as the operator by default or, with `--as-leader`, only when this
    process's pid and host match the live lock's holder — a reply must never be shown
    from a session that is not the leader."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    path = leader_chat.chat_path(runs_dir)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if a.read:
        thread = leader_chat.read_thread(existing, limit=50)
        entries = leader_chat.unread(thread, a.since)
        if a.json:
            print(json.dumps(entries))
        else:
            for entry in entries:
                print(f"{entry.get('at', '?')} {entry.get('from', '?')}: {entry.get('text', '')}")
        return 0
    if a.text is None:
        print("chat: TEXT required unless --read")
        return 2
    sender = "operator"
    if a.as_leader:
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        pid, host = _leader_identity(None, os.environ)
        now = datetime.datetime.now(datetime.UTC)
        state = chair.liveness(record, _leader_pid_alive(record), now, host, _leader_heartbeat_minutes())
        if state != "live":
            print("chat: refusing --as-leader (no live lock held)")
            return 2
        if record.get("pid") != pid or record.get("host") != host:
            print(f"chat: refusing --as-leader (held by {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')})")
            return 2
        sender = record.get("session", "leader")
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.datetime.now(datetime.UTC).isoformat(), "from": sender, "text": a.text}
    path.write_text(leader_chat.append_line(existing, entry), encoding="utf-8")
    print(f"chat: {sender}: {a.text}")
    return 0


def _chat_state_path(workspace: Path) -> Path:
    return workspace / "chair_chat_state.json"


def _read_chat_doc(path: Path) -> tuple[dict, list[dict]]:
    """Edge. The directive state and the epics the chair last saw; a missing or torn file reads as empty."""
    try:
        loaded = json.loads(_read_text_or_none(path) or "{}")
    except json.JSONDecodeError:
        loaded = {}
    doc = loaded if isinstance(loaded, dict) else {}
    base = chair_chat.empty_state()
    saved = doc.get("state") if isinstance(doc.get("state"), dict) else {}
    epics = doc.get("epics") if isinstance(doc.get("epics"), list) else []
    return {**base, **{k: v for k, v in saved.items() if k in base}}, epics


def _write_chat_doc(path: Path, state: dict, epics: list[dict]) -> None:
    """Edge. Replace the file atomically; an unchanged document is not rewritten."""
    text = json.dumps({"state": state, "epics": epics}, default=sorted, sort_keys=True)
    if _read_text_or_none(path) == text:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _chat_deps(ws: Path) -> chair_run.ChatDeps:
    """Edge. The chat edges over the workspace's `courier.jsonl` and `chair_chat_state.json`; neither is the lease."""
    courier_path, chat_path = ws / "courier.jsonl", _chat_state_path(ws)
    return chair_run.ChatDeps(
        read=lambda: courier.entries(_read_text_or_none(courier_path) or ""),  # a missing file is an empty bus
        load=lambda: _read_chat_doc(chat_path)[0],
        save=lambda state, epics: _write_chat_doc(chat_path, state, epics),
        append=lambda entry: courier.append_file(courier_path, entry),
        ack=lambda message_id: courier.ack_file(courier_path, message_id, chair_run.CHAT_CONSUMER),
    )


def _converse_append(a: argparse.Namespace, body_kind: str, payload: dict[str, str]) -> tuple[Path, str] | None:
    """Edge. Append one chat entry to the workspace courier; the lease record is never opened. None after printing why."""
    workspace = _courier_workspace(a)
    if workspace is None:
        return None
    message_id = uuid.uuid4().hex
    entry = chair_chat.build_message(a.sender, body_kind, payload, message_id, chair_run.CHAT_REF)
    courier.append_file(workspace / "courier.jsonl", entry)
    return workspace, message_id


def _route_chair_converse_send(a: argparse.Namespace) -> int:
    fields = _parse_fields(a.args or [])
    if fields is None:
        print(f"converse: bad argument in {a.args}; expected key=value")
        return 2
    payload = {**fields, "kind": a.kind}
    reason = chair_chat.validate_directive(a.kind, payload)
    if reason is not None:
        print(f"converse: {reason}")
        return 2
    sent = _converse_append(a, "directive", payload)
    if sent is None:
        return 2
    print(f"converse: sent {a.kind} directive {sent[1]}; the chair applies it on its next tick")
    return 0


def _find_chat_answer(blob: str, question_id: str) -> dict | None:
    return next((e for e in courier.entries(blob) if e.get("event") == "chat_answer" and (e.get("fields") or {}).get("question") == question_id), None)


def _await_chat_answer(
    path: Path, question_id: str, wait: float, poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
) -> dict | None:
    """Edge. The answer entry for `question_id`, polled until `wait` seconds pass; None when none came."""
    deadline = monotonic() + wait
    while True:
        found = _find_chat_answer(_read_text_or_none(path) or "", question_id)
        left = deadline - monotonic()
        if found is not None or left <= 0:
            return found
        sleep(min(poll, left))


def _route_chair_converse_ask(a: argparse.Namespace) -> int:
    if a.kind not in chair_chat.QUESTION_KINDS:
        print(f"converse: unknown question {a.kind!r}; expected one of {', '.join(chair_chat.QUESTION_KINDS)}")
        return 2
    sent = _converse_append(a, "question", {"kind": a.kind, "subject": a.subject})
    if sent is None:
        return 2
    answer = _await_chat_answer(sent[0] / "courier.jsonl", sent[1], a.wait)
    print(answer["note"] if answer else f"converse: the chair has not answered within {a.wait:g}s; the question stays queued for its next tick")
    return 0


def _route_chair_converse_state(a: argparse.Namespace) -> int:
    workspace = _courier_workspace(a)
    if workspace is None:
        return 2
    state, epics = _read_chat_doc(_chat_state_path(workspace))
    sent = courier.entries(_read_text_or_none(workspace / "courier.jsonl") or "")
    recent = [{"event": e["event"]} for e in sent if e.get("topic") == chair_chat.LABEL and e.get("event") in ("directive_applied", "directive_refused")][-5:]
    print(chair_chat.state_summary(state, epics, recent))
    print("Epics are as of the chair's last acting tick; a standby or dry-run tick does not refresh them.")
    return 0


_PROFILE_KEYS_FOR_LAUNCH = ("workspace_dir", "harness_dir", "team", "cartridges_dir", "provider_profile")


def _resolve_profile_or_refuse(a: argparse.Namespace):
    """Resolve the profile for `file`/`launch`, spec §7: unlike `context`,
    a missing profile or a profile missing a key `route.harness_argv` needs
    is a hard refusal here (exit 2), since both subcommands need real paths
    to write to or launch against, not a one-liner to print and carry on.

    Returns `(profile, None)` on success, `(None, 2)` after printing the
    reason.
    """
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return None, 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return None, 2
    for key in _PROFILE_KEYS_FOR_LAUNCH:
        if not profile.get(key):
            print(f"routing: {key} not set in profile {profile_path}")
            return None, 2
    return profile, None


def _item_shape(rel: str) -> tuple[str, tuple[str, ...]] | None:
    """(`kind`, `path_parts`) `queue_rows.parse_item` wants for one `_write_mapping` key, or `None` for a
    path with no queue row of its own (anything outside `work/`/`intake/`). A `work/<id>/initiative.md` is an
    initiative row: launch reads it from the store and refused a one-task initiative filed without one
    (2026-10-23)."""
    parts = Path(rel).parts
    if len(parts) == 4 and parts[0] == "work":
        return "task", parts[1:]
    if len(parts) == 3 and parts[0] == "work" and parts[2] == "initiative.md":
        return "initiative", parts[1:]
    if len(parts) in (2, 3) and parts[0] == "intake":
        return "intake", parts[1:]
    return None


def _write_mapping(mapping: dict, ws: Path, by: str = _UNLABELED):
    """Write `mapping` (relative path -> text) under `ws`; `None` on success, else the refusal to print.
    A `work/<initiative>/initiative.md`, `work/<initiative>/<phase>/<task>.md` or `intake/...md` path is filed
    store-first through `route.write_filed_item`, which prints its own warning or error; anything else is
    written directly, as it always was. Each `work/<initiative>/<phase>/<task>.md` written with a `state:`
    is then mirrored to the store the same as today, one call per file."""
    targets = {rel: ws / rel for rel in mapping}
    existing = [str(path) for path in targets.values() if path.exists()]
    if existing:
        return f"routing: refusing to overwrite existing path(s): {', '.join(existing)}"
    runs_dir = ws / "runs"
    for rel, path in targets.items():
        shape = _item_shape(rel)
        if shape is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(mapping[rel], encoding="utf-8")
            queue_export.record_written(ws, {rel: mapping[rel]})
        elif route.write_filed_item(runs_dir, path, *shape, mapping[rel]) != 0:
            return f"routing: {path} was recorded in the store but its own file did not write; see above"
        print(str(path))
        _mirror_filed_state(rel, mapping[rel], runs_dir, by)
    return None


def _mirror_filed_state(rel: str, text: str, runs_dir: Path, by: str) -> None:
    parts = Path(rel).parts
    if len(parts) != 4 or parts[0] != "work":
        return
    fields = route.parse_frontmatter(text)[0]
    if not fields.get("state"):
        return
    line = store_cli.mirror_state(runs_dir, parts[1], str(fields.get("id") or Path(rel).stem), fields["state"], by)
    if line is not None:
        print(line)


def _sync_filed_items(mapping: dict, ws: Path, a: argparse.Namespace) -> None:
    """Run the per-item sync for each intake or work item `mapping` wrote, so its issue exists from birth.
    A failed sync (gh down, rate-limited) never fails the file: one line names the item and the way back."""
    rels = [rel for rel in mapping
            if (len(Path(rel).parts) == 2 and Path(rel).parts[0] == "intake")
            or (len(Path(rel).parts) == 4 and Path(rel).parts[0] == "work")]
    for rel in rels:
        item_id = route.parse_frontmatter(mapping[rel])[0].get("id") or Path(rel).stem
        sync = argparse.Namespace(item=item_id, workspace=str(ws), dry_run=False, project=None, profile=a.profile)
        if _sync_skipped(sync):
            print(f"route file: wrote {item_id}; not synced: tracker is none")
            continue
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = _route_sync(sync)
        except Exception as exc:
            rc, out = 1, io.StringIO(f"{type(exc).__name__}: {exc}")
        lines = out.getvalue().strip().splitlines()
        reason = f"; {lines[-1]}" if rc != 0 and lines else ""
        print(f"synced {item_id}" if rc == 0
              else f"route file: wrote {item_id}; issue sync failed (exit {rc}{reason}); run `route sync --item {item_id}`")


def _is_git_checkout_path(value: str, home: str) -> bool:
    """A leading tilde is expanded against `home`; an empty value is never a checkout (it would read as the cwd)."""
    return bool(value) and route.is_git_checkout(Path(repo_path.expand(value, home)))


def _not_a_checkout_message(value: str) -> str:
    return f"route file: refusing, {value} is not a git checkout on this machine"


def _reach_refused(refusal: str | None, notes: tuple[str, ...]) -> bool:
    """Print the refusal and return True when the decompose would refuse; else print one lint line per note."""
    if refusal:
        print(f"routing: {refusal}")
        return True
    for note in notes:
        print(f"routing: lint: {note}")
    return False


def _filing_repo_or_refusal(repo: str, profile: dict) -> tuple[str, str | None]:
    """The repo to file against and None, or `repo` and a `routing: ...` refusal."""
    chosen = repo_clone.filing_repo(repo, profile)
    if chosen.startswith(repo_clone.REFUSAL_PREFIX):
        return repo, f"routing: {chosen.removeprefix(repo_clone.REFUSAL_PREFIX)}"
    return chosen, None


def _intake_with_repo(intake_text: str, repo: str) -> str:
    fields, body = route.parse_frontmatter(intake_text)
    return route._frontmatter({**fields, "repo": repo}.items(), body)


def _route_file_from_intake(a: argparse.Namespace, ws: Path, profile: dict) -> int:
    intake_path = Path(a.from_intake)
    if intake_path.parent.resolve() != (ws / "intake").resolve():
        print(f"routing: --from-intake must name a file directly under {ws / 'intake'}")
        return 2
    intake_text = _read_text_or_none(intake_path)
    if intake_text is None:
        print(f"routing: cannot read intake file {a.from_intake}")
        return 2
    fields, intake_body = route.parse_frontmatter(intake_text)
    [entry] = route.intake_entries({intake_path.name: intake_text})
    typed = fields.get("repo", "")
    repo, refusal = _filing_repo_or_refusal(typed, profile)
    if refusal:
        print(refusal)
        return 2
    home = str(Path.home())
    if not _is_git_checkout_path(repo, home):
        print(_not_a_checkout_message(repo))
        return 2
    # Checked on the file as authored: `_intake_with_repo` below round-trips the frontmatter through a reader that
    # keeps the quotes in `["~/x"]`, and the launch never sees that copy.
    heal = route_reach.launch_text_heal(intake_text)
    if _reach_refused(heal.refusal, heal.notes):
        return 2
    if repo != typed:
        # A redirected repo is written the way `route file --repo` writes it, in the initiative and the archived intake.
        repo_text = route.repo_field(repo, home)
        intake_text = _intake_with_repo(intake_text, repo_text)
    else:
        repo_text = repo
    candidates = route.surface_candidates(intake_body, repo)
    surfaces = [c for c in candidates if (Path(repo) / c).is_file()]
    print(f"surfaces: {', '.join(surfaces)}" if surfaces else "surfaces: none resolved from the intake body")
    budget_usd = profile.get("one_task_budget_usd", 2.0)
    try:
        mapping = route.initiative_files(
            entry["title"], intake_body, repo_text, phase=a.phase,
            surfaces=surfaces, budget_usd=budget_usd, slug=fields.get("slug"),
        )
    except ValueError as exc:
        print(f"routing: {exc}")
        return 2
    slug = route.slugify(fields.get("slug") or entry["title"])
    initiative_rel = f"work/{slug}/initiative.md"
    new_initiative_text, new_intake_text = route.link_intake(
        mapping[initiative_rel], intake_text, f"intake/{intake_path.name}", slug
    )
    mapping[initiative_rel] = new_initiative_text
    mapping[f"intake/done/{intake_path.name}"] = new_intake_text
    refusal = _write_mapping(mapping, ws, by=_holder_label(a))
    if refusal:
        print(refusal)
        return 2
    intake_path.unlink()
    print(f"removed: {intake_path}")
    _sync_filed_items(mapping, ws, a)
    return 0


def _open_issues(ws: Path) -> list[dict]:
    """Rows with an issue number whose state is not finished: task rows end `done` or `dropped`, intake rows end `landed`."""
    closed = route.TERMINAL | {"landed"}
    keyed = ((card_keys.row_keys(row)[0], row) for row in run_store.read_queue(ws / "runs"))
    return [
        {"number": number, "title": row.get("title") or ""}
        for number, row in keyed
        if number is not None and row.get("state") not in closed
    ]


def _could_be_path(value: str) -> bool:
    """False for text that cannot name a file: a newline in it, or longer than any path."""
    return "\n" not in value and len(value) <= 4096


def _body_refusal(value: str) -> str:
    """The refusal for an unreadable --body: a value that is plainly text says so, rather than echoing it back as a
    file name (2026-10-23: a whole ticket body printed as `cannot read body file <body>`)."""
    if not _could_be_path(value) or len(value) > 200:
        return f"routing: --body takes a file path, or - for stdin, not the text itself (got {len(value)} characters of text)"
    return f"routing: cannot read body file {value}"


def _filing_stage(a: argparse.Namespace, profile: Mapping) -> str:
    """The stage a session files an intake at: the flag, else the profile's `intake_stage`, else draft."""
    if a.ready:
        return "ready"
    return "draft" if a.draft else profile.get("intake_stage") or "draft"


def _known_epic_ids(rows: Sequence[Mapping]) -> set[str]:
    """The epic ids the store's initiative rows name, which are the ids the dash feed rolls up."""
    return {epic for row in rows if (epic := queue_rows.epic_of_row(row)) is not None}


def _known_repo_names(profile: Mapping, rows: Sequence[Mapping], resolved: str = "") -> set[str]:
    """The profile's `repo_map` names, the repos the store rows already hold, and this filing's resolved repo."""
    held = {str(row["repo"]) for row in rows if row.get("repo")}
    return {*profile.get("repo_map", {}), *held, *([resolved] if resolved else [])}


def _repo_refusal_with_names(message: str, known: Collection[str]) -> str:
    return f"{message}; valid repos: {', '.join(sorted(known)) or 'none known'}"


def _route_file(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    ws = Path(profile["workspace_dir"]).expanduser()
    if a.from_intake:
        return _route_file_from_intake(a, ws, profile)
    if not a.title or not a.repo:
        print("routing: --title and --repo are required unless --from-intake is given")
        return 2
    link = getattr(a, "link", None) or ""
    if link and not a.intake:
        print("routing: --link needs --intake, since only an intake carries a link")
        return 2
    after = _after_ids(getattr(a, "after", None)) or []
    if after and not a.intake:
        print("routing: --after needs --intake, since only a queued intake waits")
        return 2
    for flag, noun in (("epic", "an epic"), ("priority", "a priority"), ("target_release", "a target release")):
        if getattr(a, flag, None) is not None and not a.intake:
            print(f"routing: --{flag.replace('_', '-')} needs --intake, since only an intake carries {noun}")
            return 2
    if a.draft and a.ready:
        print("routing: --draft and --ready are exclusive; give one")
        return 2
    if (a.draft or a.ready) and not a.intake:
        print("routing: --draft and --ready need --intake, since only an intake has a stage")
        return 2
    # Repos are siblings of the workspace: a bare name is resolved there, so the row never holds a name launch cannot read.
    home = str(Path.home())
    repo = route.resolve_repo(a.repo, ws.absolute().parent, route.is_git_checkout, home)
    rows = run_store.read_queue(ws / "runs")
    if isinstance(repo, route.RepoRefused):
        print(_repo_refusal_with_names(repo.message, _known_repo_names(profile, rows)))
        return 2
    resolved = repo
    repo, refusal = _filing_repo_or_refusal(resolved, profile)
    if refusal:
        print(refusal)
        return 2
    if not _is_git_checkout_path(repo, home):
        print(_not_a_checkout_message(a.repo if repo == resolved else repo))
        return 2
    repo = route.repo_field(repo, home)
    if a.body == "-":
        body = sys.stdin.read()
    elif a.body:
        body = _read_text_or_none(Path(a.body)) if _could_be_path(a.body) else None
        if body is None:
            print(_body_refusal(a.body))
            return 2
    else:
        body = ""
    try:
        if a.intake:
            refusals = intake_fields.validate(
                repo, a.epic, a.priority, a.target_release, _known_repo_names(profile, rows, repo), _known_epic_ids(rows)
            )
            if refusals:
                print("\n".join(f"routing: {line}" for line in refusals))
                return 2
            fields = intake_fields.normalise(repo, a.epic, a.priority, a.target_release)
            # `route.intake_file` writes the title when there is no body, and the launch reads what was written.
            heal = route_reach.launch_reach_heal(body or a.title, {})
            if _reach_refused(heal.refusal, heal.notes):
                return 2
            duplicate = card_keys.find_duplicate(a.title, _open_issues(ws))
            if duplicate is not None and not a.force:
                print(f"routing: refusing {a.title}: duplicate of issue {duplicate}; pass --force to file it anyway")
                return 2
            date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
            mapping = route.intake_file(a.title, body, repo, date, link=link, ids=profile.get("ids", "slug"), after=after,
                                       stage=_filing_stage(a, profile), epic=fields.get("epic", ""),
                                       priority=fields.get("priority", ""), target_release=fields.get("target_release", ""))
        else:
            mapping = route.initiative_files(a.title, body, repo, phase=a.phase)
    except ValueError as exc:
        print(f"routing: {exc}")
        return 2
    refusal = _write_mapping(mapping, ws, by=_holder_label(a))
    if refusal:
        print(refusal)
        return 2
    _sync_filed_items(mapping, ws, a)
    if a.intake:
        _print_fresh_id_note(a, mapping, ws)
    return 0


def _print_fresh_id_note(a: argparse.Namespace, mapping: dict, ws: Path) -> None:
    """Edge. Prints the fresh-id note when a dropped initiative holds the intake id. `route file` has no dry run."""
    if work_state.work_state_mode(_lake_provider(a)[0]) != "store":
        return
    rows = run_store.read_queue(ws / "runs")
    for text in mapping.values():
        intake_id = route.parse_frontmatter(text)[0].get("id")
        if isinstance(intake_id, str) and intake_id:
            note = route_guard.fresh_id_note(intake_id, route_guard.fresh_initiative_id(intake_id, rows))
            if note:
                print(note)


def _route_priority(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    ws = Path(profile["workspace_dir"]).expanduser()
    named = [name for name in ("up", "down", "top", "bottom") if getattr(a, name)]
    if a.set is None and not named:
        return route.list_open_queue(ws / "runs")
    if not a.initiative:
        print("routing: an initiative or queued intake is required with --set, --up, --down, --top or --bottom")
        return 2
    flag = a.set if a.set is not None else named[0]
    runs_dir = ws / "runs"
    queue = run_store.read_queue(runs_dir)
    target = route_guard.resolve_target(queue, a.initiative)
    if isinstance(target, route_guard.QueuedIntake):
        intake = next(row for row in queue if row.get("kind") == "intake" and row["task_id"] == target.task_id)
        return route.set_intake_priority(runs_dir, ws, intake, flag)
    return route.set_initiative_priority(runs_dir, ws, _initiative_token(runs_dir, a.initiative), flag)


def _schedule_inputs(profile: dict, mode: str, now: datetime.datetime):
    """Edge. The scheduler's inputs from the same readers the chair uses. Not read here, so the projection is a
    lower bound: live phases (a running phase reads waiting), remote hosts, and live lane load."""
    ws = Path(profile["workspace_dir"]).expanduser()
    runs_dir = ws / "runs"
    home = str(Path.home())
    files = chair_read_quarantined.initiative_files(ws, chair_read_quarantined.read_work_files(ws))
    items = chair_facts.read_ticket_items(ws, mode)
    ids = list(dict.fromkeys(str(r["initiative"]) for r in items))
    dispatch = {"max_in_flight": _local_max_in_flight(runs_dir, profile, socket.gethostname()), "live_runs": 0, "hosts": []}
    return chair_facts.schedule_inputs_from_rows(
        items,
        run_store.read_queue(runs_dir) if mode == "store" else [],
        chair_read_stale.read_chair_actions(runs_dir, now),
        dispatch,
        local=socket.gethostname(),
        repos=_initiative_repos(files, home),
        land_at=chair_facts.land_at_facts(_team_cartridge(profile)[0], _initiative_land_at(files), ids),
        live_phases=frozenset(),
    )


def _route_schedule(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    now = datetime.datetime.now(datetime.UTC)
    inputs = _schedule_inputs(profile, work_state.work_state_mode(_lake_provider(a)[0]), now)
    schedule, context = route_schedule.plan_from_inputs(inputs, now)
    named = a.explain or a.should_jump
    lines = (
        route_schedule.format_plain(schedule)
        if not named
        else route_schedule.explain_lines(schedule, context, a.explain)
        if a.explain
        # no model asker is wired: src/graphs/harness/invoke.py holds no single-shot helper, only invoke_graphs
        else route_schedule.should_jump_lines(schedule, context, a.should_jump, None, now.isoformat())
    )
    if lines is None:
        print(f"routing: unknown initiative: {named}")
        return 2
    print("\n".join(lines))
    return 0


def _run_argv(argv: list[str]) -> tuple[int, str, str]:
    """`(returncode, stdout, stderr)`; a missing executable is returncode 127, never an exception."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        return 127, "", str(exc)
    return r.returncode, r.stdout, r.stderr.strip()


def _fetch_listing(adapter, config) -> list | None:
    """The raw listing across `config.repos`; `None` after printing when a fetch fails. An adapter with no `list_argv` lists nothing; `pr_list_argv` adds its PRs; `project_list_argv` adds project items, each tagged `_listing: project`."""
    builders = [b for b in (getattr(adapter, "list_argv", None), getattr(adapter, "pr_list_argv", None)) if b]
    project_build = getattr(adapter, "project_list_argv", None)
    # An adapter whose API wraps its listing (e.g. under `data`) supplies `unwrap(payload, repo)`.
    unwrap = getattr(adapter, "unwrap", None) or (lambda payload, _repo: payload)
    listing: list = []
    for repo in config.repos:
        for build in builders:
            rc, out, err = _run_argv(build(config, repo))
            try:
                listing.extend(unwrap(json.loads(out or "[]"), repo) if rc == 0 else ())
            except json.JSONDecodeError as exc:
                rc, err = 1, f"listing is not JSON: {exc}"
            except (KeyError, TypeError) as exc:
                rc, err = 1, f"listing has an unexpected shape: {type(exc).__name__}: {exc}"
            if rc != 0:
                print(f"routing: listing {repo} failed: {err}")
                return None
        if project_build:
            rc, out, err = _run_argv(project_build(config, repo))
            try:
                items = json.loads(out or "[]") if rc == 0 else ()
                listing.extend({**item, "_listing": "project"} for item in items)
            except json.JSONDecodeError as exc:
                rc, err = 1, f"project listing is not JSON: {exc}"
            except (TypeError, AttributeError) as exc:
                rc, err = 1, f"project listing has an unexpected shape: {type(exc).__name__}: {exc}"
            if rc != 0:
                print(f"routing: project listing {repo} failed: {err}")
                return None
    return listing


def _pull_candidates(adapter, config, listing: list) -> tuple[dict, list[str]]:
    """Candidates by link, reading only entries `candidates` accepts, one entry at a time; an unparsable entry is a problem line."""
    found: dict = {}
    problems: list[str] = []
    for raw in listing:
        try:
            if isinstance(raw, dict) and raw.get("_listing") == "project":
                project = adapter.project_candidates([raw], config)
                wanted = {c.link for c in project}
                candidate = project[0] if project else None
            else:
                wanted = {ref.link for ref in adapter.candidates(config, [raw])}
                candidate = adapter.read(raw) if wanted else None
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            name = next((str(raw[k]) for k in ("url", "link", "title") if k in raw), "an entry") if isinstance(raw, dict) else "an entry"
            problems.append(f"routing: skipping unreadable listing entry {name}: {type(exc).__name__}: {exc}")
            continue
        # A link seen with a project item id keeps it, whichever listing reads the issue second.
        if (
            candidate is not None
            and candidate.link in wanted
            and not (found.get(candidate.link) and found[candidate.link].project_item_id)
        ):
            found[candidate.link] = candidate
    return found, problems


def _taken_by_project_item(found: dict, rows: list) -> frozenset[str]:
    """Links of candidates whose project item id a store row already holds."""
    return frozenset(
        link
        for link, c in found.items()
        if c.project_item_id and card_keys.find_row(rows, None, c.project_item_id) is not None
    )


def _mark_argv(adapter, config, ref, rel: str) -> list[str]:
    """An adapter that sets `MARK_NEEDS_TOKEN_ENV` also gets the profile's `token_env`."""
    if getattr(adapter, "MARK_NEEDS_TOKEN_ENV", False):
        return adapter.mark_argv(ref, rel, config.token_env)
    return adapter.mark_argv(ref, rel)


def _pull_one(adapter, config, mapping: dict, found: dict, ws: Path, dry_run: bool) -> bool:
    """Write one planned intake file, then mark its source; True after printing why when either step failed."""
    (rel, text), = mapping.items()
    origin = found[route.parse_frontmatter(text)[0]["link"]]
    if dry_run:
        print(f"would write {rel}")
        return False
    if refusal := _write_mapping(mapping, ws):
        print(f"{refusal}; {origin.title!r} ({origin.link}) not filed")
        return True
    rc, _, err = _run_argv(_mark_argv(adapter, config, sources.Ref(origin.link, origin.repo), rel))
    if rc != 0:
        print(f"routing: wrote {rel}; marking {origin.link} failed (exit {rc}): {err}")
    return rc != 0


def _review_one(adapter, origin, profile: str, dry_run: bool) -> bool:
    """Review one PR under the pull's profile, then mark it so the next pull skips it; True after printing why when either step failed."""
    if dry_run:
        print(f"would review {origin.link}")
        return False
    rc, _, err = _run_argv(route.review_argv(origin.link, profile))
    if rc != 0:
        print(f"routing: review of {origin.link} failed (exit {rc}): {err}")
        return True
    rc, _, err = _run_argv(adapter.mark_argv(sources.Ref(origin.link, origin.repo, "pr"), ""))
    if rc != 0:
        print(f"routing: reviewed {origin.link}; marking it failed (exit {rc}): {err}")
    return rc != 0


def _route_pull(a: argparse.Namespace) -> int:
    """`--source` pulls that one; without it every source the profile configures, in profile order (the chair's bare `route pull`)."""
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    names = [a.source] if a.source else list(profile.get("sources", {}))
    if not names:
        print("routing: no sources in the profile")
        return 2
    return max([_pull_source(a, profile, name, named=len(names) > 1) for name in names])


def _held_issues(ws: Path, repo_map: dict) -> dict[tuple[str, str], str]:
    """(repository, number) to `initiative/task_id` for the store rows that name both; empty when the store is unreadable."""
    keyed = ((route.row_issue_key(row, repo_map), row) for row in run_store.read_queue(ws / "runs"))
    return {key: f"{row.get('initiative')}/{row.get('task_id')}" for key, row in keyed if key is not None}


def _pull_source(a: argparse.Namespace, profile: dict, source: str, *, named: bool) -> int:
    """One source's pull; `named` puts the source in the nothing-written line when several pull in one run."""
    config = sources.source_config(profile, source)
    adapter = sources.adapter_for(source)
    if config is None or adapter is None:
        print(f"routing: no source {source!r} in the profile and adapters")
        return 2
    listing = _fetch_listing(adapter, config)
    if listing is None:
        return 2
    ws = Path(profile["workspace_dir"]).expanduser()
    intake_root = ws / "intake"
    known = sorted(intake_root.glob("*.md")) + sorted((intake_root / "done").glob("*.md"))
    links = sources.intake_links({str(p.relative_to(ws)): p.read_text(encoding="utf-8") for p in known})
    found, unreadable = _pull_candidates(adapter, config, listing)
    taken = frozenset(link for link in found if adapter.taken(link, links)) | _taken_by_project_item(
        found, run_store.read_queue(ws / "runs")
    )
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    repo_map = profile.get("repo_map", {})
    held = _held_issues(ws, repo_map)
    plan, reviews, refusals = route.pull_plan(
        tuple(found.values()),
        taken,
        repo_map,
        date=date,
        source=source,
        ids=profile.get("ids", "slug"),
        known_issues=held,
    )
    problems = [*unreadable, *refusals]
    for line in problems:
        print(line)
    if a.dry_run:
        for candidate, label in route.held_candidates(tuple(found.values()), held):
            print(f"routing: would skip {candidate.link}: already held by {label}")
    if not plan and not reviews:
        print(f"routing: pull wrote nothing{f' from {source}' if named else ''}: no eligible candidates")
    failed = [_pull_one(adapter, config, mapping, found, ws, a.dry_run) for mapping in plan]
    failed += [_review_one(adapter, found[link], str(_profile_path(a)), a.dry_run) for link in reviews]
    return 2 if problems or any(failed) else 0


def _harness_ready_or_refuse(harness_dir: str):
    """spec §4/§7: `launch` refuses before anything starts when the
    harness venv is not where the profile says. Returns 2 after printing
    the reason, or `None` when the venv is ready.
    """
    shell_py = Path(harness_dir) / "shell.py"
    venv_python = install_root.harness_python(harness_dir)
    if not shell_py.exists() or not venv_python.exists():
        print(f"routing: harness venv missing at {harness_dir}; install it (see the harness README)")
        return 2
    return None


def _route_sync_state_path(profile_path: Path) -> Path:
    """Where the resolved `owner/N` project persists across runs: a sidecar
    next to the profile, not a key inside it. `route.parse_profile` accepts
    a fixed field set (`route.py`'s `_KNOWN_KEYS`), and `project` is not one
    of them, so writing it into the profile itself would leave that file
    unparsable by every other route command from then on."""
    return profile_path.with_name(profile_path.name + ".route-sync-project")


def _resolve_sync_project(a: argparse.Namespace, items: list, state_path: Path):
    """`(project, None)` on success, `(None, 2)` after printing the reason.
    `project` comes back `None` (no error) only for a dry-run preview run
    before any project exists — nothing to create yet, nothing to preview
    against either."""
    if a.project:
        return a.project, None
    cached = _read_text_or_none(state_path)
    if cached and cached.strip():
        return cached.strip(), None
    owner = items[0].repo.split("/", 1)[0] if items and "/" in items[0].repo else ""
    if not owner:
        print("route sync: no --project given and no repo to derive a project owner from")
        return None, 2
    ok, found = route_sync_gh.find_project(subprocess.run, owner)
    if not ok:
        print(f"route sync: {found}")
        return None, 2
    if found is not None:
        return f"{owner}/{found}", None
    if a.dry_run:
        return None, None
    ok, number = route_sync_gh.create_project(subprocess.run, owner)
    if not ok:
        print(f"route sync: {number}")
        return None, 2
    project = f"{owner}/{number}"
    state_path.write_text(f"{project}\n")
    return project, None


def _sync_context(a: argparse.Namespace) -> tuple[str, str] | str:
    """`(workspace, tracker)` a sync runs against, or why the profile is unreadable.
    The policy file is read from `a.runs_dir` when a land passes one, else `<workspace>/runs`."""
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError as exc:
        return f"profile unreadable: {exc}"
    workspace = a.workspace or profile.get("workspace_dir") or "."
    runs_dir = Path(getattr(a, "runs_dir", None) or Path(workspace).expanduser() / "runs")
    return workspace, _resolved_tracker(profile, runs_dir)


def _sync_skipped(a: argparse.Namespace) -> bool:
    """True when the sync `a` describes would mirror nothing because the tracker is `none`."""
    context = _sync_context(a)
    return not isinstance(context, str) and context[1] == "none"


def _team_cartridge(profile: dict, repo: Path | None = None, overlay_keys: frozenset[str] | None = None) -> tuple[dict, str | None]:
    """Edge. The profile's team cartridge, with `repo`'s `.agent/cartridge.yaml` overlay when a repo is given (as
    `_cartridge_checks` applies it), and `None`; or `({}, why)` when it cannot be resolved. A profile naming no
    team and no cartridges_dir has no cartridge, which is `({}, None)`. `overlay_keys` keeps only those keys of the repo
    overlay, since the same file also carries `base:`, which the cartridge overlay refuses."""
    team, cartridges_dir = profile.get("team"), profile.get("cartridges_dir")
    if not team or not cartridges_dir:
        return {}, None
    try:
        from harness.resolve import overlay_path, resolve_cartridge
        overlay_file = Path(overlay_path(str(repo))) if repo is not None else None
        overlay = yaml.safe_load(overlay_file.read_text(encoding="utf-8")) if overlay_file and overlay_file.is_file() else None
        if overlay_keys is not None and isinstance(overlay, dict):
            overlay = {key: value for key, value in overlay.items() if key in overlay_keys}
        cartridge, _ = resolve_cartridge(team, cartridges_dir=Path(cartridges_dir).expanduser(),
                                         skills_root=[Path(r).expanduser() for r in profile.get("skills_roots") or []],
                                         overlay=overlay)
    except Exception as exc:
        return {}, f"cartridge {team} cannot be resolved: {type(exc).__name__}: {exc}"
    return cartridge, None


def _tracker_scope(profile: dict, repo: Path | None = None) -> tuple[str, dict, str | None]:
    """Edge. `(scope, epic_threshold, error)`, the cartridge's `policy.tracker_scope` over the profile's.
    An unresolvable cartridge changes nothing unless the profile itself narrows to `epics`, the one scope that
    needs the cartridge's `epic_threshold`: then the scope stays `epics` and `error` carries the resolver's reason.
    A cartridge naming a scope outside `route_sync.SCOPES` is also an `error`, with the scope fixed to `epics`.
    Otherwise `error` is None; the scope is `all` when neither side names one."""
    named = profile.get("tracker_scope")
    cartridge, error = _team_cartridge(profile, repo)
    if error is not None and named == "epics":
        return "epics", {}, error
    if error is not None:
        return named or "all", {}, None
    try:
        return route_sync.resolve_scope(named, cartridge), cartridge.get("epic_threshold") or {}, None
    except ValueError as exc:
        return "epics", {}, str(exc)


def _sync_items(a: argparse.Namespace, workspace: str) -> tuple[list[route_sync.Item], str, int | None, int]:
    """Edge. `(items, scope, rc, skipped_open)`: the items this sync mirrors under the resolved `tracker_scope`, an
    exit code to stop with when `rc` is set, after printing why, and how many open items it left out. Repos resolve
    through the profile's `repo_aliases`. An item whose repo does not resolve is dropped when it is done or dropped,
    and refused by name when open; the sync goes on without it. `--item` naming an item the scope leaves out exits 0."""
    text = _read_text_or_none(_profile_path(a))
    profile = route.parse_profile(text) if text is not None else {}
    scope, threshold, error = _tracker_scope(profile)
    if error:
        print(f"route sync: {error}; refusing rather than guessing a scope")
        return [], scope, 2, 0
    everything = route_sync_gh.items_from_store(workspace, lazy=True)
    _, refusal = _only_item(everything, a.item)
    if refusal:
        print(refusal)
        return [], scope, 2, 0
    scoped_items = route_sync.scoped(everything, scope, threshold)
    kept = route_sync.unbackfilled(scoped_items, everything, scope, profile.get("tracker_backfill") == "true")
    if a.item:
        rollup = route_sync.rollup_for(everything, scope, threshold, a.item)
        chosen = [item for item in kept if item.id == a.item] or ([rollup] if rollup else [])
    else:
        chosen = kept + route_sync.rollup_items(everything, scope, threshold)
    chosen = route_sync.with_checklists(chosen, everything, scope)
    if a.item and not chosen:
        why = ("is settled with no issue; tracker_backfill: true creates one" if any(i.id == a.item for i in scoped_items)
               else f"is out of scope under tracker_scope {scope}")
        print(f"route sync: {a.item} {why}; nothing to mirror")
        return [], scope, 0, 0
    if not a.item and len(kept) < len(scoped_items):
        print(f"route sync: left {len(scoped_items) - len(kept)} settled item(s) without an issue "
              "(tracker_backfill: true creates them)")
    resolved = route_sync_gh.resolve_repos(chosen, profile.get("repo_aliases") or {})
    items, settled, unsettled = route_sync.split_unresolved(resolved, everything)
    if a.item and not items and not unsettled:
        print(f"route sync: {a.item} is settled and its repo does not resolve; nothing to mirror")
        return [], scope, 0, 0
    for line in dict.fromkeys(item.repo_error for item in unsettled):
        print(line)
    skipped = collections.Counter(item.repo_named for item in [*settled, *unsettled])
    if skipped:
        listed = ", ".join(f"{repo} ({count})" for repo, count in skipped.items())
        print(f"route sync: skipped {sum(skipped.values())} item(s): {listed}")
    return items, scope, (2 if unsettled and not items else None), len(unsettled)


def _only_item(items: list[route_sync.Item], item_id: str | None) -> tuple[list[route_sync.Item], str | None]:
    """The items, narrowed to `item_id` when given, and a refusal line unless that leaves exactly one."""
    if not item_id:
        return items, None
    kept = [item for item in items if item.id == item_id]
    # Never fall through to the full listing: an unknown or shared id would run `item-list`.
    if len(kept) != 1:
        return kept, f"route sync: --item {item_id!r} matches {len(kept)} work-store items, expected exactly one"
    return kept, None


def _route_sync_registered(a: argparse.Namespace, name: str, mirror: object, workspace: str) -> int:
    """Drive a registered tracker through `sync`; no `gh` call is made on this path."""
    sync = getattr(mirror, "sync", None)
    if not callable(sync):
        print(f"route sync: tracker {name} has no sync(workspace, items, *, dry_run)")
        return 2
    items, _, rc, skipped = _sync_items(a, workspace)
    if rc is not None:
        return rc
    try:
        ok, lines = sync(workspace, items, dry_run=a.dry_run)
    except Exception as exc:
        print(f"route sync: tracker {name} failed: {type(exc).__name__}: {exc}")
        return 1
    for line in lines:
        print(line)
    return 1 if not ok else 2 if skipped else 0


def _route_sync_prune(a: argparse.Namespace, workspace: str, profile_path: Path) -> int:
    """Edge. `--prune-out-of-scope`: `--dry-run` prints the steps `route_sync.prune_steps` plans, a real run executes
    those same steps. Never creates a project; with none found, no card is listed."""
    text = _read_text_or_none(profile_path)
    profile = route.parse_profile(text) if text is not None else {}
    scope, _, error = _tracker_scope(profile)
    if error:
        print(f"route sync: {error}; refusing rather than guessing a scope")
        return 2
    if a.item:
        print("route sync: --prune-out-of-scope takes no --item")
        return 2
    items = [i for i in route_sync_gh.resolve_repos(route_sync_gh.items_from_store(workspace, lazy=True),
                                                    profile.get("repo_aliases") or {})
             if i.repo and not i.repo_error]
    if not route_sync_gh.auth_ok(subprocess.run):
        print("route sync: gh is not authenticated; run `gh auth login`")
        return 2
    # A copy with dry_run set, so resolving the project never creates one.
    project, rc = _resolve_sync_project(argparse.Namespace(**{**vars(a), "dry_run": True}), items,
                                        _route_sync_state_path(profile_path))
    if rc is not None:
        return rc
    ok, result = route_sync_gh.prune_candidates(subprocess.run, sorted({i.repo for i in items}), project)
    if not ok:
        print(f"route sync: {result}")
        return 2
    issues, cards = result
    steps = route_sync.prune_steps(items, issues, cards, scope)
    if not steps:
        print(f"route sync: nothing is out of scope under tracker_scope {scope}")
        return 0
    print("route sync: every open `coxswain`-labelled task issue counts as made by sync, one labelled by hand too")
    if a.dry_run:
        print("\n".join(route_sync.render(steps)))
        return 0
    log = route_sync_gh.execute(steps, subprocess.run, project or "", workspace, items)
    for kind, _, detail in log:
        print(f"{kind}: {detail}")
    return 1 if any(not step_ok for _, step_ok, _ in log) else 0


def _route_sync(a: argparse.Namespace) -> int:
    """Mirror the work store onto GitHub Projects: `--dry-run` renders the
    plan and touches nothing that outlives the run; otherwise `execute`
    runs it and stops at the first failed `gh` call. Refuses at once, exit
    2, when `gh` is not authenticated. The tracker is checked first: `none`
    exits 0, an unknown tracker exits 2, and a registered tracker is driven
    through its own `sync`, never through `gh`. An item's release is the line `Target release: <version>.`
    in its body; a `release:` front-matter key is not read. It sets the board's Release field, and an epic's
    release, the latest of its tasks', also sets the milestone of that name on the epic's issue. Before any
    issue is created, a State value with no board option stops the run, exit 1, naming the option."""
    context = _sync_context(a)
    if isinstance(context, str):
        print(f"route sync: {context}")
        return 2
    workspace, tracker_choice = context
    profile_path = _profile_path(a)
    if tracker_choice == "none":
        print("route sync: tracker is none; set tracker: github-projects in the profile to mirror the work store")
        return 0
    mirror = tracker.tracker_for(tracker_choice)
    if mirror is None:
        print(f"route sync: no tracker named {tracker_choice} (built in: none, github-projects)")
        return 2
    # `route_sync_gh` here is a lazy proxy, so the identity test runs against the real module.
    prune = getattr(a, "prune_out_of_scope", False)
    if mirror is not importlib.import_module("agent_tools.route_sync_gh"):
        if prune:
            print(f"route sync: --prune-out-of-scope needs the github-projects tracker, not {tracker_choice}")
            return 2
        return _route_sync_registered(a, tracker_choice, mirror, workspace)
    if prune:
        return _route_sync_prune(a, workspace, profile_path)
    # Items first: an unresolvable repo is refused by name before any `gh` call, `auth_ok` included.
    items, scope, rc, skipped = _sync_items(a, workspace)
    if rc is not None:
        return rc
    if not route_sync_gh.auth_ok(subprocess.run):
        print("route sync: gh is not authenticated; run `gh auth login`")
        return 2
    project, rc = _resolve_sync_project(a, items, _route_sync_state_path(profile_path))
    if rc is not None:
        return rc
    repo_names = sorted({item.repo for item in items if item.repo})
    if a.item and not items[0].id.startswith(route_sync.ROLLUP_PREFIX):
        ok, result = route_sync_gh.existing_item(subprocess.run, items[0].repo, items[0].issue, project)
    else:  # a roll-up has no issue number of its own to view: it is found by title among the listed issues
        ok, result = route_sync_gh.existing(subprocess.run, repo_names, project)
    if not ok:
        print(f"route sync: {result}")
        return 2
    issues, project_items, item_node_ids = result
    items = route_sync.link_rollups(items, issues)
    steps = route_sync.plan(items, issues, project_items, "github-projects", scope)
    if a.dry_run:
        print("\n".join(route_sync.render(steps)))
        return 2 if skipped else 0
    log = route_sync_gh.execute(steps, subprocess.run, project, workspace, items, item_node_ids)
    for kind, _, detail in log:
        print(f"{kind}: {detail}")
    hard_failure = any(not step_ok for kind, step_ok, _ in log if kind != "refuse")
    return 1 if hard_failure else 2 if skipped else 0


_LAUNCH_SETTLE_S = 2.0  # how long a fresh launch is watched for an immediate death before it is reported as started


# The line the harness prints once its run holds the lease (src/graphs/harness/cli.py); seeing it ends the wait early.
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
            status = subprocess.run(
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
            taken = remote_lane.refuse_taken_run_id(a.run_id, set(_taken_run_names(runs_dir)))
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
                    fields.get("repo", ""), fields.get("intake", intake), idea_body,
                )
                initiative_md.write_text(content, encoding="utf-8")
                print(f"routing: initiative.md refreshed from {idea_path}")
        else:
            initiative_content = route.initiative_text(
                a.initiative_id, idea_fields.get("title", ""), idea_fields.get("repo", ""), intake, idea_body,
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
            taken = remote_lane.refuse_taken_run_id(a.run_id, set(_taken_run_names(runs_dir)))
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

    if via_rows:
        (runs_dir / f"{run_id}.launched.json").write_text(
            json.dumps({"initiative": a.initiative_id, "at": datetime.datetime.now(datetime.UTC).isoformat()}), encoding="utf-8",
        )
        exit_code, result = _run_decompose_for_result(argv, env, log_path, result_path, pid_path)
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
        rows_code, rows_lines = route.decompose_into_rows(runs_dir, idea_path.stem, lambda _intake: plan)
        for line in rows_lines:
            print(line)
        return rows_code

    launch_env = (
        lane_capacity_env(env, _local_max_in_flight(runs_dir, dict(profile), socket.gethostname()))
        if a.graph == "epic" else env
    )
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            argv, env=launch_env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
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


def _decompose_via_rows(mode: str, dry_run: bool, host: object, intake_row: Mapping | None, *, detach: bool = False) -> bool:
    """True when a decompose launch runs to completion here and writes rows: store mode, a real run, this machine, an intake row, not detached."""
    return mode == "store" and not dry_run and host is None and intake_row is not None and not detach


_DECOMPOSE_TIMEOUT_S = 3600


def _run_decompose_for_result(
    argv: list[str], env: Mapping[str, str], log_path: Path, result_path: Path, pid_path: Path,
) -> tuple[int, dict | None]:
    """Edge: run the decompose to completion, its pid in `pid_path` while it runs and its output in the log.

    Returns `(exit code, result)`; the result is the `--result-out` JSON, None when missing, invalid or not an object.
    A run past the timeout is killed and reports exit 124.
    """
    result_path.unlink(missing_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
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


def _remote_edge(cwd: Path) -> tuple[Callable[[list[str]], int], Callable[[str], str] | None]:
    """Edge: the one seam that runs rsync and ssh for `--on`; `cwd` is the workspace the `work/<id>` copy is relative to."""

    def run(argv: list[str]) -> int:
        try:
            return subprocess.run(argv, cwd=cwd).returncode
        except OSError as exc:
            print(f"{argv[0]}: {exc}")
            return 127

    return run, None


def _lane_host_candidates(
    profile_text: str, rows: Sequence[Mapping], local: str = "",
) -> tuple[lane_hosts.LaneHost, ...] | lane_hosts.LaneHostError:
    """The table's active hosts when the table has rows, else the profile's `lane_hosts`. A row named `local` is
    never one of them: the loop's own machine is never a remote lane-host candidate for itself."""
    parsed = _profile_lane_hosts(profile_text)
    if not rows:
        return parsed
    return host_cmd.host_rows_to_lane_hosts(rows, () if isinstance(parsed, lane_hosts.LaneHostError) else parsed, local)


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
        done = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        return f"{argv[0]}: {exc}"
    return done.stdout + done.stderr


def _host_env_probe(argv: list[str], env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Edge: (exit code, text) of a `printenv` over ssh; text is stdout on 0, else stderr. An ssh that cannot run is (127, error).

    `env` is the probe's environment for a local lookup; None inherits this process's, as ssh probes do.
    Not `_host_auth_output`: ssh stderr (a host-key warning) is never the variable's value."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, env=env)
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
        preflight = remote_doctor.auth_verdict(_host_auth_output(remote_argv.ssh_argv(host.ssh, remote_argv.auth_status_argv())))
    else:
        preflight = remote_launch.env_preflight(
            env_names, {name: _host_env_probe(remote_argv.ssh_argv(host.ssh, remote_argv.env_check_argv(name))) for name in env_names}
        )
    result = remote_launch.launch_on_host(
        host, initiative, run_id, _holder_label(a), launched_at, run, locate, repo, harness_dir,
        preflight=preflight, home=home, host_checkouts=_host_checkouts(run_store.hosts(runs_dir), host.name), tools=tools,
        graph=graph, intake=intake, cartridges=_workspace_cartridges(runs_dir), probe=_workspace_probe(runs_dir.parent),
        profile=provider, phase=phase,
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


def _remote_fetch_facts(runs_dir: Path, run: str) -> tuple[bool, str | None]:
    """Edge. (lease_released, ended_at) for `run` from the store, as the docket reads them: a live lease, or no ended span, keeps a lane unfetched."""
    live = {lane.run for lane in run_store.live_lanes(runs_dir, _now_iso())}
    ended = {span_run: ended_at for span_run, _, ended_at in run_store.run_spans(runs_dir, "")}
    return run not in live, ended.get(run)


def _remote_capture(cwd: Path) -> Callable[[list[str]], str | None]:
    """Edge: runs an argv and returns its stdout, None when it cannot run or exits non-zero."""

    def capture(argv: list[str]) -> str | None:
        try:
            done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        except OSError:
            return None
        return done.stdout if done.returncode == 0 else None

    return capture


def _task_pairs_from_listing(text: str) -> list[tuple[str, str]]:
    """Pure: (phase, task) for each `<phase>/<task>.json` file in an `rsync -r --list-only` listing of a run's `tasks/`."""
    fields = (line.split(None, 4) for line in text.splitlines())
    paths = (f[4].split("/") for f in fields if len(f) == 5 and f[0].startswith("-"))
    return [(path[0], path[1].removesuffix(".json")) for path in paths if len(path) == 2 and path[1].endswith(".json")]


def _store_holds_every_record(pairs: list[tuple[str, str]], read: Callable[[str, str], dict | None]) -> bool:
    """Pure: True when `read` finds a record for every (phase, task) pair; no pairs is False."""
    held = {task for phase, task in pairs if read(phase, task) is not None}
    return remote_lane.records_already_in_store([task for _, task in pairs], held)


def _store_task_records(runs_dir: Path, run_id: str, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict | None]:
    """Edge: each pair's task record from the store; a pair the store cannot read maps to None."""

    def read(phase: str, task: str) -> dict | None:
        try:
            return run_store.task_record(runs_dir, run_id, phase, task)
        except (OSError, ValueError):
            return None

    return {(phase, task): read(phase, task) for phase, task in pairs}


def _branch_only_repos(
    runs_dir: Path, host: lane_hosts.LaneHost, locate: Callable[[str], str] | None, run_id: str, recorded: list[str],
) -> list[str] | None:
    """Edge. The repos to fetch branches for when the store holds every task record of the remote run, else None.

    The records are not copied, so the repos come from the store records, then from the remote record. With neither the
    branch fetch has nowhere to go, and the caller copies the records as before."""
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    tasks_dir = f"{host.workspace_dir.rstrip('/')}/runs/{run_id}/tasks/"
    listing = _remote_capture(runs_dir.parent)(["rsync", "-r", "--list-only", place(tasks_dir)])
    pairs = _task_pairs_from_listing(listing or "")
    records = _store_task_records(runs_dir, run_id, pairs)
    complete = _store_holds_every_record(pairs, lambda phase, task: records[(phase, task)])
    if remote_lane.fetch_scope(complete) != "branches":
        return None
    stored = [r["repo"] for r in records.values() if r is not None and isinstance(r.get("repo"), str) and r["repo"]]
    return list(dict.fromkeys(stored or recorded)) or None


def _store_run_rows(runs_dir: Path, run_id: str, wanted: set[str] | None = None) -> dict[tuple[str, str], dict | None]:
    """Edge. The run's `task_records` rows from the store alone, keyed (phase, task); the remote is never listed.
    `wanted` limits the read to those task ids. A row the store cannot read is left out."""
    if wanted is not None and not wanted:
        return {}
    tasks = [task for task in run_store.run_task_ids(runs_dir, run_id) if wanted is None or task in wanted]
    pairs = [(phase, task) for task in tasks for phase in run_store.task_record_phases(runs_dir, run_id, task)]
    return {pair: record for pair, record in _store_task_records(runs_dir, run_id, pairs).items() if record is not None}


def _store_held_runs(runs_dir: Path) -> list[str]:
    """Edge. Fetched remote runs: a store-mode fetch leaves their records in the store, sometimes beside a local record
    file for one task, so a local file does not make the run's other tasks local. A landed-elsewhere marker names a run
    with nothing left to land, so it is not one."""
    suffix = ".fetched.json"
    fetched = [p.name[: -len(suffix)] for p in sorted(runs_dir.glob(f"*{suffix}"))]
    return [
        run for run in fetched
        if not remote_lane.is_landed_elsewhere_marker(_read_text_or_none(remote_lane.fetched_record_path(runs_dir, run)))
    ]


def _local_record_tasks(runs_dir: Path, run: str) -> set[str]:
    """Edge. The task ids that have a record file under `runs/<run>/tasks/`; those are read from the file, not the store."""
    return {path.stem for path in (runs_dir / run / "tasks").glob("*/*.json")}


def _store_held_records(runs_dir: Path, items: list[dict]) -> list[dict]:
    """Edge. The store's task records of every `_store_held_runs` run, for tasks whose item is ready or approved,
    each carrying `run`, `task` and `phase` as a record read from `runs/<run>/tasks/` does."""
    live = {(item["initiative"], item["id"]) for item in items if item.get("state") in ("ready", "approved")}
    return [
        {"run": run, "task": task, "phase": phase, **record}
        for run in _store_held_runs(runs_dir)
        for (phase, task), record in _store_run_rows(
            runs_dir, run,
            {task for initiative, task in live if initiative == chair_facts.run_initiative(run)} - _local_record_tasks(runs_dir, run),
        ).items()
        if record is not None
    ]


def _approved_store_rows(records: Mapping[tuple[str, str], dict | None]) -> list[tuple[str, str, dict]]:
    """Pure: (phase, task, record) for every store row whose review and arbitration verdicts approve it
    (`land._approved` is None); a row the store could not read is left out."""
    return [
        (phase, task, record)
        for (phase, task), record in records.items()
        if record is not None and land._approved(record) is None
    ]


def _store_approved_task_facts(
    runs_dir: Path, work_root: Path, run_id: str, records: Mapping[tuple[str, str], dict | None], by: str,
) -> list[dict]:
    """Edge. One approved-task fact per store row that is approved and whose work item moves from ready to
    approved; each fact carries `run = run_id` so a `land_phase` planned from it names a run. A row whose
    initiative cannot be found, or whose compare-and-set the store refuses, contributes no fact."""
    facts = []
    started = run_store.run_started_at(runs_dir, run_id)
    for phase, task, _record in _approved_store_rows(records):
        initiative = _initiative_of(work_root, task)
        if initiative is None:
            continue
        row = next((r for r in run_store.work_items(runs_dir, initiative) if r.get("task_id") == task), None)
        if row is not None and remote_fetch.changed_after_start(row.get("updated_at"), started):
            print(f"fetch: {task} kept {row.get('state')}, changed after run {run_id} started")
            continue
        moved = land.set_state_stop(store_cli.set_state(runs_dir, initiative, task, "approved", by, expected="ready")) is None
        if moved:
            facts.append({"id": task, "initiative": initiative, "phase": phase, "run": run_id})
    return facts


def _landed_elsewhere(
    runs_dir: Path, host: lane_hosts.LaneHost, locate: Callable[[str], str] | None, run_id: str, ended: bool,
) -> bool:
    """Edge. `remote_lane.landed_elsewhere` for the run: the store is read only once it has ended, and the remote is
    listed only once every stored task is done. A task whose initiative cannot be found counts as not done, and a
    listing that fails is passed on as None, so the caller fetches as before."""
    work_root = runs_dir.parent / "work"

    def state(task: str) -> str | None:
        initiative = _initiative_of(work_root, task)
        return None if initiative is None else run_store.task_state_of(runs_dir, initiative, task)

    states = {task: state(task) for task in run_store.run_task_ids(runs_dir, run_id)} if ended else {}
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    tasks_dir = f"{host.workspace_dir.rstrip('/')}/runs/{run_id}/tasks/"
    listing = _remote_capture(runs_dir.parent)(["rsync", "-r", "--list-only", place(tasks_dir)]) if remote_lane.all_landed(states) else None
    listed = None if listing is None else [task for _, task in _task_pairs_from_listing(listing)]
    return remote_lane.landed_elsewhere(states, listed, ended)


def _fetch_one(runs_dir: Path, hosts, run_id: str, mode: str = "files", by: str = _UNLABELED) -> tuple[str, list[str], str]:
    """Edge. (outcome, lines, host name) for one remote run; outcome is fetched, live or failed.

    The task records are always copied, their host paths rewritten to this machine's, and the marker lists them.
    Under `mode` "store", when the store already holds every one of them, the
    approved ones move their work item from ready to approved in the store instead, so an approved-task fact
    built from them carries this run. An ended run whose stored tasks are all done, and whose remote lists no
    task the store lacks, is marked fetched with nothing pulled. The remote is listed to check that, so an
    unreachable remote is fetched as before and fails there."""
    text = _read_text_or_none(remote_lane.remote_record_path(runs_dir, run_id))
    record = remote_lane.parse_remote_record(text) if text is not None else None
    if record is None:
        return "failed", [f"fetch: no {run_id}.remote.json in {runs_dir}; the run was not launched with --on"], ""
    if isinstance(hosts, lane_hosts.LaneHostError):
        return "failed", [f"fetch: {hosts.message}"], ""
    host = lane_hosts.find_lane_host(hosts, record["host"])
    if host is None:
        return "failed", [f"fetch: host {record['host']} is in neither the hosts table nor the profile lane_hosts",
                          f"configured: {', '.join(h.name for h in hosts) or 'none'}"], ""
    lease_released, ended_at = _remote_fetch_facts(runs_dir, run_id)
    run, locate = _remote_edge(runs_dir.parent)
    if remote_lane.is_decompose_record(record):
        # parse_remote_record keeps only the fields remote_record knows, so the initiative is read from the raw text.
        initiative = json.loads(text).get("initiative")
        return _fetch_decompose(runs_dir, host, initiative, run_id, lease_released, ended_at, run)
    ended = remote_fetch.refuse_unended(lease_released, ended_at) is None
    if mode == "store" and _landed_elsewhere(runs_dir, host, locate, run_id, ended):
        remote_lane.fetched_record_path(runs_dir, run_id).write_text(json.dumps(remote_lane.landed_elsewhere_marker(_now_iso())))
        return "fetched", [remote_lane.landed_elsewhere_line(run_id)], host.name
    recorded = [record["repo"]] if record.get("repo") else []
    only = _branch_only_repos(runs_dir, host, locate, run_id, recorded) if mode == "store" else None

    def repo_paths(run_dir: Path) -> list[str]:
        return (remote_fetch.task_repos(run_dir) or recorded) if only is None else only

    diverged: list[str] = []
    result = remote_fetch.fetch_run(host, run_id, runs_dir, repo_paths, run, locate,
                                    lease_released=lease_released, ended_at=ended_at, home=os.path.expanduser("~"),
                                    diverged=diverged)
    if isinstance(result, remote_fetch.FetchError):
        detail = f"{run_id} is still live on {host.name}: {result.message}" if result.step == "refuse" else result.message
        return ("live" if result.step == "refuse" else "failed"), [f"fetch: {result.step}: {detail}"], host.name
    wanted = remote_fetch.phase_branches(run_store.phase_manifests(runs_dir, run_id))
    brought = (remote_fetch.fetch_phase_branches(host, run_id, runs_dir, list(result), wanted, run, locate,
                                                 home=os.path.expanduser("~"), diverged=diverged)
               if wanted else ())
    # A diverged phase branch kept its local tip; the line names the side ref holding the lane's, so nothing is lost
    # silently. Printed here because `--all` drops a fetched run's lines, and the chair reads this output.
    for line in dict.fromkeys(diverged):
        print(f"{run_id}: {line}")
    marker = {"fetched_at": _now_iso(), "repos": list(result), "tasks": remote_fetch.task_record_ids(runs_dir / run_id),
              "phase_branches": list(brought)}
    remote_lane.fetched_record_path(runs_dir, run_id).write_text(json.dumps(marker))
    if only is not None:
        records = _store_run_rows(runs_dir, run_id)
        moved = _store_approved_task_facts(runs_dir, runs_dir.parent / "work", run_id, records, by)
        if moved:
            print(f"{run_id}: {len(moved)} task(s) approved from the store")
    return "fetched", [f"run {run_id}", f"host {host.name}", "\n".join(f"repo {repo}" for repo in result)], host.name


def _repo_restore(pulled: str, intake: str | None) -> tuple[str, str] | None:
    """Pure. (initiative text carrying the intake's repo, that repo), or None when the pulled text already has a repo or the intake gives none."""
    held = route.parse_frontmatter(pulled)[0].get("repo")
    found = route.parse_frontmatter(intake)[0].get("repo") if intake is not None else None
    if (isinstance(held, str) and held) or not (isinstance(found, str) and found):
        return None
    return route.initiative_with_repo(pulled, found), found


def _restore_intake_repo(workspace: Path, pulled: str) -> tuple[str, str]:
    """Edge. The staged initiative.md text with the intake's repo put on it, and that repo; the text unchanged and "" when nothing changed."""
    named = route.parse_frontmatter(pulled)[0].get("intake")
    intake = _read_text_or_none(workspace / named) if isinstance(named, str) and named else None
    restored = _repo_restore(pulled, intake)
    return (pulled, "") if restored is None else restored


def _store_initiative_repo(runs_dir: Path, initiative: str, repo: str) -> str:
    """Edge. Set `extra.repo` on the initiative's stored row when it differs; "" when done or nothing to do, else the store's refusal.
    The store fill leaves an existing row whose `extra` is not null alone, so a repo missing there is set here."""
    row = next((r for r in run_store.read_queue(runs_dir, initiative, kind="initiative") if r.get("task_id") == initiative), None)
    extra = (row or {}).get("extra") or {}
    if row is None or extra.get("repo") == repo:
        return ""
    return run_store.upsert_row_detail(runs_dir, {**{k: v for k, v in row.items() if k not in ("holder", "epoch", "expires_at")}, "extra": {**extra, "repo": repo}})


def _fetch_decompose(
    runs_dir: Path, host, initiative: object, run_id: str, lease_released: bool, ended_at: str | None,
    run: Callable[[list[str]], int],
) -> tuple[str, list[str], str]:
    """Edge. Fetch an ended remote decompose: its `work/<initiative>` files and log, rows first, files after.

    A decompose leaves files and a log, never a branch, so no git runs and no task path is rewritten. The files are
    staged under `runs/.fetch-staging/<run>/`, their rows are written to the store and read back, and only then are
    they copied into `work/` (recorded as written), the staging removed and the marker written. Any store failure
    returns "failed" with the staged files kept and no marker, so the next tick pulls and tries again."""
    from agent_tools import store_fill  # imported here: cli loads a command module only when it runs

    refusal = remote_fetch.refuse_unended(lease_released, ended_at)
    if refusal is not None:
        return "live", [f"fetch: refuse: {run_id} is still live on {host.name}: {refusal}"], host.name
    if not isinstance(initiative, str) or not initiative or Path(initiative).name != initiative:
        return "failed", [f"fetch: the {run_id} remote record names no initiative"], host.name
    workspace = runs_dir.parent
    staging = Path(remote_fetch.staging_dir(str(workspace), run_id))
    staged_work = staging / "work" / initiative
    staged_work.mkdir(parents=True, exist_ok=True)
    for argv in remote_fetch.decompose_pull_argvs(host.ssh, host.workspace_dir, str(workspace), initiative, run_id):
        code = run(argv)
        if code != 0:
            return "failed", [f"fetch: rsync: {' '.join(argv)} exited {code}"], host.name
    texts = {p.relative_to(staging).as_posix(): p.read_text(encoding="utf-8") for p in sorted(staged_work.rglob("*.md")) if p.is_file()}
    initiative_key = f"work/{initiative}/initiative.md"
    repo = ""
    if initiative_key in texts:
        texts[initiative_key], repo = _restore_intake_repo(workspace, texts[initiative_key])
    files = [(tuple(key.split("/")), text) for key, text in texts.items()]
    parsed, _ = store_fill.parse_workspace(files)
    confirmed = {key: text for key, text in texts.items() if store_fill.parse_workspace([(tuple(key.split("/")), text)])[0]}
    existing = store_fill._read_store(runs_dir)
    if existing is None:
        return "failed", [f"fetch: store: {store_fill._UNREADABLE}"], host.name
    for row in store_fill.plan_fill(parsed, existing).writes:
        detail = run_store.upsert_row_detail(runs_dir, row)
        if detail:
            return "failed", [f"fetch: store: store refused {row['initiative']}/{row['task_id']}: {detail}"], host.name
    detail = _store_initiative_repo(runs_dir, initiative, repo) if repo else ""
    if detail:
        return "failed", [f"fetch: store: {detail}"], host.name
    stored = store_fill._read_store(runs_dir)
    if stored is None:
        return "failed", [f"fetch: store: {store_fill._UNREADABLE}"], host.name
    missing = remote_fetch.unconfirmed_keys([(r["initiative"], r["task_id"]) for r in parsed], stored)
    if missing:
        return "failed", [f"fetch: store: rows not read back: {', '.join('/'.join(key) for key in missing)}"], host.name
    for key, text in confirmed.items():
        (workspace / key).parent.mkdir(parents=True, exist_ok=True)
        (workspace / key).write_text(text, encoding="utf-8")
    queue_export.record_written(workspace, confirmed)
    shutil.rmtree(staging)
    with contextlib.suppress(OSError):
        staging.parent.rmdir()  # runs/.fetch-staging goes with its last run
    tasks = [key.removeprefix(f"work/{initiative}/") for key in confirmed if key != initiative_key]
    marker = {"fetched_at": _now_iso(), "initiative": initiative, "tasks": tasks}
    remote_lane.fetched_record_path(runs_dir, run_id).write_text(json.dumps(marker))
    return "fetched", [f"run {run_id}", f"host {host.name}", f"initiative {initiative}"], host.name


def _land_refetch_phase_branch(a: argparse.Namespace, runs_dir: Path, run_id: str, repo: Path, branch: str) -> str | None:
    """Edge. The remote host name when `branch` is missing here and one more fetch of the run does not bring it, else None.

    A run with no remote record is never checked. The fetch runs in files mode so it moves nothing in the store."""
    text = _read_text_or_none(remote_lane.remote_record_path(runs_dir, run_id))
    record = remote_lane.parse_remote_record(text) if text is not None else None
    if record is None or _branch_exists(repo, f"refs/heads/{branch}"):
        return None
    hosts = _fetch_hosts(_read_text_or_none(_profile_path(a)) or "", run_store.hosts(runs_dir))
    _fetch_one(runs_dir, hosts, run_id, "files")
    return None if _branch_exists(repo, f"refs/heads/{branch}") else record["host"]


def _runs_fetch_all(runs_dir: Path, hosts, mode: str = "files", by: str = _UNLABELED) -> int:
    """Fetches every remote run with no `.fetched.json` marker; a live run is skipped, a failed fetch makes the exit 2."""
    suffix = ".remote.json"
    remote_runs = [p.name[: -len(suffix)] for p in sorted(runs_dir.glob(f"*{suffix}"))]
    fetched = {run for run in remote_runs if remote_lane.fetched_record_path(runs_dir, run).exists()}
    todo = remote_lane.unfetched(remote_runs, fetched)
    if not todo:
        print("nothing to fetch")
        return 0
    failed = False
    for run_id in todo:
        outcome, lines, host = _fetch_one(runs_dir, hosts, run_id, mode, by)
        if outcome == "live":
            print(f"{run_id}: still live on {host}")
        elif outcome == "fetched":
            marker = _read_text_or_none(remote_lane.fetched_record_path(runs_dir, run_id))
            print(remote_lane.landed_elsewhere_line(run_id) if remote_lane.is_landed_elsewhere_marker(marker) else f"{run_id}: fetched from {host}")
        else:
            failed = True
            print("\n".join([f"{run_id}: {lines[0]}", *lines[1:]]))
    return 2 if failed else 0


def _fetch_hosts(profile_text: str, rows: Sequence[Mapping]) -> tuple[lane_hosts.LaneHost, ...] | lane_hosts.LaneHostError:
    """The hosts a remote run may be fetched from: the same table-then-profile hosts `route launch --on` picks from,
    drained ones included, since a run launched before its host was drained still has to come back. Reading only
    the profile's lane_hosts refused every run launched on a hosts-table host (2026-10-23)."""
    return _lane_host_candidates(profile_text, [{**row, "state": "active"} for row in rows])


def _runs_fetch(a: argparse.Namespace) -> int:
    """Pulls an ended remote run's records, log and branches to this machine; writes nothing in the store."""
    if bool(a.run_id) == bool(a.all):
        print("fetch: give one run id, or --all")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"fetch: {reason}")
        return 2
    hosts = _fetch_hosts(_read_text_or_none(_profile_path(a)) or "", run_store.hosts(runs_dir))
    mode = work_state.work_state_mode(_lake_provider(a)[0])
    by = _holder_label(a)
    if a.all:
        return _runs_fetch_all(runs_dir, hosts, mode, by)
    outcome, lines, _ = _fetch_one(runs_dir, hosts, a.run_id, mode, by)
    print("\n".join(lines))
    return 0 if outcome == "fetched" else 2


def _export_board(runs_dir: Path, workspace: Path) -> list[str]:
    """Edge. Write the whole store board under `workspace` and return the ids of the contentless rows skipped.

    The whole board, never one initiative: `export_files` deletes every intake/ and work/ file its rows do not name.
    So it fills the store from the files first: a decompose writes its tickets as files, and an export with no fill
    deleted them, on the chair's tick and on an epic launch alike (2026-10-06). A fill that cannot read the store
    raises before anything is written or deleted."""
    from agent_tools import store_fill  # imported here: cli loads a command module only when it runs

    store_fill.fill_workspace(workspace)
    # A file with no row is not necessarily orphaned: it may be a live decompose's, whose rows are not written yet.
    live = chair_decompose_runs.live_decompose_initiatives(runs_dir)
    return queue_export.export_files(workspace, run_store.read_queue(runs_dir), hashes=queue_export.store_hashes(runs_dir), protected=live).skipped


def _launch_rows_then_export(
    runs_dir: Path, initiative_id: str, repo: str | None, is_chair: bool,
    read_rows: Callable | None = None, export: Callable[[Path, Path], list[str]] | None = None,
    fill: Callable[[Path], object] | None = None, land_at: str = "task",
) -> tuple[int, list[str]]:  # "task" is route.DEFAULT_LAND_AT
    """Edge. Fill the store from the files, read the initiative's rows, then export the whole board only when `is_chair`.

    `land_at` reaches `route.launch_from_rows`; an injected `read_rows` takes three arguments and ignores it.

    The fill runs first for every host, since a decompose writes files and the rows read sees only the store. A fill that
    cannot read the store refuses with code 2 before any rows read or export. A rows error returns before any export."""
    from agent_tools import store_fill  # imported here: cli loads a command module only when it runs

    try:
        (fill or store_fill.fill_workspace)(runs_dir.parent)
    except RuntimeError:
        return 2, [f"routing: no initiative row {initiative_id} in the store, or the store is unavailable"]
    code, lines, _ = (read_rows or (lambda *args: route.launch_from_rows(*args, land_at=land_at)))(runs_dir, initiative_id, repo)
    if code:
        return code, lines
    if not route.should_export_board(is_chair):
        return 0, []
    skipped = (export or _export_board)(runs_dir, runs_dir.parent)
    return 0, [f"export: skipped {len(skipped)} contentless row(s): {', '.join(skipped)}"] if skipped else []


def export_commit_excludes(held: Sequence[Mapping[str, str]]) -> list[str]:
    """Pure: one `:(exclude)` pathspec per held file, so the export commit stages and commits around it."""
    return [f":(exclude){row['path']}" for row in held if row.get("path")]


def workspace_git_on(profile: Mapping) -> bool:
    """Whether the chair keeps the workspace in git: `chair.workspace_git`, on unless the profile says false.

    Off, the store is the only shared copy of the board. The work/ and intake/ trees are then this machine's own
    files, written from the store so everything that reads them still can: the chair neither pulls the workspace
    nor commits and pushes what it exports, and any machine refreshes its copy with `cox route export`."""
    chair = profile.get("chair")
    return not (isinstance(chair, Mapping) and chair.get("workspace_git") is False)


def _chair_export_hook(runs_dir: Path, ids: tuple[str, ...], commit: bool = True) -> str:
    """Edge. Export the board, then commit the work/ and intake/ files; returns a tick-line note, "" when nothing is worth saying.

    With `commit` False (`chair.workspace_git: false`) the files are written and nothing is committed or pushed.

    The files the export held are left out of the commit: committing one would make the hand edit this workspace's HEAD
    baseline, and the next export would overwrite it. A git failure raises, so the tick reports it as an export failure.
    A failed push is a note, never a raise."""
    workspace = runs_dir.parent
    skipped = _export_board(runs_dir, workspace)  # fills the store from the files first; see _export_board
    if not commit:
        wrote = f"export: wrote the files for {len(ids)} changed row(s); the workspace is not in git"
        return f"{wrote}; skipped {len(skipped)} contentless row(s): {', '.join(skipped)}" if skipped else wrote
    git = ["git", "-C", str(workspace)]
    names = [name for name in ("work", "intake") if (workspace / name).exists()]  # git add refuses a path that matches nothing
    if not names:  # `git add -A --` with no path stages the whole tree
        return "export: no work/ or intake/ directory to commit"
    paths = [*names, *export_commit_excludes(queue_export.read_held_record(workspace))]
    add = subprocess.run([*git, "add", "-A", "--", *paths], capture_output=True, text=True)
    if add.returncode != 0:
        raise RuntimeError(f"git add: {add.stderr.strip()}")
    # The staged files by name, not the trees: `git commit -- work` refuses a tree whose every file is held and untracked.
    staged = subprocess.run([*git, "diff", "--cached", "--name-only", "-z", "--", *paths], capture_output=True, text=True)
    if staged.returncode != 0:
        raise RuntimeError(f"git diff: {staged.stderr.strip()}")
    files = [name for name in staged.stdout.split("\0") if name]
    if not files:
        committed = "export: no file changes"
    else:
        message = f"chair: export {len(ids)} changed row(s)"
        commit = subprocess.run([*git, "commit", "-m", message, "--", *files], capture_output=True, text=True)
        if commit.returncode != 0:
            raise RuntimeError(f"git commit: {(commit.stderr or commit.stdout).strip()}")
        pushed = workspace_sync.push_workspace(workspace)
        committed = f"export: committed {len(ids)} row(s)" + (f"; {pushed}" if pushed else "")
    return f"{committed}; skipped {len(skipped)} contentless row(s): {', '.join(skipped)}" if skipped else committed


def _initiative_head(runs_dir: Path, initiative_id: str) -> dict | None:
    """Edge. The initiative's own store row; None when the store has none."""
    rows = run_store.read_queue(runs_dir, initiative_id, kind="initiative")
    return next((r for r in rows if r.get("task_id") == initiative_id), None)


def _events_of(head: Mapping | None) -> list | None:
    return None if head is None else list((head.get("extra") or {}).get("events", []))


def _store_mode(runs_dir: Path | None, initiative_dir: Path) -> str:
    """Edge. How a merge of `initiative_dir` reaches the store: "up" (an initiative row), "headless" (rows for the
    initiative but no initiative row), "outside" (not the workspace's own `work/<initiative>`, so the store's rows
    are not its rows), else "down": no workspace, or a store that answers with nothing, which cannot be told apart."""
    if runs_dir is None:
        return "down"
    if (runs_dir.parent / "work" / initiative_dir.name).resolve() != initiative_dir:
        return "outside"
    if _initiative_head(runs_dir, initiative_dir.name) is not None:
        return "up"
    return "headless" if run_store.read_queue(runs_dir, initiative_dir.name) else "down"


def _unfiled_notice(state: str, path: Path, initiative_id: str) -> str | None:
    """Pure: the one line printed before `path` is written without a store row behind it; None when it needs none."""
    if state in ("applied", "outside"):
        return None
    if state == "headless":
        return f"routing: the store has no row for initiative {initiative_id}, filing {path} without it"
    return route._unrecorded_row_warning(path, {})


def _rows_before(runs_dir: Path, initiative_id: str, sources: Mapping[str, tuple[Path, str]]) -> dict[str, dict]:
    """Edge. Each ticket's task row as it stands, else the row its file describes: what a rollback puts back."""
    stored = {r["task_id"]: r for r in run_store.read_queue(runs_dir, initiative_id, kind="task")}
    parsed = {task: queue_rows.parse_item("task", (initiative_id, path.parent.name, path.stem), text) for task, (path, text) in sources.items()}
    return {task: row for task in sources if (row := stored.get(task) or parsed[task]) is not None}


def _put_back(runs_dir: Path, rows: Sequence[Mapping]) -> None:
    """Edge. Upserts `rows`, one printed line for each the store refuses."""
    for row in rows:
        if detail := run_store.upsert_row_detail(runs_dir, row):
            print(f"routing: could not put back {row['kind']} row {row['task_id']}: {detail}; the store and the workspace now disagree")


def _store_merge(runs_dir: Path, initiative_id: str, items: list[dict], merge: Mapping, paths: list[Path], before: Mapping[str, dict]) -> str:
    """Edge. One merge to the store: "applied", "refused" (its accepted rows put back), or "unavailable" (store gone)."""
    seen = _events_of(_initiative_head(runs_dir, initiative_id))
    rows, events = route.merge_rows(items, [merge], initiative_id)
    accepted = route.write_merge_rows(runs_dir, rows, events, initiative_id, paths)
    held = _events_of(_initiative_head(runs_dir, initiative_id))
    if seen is None or held is None:
        return "unavailable"
    if all(row["task_id"] in accepted for row in rows) and held[len(seen):] == events:
        return "applied"
    _put_back(runs_dir, [before[task] for task in accepted if task in before])
    return "refused"


def _settle_merges(
    runs_dir: Path, initiative_id: str, items: list[dict], sources: Mapping[str, tuple[Path, str]]
) -> tuple[list[dict], list[dict], list[str]]:
    """Edge. `(merged_items, merges, states)` once no merge is refused. Each round sends every merge to the store; when
    any is refused, the round's applied merges are put back, the refused tickets are held out of the merge, and the
    rest is merged again. `merge_same_phase` rewrites a `needs` from a dropped id to its survivor across all groups,
    so a refused group's rewrite must not reach the groups that still apply."""
    before = _rows_before(runs_dir, initiative_id, sources)
    held_out: set[str] = set()
    while True:
        merged, merges = route.merge_same_phase([{**i, "state": "held"} if i["id"] in held_out else i for i in items])
        head = _initiative_head(runs_dir, initiative_id)
        states = [
            _store_merge(runs_dir, initiative_id, merged, merge, [sources[m][0] for m in merge["members"]], before) for merge in merges
        ]
        refused = [merge for merge, state in zip(merges, states) if state == "refused"]
        if not refused:
            states_of = {i["id"]: i["state"] for i in items}
            return [{**m, "state": states_of[m["id"]]} if m["id"] in held_out else m for m in merged], merges, states
        applied = [merge for merge, state in zip(merges, states) if state == "applied"]
        _put_back(runs_dir, [before[m] for merge in applied for m in merge["members"] if m in before])
        if applied and head is not None:
            _put_back(runs_dir, [head])
        for merge in refused:
            held_out.update(merge["members"])
            print(f"refused: {' + '.join(merge['members'])} -> {merge['into']}")


_MERGE_RUNS_DIR: contextvars.ContextVar[Path | None] = contextvars.ContextVar("merge_runs_dir", default=None)


@contextlib.contextmanager
def _merging_into(runs_dir: Path | None):
    """Names the runs directory (the profile's `workspace_dir/runs`) that `_merge_initiative_tickets` calls in the
    block send their merges to. A context, not an argument, because `route launch` calls the merge with the
    initiative directory alone. Outside any block, or given None, the merge has no store and writes files only."""
    token = _MERGE_RUNS_DIR.set(runs_dir)
    try:
        yield
    finally:
        _MERGE_RUNS_DIR.reset(token)


def _merge_initiative_tickets(initiative_dir: Path) -> list[dict]:
    """`route.merge_same_phase` on the tickets under `initiative_dir`, each merge sent to the store before its files.
    The store is `runs/` under the profile's `workspace_dir` (`_merging_into`), and only for that workspace's own
    `work/<initiative>`. A refused merge writes no file; the rest are merged without it (`_settle_merges`).
    Without a store the files are written as before, each after the unrecorded warning `write_filed_item` prints.
    The returned list drops the `dropped` entries; each item carries `file` for `route.launch_blockers`.
    """
    root = initiative_dir.resolve()  # absolute, so the export baselines below are not skipped
    paths = sorted(root.glob("*/*.md"))
    texts = [path.read_text(encoding="utf-8") for path in paths]
    items = [
        {
            "task": fields.get("id", path.stem),
            "id": fields.get("id", path.stem),
            "title": fields.get("title", fields.get("id", path.stem)),
            "state": fields.get("state", "todo"),
            "phase": fields.get("phase", path.parent.name),
            "surfaces": fields.get("surfaces", []),
            "body": body,
            "needs": fields.get("needs", []),
            "file": f"{path.parent.name}/{path.name}",
        }
        for path, text in zip(paths, texts)
        for fields, body in [route.parse_frontmatter(text)]
    ]
    sources = {item["id"]: (path, text) for path, text, item in zip(paths, texts, items)}
    runs_dir = _MERGE_RUNS_DIR.get()
    mode = _store_mode(runs_dir, root)
    if mode == "up":
        merged_items, merges, states = _settle_merges(runs_dir, root.name, items, sources)
    else:
        merged_items, merges = route.merge_same_phase(items)
        states = [mode] * len(merges)
        if merges and mode == "outside":
            print(f"routing: {root} is not an initiative of the workspace {runs_dir.parent}; merging its files without the store")
    merged_of = {item["id"]: item for item in merged_items}
    for merge, state in zip(merges, states):
        written = [(sources[m][0], route.merged_ticket_text(sources[m][1], merged_of[m])) for m in merge["members"]]
        for path, _ in written:
            if notice := _unfiled_notice(state, path, root.name):
                print(notice)
        for path, text in written:
            path.write_text(text, encoding="utf-8")
        if runs_dir is not None:
            queue_export.record_written_paths(runs_dir.parent, [path for path, _ in written])
        for line in route.merge_report_lines([merge]):
            print(line)
    return [item for item in merged_items if item.get("state") != "dropped"]


_SURFACE_RULES = ("surface_missing", "surface_exists", "surface_wrong_repo")


def _lint_checkout(repo: str | None, items: list[dict]) -> tuple[frozenset[str], dict[str, frozenset]] | None:
    """`(paths, other_repo_tops)` for `lint_items`; None when `repo` is unset or lists no files."""
    paths = checkout.read_checkout(repo) if repo else frozenset()
    if not paths:
        return None
    home = os.path.basename(repo.rstrip("/"))
    surface_tops = {s.split("/", 1)[0] for item in items for s in item.get("surfaces", []) if "/" in s}
    foreign = surface_tops - route.top_dirs(paths)
    # Stands down like cross_repo: an unregistered repo, or no surface outside the repo's own top directories,
    # reads no other repo. Wrong belief this guards: that every repo with Coxswain siblings is a Coxswain repo.
    # Other repos are found only as flat siblings of `repo`; in a worktree with no siblings each listing is
    # empty, other_repo_tops is {}, and surface_wrong_repo stays silent rather than misfiring.
    names = [name for name in route._REPO_ROOTS if name != home] if home in route._REPO_ROOTS and foreign else []
    tops = {name: route.top_dirs(checkout.read_checkout(Path(repo).parent / name)) for name in names}
    return paths, {name: found for name, found in tops.items() if found}


def _route_lint_dir(a: argparse.Namespace) -> Path | None:
    """Edge. The lint argument as an existing directory, else as an initiative id under the profile's
    `workspace_dir/work`. None, after printing an error that names both forms, when neither exists."""
    given = Path(a.initiative_dir)
    if given.is_dir():
        return given
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        profile = {}
    workspace = profile.get("workspace_dir", "")
    by_id = Path(workspace).expanduser() / "work" / a.initiative_dir if workspace else None
    if by_id is not None and by_id.is_dir():
        return by_id
    print(
        f"routing: {a.initiative_dir} is neither an initiative directory nor a known initiative id "
        "(pass a directory path, or an initiative id found under work/ in the profile's workspace_dir)"
    )
    return None


def _route_lint(a: argparse.Namespace) -> int:
    """work-shape.md §3: `cox route lint <initiative>`, before dispatch.
    `--repo` falls back to the initiative's own `repo:` frontmatter; only
    when neither names one does the reach rule stand down, and that is
    said once here rather than left for `lint_items` to flag every path.
    A skipped reach check with any surface listed fails: skipping is never a pass.
    """
    initiative_dir = _route_lint_dir(a)
    if initiative_dir is None:
        return 2
    runs_dir, _ = _runs_dir_for_land(argparse.Namespace(runs_dir=getattr(a, "runs_dir", None), profile=getattr(a, "profile", None)))
    with _merging_into(runs_dir):
        items = _merge_initiative_tickets(initiative_dir)
    repo = a.repo
    if not repo:
        init_path = initiative_dir / "initiative.md"
        if init_path.exists():
            init_fields, _ = route.parse_frontmatter(init_path.read_text(encoding="utf-8"))
            repo = init_fields.get("repo")
    skipped_with_surfaces = not repo and any(item.get("surfaces") for item in items)
    if not repo:
        print(f"routing: no repo found for {a.initiative_dir}; reach check skipped")
    home = os.path.expanduser("~")
    # `lint_items` compares the recorded form against `home`; the checkout listing needs a real directory.
    checkout_repo = repo_path.expand(repo, home) if repo else repo
    problems = route.lint_items(
        items, repo, ("pytest", "git status", "git diff"), checkout=_lint_checkout(checkout_repo, items), home=home,
    )
    for problem in problems:
        print(f"{problem.task} {problem.rule}: {problem.detail} -> {problem.fix}")
    if skipped_with_surfaces:
        print("routing: surfaces are listed but the reach check was skipped; name a repo with repo: or --repo")
    failing = ("reach", "coupling", "phase_needs", "home_path", *_SURFACE_RULES)
    return 2 if skipped_with_surfaces or any(p.rule in failing for p in problems) else 0


def _route_groups(a: argparse.Namespace) -> int:
    """work-shape.md §5: `cox route groups` prints the newest
    `plans/intake-groups/<date>.md` file under the profile's workspace, or
    `no groups filed` when none exist.
    """
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    groups_dir = Path(workspace).expanduser() / "plans" / "intake-groups"
    names = sorted(p.name for p in groups_dir.glob("*.md")) if groups_dir.is_dir() else []
    latest = route.latest_groups_file(names)
    if latest is None:
        print("no groups filed")
        return 0
    print(str(groups_dir / latest))
    return 0


def _route_launch_sweep(a: argparse.Namespace) -> int:
    """work-shape.md §1: no harness graph exists yet, so only `--dry-run` runs."""
    argv = route.build_sweep_argv(a.idea, a.initiative_id, a.label)
    if a.dry_run:
        print(f"dry-run: {' '.join(argv)}")
        return 0
    print("routing: sweep has no harness graph to launch yet; use --dry-run")
    return 2


def _core_freshness(provider_profile: str | None) -> list[str] | None:
    """The model freshness lines for `provider_profile`; None for a core too old for the check (the doctor prints one skip row)."""
    if not provider_profile:
        return ["model freshness not checked: AGENT_TOOLS_PROVIDER_PROFILE is unset"]
    try:
        from core.model_freshness import freshness_warnings
    except ImportError:
        return None
    # load_profile reads the file with Path.read_text, so it takes a Path, never the raw str.
    profile_path = Path(provider_profile).expanduser()
    try:
        import core
        from core.catalog import load_catalog
        from core.profile import load_profile
        if not profile_path.is_file():
            return [f"model freshness not checked: provider profile not found at {profile_path}"]
        if not os.access(profile_path, os.R_OK):
            return [f"model freshness not checked: provider profile unreadable at {profile_path}"]
        catalog_path = Path(core.__file__).resolve().parent.parent / "providers" / "catalog.yaml"
        catalog = load_catalog(catalog_path)
        profile = catalog if isinstance(catalog, list) else load_profile(profile_path, catalog_path)
        errors = catalog if isinstance(catalog, list) else profile if isinstance(profile, list) else None
        if errors is None:
            return list(freshness_warnings(profile, catalog))
        return ["model freshness not checked: " + str(errors[0] if errors else "unknown error")]
    except Exception:
        return [f"model freshness not checked: could not evaluate the provider profile at {profile_path}"]


_HARNESS_PLUGIN_GROUPS = ("coxswain.system_one", "coxswain.runners")
_PLUGINS_PROBE_SCRIPT = (
    "import json, sys\n"
    "from importlib.metadata import entry_points\n"
    "print(json.dumps({g: sorted(ep.name for ep in entry_points(group=g)) for g in sys.argv[1:]}))\n"
)


def plugins_probe_argv(python: str) -> list[str]:
    """The harness interpreter listing its own plugin entry points: `cox` may run from another venv (a uv tool)."""
    return [python, "-c", _PLUGINS_PROBE_SCRIPT, *_HARNESS_PLUGIN_GROUPS]


def parse_plugins_probe(returncode: int, stdout: str) -> dict[str, list[str]] | None:
    """`{group: [names]}` for every probed group, else None, which the doctor row reads as "not checked", never as empty."""
    if returncode != 0:
        return None
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != set(_HARNESS_PLUGIN_GROUPS):
        return None
    if not all(isinstance(names, list) and all(isinstance(n, str) for n in names) for names in parsed.values()):
        return None
    return parsed


def _harness_plugins(python: str) -> dict[str, list[str]] | None:
    """Edge. The plugin entry points the harness interpreter sees; None when it cannot say."""
    try:
        done = subprocess.run(plugins_probe_argv(python), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_plugins_probe(done.returncode, done.stdout)


def _overlay_mapping(text: str) -> tuple[dict | None, str | None]:
    """Overlay YAML text as (mapping, None), or (None, one problem). An empty file is `{}`."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"project overlay is not valid YAML: {str(exc).strip().splitlines()[0]}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"project overlay must be a mapping, got {type(data).__name__}"
    return data, None


def _core_probe(cartridges_dir: str, team: str, roots: list, overlay_text: str | None, provider_profile: str | None) -> dict:
    """Edge. The cartridge API, imported in-process (one venv: core sits beside agent_tools), as one JSON-shaped dict.

    Each step's failure lands in its own key and never raises; a failed import or skill index stops the walk there."""
    out: dict = {"import": None, "load": None, "indexed": {}, "resolved": None, "skill_index": {}, "layers": None}
    try:
        from core.cartridge import load
        from core.skills import index_from_roots
    except Exception as exc:
        out["import"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        index = index_from_roots(roots)
        out["indexed"] = {root: len(index_from_roots([root])) for root in roots}
        out["skill_index"] = {name: [str(p) for p in paths] for name, paths in index.items()}
    except Exception as exc:
        # An empty mapping would read downstream as "no roots configured"; a zero
        # per root fails the skills row honestly, and the cartridge row names why.
        out["indexed"] = dict.fromkeys(roots, 0)
        out["load"] = f"skill index failed: {type(exc).__name__}: {exc}"
        return out
    try:
        out["resolved"] = load(team, cartridges_dir, skill_index=index)
    except Exception as exc:
        out["load"] = f"{type(exc).__name__}: {exc}"
    try:
        from core.cartridge import layers
        out["layers"] = [[label, resolved] for label, resolved in layers(team, cartridges_dir, skill_index=index)]
    except Exception as exc:
        # Never an empty list: a chain one layer short must be visibly unknown,
        # not silently read as "nothing set anything".
        out["layers"] = None
        out["layers_error"] = f"{type(exc).__name__}: {exc}"
    if overlay_text is None:
        out["overlay_errors"] = None
    else:
        try:
            from core.cartridge import overlay_errors
            mapping, problem = _overlay_mapping(overlay_text)
            out["overlay_errors"] = [problem] if problem else overlay_errors(mapping)
        except Exception as exc:
            out["overlay_errors"] = [f"{type(exc).__name__}: {exc}"]
    out["freshness"] = _core_freshness(provider_profile)
    return out


def _run_core_probe(cartridges_dir: str, team: str, skills_roots: list, raw_roots: list | None = None,
                     overlay_text: str | None = None, provider_profile: str | None = None) -> dict:
    """The core probe's facts (spec: setup doctor). Any failure the probe did not
    catch itself is folded into `core_import`, never raised."""
    try:
        parsed = _core_probe(cartridges_dir, team, list(skills_roots), overlay_text, provider_profile)
    except Exception as exc:
        return {"core_import": f"{type(exc).__name__}: {exc}"}
    indexed = parsed.get("indexed", {}) or {}
    if raw_roots and len(raw_roots) == len(skills_roots):
        # The probe saw expanded paths; the core keys its skills row by the
        # profile's own strings, so map the counts back by position.
        indexed = {raw: indexed.get(exp, 0) for raw, exp in zip(raw_roots, skills_roots)}
    facts = {"core_import": parsed.get("import"), "cartridge_load": parsed.get("load"),
             "skill_roots_indexed": indexed, "resolved": parsed.get("resolved"),
             "skill_index": parsed.get("skill_index", {})}
    if "overlay_errors" in parsed:
        facts["overlay_errors"] = parsed["overlay_errors"]
    if "freshness" in parsed:
        facts["freshness_lines"] = parsed["freshness"]
    # Exactly one of `provenance`/`provenance_error` is set below, on every
    # path: a fact dict carrying neither would read as "nothing to report"
    # rather than "the walk never happened", which is the silent-mislabel
    # failure this probe exists to rule out.
    parsed_layers = parsed.get("layers")
    if parsed_layers is None:
        facts["provenance_error"] = parsed.get("layers_error") or "the probe exited before the layer walk"
    elif not parsed_layers:
        facts["provenance_error"] = "the loader returned no layers"
    else:
        try:
            facts["provenance"] = provenance.attribute([(label, resolved) for label, resolved in parsed_layers])
        except Exception as exc:
            facts["provenance_error"] = f"{type(exc).__name__}: {exc}"
    return facts


def _provider_command(text) -> str | None:
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("command:"):
            # A flat YAML scalar may carry a trailing comment (`command: claude  # ...`);
            # the command is the first token, never the comment.
            value = stripped.partition(":")[2].split("#", 1)[0].strip().strip("\"'")
            return value.split()[0] if value else None
    return None


def _provider_facts(provider_profile_path: str) -> dict:
    command = _provider_command(_read_text_or_none(Path(provider_profile_path)))
    if command is None:
        return {"provider_command": None, "provider_on_path": False}
    facts = {"provider_command": command, "provider_on_path": shutil.which(command) is not None}
    if facts["provider_on_path"]:
        try:
            proc = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=20)
            lines = (proc.stdout or proc.stderr or "").strip().splitlines()
            facts["provider_version"] = lines[0] if lines else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    return facts


_NO_STORE_YET = "no store yet (it is created by the first run)"
_NO_RUNS_TABLE = "the store answers but has no runs table yet (it is created by the first run)"
_STORE_CONNECT_TIMEOUT_S = 5


def _sqlite_store_file(url: str) -> Path:
    """The file `connect_readonly_url` opens for a non-Postgres URL."""
    return Path(url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url)


def _with_connect_timeout(url: str) -> str:
    """The Postgres URL with a connect timeout added unless it sets one."""
    parts = urllib.parse.urlsplit(url)
    if "connect_timeout" in urllib.parse.parse_qs(parts.query):
        return url
    query = "&".join(q for q in (parts.query, f"connect_timeout={_STORE_CONNECT_TIMEOUT_S}") if q)
    return urllib.parse.urlunsplit(parts._replace(query=query))


def _store_error(exc: Exception) -> str:
    """A fixed message for a known cause, else the exception class; driver text can quote the URL, so it is never passed on."""
    if str(exc) == store_dialect.MISSING_PSYCOPG:
        return store_dialect.MISSING_PSYCOPG
    if type(exc).__name__ == "UndefinedTable" or (isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc)):
        return _NO_RUNS_TABLE
    return f"the store did not answer ({type(exc).__name__})"


def _store_facts(provider_profile: str, workspace_dir: str) -> dict:
    """Edge. Whether the run store answers a read; the URL never leaves this function."""
    url = store_url.resolve_store_url(provider_profile, Path(workspace_dir) / "runs")
    kind = store_url.describe_store(url).removeprefix("store: ")
    try:
        postgres = store_dialect.is_postgres(url)
    except ValueError:  # urlsplit rejects e.g. an unclosed `[`; the message would quote the URL
        return {"kind": kind, "reachable": False, "runs": None, "fresh": False, "error": "storage_url is not a valid URL"}
    # `other` is a bare path with no scheme, which `connect_readonly_url` opens as a SQLite file.
    if not postgres and kind not in ("sqlite", "other"):
        return {"kind": kind, "reachable": False, "runs": None, "fresh": False,
                "error": f"unsupported store scheme {kind}: storage_url must be a postgresql:// or sqlite:/// URL"}
    if not postgres and not _sqlite_store_file(url).exists():
        return {"kind": kind, "reachable": False, "error": _NO_STORE_YET, "runs": None, "fresh": True}
    try:
        with contextlib.closing(store_dialect.connect_readonly_url(_with_connect_timeout(url) if postgres else url)) as conn:
            runs = int(conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"])
    except Exception as exc:  # any driver failure is the answer this row reports
        error = _store_error(exc)
        return {"kind": kind, "reachable": False, "error": error, "runs": None, "fresh": error == _NO_RUNS_TABLE}
    return {"kind": kind, "reachable": True, "error": None, "runs": runs, "fresh": False}


def _workspace_facts(workspace_dir: str) -> dict:
    ws = Path(workspace_dir).expanduser()
    return {"workspace_dirs": {name: (ws / name).exists() for name in ("work", "runs", "intake")}}


def _git_and_forge_facts(profile: dict) -> dict:
    """`git_version`, the profile's forge name, whether that forge is installed,
    and `gh_auth` only for the github forge. Gathered with or without a profile."""
    try:
        done = subprocess.run(["git", "--version"], capture_output=True, text=True)
        git_version = done.stdout.strip() if done.returncode == 0 else None
    except OSError:
        git_version = None
    name = forge.forge_name(profile)
    facts = {"git_version": git_version or None, "forge": name, "forge_found": forge.forge_for(name) is not None}
    if name in ("github", "auto"):
        try:
            facts["gh_auth"] = route_sync_gh.api_login_ok(subprocess.run)
        except OSError:
            facts["gh_auth"] = False
    return facts


_TOOLS_PLUGIN_GROUPS = ("coxswain.sources", "coxswain.forges", "coxswain.trackers")


def _tools_plugin_facts() -> dict:
    """Entry-point names per group, read in tools' own environment. Listing only."""
    return {g: sorted(ep.name for ep in importlib.metadata.entry_points(group=g)) for g in _TOOLS_PLUGIN_GROUPS}


def _doctor_profile(text: str | None) -> dict | None:
    """The doctor's one parse rule: a missing or unparseable profile is None, never a refusal."""
    try:
        return route.parse_profile(text) if text is not None else None
    except route.ProfileError:
        return None


def _chair_unit_facts() -> dict:
    """Edge: the installed chair unit's text, its effective PATH across unit and drop-ins, and per absolute PATH entry the names of the doctor's unit tools executable there."""
    unit_file = chair_service.unit_path(Path.home())
    text = _read_text_or_none(unit_file)
    facts: dict = {"chair_unit_text": text}
    if text is not None:
        observed = chair_unit.read_chair_path(unit_file.parent)
        facts["chair_unit_path"] = observed
        entries = [e for e in doctor.unit_path_entries(observed.path) if e.startswith("/")] if observed else []
        facts["chair_unit_tools"] = {
            e: [n for n in doctor._UNIT_TOOLS if (Path(e) / n).is_file() and os.access(Path(e) / n, os.X_OK)]
            for e in entries
        }
    return facts


def _gather_doctor_facts(profile_path: Path, repo: Path) -> dict:
    """Gathers exactly the Facts keys `doctor.checks` reads; never refuses on
    a missing or unparseable profile, since reporting that is the doctor's
    job (unlike `_resolve_profile_or_refuse`, which is for `file`/`launch`)."""
    text = _read_text_or_none(profile_path)
    facts: dict = {"profile_path": str(profile_path), "profile_text": text}
    profile = _doctor_profile(text)
    facts.update(_git_and_forge_facts(profile or {}))
    facts["plugins_tools"] = _tools_plugin_facts()
    facts.update(_chair_unit_facts())
    facts["claude_token"] = setup_token.token_fact(claude_token.token_path(str(Path.home())))
    if profile is None:
        return facts
    singles = [profile.get(k, "") for k in ("cartridges_dir", "provider_profile", "harness_dir", "workspace_dir", "umbrella_dir")]
    roots = list(profile.get("skills_roots") or [])
    # Every profile path is expanded ONCE, here; `paths_exist` keeps the raw
    # strings as keys (doctor.py's contract) but tests the expanded path.
    expand = lambda p: str(Path(p).expanduser()) if p else ""  # noqa: E731
    facts["paths_exist"] = {p: Path(expand(p)).exists() for p in (*singles, *roots) if p}
    harness_dir = expand(profile.get("harness_dir", ""))
    python = install_root.harness_python(harness_dir) if harness_dir else None
    facts["harness_python"] = str(python) if python else None
    facts["harness_python_exists"] = bool(python) and python.exists()
    if facts["harness_python_exists"]:
        plugins = _harness_plugins(str(python))
        if plugins is not None:
            facts["plugins_harness"] = plugins
        overlay_text = _read_text_or_none(repo / ".agent" / "cartridge.yaml")
        facts.update(_run_core_probe(expand(profile.get("cartridges_dir", "")), profile.get("team", ""),
                                     [expand(r) for r in roots], raw_roots=roots, overlay_text=overlay_text,
                                     provider_profile=expand(profile.get("provider_profile", "")) or None))
    if profile.get("provider_profile"):
        facts.update(_provider_facts(expand(profile["provider_profile"])))
        if profile.get("workspace_dir"):
            facts["store"] = _store_facts(expand(profile["provider_profile"]), expand(profile["workspace_dir"]))
    if profile.get("workspace_dir"):
        facts.update(_workspace_facts(expand(profile["workspace_dir"])))
    facts["schema_versions"] = _schema_versions(harness_dir, expand(profile.get("provider_profile", "")),
                                                 [expand(r) for r in roots])
    return facts


def _parquet_traces_line(profile: dict | None) -> str | None:
    """Edge: the Parquet readability line for this profile's traces root, or None when
    the profile names no workspace_dir and so no root can be resolved."""
    if not profile or not profile.get("workspace_dir"):
        return None
    provider = profile.get("provider_profile")
    provider_data = store_url.read_provider_profile(provider) if provider else {}
    root = store_url.profile_traces_root(provider_data, Path(profile["workspace_dir"]).expanduser() / "runs")
    check = run_store.parquet_readable(root, run_store.harness_python(profile))
    endpoint = root.object_store.get("endpoint") if root.object_store else None
    return doctor.parquet_line(check.readable, check.reason, str(endpoint) if endpoint else None)


def _profile_lane_hosts(text: str) -> tuple[lane_hosts.LaneHost, ...] | lane_hosts.LaneHostError:
    """Reads `lane_hosts` straight from the YAML text; `route.parse_profile` only skips the block."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return lane_hosts.LaneHostError(f"profile is not readable as YAML: {exc}")
    return lane_hosts.parse_lane_hosts(data if isinstance(data, Mapping) else {})


def _run_ssh(argv: list[str]) -> tuple[int, str]:
    """Edge: the one real subprocess call for `setup doctor --host`; stderr joins the output rows."""
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return 127, f"{argv[0]}: {exc}"
    return done.returncode, done.stdout


def _setup_doctor_host(a: argparse.Namespace, run) -> int:
    """Prints the named lane host's doctor rows under a header; non-zero when the host is unknown or its doctor fails."""
    path = _profile_path(a)
    text = _read_text_or_none(path)
    if text is None:
        print(f"cannot read profile: {path}")
        return 1
    try:
        workspace = route.parse_profile(text).get("workspace_dir", "")
    except route.ProfileError:
        workspace = ""
    table = run_store.hosts(Path(workspace).expanduser() / "runs") if workspace else []
    hosts = _lane_host_candidates(text, table)
    if isinstance(hosts, lane_hosts.LaneHostError):
        print(hosts.message)
        return 1
    host = lane_hosts.find_lane_host(hosts, a.host)
    if host is None:
        print(f"unknown lane host: {a.host}")
        print(f"configured: {', '.join(h.name for h in hosts) or 'none'}")
        return 1
    code, rows = remote_doctor.doctor_on_host(host, run)
    print(f"doctor on {host.name} ({host.ssh})")
    print("\n".join(rows))
    return code


def _doctor_store_rows(profile: dict | None) -> list[dict]:
    """Edge. The chosen store's rows; the probes run only when store.mode is shared and the provider profile names a store."""
    profile = profile or {}
    mode = (profile.get("store") or {}).get("mode", "files")
    provider = profile.get("provider_profile")
    data = store_url.read_provider_profile(provider) if provider else {}
    runs_dir = Path(profile.get("workspace_dir") or "").expanduser() / "runs"
    traces = store_url.profile_traces_root(data, runs_dir).url
    probed = mode == "shared" and all(k in data for k in ("storage_url", "traces_url", "object_store"))
    return doctor.store_rows(mode, store_url.profile_store_url(data, runs_dir), traces, _probe_store(data, {}) if probed else [])


_DOCTOR_HOME_CHECKS = (
    "home root", "marketplace registered", "marketplace location", "old layout", "home paths", "home recorded",
    "cartridge links", "lane host paths",
)


def _doctor_legacy_path(profile: Mapping, locs) -> str | None:
    """Edge. A component checkout still a real directory in the installs folder, else the pre-home checkout folder
    when it is not the installs folder and holds a real coxswain-graphs directory."""
    left = [p for p in (Path(locs.installs) / name for name in setup_migrate.RETIRED) if p.is_dir() and not p.is_symlink()]
    if left:
        return str(left[0])
    old = _migrate_old_root(profile, locs)
    graphs = Path(old) / setup_migrate.GRAPHS if old else None
    return old if old and old != locs.installs and graphs.is_dir() and not graphs.is_symlink() else None


def _doctor_home_rows(profile: dict | None) -> list[dict]:
    """Edge. The coxswain home rows; every one is skipped without a profile. All reads happen here, `doctor.home_rows` judges."""
    if profile is None:
        return [doctor._skip(check) for check in _DOCTOR_HOME_CHECKS]
    locs = home_paths.resolve_here()
    if isinstance(locs, home_paths.Refused):
        return [{"check": "home root", "ok": False, "detail": locs.reason}]
    listing = _marketplace_listing()
    source = (listing or {}).get(setup_install.MARKETPLACE)
    workspace = profile.get("workspace_dir")
    in_project = bool(source and workspace and Path(source).parent == Path(workspace).expanduser().parent)
    record = _read_text_or_none(Path(locs.record))
    rows = doctor.home_rows(
        locs,
        listing,
        Path(locs.home).is_dir(),
        os.access(locs.home, os.W_OK),
        _doctor_legacy_path(profile, locs),
        in_project,
        _migrate_cartridge_links(profile),
        _doctor_install_sources(_migrate_install_bins(), setup_migrate.read_venv_source(setup_install.layout(locs.installs).checkout)),
        home_record=(record or "").strip() or None,
        skills_roots=profile.get("skills_roots") or (),
        provider_profile=profile.get("provider_profile") or "",
        harness_dir=profile.get("harness_dir") or "",
        user_home=str(Path.home()),
        workspace_dir=workspace or "",
    )
    return [*rows, doctor.lane_host_paths_row(_migrate_observe_hosts(locs))]


def _doctor_dash_build_rows(profile: dict | None) -> list[dict]:
    """Edge. The `dash build` row; none without a profile or a resolvable home. A development install is one whose
    venv is editable from the checkout, the source the `install bins` row reads."""
    if profile is None:
        return []
    locs = home_paths.resolve_here()
    if isinstance(locs, home_paths.Refused):
        return []
    checkout = setup_install.layout(locs.installs).checkout
    dev_install = setup_migrate.read_venv_source(checkout) == checkout
    dash_present = Path(install_exec.clone_dir(locs.installs, "dash")).is_dir()
    return [doctor.dash_build_row(dev_install, dash_present, shutil.which("cargo") is not None)]


def _doctor_steward_rows(profile: dict | None) -> list[dict]:
    """Edge. The steward row; skipped without a profile. A missing or malformed `chair` reads as off."""
    if profile is None:
        return [doctor._skip("steward")]
    chair = profile.get("chair")
    raw = chair.get("steward") if isinstance(chair, Mapping) else None
    return [doctor.steward_row(chair_facts.steward_asked_for(raw))]


def _setup_doctor(a: argparse.Namespace) -> int:
    if getattr(a, "host", None):
        return _setup_doctor_host(a, _run_ssh)
    repo = Path(a.repo).expanduser() if a.repo else Path.cwd()
    facts = _gather_doctor_facts(_profile_path(a), repo)
    rows = doctor.checks(facts)
    if "freshness_lines" in facts:
        lines = facts["freshness_lines"]
        fn = (lambda *_: lines) if isinstance(lines, list) else None
        rows = [*rows, *doctor.freshness_rows(None, None, None, fn)]
    profile = _doctor_profile(facts["profile_text"])
    rows = [
        *rows,
        *_doctor_home_rows(profile),
        *_doctor_store_rows(profile),
        *_doctor_dash_build_rows(profile),
        *_doctor_steward_rows(profile),
    ]
    rc = doctor.exit_code(rows)
    # Informational: outside rows, so outside rc. Reuses the text the facts already read.
    parquet = _parquet_traces_line(profile)
    if a.json:
        print(json.dumps({"rows": rows, "ok": rc == 0, "parquet_traces": parquet}, indent=2))
    else:
        print(doctor.render(rows))
        if parquet:
            print(parquet)
    return rc


def _marketplace_paths(parsed: object) -> dict[str, str] | None:
    """Maps `claude plugin marketplace list --json` entries to name -> path; "" when an entry has no path."""
    if not isinstance(parsed, list):
        return None
    return {
        e["name"]: str(e.get("path") or "") for e in parsed if isinstance(e, dict) and isinstance(e.get("name"), str)
    }


def _marketplace_listing() -> dict[str, str] | None:
    """Edge: the installed marketplaces, or None when claude is absent, fails, or prints non-JSON."""
    if shutil.which("claude") is None:
        return None
    try:
        r = subprocess.run(["claude", "plugin", "marketplace", "list", "--json"], capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    try:
        return _marketplace_paths(json.loads(r.stdout))
    except ValueError:
        return None


def _marketplace_plugin_ids(parsed: object) -> list[str]:
    """Maps `claude plugin list --json` entries to their ids ending @coxswain-cartridges, in order."""
    if not isinstance(parsed, list):
        return []
    ids = (e.get("id") for e in parsed if isinstance(e, dict))
    return [i for i in ids if isinstance(i, str) and i.endswith("@coxswain-cartridges")]


def _marketplace_plugins() -> list[str]:
    """Edge: the plugin ids installed from coxswain-cartridges; [] when claude is absent, fails, or prints non-JSON."""
    if shutil.which("claude") is None:
        return []
    try:
        r = subprocess.run(["claude", "plugin", "list", "--json"], capture_output=True, text=True)
    except OSError:
        return []
    if r.returncode != 0:
        return []
    try:
        return _marketplace_plugin_ids(json.loads(r.stdout))
    except ValueError:
        return []


def _install_home_refusal(home: str, record: str | None, named: bool) -> str | None:
    """The refusal text when a home named by flag or environment differs from a non-empty record."""
    recorded = (record or "").strip()
    if named and recorded and recorded != home:
        return f"the home record names {recorded}, not {home}; run cox setup migrate --home {home}"
    return None


def _install_facts(a: argparse.Namespace) -> dict:
    """Gathers exactly what `install_plan` needs and nothing it decides:
    PATH lookups, the coxswain tool's receipt extras, the profile's existence, and the
    config/settings paths expanded once, here, and the platform, home, cox path and
    whether the chair service file exists, and the default garage.env and its `export` lines.
    A `--root` install passes no home and no env file. Without `--root` the checkouts,
    profile and workspace come from the resolved coxswain home; a refusal
    raises ValueError for the caller to print."""
    config_dir = str(Path("~/.config").expanduser())
    claude_settings_path = str(Path("~/.claude/settings.json").expanduser())
    if a.root:
        if not a.workspace:
            raise ValueError("--workspace is required with --root")
        root, locs, workspace_exists, registered = a.root, None, False, None
        profile_file = setup_install.profile_path(config_dir)
    else:
        locs = home_paths.resolve_here(a.home)
        if isinstance(locs, home_paths.Refused):
            raise ValueError(locs.reason)
        refusal = _install_home_refusal(
            locs.home, _read_text_or_none(Path(locs.record)), bool(a.home or os.environ.get("COXSWAIN_HOME")))
        if refusal:
            raise ValueError(refusal)
        root, profile_file = locs.installs, locs.profile
        workspace_exists = Path(locs.workspaces, "main").exists()
        listing = _marketplace_listing() if a.plugins and shutil.which("claude") is not None else None
        registered = (listing or {}).get(setup_install.MARKETPLACE)
    tool_dir = setup_receipts.default_tool_dir(os.environ, str(Path.home()))
    paths = setup_install.layout(root)
    # A `--root` install is a sandbox: it never writes into the real user's service folders.
    service_home = "" if a.root else str(Path.home())
    service_file = chair_service.service_file(sys.platform, Path(service_home)) if service_home else None
    chair_env_file = None if a.root else _chair_default_environment_file()
    chair_env_text = _read_text_or_none(Path(chair_env_file)) if chair_env_file else None
    return {
        "tool_extras": setup_receipts.read_tool_extras(tool_dir, setup_install.TOOL),
        "root": root,
        "team": a.team,
        "workspace": a.workspace or "",
        "locations": locs,
        "registered_marketplace": registered,
        "workspace_exists": workspace_exists,
        "provider_profile": a.provider_profile or paths.provider_profile,
        "skills_root": a.skills_root or paths.skills_root,
        "uv_on_path": shutil.which("uv") is not None,
        "claude_on_path": shutil.which("claude") is not None,
        "profile_exists": Path(profile_file).exists(),
        "force_profile": a.force_profile,
        "plugins": a.plugins,
        "hook": a.hook,
        "config_dir": config_dir,
        "claude_settings_path": claude_settings_path,
        "assume": a.assume,
        "window_ceiling_usd": a.window_ceiling_usd,
        "weekly_ceiling_usd": a.weekly_ceiling_usd,
        "platform": sys.platform,
        "home": service_home,
        "cox_path": shutil.which("cox"),
        "chair_service_exists": service_file is not None and service_file.exists(),
        "chair_env_file": chair_env_file,
        "chair_env_exports": tuple(chair_service.export_lines(chair_env_text or "")),
    }


def _install_run(step: dict) -> str | None:
    argv = " ".join(step["argv"])
    try:
        proc = subprocess.run(step["argv"], cwd=step.get("cwd"), capture_output=True, text=True)
    except OSError as exc:
        if step.get("warn_only"):
            print(f"warn: {argv}: {exc}")
            return None
        print(f"FAILED: {argv}: {exc}")
        return f"{argv}: {exc}"
    if proc.returncode == 0:
        print(f"run: {argv}")
        return None
    tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
    if step.get("warn_only"):
        print(f"warn: {argv} exited {proc.returncode}: {tail}")
        return None
    print(f"FAILED: {argv} exited {proc.returncode}")
    print(tail)
    return f"{argv} exited {proc.returncode}"


def _execute_step(step: dict) -> str | None:
    op = step["op"]
    if op == "run":
        return _install_run(step)
    if op == "write":
        path = Path(step["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(step["text"], encoding="utf-8")
        print(f"write {path}")
        return None
    if op == "skip":
        print(f"skip {step['what']} — {step['why']}")
        return None
    if op == "mkdir":
        Path(step["path"]).mkdir(parents=True, exist_ok=True)
        print(f"mkdir {step['path']}")
        return None
    if op == "hook":
        path = Path(step["path"])
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError as exc:
            print(f"FAILED: {path} is not valid JSON: {exc}")
            return f"{path} is not valid JSON: {exc}"
        new_settings, changed = setup_install.hook_settings(existing)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_settings, indent=2) + "\n", encoding="utf-8")
            print(f"hook added to {path}")
        else:
            print(f"hook already present in {path}")
        return None
    if op == "symlink":
        return _migrate_link(step)
    if op == "print":
        print(step["text"])
        return None
    return f"unknown step op: {op!r}"


def _setup_install(a: argparse.Namespace) -> int:
    try:
        steps = setup_install.install_plan(**_install_facts(a))
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2
    if a.dry_run:
        print(setup_install.render_plan(steps))
        return 0
    _load_lazy_modules()  # a `uv tool install --force` step replaces this cox tool's own environment mid-run
    for step in steps:
        failure = _execute_step(step)
        if failure is not None:
            print(f"setup install: stopped: {failure}")
            return 1
    return 0


def _migrate_profile(locs) -> dict:
    """Edge. The profile as a mapping; empty when it is absent or unreadable."""
    text = _read_text_or_none(Path(locs.profile))
    try:
        data = yaml.safe_load(text) if text is not None else None
    except yaml.YAMLError:
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _migrate_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "dir"
    return "file" if path.exists() else "absent"


def _migrate_worktrees(checkout: Path) -> list[str] | None:
    """Edge. Every path `git worktree list` names for the checkout, main one first; None when git cannot list."""
    try:
        done = subprocess.run(["git", "-C", str(checkout), "worktree", "list", "--porcelain"],
                              capture_output=True, text=True)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    return [line[len("worktree "):] for line in done.stdout.splitlines() if line.startswith("worktree ")]


def _migrate_cartridge_links(profile: Mapping) -> dict | None:
    """Edge. Lexical target of each base and local link under cartridges_dir, at that folder and one team deeper."""
    folder = profile.get("cartridges_dir")
    if not isinstance(folder, str):
        return None
    root = Path(folder).expanduser()
    found = {*root.glob("*/base"), *root.glob("*/local"), root / "base", root / "local"}
    return {str(p): os.readlink(p) for p in sorted(found) if p.is_symlink()}


def _migrate_checkouts() -> tuple[str, ...]:
    """Pure. The checkout names the migration looks at: the required ones, then the optional umbrella once."""
    return tuple(dict.fromkeys((*setup_migrate.CHECKOUTS, setup_migrate.UMBRELLA)))


def _migrate_old_root(profile: Mapping, locs) -> str | None:
    """Pure. The folder holding the old checkouts: the parent of the first checkout-named path component in a
    profile setting that lies outside the installs folder, else the installs folder when every setting is already
    there. None when no setting names a checkout, so nothing is observed and the planner refuses."""
    raw = [item for key in setup_migrate.SETTING_KEYS for item in (profile.get(key) if isinstance(profile.get(key), list) else [profile.get(key)])]
    roots = [
        str(p.parent)
        for value in raw
        if isinstance(value, str)
        for p in [Path(value).expanduser(), *Path(value).expanduser().parents]
        if p.name in setup_migrate.CHECKOUTS and p.name != setup_migrate.UMBRELLA  # "coxswain" also names config and state folders
    ]
    outside = [r for r in roots if r != locs.installs]
    return (outside or roots or [None])[0]


def _migrate_tool_dir() -> str | None:
    """Edge. The output of `uv tool dir`; None when uv is missing, exits non-zero or prints nothing."""
    try:
        done = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True)
    except OSError:
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def _migrate_install_bins() -> dict | None:
    """Edge. `setup_migrate.observe_install_bins` for this machine; None when the uv tool dir is unknown."""
    tool_dir = _migrate_tool_dir()
    return None if tool_dir is None else setup_migrate.observe_install_bins(tool_dir)


def _doctor_install_sources(bins: Mapping | None, venv_source: str | None) -> dict[str, str | None] | None:
    """Pure. The two editable sources `doctor.home_rows` compares with the checkout: the venv's `coxswain` and the
    `coxswain` uv tool's receipt. None when the uv tool dir is unknown."""
    if bins is None:
        return None
    return {"venv": venv_source, "tool receipt": (bins.get(setup_install.TOOL) or {}).get("editable")}


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
        return subprocess.run(argv, capture_output=True, text=True).returncode == 0
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
        proc = subprocess.run(argv, capture_output=True, text=True)
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


def _migrate_observe_hosts(locs) -> list:
    """Edge. One ObservedHost per beaten active table row, then per profile lane host the table does not list."""
    profile = _migrate_profile(locs)
    declared = _profile_lane_hosts(_read_text_or_none(Path(locs.profile)) or "")
    declared = () if isinstance(declared, lane_hosts.LaneHostError) else declared
    workspace = profile.get("workspace_dir")
    runs = Path(workspace).expanduser() / "runs" if isinstance(workspace, str) else None
    rows = run_store.hosts(runs) if runs is not None and runs.is_dir() else []
    by_name = {str(r["name"]): r for r in rows}
    from_profile = {h.name: h.workspace_dir for h in declared}
    tabled = host_cmd.host_rows_to_lane_hosts(rows, (), socket.gethostname())
    seen = [
        setup_migrate_hosts.ObservedHost(
            h.name, h.ssh, h.workspace_dir,
            {Path(p).name: p for p in host_cmd.recorded_repos(by_name[h.name])},
            from_profile.get(h.name),
            "beat" if by_name[h.name].get("beat_at") is not None else "fallback",
        )
        for h in tabled
    ]
    listed = {h.name for h in tabled}
    return [*seen, *(setup_migrate_hosts.ObservedHost(h.name, h.ssh, h.workspace_dir, {}, h.workspace_dir, "fallback")
                     for h in declared if h.name not in listed)]


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


def _migrate_link(step: dict) -> str | None:
    path = Path(step["path"])
    kind = _migrate_kind(path)
    if kind in ("dir", "file"):
        return f"{path} is a real {kind}, so it was not replaced by a link"
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        path.unlink()
    path.symlink_to(step["target"])
    print(f"link {path} -> {step['target']}")
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
        done = subprocess.run(step["argv"], capture_output=True, text=True)
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
        return subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True, check=False)
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
            done = subprocess.run(step.argv, capture_output=True, text=True)
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
    """Edge. One repo lease per checkout path as (LeaseOutcome, epoch or None); an exception is an error outcome."""
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
    # The release spawns the interpreter the profile names now; the one cached at acquire time may have been moved.
    run_store._harness_python_for.cache_clear()
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


# (attribute, prompt label, flag). The first seven are connection details; the last three name environment variables.
_STORE_DETAILS = (
    ("host", "Postgres host", "--host"),
    ("port", "Postgres port", "--port"),
    ("database", "Postgres database", "--database"),
    ("user", "Postgres user", "--user"),
    ("bucket", "S3 bucket", "--bucket"),
    ("endpoint", "S3 endpoint (http:// or https://)", "--endpoint"),
    ("region", "S3 region", "--region"),
)
# (SharedConnection field, attribute, prompt label, flag)
_STORE_CREDENTIALS = (
    ("pg_password", "password_env", "Environment variable holding the Postgres password", "--password-env"),
    ("s3_access_key_id", "access_key_env", "Environment variable holding the S3 access key id", "--access-key-env"),
    ("s3_secret_access_key", "secret_key_env", "Environment variable holding the S3 secret key", "--secret-key-env"),
)


def _git_exclude_file(directory: Path) -> Path | None:
    """Edge. The enclosing repository's info/exclude; None outside a repository or without git."""
    try:
        done = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=directory,
                              capture_output=True, text=True, check=False)
    except OSError:
        return None
    found = done.stdout.strip()
    return directory / found if done.returncode == 0 and found else None


@contextlib.contextmanager
def _env_overlay(overlay: Mapping[str, str]):
    """Edge. `store_probe` reads the S3 keys from os.environ under the names in the profile; the user's own variables
    may differ, so the resolved values sit under those names for the probes only and the environment is restored."""
    saved = {name: os.environ.get(name) for name in overlay}
    os.environ.update(overlay)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _probe_store(profile: Mapping, overlay: Mapping[str, str]) -> list:
    """Edge, the default probes: one read-only Postgres connection and one bucket head. Never starts either service."""
    with _env_overlay(overlay):
        return [store_probe.probe_postgres(profile["storage_url"]),
                store_probe.probe_object_store(profile["object_store"], profile["traces_url"])]


def _asked_or_given(value, label: str, ask: Callable[[str], str], interactive: bool):
    if value is not None:
        return value
    return ask(f"{label}: ").strip() if interactive else ""


def _store_choice(a: argparse.Namespace, ask: Callable[[str], str], interactive: bool) -> str | None:
    """Edge. The flag beats the prompt; no terminal means the default, with no prompt. None when the value is invalid."""
    default = setup_store.default_store_choice()
    if a.store is not None:
        return setup_store.parse_store_choice(a.store)
    if not interactive:
        return default
    answer = ask(f"Store for run records: {default} or shared [{default}]: ")
    return setup_store.parse_store_choice(answer) if answer.strip() else default


def _store_files(a: argparse.Namespace, settings_path: Path) -> int:
    if a.dry_run:
        print("dry run: would record store.mode files; nothing was written")
        return 0
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        setup_store_io.apply_files_choice(settings_path)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("store: files; store.mode recorded and no shared profile written")
    return 0


def _shared_connection(a: argparse.Namespace, env: Mapping[str, str], ask: Callable[[str], str],
                       interactive: bool) -> tuple[setup_store.SharedConnection | None, int]:
    """Edge. The validated SharedConnection and 0, or None and the exit code after printing why."""
    fields = (*_STORE_DETAILS, *((attr, label, flag) for _f, attr, label, flag in _STORE_CREDENTIALS))
    values = {attr: _asked_or_given(getattr(a, attr), label, ask, interactive) for attr, label, _flag in fields}
    missing = [flag for attr, _label, flag in fields if values[attr] in (None, "")]
    if missing:
        print(f"error: the shared store needs {', '.join(missing)}; pass the flags or run in a terminal",
              file=sys.stderr)
        return None, 2
    creds = setup_store_io.credentials_from_env(
        env, {field: values[attr] for field, attr, _label, _flag in _STORE_CREDENTIALS})
    if creds["unset"]:
        for var in creds["unset"]:
            print(f"error: environment variable {var} is not set or is empty", file=sys.stderr)
        return None, 2
    try:
        port = int(values["port"])
    except ValueError:
        print("error: the Postgres port must be a whole number", file=sys.stderr)
        return None, 2
    conn = setup_store.SharedConnection(
        pg_host=values["host"], pg_port=port, pg_database=values["database"], pg_user=values["user"],
        pg_password=creds["values"]["pg_password"], s3_endpoint=values["endpoint"], s3_bucket=values["bucket"],
        s3_region=values["region"], s3_access_key_id=creds["values"]["s3_access_key_id"],
        s3_secret_access_key=creds["values"]["s3_secret_access_key"],
    )
    problems = setup_store.validate_shared(conn)
    if problems:
        print("\n".join(f"error: {sentence}" for sentence in problems), file=sys.stderr)
        return None, 2
    return conn, 0


def _store_shared(a: argparse.Namespace, settings_path: Path, env: Mapping[str, str], ask: Callable[[str], str],
                  interactive: bool, probes: Callable | None, exclude_file: Path | None) -> int:
    conn, code = _shared_connection(a, env, ask, interactive)
    if conn is None:
        return code
    if a.dry_run:
        print(f"dry run: {setup_store.redacted_summary(conn)}; nothing was written")
        return 0
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        print(setup_store_io.apply_shared_choice(
            conn, settings_path, exclude_file or _git_exclude_file(settings_path.parent)))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not write the shared store files ({type(exc).__name__})", file=sys.stderr)
        return 1
    overlay = {setup_store.ACCESS_KEY_ENV: conn.s3_access_key_id, setup_store.SECRET_KEY_ENV: conn.s3_secret_access_key}
    results = (probes or _probe_store)(setup_store.build_shared_profile(conn), overlay)
    print(store_probe.summarize(results))
    if all(r.ok for r in results):
        return 0
    print("warning: the shared store did not answer from this machine; the profile was kept", file=sys.stderr)
    return 0 if a.allow_unreachable else 1


def _setup_store(a: argparse.Namespace, *, env: Mapping[str, str] | None = None, ask: Callable[[str], str] = input,
                 interactive: bool | None = None, probes: Callable | None = None,
                 exclude_file: Path | None = None) -> int:
    """Edge. Choose the store once (flag, else prompt in a terminal, else files) and hand off to the pure cores.
    Starts no service: the user's Postgres and object store are only probed."""
    tty = (sys.stdin.isatty() and sys.stdout.isatty()) if interactive is None else interactive
    choice = _store_choice(a, ask, tty)
    if choice is None:
        print("error: the store must be files or shared", file=sys.stderr)
        return 2
    settings_path = _profile_path(a)
    if choice == "files":
        return _store_files(a, settings_path)
    return _store_shared(a, settings_path, os.environ if env is None else env, ask, tty, probes, exclude_file)


def _setup_token(a: argparse.Namespace) -> int:
    """Edge. Store the long-lived Claude token: getpass for the prompt, a prompt only when both ends are terminals."""
    return setup_token.store(
        a.token, a.from_file,
        path=claude_token.token_path(str(Path.home())),
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        ask_secret=setup_token.prompt_secret,
        read_text=setup_token.read_file_text,
    )


def _setup_token_status(a: argparse.Namespace) -> int:
    """Edge. Report the stored token's state; `--probe` spends one small claude request to prove it works."""
    return setup_token.status(
        a.probe,
        path=claude_token.token_path(str(Path.home())),
        read_token=claude_token.read_token,
        run=setup_token.run_command,
    )


def _store_mode_and_runs(settings_path: Path) -> tuple[str, Path | None]:
    """Edge. The store.mode setting (files when unset) and the runs directory under the profile's workspace_dir."""
    profile = _doctor_profile(_read_text_or_none(settings_path)) or {}
    workspace = profile.get("workspace_dir")
    return (profile.get("store") or {}).get("mode", "files"), (Path(workspace).expanduser() / "runs" if workspace else None)


def _switch_env(a: argparse.Namespace, env: Mapping[str, str]) -> dict[str, str]:
    """`env` with the S3 keys also under the names the profile's object_store block reads, when the flags name them."""
    named = {setup_store.ACCESS_KEY_ENV: getattr(a, "access_key_env", None),
             setup_store.SECRET_KEY_ENV: getattr(a, "secret_key_env", None)}
    return {**env, **{key: env[var] for key, var in named.items() if var and env.get(var)}}


def _setup_store_switch(a: argparse.Namespace, *, env: Mapping[str, str] | None = None, copy_store: Callable | None = None,
                        copy_traces: Callable | None = None, probes: Callable | None = None,
                        settings_path: Path | None = None) -> int:
    """Edge. Copy the run store and traces to the other backend, then record store.mode. Deletes nothing from the source."""
    env = os.environ if env is None else env
    settings_path = settings_path or _profile_path(a)
    mode, runs_dir = _store_mode_and_runs(settings_path)
    if mode == a.target:
        print(f"store is already {mode}; nothing to do")
        return 0
    if runs_dir is None:
        print("error: the profile names no workspace_dir, so there is no runs directory to switch", file=sys.stderr)
        return 2
    profile_file = setup_store_io.default_profile_path(settings_path)
    to_shared = a.target == "shared"
    conn = None
    source_file = profile_file
    if to_shared:
        conn, code = _shared_connection(a, env, input, sys.stdin.isatty() and sys.stdout.isatty())
        if conn is None:
            return code
        shared = setup_store.build_shared_profile(conn)
    else:
        provider = (_doctor_profile(_read_text_or_none(settings_path)) or {}).get("provider_profile")
        source_file = Path(provider).expanduser() if provider else profile_file
        shared = dict(store_url.read_provider_profile(source_file))
    local = (store_url.default_url(runs_dir), store_url.default_traces_root(runs_dir))
    remote = (shared.get("storage_url", ""), shared.get("traces_url", ""))
    source, target = (local, remote) if to_shared else (remote, local)
    try:
        steps = store_switch.plan_switch("to_shared" if to_shared else "to_files", source[0], target[0], source[1], target[1])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not steps:
        print(f"error: {source_file} does not name a postgresql store, so nothing was switched", file=sys.stderr)
        return 2
    print(store_switch.render_plan(steps))
    if a.dry_run:
        print("dry run: nothing was written or copied")
        return 0
    try:
        if to_shared:
            setup_store_io.write_shared_profile(profile_file, shared, _git_exclude_file(profile_file.parent))
        results = store_switch.run_plan(
            steps, copy_store or store_switch.default_copy_store,
            copy_traces or store_switch.traces_copier(shared.get("object_store"), _switch_env(a, env)))
        failed = next((r for r in results if not r["ok"]), None)
        if failed is not None:
            print(f"error: {failed['kind']} failed: {failed['error']}", file=sys.stderr)
            return 1
        setup_store_io.record_store_mode(a.target, settings_path)
        if not to_shared:
            profile_file.unlink(missing_ok=True)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not write the store files ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"store: {a.target}; store.mode recorded")
    if conn is not None:
        overlay = {setup_store.ACCESS_KEY_ENV: conn.s3_access_key_id, setup_store.SECRET_KEY_ENV: conn.s3_secret_access_key}
        print(store_probe.summarize((probes or _probe_store)(shared, overlay)))
    return 0


def _setup_fields(a: argparse.Namespace) -> tuple[str, str, str]:
    """(root, team, workspace) to prefill the TUI with, from the profile
    when it resolves, else empty strings — never a hard refusal here."""
    text = _read_text_or_none(_profile_path(a))
    if text is None:
        return "", "", ""
    try:
        profile = route.parse_profile(text)
    except route.ProfileError:
        return "", "", ""
    harness_dir = profile.get("harness_dir", "")
    root = str(Path(harness_dir).parent) if harness_dir else ""
    return root, profile.get("team", ""), profile.get("workspace_dir", "")


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
    describe = subprocess.run(["git", "-C", str(path), "describe", "--tags", "--exact-match"],
                               capture_output=True, text=True)
    tag = describe.stdout.strip() if describe.returncode == 0 else None
    status = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True)
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)

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
    program or working folder) is exit 127 with the reason, never a traceback."""
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
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
    """Move the one checkout to the target tag, sync its venv, reinstall cox and the crew links, then move any
    optional component the moved manifest pins, then run the doctor."""
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
    target = upgrade_plan.release_tag(a.to) if a.to else upgrade_exec.newest_release(_offered_tags(checkout))
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
    return 0


def _console_spend(runs_dir: Path, profile: dict, now: datetime.datetime) -> dict:
    """Edge. The 5-hour and weekly spend fractions and the weekly hard-stop fraction, built from the same three
    sources `_chair_run_deps`'s `facts_deps` builds (`usage_window.gather`, `usage_window.gather_weekly` with the
    store's own spend, and `_resolved_pacing_policy`), so the console's header never drifts from what the chair
    loop itself is pacing against. A fraction is None when its ceiling is absent or not positive."""
    weekly_reset = usage_window.parse_weekly_reset(profile.get("weekly_reset"))
    window = usage_window.gather(runs_dir, now, ceiling_usd=profile.get("window_ceiling_usd"))
    weekly = usage_window.gather_weekly(
        runs_dir, now, profile.get("weekly_ceiling_usd"),
        store_spend=lambda since: run_store.cost_since(runs_dir, since),
        reset=weekly_reset,
    )
    policy = _resolved_pacing_policy(runs_dir)

    def fraction(spent: float, ceiling: float | None) -> float | None:
        return None if not ceiling or ceiling <= 0 else spent / ceiling

    return {
        "five_hour": fraction(window.spent_usd, window.ceiling_usd),
        "weekly": fraction(weekly.spent_usd, weekly.ceiling_usd),
        "hard_stop": policy.weekly_hard_stop_fraction,
    }


def _tolerant_profile(a: argparse.Namespace) -> dict:
    """Edge. The parsed profile, resolved the way `_usage_assess` does: a missing or unreadable one just
    means no cartridge cap and no ceiling, so it is `{}`, not a raise."""
    text = _read_text_or_none(_profile_path(a))
    try:
        return route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        return {}


def _console(a: argparse.Namespace, launch=None) -> int:
    """Open towpath on a terminal; otherwise, and always with --once, print the one-shot text."""
    if not a.once and (launch or towpath_launch.launch)():
        return 0
    profile = _tolerant_profile(a)
    runs_dir = Path(a.runs_dir)
    now = datetime.datetime.now(datetime.UTC)
    tz = now.astimezone().tzinfo
    sections = console_screen.gather(
        runs_dir, Path(a.work_dir), now.isoformat(), socket.gethostname(), _chair_max_in_flight(runs_dir, profile),
        _console_spend(runs_dir, profile, now),
    )
    print("\n".join(console_screen.render(sections, -1, 120, now, tz)))
    return 0


_DASH_DETAIL_KINDS_NEEDING_AN_ID = ("run", "initiative", "machine")


def _dash_detail(kind: str, id_: str | None, runs_dir: Path, work_dir: Path, now: str) -> tuple[dict | None, int]:
    """Edge. Dispatches `--detail <kind> [id]` to the matching `dash_detail_*.build`, each called with the
    argument order that module actually takes -- the builders differ from one another. A kind that needs an
    id and got none, or a kind this command does not recognise, prints to stderr and returns exit 2 instead
    of raising."""
    if kind in _DASH_DETAIL_KINDS_NEEDING_AN_ID and not id_:
        print(f"cox dash --detail {kind}: an id is required", file=sys.stderr)
        return None, 2
    if kind == "run":
        return dash_detail_run.build(_resolved_run_id(runs_dir, id_), runs_dir, now), 0
    if kind == "initiative":
        return dash_detail_initiative.build(_initiative_token(runs_dir, id_), work_dir, runs_dir, now), 0
    if kind == "machine":
        return dash_detail_machine.build(id_, runs_dir, now), 0
    if kind == "spend":
        return dash_detail_spend.build(runs_dir, now), 0
    if kind == "health":
        return dash_detail_health.build(runs_dir, work_dir, now), 0
    if kind == "release":
        return dash_detail_release.build(now), 0
    print(f"cox dash --detail: unknown kind {kind!r}", file=sys.stderr)
    return None, 2


def _dash_dirs(runs_dir: str | None, work_dir: str | None, profile: dict | None) -> tuple[Path, Path]:
    """The feed's runs and work directories: a flag wins, then the profile's workspace, then `runs` and `.` under the
    current directory. towpath starts the feed wherever it was launched, so a launch outside the workspace still reads
    the chair's records."""
    workspace = Path(profile["workspace_dir"]).expanduser() if profile and profile.get("workspace_dir") else None
    runs = Path(runs_dir) if runs_dir else (workspace / "runs" if workspace else Path("runs"))
    work = Path(work_dir) if work_dir else (workspace if workspace else Path("."))
    return runs, work


_DASH_SHARE_DIR = ".dash-feed"


def _dash_feed_loop(runs_dir: Path, work_dir: Path, profile: dict | None, interval: float) -> int:
    """Edge. One process per runs dir owns the share lock and computes; the others print what it publishes.
    A follower tries the lock every pass, so it takes over the pass after the owner exits. Only this loop
    touches the share files; `--once` and `--detail` never do."""
    share_dir = runs_dir / _DASH_SHARE_DIR
    handle = dash_feed_share.try_own(share_dir)
    cache = dash_feed.EMPTY_CACHE
    last_stamp = None
    try:
        while True:
            handle = handle or dash_feed_share.try_own(share_dir)
            if handle is not None:
                now = datetime.datetime.now(datetime.UTC).isoformat()
                feed, cache = dash_feed.gather_feed_cached(runs_dir, work_dir, now, profile, cache)
                line = json.dumps(feed)
                dash_feed_share.publish(share_dir, line)
                print(line)
            else:
                line, last_stamp = dash_feed_share.read_if_changed(share_dir, last_stamp)
                if line is not None:
                    print(line)
            sys.stdout.flush()
            time.sleep(interval)
    finally:
        if handle is not None:
            dash_feed_share.release(handle)


def _dash(a: argparse.Namespace) -> int:
    """Edge. `--once` prints one `dash_feed.gather_feed` snapshot; `--feed` prints one per pass forever,
    `interval` seconds apart; `--detail <kind> [id]` prints one `dash_detail_*.build` result. `--feed` and
    `--once` together is a stderr message and exit 2, never a silent pick of one."""
    if a.feed and a.once:
        print("cox dash: --feed and --once are mutually exclusive", file=sys.stderr)
        return 2
    profile = _tolerant_profile(a)
    runs_dir, work_dir = _dash_dirs(a.runs_dir, a.work_dir, profile)
    if a.detail:
        kind, id_ = a.detail[0], (a.detail[1] if len(a.detail) > 1 else None)
        now = datetime.datetime.now(datetime.UTC).isoformat()
        result, rc = _dash_detail(kind, id_, runs_dir, work_dir, now)
        if rc == 0:
            print(json.dumps(result))
        return rc
    if a.once:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        print(json.dumps(dash_feed.gather_feed(runs_dir, work_dir, now, profile)))
        return 0
    if a.feed:
        return _dash_feed_loop(runs_dir, work_dir, profile, a.interval)
    print("cox dash: pass --feed, --once or --detail", file=sys.stderr)
    return 2


def _home(a: argparse.Namespace) -> int:
    # `--profile` skips towpath: towpath takes no profile
    if a.profile is None and towpath_launch.launch(which=shutil.which, execv=os.execv, stdout=sys.stdout, stdin=sys.stdin):
        return 0
    return _route_context(argparse.Namespace(profile=a.profile, json=False))


def _session(a: argparse.Namespace) -> int:
    """`cox session`: the coxswain Claude session, with the argv bare `cox` uses."""
    return _launcher(
        argparse.Namespace(launcher_profile=a.profile, no_plugin=a.session_no_plugin, print_argv=a.session_print_argv),
        a.session_extra,
    )


def _setup_tui(a: argparse.Namespace) -> int:
    """Bare `cox setup`: plain towpath, which takes no settings-open argument yet."""
    launched = towpath_launch.launch(which=shutil.which, execv=os.execv, stdout=sys.stdout, stdin=sys.stdin)
    if launched:
        return 0
    print("setup: needs a terminal and towpath; use setup doctor / setup install directly")
    return 2


_USAGE_ASSESS_EXIT = {"go": 0, "go_degraded": 0, "hold": 3, "stop": 4}

_GATE_LEVELS = ("ticket", "phase", "epic", "full")


def _resolved_gate_level(runs_dir: Path) -> str:
    """`level` from `<runs_dir>/policy.gate.json`; `ticket` when absent, unreadable or unknown, never `full`."""
    text = _read_text_or_none(runs_dir / "policy.gate.json")
    try:
        raw = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        raw = None
    level = raw.get("level") if isinstance(raw, dict) else None
    return level if level in _GATE_LEVELS else "ticket"


def _is_number(value: object) -> bool:
    """A finite int or float; a bool, NaN or Infinity is not one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number_or(raw: object, default: float) -> float:
    return float(raw) if _is_number(raw) else default


def _weekly_pace_lanes(
    raw: object, default: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """`raw` as pairs if it is a list of two-number lists, so `[]` is `()`: no throttling. Else `default` whole."""
    ok = isinstance(raw, list) and all(
        isinstance(pair, list) and len(pair) == 2 and all(_is_number(n) for n in pair) for pair in raw
    )
    return tuple((float(a), float(b)) for a, b in raw) if ok else default


def _resolved_pacing_policy(runs_dir: Path) -> pacing.Policy:
    """The cartridge's resolved `policy.pacing`, read from
    `<runs_dir>/policy.pacing.json` when a landing step has dropped one there;
    the unmeasured-and-uncapped `usage_window.DEFAULT_POLICY` when the file is
    absent, unreadable, or not a mapping — same skip-not-raise contract as
    `records.ceiling_for`'s own `<run_id>.ceiling.json` handling. A key the
    file omits falls back to the matching `DEFAULT_POLICY` field, not to a
    guess. A malformed `weekly_target_fraction` or `weekly_pace_lanes` also
    falls back; the ladder falls back whole. The file's `max_in_flight` is not a `Policy` field: `chair run`
    reads it as its lane cap after the team cartridge's (`_chair_max_in_flight`, 3 when absent)."""
    default = usage_window.DEFAULT_POLICY
    text = _read_text_or_none(runs_dir / "policy.pacing.json")
    if text is None:
        return default
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return default
    if not isinstance(raw, dict):
        return default
    return pacing.Policy(
        pace_thresholds=tuple(raw.get("pace_thresholds", default.pace_thresholds)),
        tier_ladder=tuple(raw.get("tier_ladder", default.tier_ladder)),
        effort_ladder=tuple(raw.get("effort_ladder", default.effort_ladder)),
        min_headroom_usd=float(raw.get("min_headroom_usd", default.min_headroom_usd)),
        hard_stop_fraction=float(raw.get("hard_stop_fraction", default.hard_stop_fraction)),
        weekly_hard_stop_fraction=float(raw.get("weekly_hard_stop_fraction", default.weekly_hard_stop_fraction)),
        weekly_target_fraction=_number_or(raw.get("weekly_target_fraction"), default.weekly_target_fraction),
        weekly_pace_lanes=_weekly_pace_lanes(raw.get("weekly_pace_lanes"), default.weekly_pace_lanes),
    )


def _chair_meter(
    local: usage_meter.Meter | None, now: datetime.datetime, runs_dir,
) -> tuple[usage_meter.Meter | None, str]:
    """A fresh local reading is 'meter' and beats any store row, including a newer-looking row from an older plan;
    else a store row under 15 minutes old is 'meter, store'; else the local reading (stale, or None) with 'est',
    so a stale local reading is still carried forward by `usable_percentage`."""
    if local is not None and usage_meter.fresh(local, now):
        return local, "meter"
    doc = run_store.latest_chair_meter(Path(runs_dir))
    chair = usage_meter.parse(doc) if doc else None
    # A naive `observed_at` cannot be compared with an aware `now`; `fresh` would raise, so it is not adopted.
    if (
        chair is not None and chair.observed_at.tzinfo is not None
        and usage_meter.fresh(chair, now, max_age=datetime.timedelta(minutes=15))
    ):
        return chair, "meter, store"
    return local, "est"


def _reconcile_ceilings(meter: usage_meter.Meter | None, source: str, runs_dir) -> None:
    """Edge. Run before any learned ceiling is read: a plan change drops the old plan's ceilings here. Only a fresh
    local reading (source 'meter') may reconcile, the same reading that may calibrate."""
    if meter is not None and source == "meter":
        usage_meter.reconcile_implied_ceilings(meter, Path(runs_dir))


def _tolerant_cost_since(runs_dir, since: str, until: str | None = None) -> float | None:
    """Edge. The store's spend since `since` (and before `until`, when given), or None on any store error, so a broken
    store falls back to the usage files instead of failing the gate and every surface that narrates it (route context
    still prints its docket)."""
    try:
        bounds = (since,) if until is None else (since, until)
        return run_store.cost_since(Path(runs_dir), *bounds)
    except (*run_store._db_errors(), ValueError, TypeError, RuntimeError):  # RuntimeError: psycopg not installed
        return None


def _budget_zone(budget_timezone: str | None, weekly_reset: str | None) -> str:
    """Pure. The month's zone: the budget's own, else the weekly reset's, else UTC."""
    reset = usage_window.parse_weekly_reset(weekly_reset)
    return budget_timezone or (reset.tz if reset is not None else "UTC")


def _month_for(
    runs_dir, now: datetime.datetime, monthly_budget_usd: float | None, budget_timezone: str | None,
    weekly_reset: str | None, min_elapsed_fraction: float,
):
    """Edge. The month's spend against its budget, or None with no budget set. The store's spend wins."""
    if monthly_budget_usd is None:
        return None
    return usage_window.gather_month(
        runs_dir, now, _budget_zone(budget_timezone, weekly_reset), monthly_budget_usd,
        cost_between=lambda since, until: _tolerant_cost_since(runs_dir, since, until),
        min_elapsed_fraction=min_elapsed_fraction,
    )


def _profile_month(runs_dir, profile: dict, now: datetime.datetime, min_elapsed_fraction: float):
    """Edge. `_month_for` read from a parsed profile's three keys, for every caller that holds a profile."""
    return _month_for(
        runs_dir, now, profile.get("monthly_budget_usd"), profile.get("budget_timezone"),
        profile.get("weekly_reset"), min_elapsed_fraction,
    )


def _usage_assessment(
    runs_dir, window_ceiling_usd: float | None = None, weekly_ceiling_usd: float | None = None,
    weekly_reset: str | None = None, monthly_budget_usd: float | None = None, budget_timezone: str | None = None,
) -> pacing.Assessment:
    """Computed once via the gatherer and shared by every surface that
    narrates it: `usage assess`, `route context`'s docket line, and `route
    launch`'s gate all call this so the same window yields the same reason.
    """
    now = datetime.datetime.now(datetime.UTC)
    reset = usage_window.parse_weekly_reset(weekly_reset)
    meter, source = _chair_meter(usage_meter.read(), now, runs_dir)
    _reconcile_ceilings(meter, source, runs_dir)
    policy = _resolved_pacing_policy(Path(runs_dir))
    month = _month_for(runs_dir, now, monthly_budget_usd, budget_timezone, weekly_reset, policy.min_elapsed_fraction)
    if (
        monthly_budget_usd is not None and meter is None
        and window_ceiling_usd is None and weekly_ceiling_usd is None
        and usage_meter.implied_ceiling("five_hour", now) is None and usage_meter.implied_ceiling("weekly", now) is None
    ):
        # The budget is the only limit: the five-hour and weekly windows are neither gathered, shown nor judged.
        return pacing.assess(usage_meter.unlimited_window(now), policy, now, month=month)
    usage = usage_window.read_usage(runs_dir, now, reset)
    # Calibration divides spend up to `now` by the reading's percentage, so only a fresh local reading may write it.
    calibrate = source == "meter"

    if meter is not None and meter.five_hour is None:
        # This plan has no five-hour limit: no estimate fallback, no ceiling read, nothing to calibrate.
        window = usage_meter.unlimited_window(now)
    else:
        five_hour_ceiling = usage_meter.implied_ceiling("five_hour", now)
        if five_hour_ceiling is None:
            five_hour_ceiling = window_ceiling_usd
        estimate_window = usage_window.gather(runs_dir, now, ceiling_usd=five_hour_ceiling, usage=usage)
        five_hour_usable = (
            usage_meter.usable_percentage(
                "five_hour", meter.five_hour, meter.observed_at, now, Path(runs_dir), five_hour_ceiling,
            )
            if meter is not None else None
        )
        meter_window = (
            usage_meter.as_window(
                dataclasses.replace(meter.five_hour, used_percentage=five_hour_usable), now, datetime.timedelta(hours=5),
            )
            if five_hour_usable is not None else None
        )
        window = usage_meter.prefer(meter_window, estimate_window)
        if calibrate:
            usage_meter.record_implied_ceiling("five_hour", meter.five_hour, Path(runs_dir), now)

    weekly_ceiling = usage_meter.implied_ceiling("weekly", now)
    if weekly_ceiling is None:
        weekly_ceiling = weekly_ceiling_usd
    # The store's spend, the same source the chair's facts and the console read: a lane host's own usage files
    # also hold runs fetched from or synced with other machines (one lane host read $4093 against a store $1349).
    estimate_weekly = usage_window.gather_weekly(
        runs_dir, now, weekly_ceiling, usage=usage, reset=reset,
        store_spend=lambda since: _tolerant_cost_since(runs_dir, since),
    )
    weekly_usable = (
        usage_meter.usable_percentage(
            "weekly", meter.seven_day, meter.observed_at, now, Path(runs_dir), weekly_ceiling,
        )
        if meter is not None else None
    )
    meter_weekly = (
        usage_meter.as_window(
            dataclasses.replace(meter.seven_day, used_percentage=weekly_usable), now, datetime.timedelta(days=7),
        )
        if weekly_usable is not None else None
    )
    weekly = usage_meter.prefer(meter_weekly, estimate_weekly)
    if calibrate:
        usage_meter.record_implied_ceiling("weekly", meter.seven_day, Path(runs_dir), now)

    return pacing.assess(window, policy, now, weekly=weekly, **({} if month is None else {"month": month}))


def _usage_assess(a: argparse.Namespace) -> int:
    # Resolved the same tolerant way `_route_context` resolves its profile:
    # a missing or unreadable profile just means no ceiling, not a raise.
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        profile = {}
    result = _usage_assessment(
        a.runs_dir, profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd"), profile.get("weekly_reset"),
        profile.get("monthly_budget_usd"), profile.get("budget_timezone"),
    )
    if a.json:
        d = dataclasses.asdict(result)
        d["hold_until"] = result.hold_until.isoformat() if result.hold_until else None
        print(json.dumps(d, indent=2))
    else:
        month_line = (
            f" [month to date ${result.month_spent_usd:.2f}, projected ${result.month_projected_usd:.2f}, "
            f"{result.month_verdict}]"
            if result.month_verdict is not None else ""
        )
        print(f"{result.verdict}: {result.reason}{month_line}")
    return _USAGE_ASSESS_EXIT[result.verdict]


def _bare_group(parser: argparse.ArgumentParser):
    """Default `fn` for a group parser whose subcommand is optional: an
    operator who runs the group alone sees that group's own help and a exit
    code of 2, not a traceback or silence."""
    def _fn(_a: argparse.Namespace) -> int:
        parser.print_help()
        return 2
    return _fn


RUNS_GROUP = commands.Group(
    name="runs", help="what a harness run recorded, and cleaning up after it",
    description="What a harness run recorded, and cleaning up after it.",
    epilog="examples:\n  cox runs fetch <run>\n  cox runs land <run> --repo PATH --apply\n  cox runs recover <run> <task> --repo PATH\n"
           "  cox runs usage <run> --json\n  cox runs detail <run> --json",
)
RUNS_COMMANDS = [
    commands.Command(
        "usage", "runs", "usage stats and cost for one run",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_usage", False, (),
    ),
    commands.Command(
        "trace", "runs", "the tool-call trace for one run",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--role",)), commands.Arg(("-v", "--verbose"), {"action": "store_true"})),
        "agent_tools.cli:_runs_trace", False, (),
    ),
    commands.Command(
        "clean", "runs", "delete a run's worktree and branches locally",
        (
            commands.Arg(("run_id",)), commands.Arg(("--repo",), {"required": True}), commands.Arg(("--worktree-root",), {"default": "~/worktrees"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--apply",), {"action": "store_true"}), commands.Arg(("--force",), {"action": "store_true", "help": "delete every branch regardless of whether its task is on main"}),
        ),
        "agent_tools.cli:_runs_clean", False, (),
    ),
    commands.Command(
        "land", "runs", "merge a run's branch into the target repo",
        (
            commands.Arg(("run_id",)), commands.Arg(("--repo",), {"required": True}), commands.Arg(("--task",)),
            commands.Arg(("--phase",), {"help": "land the whole phase off its own epic branch instead of one task"}), commands.Arg(("--label",)),
            commands.Arg(("--force",), {"action": "store_true", "help": "land despite a foreign live leader"}),
            commands.Arg(("--no-claim",), {"action": "store_true", "help": "land without taking an unheld or stale loop"}),
            commands.Arg(("--behind-limit",), {"type": int, "default": 20, "help": "warn when the task's base is more than this many commits behind the base branch"}),
            commands.Arg(("--base",), {"help": "branch to land onto; default: the repo's base: setting, then origin/HEAD, then main"}),
            commands.Arg(("--worktree-root",), {"default": "~/worktrees"}), commands.Arg(("--apply",), {"action": "store_true"}), commands.Arg(("--no-merge",), {"action": "store_true"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--gate",), {"choices": ["ticket", "phase", "epic", "full"], "help": "override the resolved gate level for this invocation"}),
            commands.Arg(("--wait-lock",), {"action": "store_true", "help": "when another land holds the repo lease, wait for it instead of refusing"}),
            commands.Arg(("--wait-lock-timeout",), {"type": int, "default": 600, "help": "seconds --wait-lock waits before refusing (default 600)"}),
        ),
        "agent_tools.cli:_runs_land", False, (),
    ),
    commands.Command(
        "land-batch", "runs", "land disjoint approved phases together as one pull request (the merge queue)",
        (
            commands.Arg(("--repo",), {"required": True}), commands.Arg(("--label",)),
            commands.Arg(("--source",), {"action": "append", "help": "initiative/phase/run to queue, repeatable; default: every ready phase"}),
            commands.Arg(("--batch-size",), {"type": int, "help": "sources per batch; default: the profile's merge_queue.batch_size"}),
            commands.Arg(("--wait-s",), {"type": float, "help": "seconds to wait for a full batch; default: the profile's merge_queue.wait_s"}),
            commands.Arg(("--force",), {"action": "store_true", "help": "land despite a foreign live leader"}),
            commands.Arg(("--no-claim",), {"action": "store_true", "help": "land without taking an unheld or stale loop"}),
            commands.Arg(("--base",), {"help": "branch to land onto; default: the repo's base: setting, then origin/HEAD, then main"}),
            commands.Arg(("--worktree-root",), {"default": "~/worktrees"}), commands.Arg(("--apply",), {"action": "store_true"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--gate",), {"choices": ["ticket", "phase", "epic", "full"], "help": "override the resolved gate level for this invocation"}),
        ),
        "agent_tools.cli:_runs_land_batch", False, (),
    ),
    commands.Command(
        "fetch", "runs", "pull an ended remote lane's run directory, log and branches to this machine",
        (
            commands.Arg(("--runs-dir",), {"help": "override: resolve the run's records here instead of the profile's workspace_dir"}),
            commands.Arg(("--profile",)),
            # Not an argparse group: Python 3.14 prints a required group holding a positional as `(--all | run_id)`
            # where 3.12 prints `[--all] [run_id]`, so the help could not read the same on both. _runs_fetch checks it.
            commands.Arg(("run_id",), {"nargs": "?"}),
            commands.Arg(("--all",), {"action": "store_true", "help": "fetch every remote run with no local tasks directory yet; a live run is skipped"}),
        ),
        "agent_tools.cli:_runs_fetch", False, (),
    ),
    commands.Command(
        "review", "runs", "review a contributor PR with the review graph and post the verdict",
        (commands.Arg(("--pr",), {"required": True, "help": "https://github.com/<owner>/<repo>/pull/<n>"}), commands.Arg(("--profile",))),
        "agent_tools.cli:_runs_review", False, (),
    ),
    commands.Command(
        "recover", "runs", "merge an approved task's commit into its phase branch after an escalated merge",
        (
            commands.Arg(("run_id",)), commands.Arg(("task_id",)), commands.Arg(("--repo",), {"required": True}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the merge that would be made and exit without touching anything"}),
        ),
        "agent_tools.cli:_runs_recover", False, (),
    ),
    commands.Command(
        "series", "runs", "per-run summary rows across a runs directory",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"}), commands.Arg(("--append",))),
        "agent_tools.cli:_runs_series", False, (),
    ),
    commands.Command(
        "wait", "runs", "block until a busy lane's run exits, then print its outcome lines",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--max-seconds",), {"type": float, "default": 3600}),
            commands.Arg(("--interval",), {"type": float, "default": 15}), commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_wait", False, (),
    ),
    commands.Command(
        "events", "runs", "poll a run's log for structured events",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--follow",), {"action": "store_true"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_events", False, (),
    ),
    commands.Command(
        "top", "runs", "live table of busy lanes (runs in flight); --once prints it and exits",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--interval",), {"type": float, "default": 3}), commands.Arg(("--once",), {"action": "store_true"})),
        "agent_tools.cli:_runs_top", False, (),
    ),
    commands.Command(
        "bar", "runs", "one Waybar JSON line: busy lanes, cost, class idle|running|attention",
        (commands.Arg(("--runs-dir",), {"default": "runs"}),),
        "agent_tools.cli:_runs_bar", False, (),
    ),
    commands.Command(
        "herdr", "runs", "report fleet state (idle|working|blocked) to the herdr pane this runs in; no-op outside herdr",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--interval",), {"type": float, "default": 10}), commands.Arg(("--once",), {"action": "store_true"})),
        "agent_tools.cli:_runs_herdr", False, (),
    ),
    commands.Command(
        "notify", "runs", "desktop notifications for exits, quarantines, budget stops and cost",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--once",), {"action": "store_true"}), commands.Arg(("--interval",), {"type": float, "default": 10}),
            commands.Arg(("--replay",), {"action": "store_true", "help": "emit history on first start; default is silent for existing runs when no state file is present"}),
        ),
        "agent_tools.cli:_runs_notify", False, (),
    ),
    commands.Command(
        "detail", "runs", "one run's timeline, objection and last tool calls",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_detail", False, (),
    ),
    commands.Command(
        "stranded", "runs", "every approved task record whose work item is not done, with its remedy",
        (
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_stranded", False, (),
    ),
    commands.Command(
        "cause", "runs", "record why one run's attempt at a task was quarantined",
        (
            commands.Arg(("run_id",)), commands.Arg(("task_id",)), commands.Arg(("cause",), {"choices": list(_STATS_CAUSES)}),
            commands.Arg(("--note",), {"help": "free text kept beside the cause"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_runs_cause", False, (),
    ),
    commands.Command(
        "stop", "runs", "stop a run's process, local or remote, and wait for it to exit",
        (
            commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--profile",)),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_stop", False, (),
    ),
    commands.Command(
        "pause", "runs", "mark a run paused in the store",
        (
            commands.Arg(("run_id",)), commands.Arg(("--reason",)), commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_pause", False, (),
    ),
    commands.Command(
        "resume", "runs", "mark a paused run resumed in the store",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_resume", False, (),
    ),
    commands.Command(
        "move", "runs", "stop a run at its node boundary and relaunch it on another host",
        (
            commands.Arg(("run_id",)), commands.Arg(("--to",), {"required": True}), commands.Arg(("--reason",)),
            commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--profile",)),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_move", False, (),
    ),
    commands.Command(
        "triage", "runs", "print a quarantined task's facts; reads the store and git only, makes no model call",
        (
            commands.Arg(("task_id",)),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--repo",), {"default": ".", "metavar": "PATH", "help": "the task's git repository (default: the current directory)"}),
        ),
        "agent_tools.cli:_runs_triage", False, (),
        defaults={"profile": None},
    ),
]

VERSIONS_GROUP = commands.Group(
    name="versions", help="component versions against the manifest", description="", epilog="",
    args=(
        commands.Arg(("--root",)),
        commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
        commands.Arg(("--manifest",)),
    ),
    fn="agent_tools.cli:_versions",
)

CONSOLE_GROUP = commands.Group(
    name="console", help="one screen of hosts, drafts, lanes and the chair; keys run cox commands after a y/n",
    description="", epilog="",
    args=(
        commands.Arg(("--runs-dir",), {"default": "runs"}),
        commands.Arg(("--work-dir",), {"default": "."}),
        commands.Arg(("--interval",), {"type": float, "default": 5}),
        commands.Arg(("--once",), {"action": "store_true"}),
        commands.Arg(("--profile",)),
    ),
    fn="agent_tools.cli:_console",
)

DASH_GROUP = commands.Group(
    name="dash",
    help="a versioned JSON snapshot of the workspace: --feed streams it, --once prints one, --detail drills into one kind",
    description="", epilog="",
    args=(
        commands.Arg(("--runs-dir",), {"default": None}),
        commands.Arg(("--work-dir",), {"default": None}),
        commands.Arg(("--feed",), {"action": "store_true"}),
        commands.Arg(("--once",), {"action": "store_true"}),
        commands.Arg(("--interval",), {"type": float, "default": 5}),
        commands.Arg(("--detail",), {"nargs": "+", "metavar": "KIND"}),
    ),
    fn="agent_tools.cli:_dash",
)

def _settings_get(a: argparse.Namespace) -> int:
    from agent_tools import cox_settings
    return cox_settings.run_get(_profile_path(a), a.json)


def _settings_set(a: argparse.Namespace) -> int:
    from agent_tools import cox_settings
    return cox_settings.run_set(_profile_path(a), a.scope, a.key, a.value, a.dry_run, a.commit)


SETTINGS_GROUP = commands.Group(
    name="settings", help="the cartridge and profile settings, with where each lives and whether git tracks it",
    description="The cartridge and profile settings, with where each lives and whether git tracks it.",
    epilog="examples:\n  cox settings get\n  cox settings get --json\n  cox settings set cartridge policy.dispatch.max_in_flight 4 --dry-run",
)
SETTINGS_COMMANDS = [
    commands.Command(
        "get", "settings", "print every setting, grouped by section; unset keys appear as built-in defaults, which set can override",
        (commands.Arg(("--json",), {"action": "store_true"}), commands.Arg(("--profile",))),
        "agent_tools.cli:_settings_get", False, (),
    ),
    commands.Command(
        "set", "settings", "change one setting: print the diff and write the file; --commit also commits a git-tracked cartridge",
        (
            commands.Arg(("scope",), {"choices": ("cartridge", "profile", "host")}),
            commands.Arg(("key",)),
            commands.Arg(("value",)),
            commands.Arg(("--dry-run",), {"action": "store_true"}),
            commands.Arg(("--commit",), {"action": "store_true", "help": "commit the changed cartridge file in git; profile files are never committed"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_settings_set", False, (),
    ),
]

INSTALL_GROUP = commands.Group(
    name="install", help="clone/update coxswain components against the manifest", description="", epilog="",
    args=(
        commands.Arg(("--root",), {"help": "the installs folder; defaults to the home's installs folder"}),
        commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
        commands.Arg(("--manifest",)),
        commands.Arg(("--provider",), {"default": "claude-code"}),
        commands.Arg(("--with",), {"action": "append", "default": None, "dest": "with_", "metavar": "FLAG"}),
        commands.Arg(("--team",)),
        commands.Arg(("--workspace",)),
        commands.Arg(("--edge",), {"action": "store_true"}),
        commands.Arg(("--dry-run",), {"action": "store_true"}),
    ),
    fn="agent_tools.cli:_install",
)

UPGRADE_GROUP = commands.Group(
    name="upgrade", help="fetch and check out newer pinned versions; refuses dirty checkouts", description="", epilog="",
    args=(
        commands.Arg(("--root",), {"help": "the installs folder; defaults to the home's installs folder"}),
        commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
        commands.Arg(("--manifest",)),
        commands.Arg(("--provider",), {"default": "claude-code"}),
        commands.Arg(("--with",), {"action": "append", "default": None, "dest": "with_", "metavar": "FLAG"}),
        commands.Arg(("--team",)),
        commands.Arg(("--workspace",)),
        commands.Arg(("--to",)),
        commands.Arg(("--dry-run",), {"action": "store_true"}),
    ),
    fn="agent_tools.cli:_upgrade",
)

HOME_GROUP = commands.Group(
    name="home", help="the live dashboard: runs, leader, backlog", description="", epilog="",
    args=(commands.Arg(("--profile",)),), fn="agent_tools.cli:_home",
)

SESSION_GROUP = commands.Group(
    name="session", help="open the coxswain Claude session",
    description="Open the coxswain Claude session, with the same argv bare `cox` uses.",
    epilog="examples:\n  cox session\n  cox session --print-argv\n  cox session -- -r resume-me",
    args=(
        commands.Arg(("--profile",), {"help": "the profile to launch claude against"}),
        # distinct dests: the top-level parser owns no_plugin and print_argv, and argparse copies a matched subparser's namespace over it
        commands.Arg(("--no-plugin",), {"action": "store_true", "dest": "session_no_plugin", "help": "start claude without --plugin-dir"}),
        commands.Arg(("--print-argv",), {"action": "store_true", "dest": "session_print_argv", "help": "print the claude argv and cwd instead of exec'ing it"}),
        commands.Arg(("session_extra",), {"nargs": "*", "metavar": "CLAUDE_ARG", "help": "after --: passed to claude verbatim, as bare cox does"}),
    ),
    fn="agent_tools.cli:_session",
)

USAGE_GROUP = commands.Group(
    name="usage", help="spend pacing against the ceiling for the current window",
    description="Spend pacing against the ceiling for the current window.",
    epilog="examples:\n  cox usage assess\n  cox usage assess --json --runs-dir runs",
)
USAGE_COMMANDS = [
    commands.Command(
        "assess", "usage", "the pacing verdict for the current spend window",
        (
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the window ceiling (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        "agent_tools.cli:_usage_assess", False, (),
    ),
]

STATS_GROUP = commands.Group(
    name="stats", help="load the run corpus into the stats store",
    description="Load the run corpus into the stats store.",
    epilog="examples:\n  cox stats ingest\n  cox stats ingest runs --db workspace/stats/stats.db"
           "\n  cox stats roles --json\n  cox stats tiers --since 2026-09-01\n  cox stats gates --since 2026-09-01\n  cox stats explain build --json\n  cox stats series --json"
           "\n  cox stats coverage --json\n  cox stats bounds --json\n  cox stats spend-mix --json"
           "\n  cox stats examples --role handoff --out examples.jsonl"
           "\n  cox stats system-one --role handoff --propose",
)
STATS_COMMANDS = [
    commands.Command(
        "ingest", "stats", "load usage, task, node and launch records into stats.db",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--work-store-root",), {"default": "work"}),
            commands.Arg(("--cartridges-repo",), {"default": None}),
        ),
        "agent_tools.cli:_stats_ingest", False, (),
    ),
    commands.Command(
        "roles", "stats", "landed rate, attempts-to-land and $/landed per role and model",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--runs-dir",), {"default": "runs", "help": "where lands are read from"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--cartridge-sha",), {"default": None, "help": "keep only runs on this cartridge_sha"}),
            commands.Arg(("--provider-profile",), {"default": None, "help": "keep only runs on this provider_profile"}),
        ),
        "agent_tools.cli:_stats_roles", False, (),
    ),
    commands.Command(
        "tiers", "stats", "per-role model summaries, a cost-aware pick and the spend it would save",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--min-samples",), {"type": int, "default": 20, "help": "tasks a model needs before it can be picked (default 20)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_tiers", False, (),
    ),
    commands.Command(
        "models", "stats", "per-role cost, tokens, turns, $/landed and first-try per model id",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--role",), {"default": None, "help": "keep only this role"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only calls whose run started on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_models", False, (),
    ),
    commands.Command(
        "causes", "stats", "quarantined attempts by cause and kind, with sample reasons",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_causes", False, (),
    ),
    commands.Command(
        "efficiency", "stats", "cost per turn, cost per completion, first-try rate, waste share, per day",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--days",), {"type": int, "default": 7, "help": "UTC days to report, today included (default 7)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_efficiency", False, (),
    ),
    commands.Command(
        "gates", "stats", "what each review, validation and plan gate costs and how often it changes the outcome",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--store",), {"action": "store_true", "help": "read task and call rows from the run store instead of stats.db"}),
            commands.Arg(("--runs-dir",), {"default": "runs", "help": "the run store's directory, with --store"}),
            commands.Arg(("--min-sample",), {"type": int, "default": _GATES_MIN_SAMPLE, "help": "decided tasks a role needs before it is judged, else 'not enough data' (default %(default)s)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_gates", False, (),
    ),
    commands.Command(
        "explain", "stats", "the failure-class breakdown behind one role",
        (
            commands.Arg(("role",)),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_explain", False, (),
    ),
    commands.Command(
        "series", "stats", "per-run summary rows read from the stats store",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--cartridge-sha",), {"default": None, "help": "keep only runs on this cartridge_sha"}),
            commands.Arg(("--provider-profile",), {"default": None, "help": "keep only runs on this provider_profile"}),
        ),
        "agent_tools.cli:_stats_series", False, (),
    ),
    commands.Command(
        "coverage", "stats", "known/total provenance rows for runs, calls and tasks",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_coverage", False, (),
    ),
    commands.Command(
        "bounds", "stats", "n/p50/p95/max and strict/moderate/liberal candidate ceilings per role and model",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--level",), {"choices": ("strict", "moderate", "liberal"), "default": None}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--write",), {"default": None, "help": "also write the JSON table to PATH inside the repo checkout"}),
        ),
        "agent_tools.cli:_stats_bounds", False, (),
    ),
    commands.Command(
        "spend-mix", "stats", "per-model token counts and cost share by class, plus the build-only split",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_spend_mix", False, (),
    ),
    commands.Command(
        "chair", "stats", "harness PRs and $, chair $ per PR, hand-finished lands and quarantine $ by cause",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only records and transcript lines dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--session",), {"action": "append", "default": None, "help": "a Claude session id to count as chair cost, beside those in chair.json (repeatable)"}),
            commands.Arg(("--work-store-root",), {"default": "work"}),
            commands.Arg(("--projects-dir",), {"default": "~/.claude/projects"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming cartridges_dir (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        "agent_tools.cli:_stats_chair", False, (),
    ),
    commands.Command(
        "examples", "stats", "the local system-one backend's training file (JSON lines) from task records",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--role",), {"action": "append", "required": True, "help": "handoff or review_charter; repeat for several"}),
            commands.Arg(("--out",), {"default": None, "help": "write the file to PATH (default: stdout)"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only records dated on or after DATE (YYYY-MM-DD)"}),
        ),
        "agent_tools.cli:_stats_examples", False, (),
    ),
    commands.Command(
        "flaky", "stats", "tests that needed a serial rerun three times in a week, from task records",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--days",), {"type": int, "default": 7, "help": "the window, today included"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_flaky", False, (),
    ),
    commands.Command(
        "system-one", "stats", "shadow-to-on graduation report per role, and a proposal for the maintainer to approve",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--role",), {"default": None, "help": "report this role only"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--propose",), {"action": "store_true", "help": "write a graduation proposal for each READY role; never edits a profile"}),
            commands.Arg(("--plans-dir",), {"default": "plans", "help": "where --propose writes system-one-graduation-<role>-<date>.md"}),
        ),
        "agent_tools.cli:_stats_system_one", False, (),
    ),
    commands.Command(
        "shadow-tier", "stats", "cheap-tier shadow trial per role: agreement, costly rate, cost saved, and the graduation bar; changes nothing",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--role",), {"default": None, "help": "report this role only"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_shadow_tier", False, (),
    ),
    commands.Command(
        "lanes", "stats", "per hour, the average and peak busy lanes and the idle minutes, from the store's run spans",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--hours",), {"type": int, "default": 24, "help": "how many clock hours back to report"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_lanes", False, (),
    ),
]


def _lake_provider(a: argparse.Namespace) -> tuple[Mapping, str | None]:
    """Edge. (provider profile, problem). A profile that is named but unusable is a problem, not a silent default lake.

    Only an unnamed profile falls back to files in the runs dir."""
    explicit = a.profile or os.environ.get("AGENT_TOOLS_PROFILE")
    text = _read_text_or_none(_profile_path(a))
    if text is None:
        return {}, f"no profile at {_profile_path(a)}" if explicit else None
    try:
        routing_profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return {}, f"profile unreadable: {exc}"
    provider_path = routing_profile.get("provider_profile")
    if not provider_path:
        return {}, None
    provider_text = _read_text_or_none(Path(provider_path).expanduser())
    if provider_text is None:
        return {}, f"provider profile not readable: {provider_path}"
    provider = store_url.read_provider_profile(provider_path)
    has_content = any(line.strip() and not line.lstrip().startswith("#") for line in provider_text.splitlines())
    if has_content and not provider:
        return {}, f"provider profile is not a YAML mapping: {provider_path}"
    return provider, None


def _lake_sync_report(results: Sequence, traces: tuple[int, int, int], dry_run: bool, new_tables: Sequence[str],
                      catalog: str, warehouse: str, traces_root: str) -> dict:
    """`results` are `SyncResult`s and `traces` is (found, registered, skipped). Every URL is redacted."""
    found, registered, skipped = traces
    return {
        "dry_run": dry_run,
        "new_tables": list(new_tables),
        "catalog": lake_config.redact(catalog),
        "warehouse": lake_config.redact(warehouse),
        "traces_root": lake_config.redact(traces_root),
        "tables": [
            {"table": r.table, "rows_found": r.rows_found, "rows_appended": r.rows_appended, "mark": r.new_mark}
            for r in results
        ],
        "traces": {"found": found, "registered": registered, "skipped": skipped, "would_register": found - skipped},
    }


def _lake_sync_lines(report: dict) -> list[str]:
    """A dry run says what a real run would do; its mark is the one it would set."""
    dry = report["dry_run"]
    created = [f"lake: would create {', '.join(report['new_tables'])}"] if report["new_tables"] else []
    tables = [
        f"{t['table']}: would append {t['rows_found']} (mark {t['mark'] or 'none'})" if dry
        else f"{t['table']}: {t['rows_appended']} appended (mark {t['mark'] or 'none'})"
        for t in report["tables"]
    ]
    traces = report["traces"]
    trace_line = (
        f"traces: would register {traces['would_register']}, {traces['skipped']} skipped" if dry
        else f"traces: {traces['registered']} registered, {traces['skipped']} skipped"
    )
    logs = report.get("logs")
    log_line = [] if logs is None or not logs["due"] else [
        f"logs: would archive {logs['due']} past {logs['retention_days']} days" if dry
        else f"logs: {logs['archived']} of {logs['due']} archived past {logs['retention_days']} days"
        + (f", {len(logs['failed'])} failed: {logs['failed'][0]}" if logs["failed"] else "")
    ]
    return [*created, *tables, trace_line, *log_line]


def _lake_catalog_missing(catalog_uri: str) -> bool:
    """True for a `sqlite:///` catalog whose file does not exist yet; any other catalog is assumed to exist."""
    return catalog_uri.startswith("sqlite:///") and not Path(catalog_uri.removeprefix("sqlite:///")).exists()


def _lake_real_run(config: lake_config.LakeConfig, store: str, root: str,
                   runs_dir: Path | None = None, retention: int | None = None) -> Callable[[], dict]:
    """Edge. Create any missing table; return the callable a lease guards that appends the new rows, registers
    traces, and archives ended runs' logs past `retention` days beside the traces before removing the local copies."""
    retention = run_logs.DEFAULT_RETENTION_DAYS if retention is None else retention
    catalog = lake_config.load_catalog(config)
    # Imported here: these modules import pyiceberg and pyarrow at module top, and cli.py must load without the extra.
    from agent_tools import lake_sync, lake_tables, lake_traces

    lake_tables.ensure_tables(catalog)

    def sync() -> dict:
        results = lake_sync.sync(catalog, store)
        traces = lake_traces.register_traces(catalog, root)
        report = _lake_sync_report(results, traces, False, (), config.catalog_uri, config.warehouse, root)
        if runs_dir is not None:
            report["logs"] = _run_logs_report(_archive_run_logs(config, root, runs_dir, retention, dry_run=False), retention)
        return report

    return sync


def _archive_run_logs(config: lake_config.LakeConfig, root: str, runs_dir: Path, retention: int,
                      dry_run: bool) -> run_logs.Outcome:
    """Edge. Ended runs' logs past retention, archived beside the traces root through the lake's own object-store settings."""
    from pyiceberg.io import load_file_io

    target = run_logs.logs_root(root)
    io = load_file_io(lake_config.iceberg_properties(config.object_store, os.environ), target)
    ended = {str(row.get("run_id") or "") for row in chair_read_exits.exit_rows(runs_dir) if chair_read_exits.row_exited(row)}
    now = datetime.datetime.now(datetime.UTC)
    return run_logs.archive_and_prune(runs_dir, target, io, ended, now, retention, dry_run=dry_run)


def _run_logs_report(outcome: run_logs.Outcome, retention: int) -> dict:
    return {"retention_days": retention, "due": outcome.due, "archived": outcome.archived, "failed": outcome.failed}


def _lake_open_readonly(config: lake_config.LakeConfig, name: str):
    """Edge. The configured catalog opened so that it creates nothing; None when it holds no catalog tables yet."""
    from pyiceberg.catalog.sql import IcebergTables, SqlCatalog
    from sqlalchemy import inspect as sql_inspect

    # Measured: a SqlCatalog creates its SQLite file on first connect, and its catalog tables on construction on any backend.
    if _lake_catalog_missing(config.catalog_uri):
        return None
    catalog = SqlCatalog(name, uri=config.catalog_uri, warehouse=config.warehouse, init_catalog_tables="false")
    return catalog if sql_inspect(catalog.engine).has_table(IcebergTables.__tablename__) else None


def _lake_dry_run(config: lake_config.LakeConfig, store: str, root: str) -> dict:
    """Edge. What a real run would do, read from the lake without creating or writing anything.

    A table the lake lacks reads as empty: no mark, no data files."""
    # An in-memory catalog: it raises LakeUnavailable when the extra is missing, names the catalog, and touches no file.
    probe = lake_config.load_catalog(lake_config.LakeConfig("sqlite:///:memory:", config.warehouse))
    from pyiceberg.exceptions import NoSuchTableError
    from pyiceberg.io import load_file_io

    from agent_tools import lake_sync, lake_tables, lake_traces

    real = _lake_open_readonly(config, probe.name)
    names = [f"{lake_tables.NAMESPACE}.{name}" for name in lake_tables.TABLES]
    new_tables = names if real is None else [n for n in names if not real.table_exists(n)]
    # The three attributes a dry run reads from a table; tests pin that lake_sync and lake_traces read no others.
    absent = types.SimpleNamespace(
        properties={}, io=load_file_io(dict(probe.properties), root),
        scan=lambda: types.SimpleNamespace(plan_files=lambda: []),
    )

    def load_table(identifier: str):
        try:
            return absent if real is None else real.load_table(identifier)
        except NoSuchTableError:
            return absent

    # The one catalog method a dry run calls; tests pin that lake_sync and lake_traces call no other.
    preview = types.SimpleNamespace(load_table=load_table)
    results = lake_sync.sync(preview, store, dry_run=True)
    traces = lake_traces.register_traces(preview, root, dry_run=True)
    return _lake_sync_report(results, traces, True, new_tables, config.catalog_uri, config.warehouse, root)


def _lake_refuse(a: argparse.Namespace, message: str) -> int:
    print(json.dumps({"error": message}) if a.json else message)
    return 2


def _lake_sync_done(a: argparse.Namespace, report: dict) -> int:
    print(json.dumps(report, indent=2) if a.json else "\n".join(_lake_sync_lines(report)))
    return 0


def _lake_sync_outcome(a: argparse.Namespace, outcome: lake_lease.SyncOutcome) -> int:
    """Report a real run's outcome once the lease decided whether it ran; a held or errored sync still exits 0."""
    if isinstance(outcome, lake_lease.Synced):
        return _lake_sync_done(a, outcome.value)
    if isinstance(outcome, lake_lease.SkippedHeld):
        message = f"lake: sync skipped (held by {outcome.holder})"
        print(json.dumps({"status": "held", "holder": outcome.holder}, indent=2) if a.json else message)
        return 0
    message = f"lake: sync skipped ({outcome.name})"
    print(json.dumps({"status": outcome.name}, indent=2) if a.json else message)
    return 0


def _log_retention_days(a: argparse.Namespace) -> int:
    """Edge. The routing profile's log_retention_days; the default when the profile is missing or unreadable."""
    try:
        return run_logs.retention_days(route.parse_profile(_read_text_or_none(_profile_path(a)) or ""))
    except route.ProfileError:
        return run_logs.DEFAULT_RETENTION_DAYS


def _lake_sync(a: argparse.Namespace) -> int:
    """Edge. Sync the run store and the trace files into the Iceberg lake; `--dry-run` creates and writes nothing."""
    # Absolute: `file://runs/lake` would name a host, and a relative trace path registered once would break from another cwd.
    runs_dir = Path(a.runs_dir).resolve()
    provider, problem = _lake_provider(a)
    if problem:
        return _lake_refuse(a, f"lake: {problem}")
    config = lake_config.resolve_lake(provider, runs_dir)
    store = store_url.profile_store_url(provider, runs_dir)
    root = store_url.profile_traces_root(provider, runs_dir).url
    root = root if "://" in root else str(Path(root).resolve())
    try:
        if a.dry_run:
            return _lake_sync_done(a, _lake_dry_run(config, store, root))
        retention = _log_retention_days(a)
        outcome = lake_lease.sync_under_lease(_lake_real_run(config, store, root, runs_dir, retention), runs_dir)
    except lake_config.LakeUnavailable as err:
        return _lake_refuse(a, str(err))
    except ImportError as err:
        # pyiceberg imported, but a module the lake also needs, such as pyarrow, did not.
        return _lake_refuse(a, f"the Iceberg lake needs the optional extra `lake`; {err.name or err} is missing: "
                               "pip install 'coxswain[lake]'")
    return _lake_sync_outcome(a, outcome)


def _lake_config_for(a: argparse.Namespace) -> tuple[lake_config.LakeConfig | None, str | None]:
    """Edge. (lake config, problem) for a read command; the same resolution `cox lake sync` uses."""
    provider, problem = _lake_provider(a)
    if problem:
        return None, f"lake: {problem}"
    return lake_config.resolve_lake(provider, Path(a.runs_dir).resolve()), None


def _lake_extra_missing(a: argparse.Namespace, err: ImportError) -> int:
    # pyiceberg or a module the lake also needs, such as duckdb or pyarrow, did not import.
    return _lake_refuse(a, f"the Iceberg lake needs the optional extra `lake`; {err.name or err} is missing: "
                           "pip install 'coxswain[lake]'")


def _lake_query(a: argparse.Namespace) -> int:
    """Edge. Run DuckDB SQL over the lake tables and print the rows as a table, or as JSON with `--json`."""
    config, problem = _lake_config_for(a)
    if config is None:
        return _lake_refuse(a, problem or "lake: no config")
    try:
        import duckdb

        from agent_tools import lake_query

        columns, rows = lake_query.query(lake_config.load_catalog(config), a.sql)
    except lake_config.LakeUnavailable as err:
        return _lake_refuse(a, str(err))
    except ImportError as err:
        return _lake_extra_missing(a, err)
    except duckdb.Error as err:
        return _lake_refuse(a, str(err))
    print(json.dumps(lake_query.rows_to_json(columns, rows), indent=2) if a.json else lake_query.render_table(columns, rows))
    return 0


def _lake_doctor(a: argparse.Namespace) -> int:
    """Edge. One line per lake check; exit 1 when the verdict is fail."""
    config, problem = _lake_config_for(a)
    if config is None:
        return _lake_refuse(a, problem or "lake: no config")
    try:
        from agent_tools import lake_doctor
    except ImportError as err:
        return _lake_extra_missing(a, err)
    checks = lake_doctor.run_checks(config)
    verdict = lake_doctor.verdict(checks)
    if a.json:
        print(json.dumps({"verdict": verdict, "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks]}, indent=2))
    else:
        print("\n".join([*(f"{c.status} {c.name}: {c.detail}" for c in checks), f"verdict: {verdict}"]))
    return 1 if verdict == lake_doctor.FAIL else 0


LAKE_GROUP = commands.Group(
    name="lake", help="the Iceberg lake: sync the run store and traces into it, query it, check it",
    description="The Iceberg lake: sync the run store and traces into it, query it with SQL, and check it. Needs the optional extra `lake`.",
    epilog="examples:\n  cox lake sync\n  cox lake sync --dry-run --json\n"
           "  cox lake query \"SELECT count(*) AS n FROM runs\"\n  cox lake doctor",
)
LAKE_COMMANDS = [
    commands.Command(
        "sync", "lake", "append new run-store rows and register new trace files; --dry-run writes nothing",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "report what would be synced; create and write nothing"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_lake_sync", False, (),
    ),
    commands.Command(
        "query", "lake", "run DuckDB SQL over the lake tables (runs, phases, ...) and print the rows",
        (
            commands.Arg(("sql",), {"help": "the SQL to run; a lake table is named by its bare name, such as runs"}),
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_lake_query", False, (),
    ),
    commands.Command(
        "doctor", "lake", "check the lake: catalog, namespace, warehouse and each table; exits 1 on a failed check",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_lake_doctor", False, (),
    ),
]


_DEV_MOVED = "moved: run `uv run --frozen python -m devtools <command> ...` from the coxswain checkout (from 0.15.0)"


def _dev_moved(a: argparse.Namespace) -> int:
    print(_DEV_MOVED)
    return 2


_RELEASE_HELP = "deploy a release to every host (the other old release commands moved to the checkout)"


def _release_notification(event) -> notify.Notification | None:
    """Pure. The notice for one deploy event: `deployed` is low, `deploy_failed` critical, `released` normal; any other kind None.
    `notify.notifications` is not used: it names only run events, so it would drop every deploy kind."""
    detail = event.detail
    if event.kind == "deployed":
        return notify.Notification(f"{event.run} deployed", str(detail.get("host", "")), "low")
    if event.kind == "deploy_failed":
        return notify.Notification(f"{event.run} deploy failed", f"{detail.get('host', '')} failed at {detail.get('step', '')}", "critical")
    if event.kind == "released":
        return notify.Notification(f"{event.run} released", str(detail.get("version", "")), "normal")
    return None


def _release_notify(event) -> None:
    """Edge. Sends the event's notice the way `cox runs notify` does: notify-send when installed, else one printed line.
    A notifier that cannot start never changes the deploy's exit code."""
    note = _release_notification(event)
    if note is None:
        return
    if shutil.which("notify-send") is None:
        print(f"[{note.urgency}] {note.title}: {note.body}")
        return
    with contextlib.suppress(OSError):
        subprocess.run(notify.notify_argv(note))


def _release_deploy(a: argparse.Namespace) -> int:
    """Edge. Hands the hosts rows and this host's name to the deploy edge and returns its exit code."""
    _, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    # `cox upgrade` replaces this tool's environment mid-run, so import everything first. The edge is imported here and
    # is not a `_LazyModule`: a registered one would make `_load_lazy_modules` fail on every install before it lands.
    edge = importlib.import_module("agent_tools.release_deploy_edge")
    _load_lazy_modules()
    rows = run_store.hosts(runs_dir)
    return edge.deploy_release(
        a.version, rows, socket.gethostname(), lambda argv: _real_run(list(argv), None), _release_notify
    )


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


def version_line(version: str) -> str:
    return f"cox {version}"


class _VersionAction(argparse.Action):
    """The metadata lookup runs here, at parse time, so ordinary commands never pay for it."""

    def __init__(
        self, option_strings: Sequence[str], dest: str = argparse.SUPPRESS, default: object = argparse.SUPPRESS, help: str | None = None
    ) -> None:
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(
        self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: object, option_string: str | None = None
    ) -> None:
        from agent_tools.version_info import package_version  # local: keeps the import off every other command's path

        print(version_line(package_version()))
        parser.exit(0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cox",
        description=__doc__,
        epilog=(
            "examples:\n"
            "  bare cox opens the coxswain Claude session\n"
            "  cox home opens the towpath dashboard\n"
            "  cox session opens the coxswain Claude session, with the same argv\n"
            "  cox setup doctor checks this machine\n"
            "  cox route launch epic runs a filed initiative\n"
            "  cox release deploy 2026.11.6 upgrades every host and verifies each"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # dest="launcher_profile", not "profile": eight subparsers below declare
    # their own `--profile` with default None, and argparse copies a matched
    # subparser's namespace back over the parent's, so a shared dest would
    # let `cox --profile P route status` silently lose P to that default.
    p.add_argument("--profile", dest="launcher_profile", help="bare cox: the profile to launch claude against")
    p.add_argument("--no-plugin", action="store_true", help="bare cox: start claude without --plugin-dir")
    p.add_argument("--print-argv", action="store_true", help="bare cox: print the claude argv and cwd instead of exec'ing it")
    p.add_argument("--version", "-V", action=_VersionAction, help="print the cox version and exit")
    sub = p.add_subparsers(dest="group", required=False)

    group, rows = _table_entry("runs")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("stats")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("lake")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("usage")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("router")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("steward")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("epic")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("pr")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("plan")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("route")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("chair")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("lander")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("host")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("courier")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("install")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("upgrade")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("versions")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("console")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("dash")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("settings")
    commands.build_parser(rows, [group], sub)

    dev = sub.add_parser("dev", help="moved: maintainer commands now run from the coxswain checkout",
                         description=_DEV_MOVED)
    # swallow every subcommand and flag the old group took, so the pointer prints instead of argparse erroring
    dev.add_argument("rest", nargs=argparse.REMAINDER)
    dev.set_defaults(fn=_dev_moved)

    release_p = sub.add_parser("release", help=_RELEASE_HELP,
                               description=f"cox release deploy VERSION upgrades every host and verifies each.\nOther release commands: {_DEV_MOVED}",
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    # `deploy` is read from `rest` by `_release`; the old maintainer subcommands and flags land there too and print the pointer
    release_p.add_argument("rest", nargs=argparse.REMAINDER)
    release_p.set_defaults(fn=_release)

    group, rows = _table_entry("home")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("session")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("setup")
    setup_p = commands.build_parser(rows, [group], sub)["setup"]
    # commands.build_parser's generic bare-group fallback prints help and
    # exits 2; a bare `cox setup` instead hands off to towpath, so override it here.
    setup_p.set_defaults(fn=_setup_tui)
    return p


def _plugin_root(skills_roots, name: str = "coxswain"):
    """First skills root carrying `<root>/<name>/.claude-plugin/plugin.json`,
    or None if none of them do."""
    for root in skills_roots:
        candidate = Path(root).expanduser() / name
        if (candidate / ".claude-plugin" / "plugin.json").exists():
            return candidate
    return None


def _launcher_argv(plugin_root, skills_roots, no_plugin: bool, extra_args: list[str]):
    """spec §7: the argv for a bare `cox` — real Claude Code with the
    coxswain plugin loaded, unless `no_plugin` or no skills root carries one.
    Takes the already-resolved `plugin_root` (a Path, or None if none of
    `skills_roots` carries one) as data: no filesystem probe here, so this is
    testable with literals. `extra_args` are appended as given — the caller's
    own `--` already did the job of separating them from cox's own flags, so
    none is re-inserted here. Returns (argv, warning); warning is the
    one-line fallback notice, or None when a plugin was found or none was
    asked for."""
    plugin_flag = [] if no_plugin or plugin_root is None else ["--plugin-dir", str(plugin_root)]
    warning = None if no_plugin or plugin_root is not None else f"coxswain plugin not found under {skills_roots}; starting plain claude"
    return ["claude", *plugin_flag, *extra_args], warning


def _spawn(argv: list[str]) -> subprocess.Popen:
    """Detached: own session via `start_new_session`, stdio to devnull, so `argv` outlives the caller's later `execvp`."""
    return subprocess.Popen(
        argv, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _courier_profile(a: argparse.Namespace) -> dict | None:
    """The parsed profile when it names a workspace; None, after saying why, when it does not."""
    profile, reason, *_ = _gather_context(_profile_path(a))
    if reason: print(f"routing: {reason}"); return None
    return profile


def _courier_workspace(a: argparse.Namespace) -> Path | None:
    profile = _courier_profile(a)
    return None if profile is None else Path(profile["workspace_dir"]).expanduser()


def _parse_fields(pairs: list[str]) -> dict | None:
    """`key=value` pairs split on the first `=`; None when a pair has no `=` or an empty key."""
    split = [p.partition("=") for p in pairs]
    return None if any(not key or not sep for key, sep, _ in split) else {key: value for key, _, value in split}


def _courier_send(a: argparse.Namespace) -> int:
    workspace = _courier_workspace(a)
    if workspace is None: return 2
    ref = courier.parse_reference(a.ref)
    if ref is None or courier.resolve(ref, workspace) is None:
        print(f"courier: {a.ref} does not resolve"); return 2
    pairs = getattr(a, "field", None) or []
    fields = _parse_fields(pairs)
    if fields is None:
        print(f"courier: bad field in {pairs}; expected key=value"); return 2
    path = workspace / "courier.jsonl"
    sender = (chair.read(workspace / "runs") or {}).get("session") or "cli"
    try:
        entry = courier.send(
            ref, sender, a.to, a.note, uuid.uuid4().hex,
            event=getattr(a, "event", None), fields=fields or None, topic=getattr(a, "topic", None),
        )
    except ValueError as exc:
        print(f"courier: {exc}"); return 2
    path.write_text(courier.append_line(_read_text_or_none(path) or "", entry), encoding="utf-8")
    return 0


def _inbox_line(entry: dict) -> str:
    typed = " ".join(f"{k}={entry[k]}" for k in ("event", "topic") if entry.get(k))
    return f"{entry['id']}: {entry['from']} -> {entry['to']}: {entry['note']} ({entry['ref']})" + (f" [{typed}]" if typed else "")


def _print_inbox(
    blob: str, label: str | None, holder: str | None,
    *, topic: str | None = None, event: str | None = None, consumer: str | None = None, wake: bool = False,
) -> None:
    listed = courier.inbox(blob, label, holder=holder, topic=topic, event=event, consumer=consumer)
    for entry in (e for e in listed if not wake or courier.needs_model(e)):
        print(_inbox_line(entry))


def _lock_holder_or_none(runs_dir: Path) -> str | None:
    """A missing, unreadable, or torn lock file has no holder; it does not refuse
    a read-only listing (cf. `runs_detail_screen.facts_for`'s same degrade)."""
    try:
        return (chair.read(runs_dir) or {}).get("session")
    except (OSError, json.JSONDecodeError):
        return None


def _courier_inbox(a: argparse.Namespace) -> int:
    workspace = _courier_workspace(a)
    if workspace is None: return 2
    holder = _lock_holder_or_none(workspace / "runs")
    _print_inbox(
        _read_text_or_none(workspace / "courier.jsonl") or "", a.label, holder,
        topic=getattr(a, "topic", None), event=getattr(a, "event", None),
        consumer=getattr(a, "consumer", None), wake=getattr(a, "wake", False),
    )
    return 0


def _courier_ack(a: argparse.Namespace) -> int:
    workspace = _courier_workspace(a)
    if workspace is None: return 2
    path = workspace / "courier.jsonl"
    if not courier.ack_file(path, a.id, getattr(a, "consumer", None)): print(f"courier: no entry {a.id}"); return 2
    return 0


COURIER_GROUP = commands.Group(name="courier", help="the courier bus: hand a reference to another label", description="", epilog="")
COURIER_COMMANDS = [
    commands.Command(
        "send", "courier", "append a bus entry naming a courier reference",
        (commands.Arg(("ref",)), commands.Arg(("--to",), {"required": True}), commands.Arg(("--note",), {"required": True}), commands.Arg(("--profile",)),
         commands.Arg(("--event",), {"help": "a typed event name"}), commands.Arg(("--topic",), {"help": "a topic to file the entry under"}),
         commands.Arg(("--field",), {"action": "append", "help": "key=value, repeatable"})),
        "agent_tools.cli:_courier_send", False, (),
    ),
    commands.Command(
        "inbox", "courier", "list this label's unacknowledged bus entries",
        (commands.Arg(("--label",)), commands.Arg(("--profile",)),
         commands.Arg(("--topic",)), commands.Arg(("--event",)), commands.Arg(("--consumer",)),
         commands.Arg(("--wake",), {"action": "store_true", "help": "only entries that need a model"})),
        "agent_tools.cli:_courier_inbox", False, (),
    ),
    commands.Command(
        "ack", "courier", "acknowledge one bus entry by id",
        (commands.Arg(("id",)), commands.Arg(("--profile",)), commands.Arg(("--consumer",))),
        "agent_tools.cli:_courier_ack", False, (),
    ),
]

PLAN_GROUP = commands.Group(
    name="plan", help="visual plans through the local bridge",
    description="Visual plans through the local bridge.",
    epilog="examples:\n  cox plan serve work/<id> --kind plan\n  cox plan serve work/<id> --check",
)
PLAN_COMMANDS = [
    commands.Command(
        "serve", "plan", "serve a visual plan through the local bridge",
        (commands.Arg(("dir",)), commands.Arg(("--kind",), {"default": "plan"}), commands.Arg(("--check",), {"action": "store_true"}), commands.Arg(("--no-open",), {"action": "store_true"})),
        "agent_tools.cli:_plan_serve", False, (),
    ),
]

EPIC_GROUP = commands.Group(
    name="epic", help="watch a detached run",
    description="Watch a detached run.",
    epilog="examples:\n  cox epic watch RUN.pid --log RUN.log\n  cox epic watch RUN.pid --json",
)
EPIC_COMMANDS = [
    commands.Command(
        "watch", "epic", "poll a detached run's pidfile until it exits",
        (
            commands.Arg(("pidfile",)), commands.Arg(("--log",)),
            commands.Arg(("--max-seconds",), {"type": float, "default": 570}), commands.Arg(("--interval",), {"type": float, "default": 20}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_epic_watch", False, (),
    ),
]

PR_GROUP = commands.Group(
    name="pr", help="land a pull request once its required checks pass",
    description="Land a pull request once its required checks pass.",
    epilog="examples:\n  cox pr land 123 --repo ~/repos/x",
)
PR_COMMANDS = [
    commands.Command(
        "land", "pr", "wait for the required checks, squash-merge a pull request and mark its tasks done",
        (
            commands.Arg(("number",), {"type": int, "help": "pull request number"}), commands.Arg(("--repo",), {"required": True}),
            commands.Arg(("--timeout",), {"type": float, "default": 600, "help": "seconds to wait for the required checks (default 600)"}),
        ),
        "agent_tools.cli:_pr_land", False, (),
    ),
]

ROUTE_GROUP = commands.Group(
    name="route", help="file work for the harness, and see what is queued or running",
    description="File work for the harness, and see what is queued or running.",
    epilog="examples:\n  cox route launch epic --initiative work/<id> --repo PATH\n  cox route status --profile PATH",
)
_CHAIR_PID_ARG = commands.Arg(("--pid",), {"type": int, "help": "the durable pid that owns the loop (default: the parent process)"})
_LAUNCH_SHARED_ARGS = (
    commands.Arg(("--tier-ceiling",), {"choices": ("cheap", "standard", "deep")}),
    commands.Arg(("--effort-ceiling",), {"choices": ("low", "high")}),
    commands.Arg(("--force",), {"action": "store_true", "help": "launch despite a usage stop, a foreign live leader or a full local host"}),
    commands.Arg(("--no-claim",), {"action": "store_true", "help": "launch without taking an unheld or stale loop"}),
    commands.Arg(("--label",)),
)
ROUTE_COMMANDS = [
    commands.Command(
        "context", "route", "the routing profile's resolved context",
        (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_route_context", False, (),
    ),
    commands.Command(
        "status", "route", "what is queued or running for this profile",
        (
            commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--all",), {"action": "store_true", "help": "every run, not only live and recent ones"}),
        ),
        "agent_tools.cli:_route_status", False, (),
    ),
    commands.Command(
        "file", "route", "file a new ticket for the harness",
        (
            commands.Arg(("--profile",)), commands.Arg(("--repo",)), commands.Arg(("--title",)), commands.Arg(("--body",)),
            commands.Arg(("--phase",), {"default": "build"}), commands.Arg(("--intake",), {"action": "store_true"}),
            commands.Arg(("--from-intake",), {"help": "link and file an existing intake file's initiative, then retire it"}),
            commands.Arg(("--force",), {"action": "store_true", "help": "file an intake even when its title duplicates an open issue"}),
            commands.Arg(("--link",), {"default": None, "help": "with --intake: the issue url this intake is filed from, so a later pull skips it"}),
            commands.Arg(("--after",), {"action": "append", "default": None, "help": "with --intake: ids this intake waits on; repeat the flag or comma-separate"}),
            commands.Arg(("--draft",), {"action": "store_true", "help": "with --intake: file it as a draft, which the chair skips until it is ready (the default)"}),
            commands.Arg(("--ready",), {"action": "store_true", "help": "with --intake: file it ready for the chair to pick up"}),
            commands.Arg(("--epic",), {"default": None, "help": "with --intake: the epic id this intake belongs to; it must already exist"}),
            commands.Arg(("--priority",), {"default": None, "help": "with --intake: a whole number; higher runs first"}),
            commands.Arg(("--target-release",), {"default": None, "help": "with --intake: the release it ships in, a dotted version such as 2026.11.6"}),
        ),
        "agent_tools.cli:_route_file", False, (),
    ),
    commands.Command(
        "priority", "route", "move an initiative or a queued intake up or down the launch order",
        (
            commands.Arg(("initiative",), {"nargs": "?", "help": "an initiative or a queued intake; required with a flag, omit it to list the open queue"}),
            commands.Arg(("--profile",)),
            commands.Arg(("--set",), {"type": int, "default": None, "metavar": "N", "help": "write N on every item"}),
            commands.Arg(("--up",), {"action": "store_true", "help": "pass the nearest higher open initiative"}),
            commands.Arg(("--down",), {"action": "store_true", "help": "pass the nearest lower open initiative"}),
            commands.Arg(("--top",), {"action": "store_true", "help": "one above every other open initiative"}),
            commands.Arg(("--bottom",), {"action": "store_true", "help": "one below every other open initiative"}),
        ),
        "agent_tools.cli:_route_priority", False, (),
        exclusive=("--set", "--up", "--down", "--top", "--bottom"),
    ),
    commands.Command(
        "schedule", "route", "project the whole queue as one schedule",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--explain",), {"metavar": "INITIATIVE", "default": None, "help": "why this initiative sits where it does"}),
            commands.Arg(("--should-jump",), {"metavar": "INITIATIVE", "default": None, "help": "ask whether this initiative should move up the queue"}),
        ),
        "agent_tools.cli:_route_schedule", False, (),
        exclusive=("--explain", "--should-jump"),
    ),
    commands.Command(
        "pull", "route", "file intake tickets from a source, once per link",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--source",), {"default": None, "help": "one source; default every source in the profile"}),
            commands.Arg(("--dry-run",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_route_pull", False, (),
    ),
    commands.Command(
        "lint", "route", "static ticket lint over a filed initiative, work-shape.md §3",
        (commands.Arg(("initiative_dir",)), commands.Arg(("--repo",), {"default": None})),
        "agent_tools.cli:_route_lint", False, (),
    ),
    commands.Command(
        "groups", "route", "print the newest plans/intake-groups/<date>.md file, work-shape.md §5",
        (commands.Arg(("--profile",)),),
        "agent_tools.cli:_route_groups", False, (),
    ),
    commands.Command(
        "export", "route", "write this machine's work/ and intake/ files from the store; nothing is committed",
        (commands.Arg(("--profile",)),),
        "agent_tools.cli:_route_export", False, (),
        description=(
            "The store holds the board. This writes it out as files under the workspace so they can be read, and "
            "removes files no row names.\nIt first loads any ticket a decompose wrote as a file and the store has "
            "not seen. Under work_state files it does nothing."
        ),
    ),
    commands.Command(
        "drift", "route", "items whose store state and file state differ",
        (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_route_drift", False, (),
    ),
    commands.Command(
        "import", "route", "load the current work item files into the store's work_items table",
        (commands.Arg(("--profile",)), commands.Arg(("--workspace",))),
        "agent_tools.cli:_route_import", False, (),
    ),
    commands.Command(
        "paths", "route", "rewrite recorded repo paths to the tilde form, dry run first",
        (
            commands.Arg(("--profile",)), commands.Arg(("--workspace",)),
            commands.Arg(("--apply",), {"action": "store_true", "help": "write the changes; the default only lists them"}),
            commands.Arg(("--move",), {"nargs": 2, "metavar": ("OLD", "NEW"), "help": "swap repo prefix OLD for NEW in open records only; done and dropped records are left alone"}),
        ),
        "agent_tools.cli:_route_paths", False, (),
    ),
    commands.Command(
        "store-fill", "route", "fill the store's work_items from the workspace files",
        (commands.Arg(("--profile",)), commands.Arg(("--workspace",))),
        "agent_tools.store_fill:main", False, (),
    ),
    commands.Command(
        "approve", "route", "approve a draft initiative's todo tickets: todo becomes ready",
        (
            commands.Arg(("initiative",)), commands.Arg(("--task",), {"help": "approve this one task only"}),
            commands.Arg(("--by",), {"help": "who approves; default the OS user"}), commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_approve", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "decline", "route", "decline a draft initiative: its todo tickets become dropped",
        (
            commands.Arg(("initiative",)), commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who declines; default the OS user"}), commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_decline", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "edit", "route", "change a queued intake, an initiative or a task ticket",
        (
            commands.Arg(("id",), {"help": "a queued intake, an initiative or a task ticket"}), commands.Arg(("--title",)),
            commands.Arg(("--body",)), commands.Arg(("--body-file",)),
            commands.Arg(("--note",), {"default": None, "help": "task ticket only: append this text to the body once, with or without --state; not with --body or --body-file"}),
            commands.Arg(("--repo",)),
            commands.Arg(("--surfaces",), {"help": "task ticket only: comma-separated paths that replace the list"}),
            commands.Arg(("--state",), {"help": "task ticket only: ready, approved, done or dropped; never backward"}),
            commands.Arg(("--pr",), {"help": "with --state done: the merged PR url to record on the task, also on a task that is already done; without it the merged PR is looked up with gh"}),
            commands.Arg(("--rebuild",), {"action": "store_true", "help": "with --state ready: send an approved task back to build on current main; needs a recorded land conflict or --force"}),
            commands.Arg(("--force",), {"action": "store_true", "help": "with --rebuild: allow it with no recorded land conflict"}),
            commands.Arg(("--link",), {"default": None, "help": "queued intake only: set the issue url; an empty value clears the link"}),
            commands.Arg(("--after",), {"action": "append", "default": None, "help": "queued intake only: ids it waits on, repeated or comma-separated; an empty value clears the list"}),
            commands.Arg(("--epic",), {"default": None, "help": "initiative only: join the epic with this id, or none to leave it"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the diff and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_edit", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "epic", "route", "create an epic definition file: `route epic new <id>`",
        (
            commands.Arg(("action",), {"help": "only `new`"}), commands.Arg(("id",), {"help": "the epic id, a lowercase slug"}),
            commands.Arg(("--title",)), commands.Arg(("--target-branch",)),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_epic", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "remove", "route", "remove queued intakes or initiatives that have no live run",
        (
            commands.Arg(("id",), {"nargs": "*", "help": "queued intakes or initiatives; repeat for several"}),
            commands.Arg(("--match",), {"metavar": "GLOB", "help": "also remove every queued intake whose file stem or id matches"}),
            commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who removes; default the OS user"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_remove", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "hold", "route", "hold a queued intake or an initiative so the chair skips it until released; for an intake, an alias for draft with a reason",
        (
            commands.Arg(("id",), {"help": "a queued intake or an initiative"}),
            commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who holds; default the OS user"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_hold", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "release", "route", "release a held intake or initiative back to the queue; an intake moves to ready",
        (
            commands.Arg(("id",), {"help": "a held intake or initiative"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_release", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "ready", "route", "move a queued intake to ready so the chair can pick it up",
        (
            commands.Arg(("id",), {"help": "a queued intake"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_stage_ready", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "draft", "route", "move a queued intake to draft so the chair skips it until it is ready",
        (
            commands.Arg(("id",), {"help": "a queued intake"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_stage_draft", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "requeue", "route", "requeue the intake of an initiative that has no live tasks",
        (
            commands.Arg(("id",), {"help": "an intake or an initiative"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_requeue", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "leader", "route", "whether this session is the chair (the SessionStart line)", (), None, False, (),
        subcommands=(
            commands.Command(
                "status", "route", "print one line: held by this session, taken over, or not the chair",
                (commands.Arg(("--profile",)),),
                "agent_tools.cli:_route_leader_status", False, (),
            ),
        ),
        sub_dest="leader_cmd", sub_required=False,
    ),
    commands.Command(
        "chair", "route", "the chair lock for the landing loop (runs/chair.json)", (), None, False, (),
        subcommands=(
            commands.Command(
                "take", "route", "take the chair lock; --steal overrides a live holder and names it",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--steal",), {"action": "store_true", "help": "override a live holder and name it"}),
                    commands.Arg(("--hours",), {"type": float, "default": _leader_takeover_hours(), "help": "how long the takeover lasts before the loop may steal it (default: policy.leader.takeover_hours)"}),
                    commands.Arg(("--model",), {"choices": ["sonnet", "opus", "haiku"], "default": None, "help": "the model alias this chair runs on, recorded in the chair record (default: not recorded)"}),
                ),
                "agent_tools.cli:_route_chair_take", False, (),
            ),
            commands.Command(
                "extend", "route", "move the end of the chair takeover this session holds",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--hours",), {"type": float, "default": _leader_takeover_hours(), "help": "hours from now (default: policy.leader.takeover_hours)"}),
                ),
                "agent_tools.cli:_route_chair_extend", False, (),
            ),
            commands.Command(
                "beat", "route", "refresh the chair lock's heartbeat",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--run",)),
                ),
                "agent_tools.cli:_route_chair_beat", False, (),
            ),
            commands.Command(
                "release", "route", "release the chair lock this session holds",
                (commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG),
                "agent_tools.cli:_route_chair_release", False, (),
            ),
            commands.Command(
                "status", "route", "the chair lock's holder and computed state",
                (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
                "agent_tools.cli:_route_chair_status", False, (),
            ),
            commands.Command(
                "clear", "route", "remove the chair lock file, refusing a live holder unless --force",
                (
                    commands.Arg(("--profile",)),
                    commands.Arg(("--force",), {"action": "store_true", "help": "clear the lock even if its recorded pid is live"}),
                ),
                "agent_tools.cli:_route_chair_clear", False, (),
            ),
            commands.Command(
                "chat", "route", "append to or read the leader chat thread (runs/leader.chat.jsonl)",
                (
                    commands.Arg(("text",), {"nargs": "?"}), commands.Arg(("--profile",)),
                    commands.Arg(("--read",), {"action": "store_true"}),
                    commands.Arg(("--since",)), commands.Arg(("--json",), {"action": "store_true"}),
                    commands.Arg(("--as-leader",), {"action": "store_true", "help": "send as the lock's holder; refuses unless this process is the live holder"}),
                ),
                "agent_tools.cli:_route_chair_chat", False, (),
            ),
            commands.Command(
                "converse", "route", "talk to the running chair through the courier, without the lease (`chat` is the leader thread)", (), None, False, (),
                subcommands=(
                    commands.Command(
                        "send", "route", "send the chair a directive; it applies on the chair's next tick",
                        (
                            commands.Arg(("kind",), {"help": "one of: priority, hold, release, scope, feature, cut_release"}),
                            commands.Arg(("args",), {"nargs": "*", "help": "key=value pairs, e.g. target_kind=task target=t1 rank=1"}),
                            commands.Arg(("--profile",)),
                            commands.Arg(("--sender",), {"default": "session"}),
                        ),
                        "agent_tools.cli:_route_chair_converse_send", False, (),
                    ),
                    commands.Command(
                        "ask", "route", "ask the chair a question and wait a bounded time for its answer",
                        (
                            commands.Arg(("kind",), {"help": "one of: left_on_release, why_not_launched, what_failed"}),
                            commands.Arg(("subject",), {"nargs": "?", "default": ""}),
                            commands.Arg(("--profile",)),
                            commands.Arg(("--sender",), {"default": "session"}),
                            commands.Arg(("--wait",), {"type": float, "default": 30.0, "help": "seconds to wait for the answer (default 30)"}),
                        ),
                        "agent_tools.cli:_route_chair_converse_ask", False, (),
                    ),
                    commands.Command(
                        "state", "route", "print the directives in force, the epics the chair last saw, and recent events",
                        (commands.Arg(("--profile",)),),
                        "agent_tools.cli:_route_chair_converse_state", False, (),
                    ),
                ),
                sub_dest="converse_cmd", sub_required=False,
            ),
        ),
        sub_dest="chair_cmd", sub_required=False,
    ),
    commands.Command(
        "sync", "route", "mirror the work store onto the profile's tracker",
        (
            commands.Arg(("--profile",)), commands.Arg(("--item",)), commands.Arg(("--project",)),
            commands.Arg(("--workspace",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
            commands.Arg(("--prune-out-of-scope",), {"action": "store_true", "help": "close open task issues outside tracker_scope as not planned and remove their cards; with --dry-run, list them only. A coxswain label added by hand counts as made by sync"}),
        ),
        "agent_tools.cli:_route_sync", False, (),
    ),
    commands.Command(
        "launch", "route", "run one of the harness's graphs directly; `epic` is the run of one initiative", (), None, False, (),
        subcommands=(
            commands.Command(
                "epic", "route", "launch the run of one filed initiative (the epic graph; epic is its historical name)",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--initiative",), {"required": True}),
                    commands.Arg(("--repo",)),
                    commands.Arg(("--fix-attempts",), {"type": int, "default": None}),
                    commands.Arg(("--base",), {"help": "branch phase branches are cut from, as origin/<branch>; default: the repo's base: setting, then origin/HEAD, then main"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    commands.Arg(("--on",), {"help": "start the lane on this host from the hosts table; `local` is this machine. Without it the table places the lane"}),
                    commands.Arg(("--local",), {"action": "store_true", "help": "start the lane on this machine, not where the hosts table places it"}),
                    commands.Arg(("--include-blocked",), {"action": "store_true", "help": "launch despite a ready task behind a blocked item (--force does not)"}),
                    commands.Arg(("--phase",), {"action": "append", "default": None, "help": "run only the named phases of the initiative; repeat to start one run per phase, each on its own lane"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "epic"},
            ),
            commands.Command(
                "decompose", "route", "launch the decompose graph against an idea",
                (
                    commands.Arg(("--profile",)),
                    commands.Arg(("--idea",), {"required": True, "metavar": "PATH", "help": "the intake/idea markdown file"}),
                    commands.Arg(("--initiative-id",), {"required": True}),
                    commands.Arg(("--task-ids",), {"choices": ["ordinal"], "help": "name the graph's own tasks <initiative-id>-t1, -t2, ..."}),
                    commands.Arg(("--local",), {"action": "store_true", "help": "start the lane on this machine, not where the hosts table places it"}),
                    commands.Arg(("--detach",), {"action": "store_true", "help": "start the decompose in the background and return once it has started"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "decompose"},
            ),
            commands.Command(
                "rescue", "route", "launch the rescue graph against a task",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--initiative",), {"required": True}),
                    commands.Arg(("--task",), {"required": True}),
                    commands.Arg(("--repo",)),
                    commands.Arg(("--base",), {"help": "branch the rescue checks against, as origin/<branch>; default: the repo's base: setting, then origin/HEAD, then main"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "rescue"},
            ),
            commands.Command(
                "cos", "route", "launch the cos graph",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "cos"},
            ),
            commands.Command(
                "sweep", "route", "launch the sweep graph against an idea",
                (
                    commands.Arg(("--idea",), {"required": True}),
                    commands.Arg(("--initiative-id",), {"required": True}),
                    commands.Arg(("--label",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
                ),
                "agent_tools.cli:_route_launch_sweep", False, (),
            ),
        ),
        sub_dest="graph", sub_required=True,
    ),
]

def _setup_doctor_row(a: argparse.Namespace) -> int:
    """Indirect through the module global, not a frozen reference: SETUP_COMMANDS
    is built once at import, but test_setup_screen.py monkeypatches
    `cli._setup_doctor` per test and expects the swap honoured."""
    return _setup_doctor(a)


SETUP_GROUP = commands.Group(
    name="setup", help="does this machine's profile actually work",
    description="Does this machine's profile actually work.",
    epilog="examples:\n  cox setup doctor --profile PATH\n  cox setup install --root PATH --team NAME --workspace PATH",
    args=(commands.Arg(("--profile",)),),
)
# The connection flags and --dry-run, shared by `store` and `store switch`.
_STORE_FLAGS = (
    commands.Arg(("--host",), {"help": "shared: Postgres host"}),
    commands.Arg(("--port",), {"type": int, "help": "shared: Postgres port"}),
    commands.Arg(("--database",), {"help": "shared: Postgres database"}),
    commands.Arg(("--user",), {"help": "shared: Postgres user"}),
    commands.Arg(("--bucket",), {"help": "shared: S3 bucket for traces"}),
    commands.Arg(("--endpoint",), {"help": "shared: S3 endpoint URL"}),
    commands.Arg(("--region",), {"help": "shared: S3 region"}),
    commands.Arg(("--password-env",), {"help": "shared: NAME of the variable holding the Postgres password"}),
    commands.Arg(("--access-key-env",), {"help": "shared: NAME of the variable holding the S3 access key id"}),
    commands.Arg(("--secret-key-env",), {"help": "shared: NAME of the variable holding the S3 secret key"}),
    commands.Arg(("--dry-run",), {"action": "store_true", "help": "validate and print; write and probe nothing"}),
)


def _suppressed(args: tuple) -> tuple:
    """Copies of `args` that leave the namespace alone when absent, so a flag given before the verb is kept."""
    return tuple(commands.Arg(arg.flags, {**arg.kwargs, "default": argparse.SUPPRESS}) for arg in args)


SETUP_COMMANDS = [
    commands.Command(
        "doctor", "setup", "check this machine's profile against what it needs",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--repo",), {"help": "target repo to check for a .agent/cartridge.yaml overlay (default: cwd)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--host",), {"help": "run the doctor on this lane host from lane_hosts; --json is ignored"}),
        ),
        "agent_tools.cli:_setup_doctor_row", False, (),
    ),
    commands.Command(
        "install", "setup", "sync the checkout's venv, install cox and write a profile for this machine",
        (
            commands.Arg(("--root",)), commands.Arg(("--team",), {"required": True}), commands.Arg(("--workspace",)),
            commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
            commands.Arg(("--provider-profile",)), commands.Arg(("--skills-root",)), commands.Arg(("--assume",), {"default": "a", "choices": ("a", "r")}),
            commands.Arg(("--plugins",), {"action": "store_true"}), commands.Arg(("--hook",), {"action": "store_true"}),
            commands.Arg(("--force-profile",), {"action": "store_true"}), commands.Arg(("--dry-run",), {"action": "store_true"}),
            commands.Arg(("--window-ceiling-usd",), {"type": float, "default": None, "dest": "window_ceiling_usd", "help": "write spend: window_ceiling_usd into the profile"}),
            commands.Arg(("--weekly-ceiling-usd",), {"type": float, "default": None, "dest": "weekly_ceiling_usd", "help": "write spend: weekly_ceiling_usd into the profile"}),
        ),
        "agent_tools.cli:_setup_install", False, (),
    ),
    commands.Command(
        "extras", "setup", "install the optional extras into this venv, pinned and hash-checked from uv.lock",
        (
            commands.Arg(("--with",), {"nargs": "+", "dest": "with_extras", "metavar": "EXTRA", "help": "extras to install; default traces parquet lake postgres"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print what would be installed; change nothing"}),
        ),
        "agent_tools.cli:_setup_extras", False, (),
    ),
    commands.Command(
        "migrate", "setup", "plan moving this host to the one monorepo checkout; --apply migrates",
        (
            commands.Arg(("--home",), {"help": "absolute path of the coxswain home to migrate to"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "same as the default: print the plans; change nothing"}),
            commands.Arg(("--apply",), {"action": "store_true", "help": "execute the plans; without it the command only prints them"}),
            commands.Arg(("--hosts",), {"action": "store_true", "help": "with --apply, also run the host steps; use it on the chair's machine"}),
            commands.Arg(("--finish",), {"action": "store_true", "help": "own mode, after every host migrated: rewrite queue paths, remove old links"}),
            commands.Arg(("--old-root",), {"metavar": "PATH", "help": "with --finish, the old checkout root when no marker names it"}),
            commands.Arg(("--to",), {"metavar": "VERSION", "help": "the release the umbrella checkout becomes; default the newest"}),
        ),
        "agent_tools.cli:_setup_migrate", False, (),
    ),
    commands.Command(
        "store", "setup", "choose files or a shared store for run records",
        (
            commands.Arg(("--store",), {"help": "files or shared; asked in a terminal when omitted, else files"}),
            *_STORE_FLAGS,
            commands.Arg(("--allow-unreachable",), {"action": "store_true", "help": "exit 0 even when a probe fails"}),
        ),
        "agent_tools.cli:_setup_store", False, (),
        subcommands=(
            commands.Command(
                "switch", "setup", "copy the run store and traces to the other backend, then record store.mode",
                (commands.Arg(("target",), {"choices": ("files", "shared")}), *_suppressed(_STORE_FLAGS)),
                "agent_tools.cli:_setup_store_switch", False, (),
            ),
        ),
        sub_dest="store_cmd", sub_required=False,
    ),
    commands.Command(
        "token", "setup", "store the long-lived Claude token that coxswain's own claude runs use",
        (
            commands.Arg(("--token",), {"help": "the token printed by `claude setup-token`; prefer --from-file, a flag lands in shell history"}),
            commands.Arg(("--from-file",), {"metavar": "PATH", "help": "read the token from this file; else asked with hidden input in a terminal"}),
        ),
        "agent_tools.cli:_setup_token", False, (),
        subcommands=(
            commands.Command(
                "status", "setup", "report whether the stored token file exists and claude accepts it",
                (commands.Arg(("--probe",), {"action": "store_true", "help": "also send one small claude request; exits 1 when it fails"}),),
                "agent_tools.cli:_setup_token_status", False, (),
            ),
        ),
        sub_dest="token_cmd", sub_required=False,
        description=(
            "The token comes from `claude setup-token`.\n"
            "It is stored mode 600 in coxswain's config folder.\n"
            "Interactive claude sessions are untouched: only coxswain's own runs read the file."
        ),
    ),
]


@dataclasses.dataclass(frozen=True)
class _ChairUnwired:
    """A reader or recorder with no edge yet; `_chair_run` refuses to start while any field of either bundle holds one."""

    source: str

    def __call__(self, *_args: object) -> object:
        raise RuntimeError(f"chair run: the {self.source} source is not wired to a reader yet")


def _chair_unwired_sources(deps: chair_run.RunDeps) -> list[str]:
    """Named unwired fields of the facts and exec bundles, in field order."""
    return [
        v.source
        for bundle in (deps.facts_deps, deps.exec_deps)
        for f in dataclasses.fields(bundle)
        if isinstance(v := getattr(bundle, f.name), _ChairUnwired)
    ]


def _chair_once_exit(last_line: str) -> int:
    """1 when the status line has the shape `chair_run.error_line` prints; a test builds one with it, so a format change fails there."""
    return 1 if "| tick error: " in last_line else 0


def _chair_max_in_flight(runs_dir: Path, profile: dict) -> int:
    """Edge. The lane cap `chair_capacity.chair_max_in_flight` reads, the same one the `cox dash` feed reads."""
    return chair_capacity.chair_max_in_flight(runs_dir, profile)


def _local_max_in_flight(runs_dir: Path, profile: dict, hostname: str) -> int:
    """The local lane cap: the hosts-table row's own `capacity` when one exists for `hostname`, else
    `_chair_max_in_flight`'s existing cartridge, then pacing-file, then default-3 chain."""
    row = next((r for r in run_store.hosts(runs_dir) if r.get("name") == hostname), None)
    return _chair_max_in_flight(runs_dir, profile) if row is None or row.get("capacity") is None else int(row["capacity"])


def _check_capacity(runs_dir: Path, profile: dict, hostname: str) -> int | None:
    """Edge. The capacity a check's worker count divides cores by: `COX_LANE_CAPACITY` when set, else `_local_max_in_flight`.
    A set value that is not a whole number of 1 or more is None, so the checks run uncapped, as `lane_workers_from` reads it."""
    raw = os.environ.get("COX_LANE_CAPACITY")
    if raw is None:
        return _local_max_in_flight(runs_dir, profile, hostname)
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 1 else None


def _chair_stranded_inputs(
    ws: Path, mode: str, files: Mapping[Path, str] | None = None, store_rows: list | None = None,
) -> tuple[list[dict], list[dict]]:
    """Edge. The inputs of `runs_stranded.stranded`. Item states come from the store under mode "store", as the docket's do.

    Under mode "store" a fetched run whose records stayed in the store adds its store rows too: without them an
    approved task of that run gets no run, and `plan_lands` can only ask the chair for one.

    `files` (`chair_read_quarantined.read_work_files`) and `store_rows` (`run_store.work_items`) stand in for the
    work-file and store reads when a tick already holds them; None reads them here."""
    task_records = []
    for path in sorted((ws / "runs").glob("*/tasks/*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        record.setdefault("run", path.parent.parent.parent.name)
        record.setdefault("task", path.stem)
        record.setdefault("phase", path.parent.name)
        task_records.append(record)
    if files is None:
        initiative_texts, work_items = _initiative_texts(ws), _stored_work_items(ws, mode)
    else:
        initiative_texts = {p.parent.name: t for p, t in chair_read_quarantined.initiative_files(ws, files).items()}
        rows = store_rows if store_rows is not None else (run_store.work_items(ws / "runs") if mode == "store" else [])
        work_items = route.with_store_states(
            [
                route.work_item(route.parse_frontmatter(text)[0], initiative=p.parent.parent.name, phase_dir=p.parent.name, stem=p.stem)
                for p, text in chair_read_quarantined.task_files(ws, files).items()
                if p.name != "initiative.md"
            ],
            rows, mode,
        )
    items = [
        {**item, "repo": route.parse_frontmatter(initiative_texts.get(item["initiative"], ""))[0].get("repo")}
        for item in work_items
    ]
    stored = _store_held_records(ws / "runs", items) if mode == "store" else []
    return task_records + stored, items


def _initiative_repos(initiative_files: Mapping[Path, str], home: str) -> dict[str, str]:
    """Pure. Initiative id to the `repo:` in its `initiative.md` text expanded under `home`, as `chair_facts.read_initiative_repos` reads it from disk."""
    fields = {p.parent.name: route.parse_frontmatter(text)[0] for p, text in initiative_files.items()}
    return {name: repo_path.expand(f["repo"], home) for name, f in fields.items() if isinstance(f.get("repo"), str) and f["repo"]}


class _TargetBranchRefusal(Exception):
    """A target branch is set and cannot be used; the message names the file, the value and the initiatives affected."""


def _unusable_branch(text: str, path: Path) -> str | None:
    """Pure. Why the `target_branch:` in one epic file's text is set and not a valid branch name, else None."""
    from core.landing import valid_branch  # lazy: core sits beside agent_tools in one venv
    from core.workstore import WorkStoreError, _split_frontmatter

    try:
        branch = _split_frontmatter(text, path)[0].get("target_branch")
    except WorkStoreError:
        return None
    if branch is None:
        return None
    try:
        valid_branch(branch, "epic")
    except ValueError as exc:
        return str(exc)
    return None


def _read_epics(ws: Path) -> tuple[dict[str, dict], dict[str, str], set[str]]:
    """Edge. Each `<ws>/epics/*.md` read on its own, so one broken file never changes another epic:
    `(definitions, refusals, unread)`. A file that cannot be read or parsed is a `note:` on stderr and its stem is in
    `unread`. A file whose `target_branch` is set and not a valid branch name is in `refusals`, keyed by the epic id
    (the file stem), with the file and the value in the message."""
    from core.epic_files import parse_epic  # lazy: core sits beside agent_tools in one venv

    definitions: dict[str, dict] = {}
    refusals: dict[str, str] = {}
    unread: set[str] = set()
    directory = ws / "epics"
    for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
        text = ""
        try:
            text = path.read_text(encoding="utf-8")
            definition = parse_epic(text, path.name)
        except (OSError, ValueError) as exc:
            reason = None if isinstance(exc, OSError) else _unusable_branch(text, path)
            if reason is not None:
                refusals[path.stem] = f"{path}: {reason}"
            else:
                first_line = (str(exc).splitlines() or [type(exc).__name__])[0]
                print(f"note: epic file {path} not read ({first_line}); using the repo default for it", file=sys.stderr)
                unread.add(path.stem)
            continue
        definitions[definition["id"]] = definition
    return definitions, refusals, unread


def _epic_users(ws: Path) -> dict[str, list[str]]:
    """Edge. Epic id to the initiatives whose `initiative.md` names it in a string `epic:`."""
    users: dict[str, list[str]] = {}
    for path in sorted((ws / "work").glob("*/initiative.md")):
        text = _read_text_or_none(path)
        epic = route.parse_frontmatter(text)[0].get("epic") if text is not None else None
        if isinstance(epic, str):
            users.setdefault(epic, []).append(path.parent.name)
    return users


def _epic_definitions(ws: Path) -> dict[str, dict]:
    """Edge. The valid epic definitions of `ws`, for the chair. A bad `target_branch` in the file of an epic that an
    initiative names raises `_TargetBranchRefusal`, as `_initiative_target_branch` refuses that initiative. A bad file
    no initiative names is a `note:`, so it cannot stop the chair for unrelated epics."""
    definitions, refusals, _ = _read_epics(ws)
    users = _epic_users(ws) if refusals else {}
    for epic_id, message in refusals.items():
        if epic_id in users:
            raise _TargetBranchRefusal(f"{message} (epic of initiative {', '.join(users[epic_id])})")
        print(f"note: epic target branch unusable ({message}); no initiative names that epic", file=sys.stderr)
    return definitions


def _initiative_target_branch(profile: dict, repo: Path | None, ws: Path, initiative: str | None) -> tuple[str | None, str | None]:
    """Edge. `(branch, refusal)`: the initiative's resolved target branch (its own `target_branch:`, else its epic's,
    else the cartridge's `landing_areas.target_branch`), None when none is set. `refusal` is the message when the
    cartridge does not resolve or a set target branch is invalid; every other gap falls back to None with a `note:`."""
    from core.cartridge import OVERLAY_ALLOWED_KEYS  # lazy: core sits beside agent_tools in one venv
    from core.landing import resolve_epic_stack, resolve_target_branch

    cartridge, error = _team_cartridge(profile, repo, OVERLAY_ALLOWED_KEYS)
    if error:
        return None, error
    definitions, refusals, unread = _read_epics(ws)
    try:
        text = _read_text_or_none(ws / "work" / initiative / "initiative.md") if initiative else None
        fields = route.parse_frontmatter(text)[0] if text is not None else {}
        if text is not None and not fields:
            print(f"note: work/{initiative}/initiative.md has no front matter; using the repo default", file=sys.stderr)
        epic_id = fields.get("epic")
        if "epic" in fields and not isinstance(epic_id, str):
            print(f"note: work/{initiative}/initiative.md epic: is not a string; ignoring it", file=sys.stderr)
            epic_id = None
        if epic_id in refusals:
            return None, refusals[epic_id]
        epic = definitions.get(epic_id) if epic_id else None
        if epic_id and epic is None and epic_id not in unread:
            print(f"note: epic {epic_id} has no file under epics/; using the repo default", file=sys.stderr)
        resolved = resolve_target_branch(cartridge, epic["target_branch"] if epic else None, fields.get("target_branch"))
        if epic and initiative and not fields.get("target_branch") and resolve_epic_stack(cartridge, epic.get("epic_stack")):
            # A stacked epic: an initiative that needs an unlanded initiative of the same epic forks from its branch.
            from agent_tools import lander_ports
            resolved = lander_ports.stack_base(
                ws, initiative, str(epic_id), epic["target_branch"], lander_ports.read_records(ws / "runs")
            ) or resolved
        return resolved, None
    except ValueError as exc:
        return None, str(exc)


def _land_initiative(runs_dir: Path, run_id: str, phase: str | None, task: str | None) -> str | None:
    """Edge. The initiative a land is for: the phase's, else the task record's, else the run id's own."""
    if phase:
        return _initiative_of_phase(runs_dir.parent / "work", phase, run_id)
    record = _land_load(runs_dir, run_id, task)[0]
    return (record or {}).get("initiative") or chair_facts.run_initiative(run_id)


def _chair_gather(ws: Path) -> chair_run.Gather:
    """Pure. The chair's gatherer with the epic definitions of `ws` read on every tick through `_epic_definitions`."""
    return lambda deps, now: chair_facts.gather_facts(deps, now, epic_definitions=_epic_definitions(ws))


def _initiative_land_at(initiative_files: Mapping[Path, str]) -> dict[str, str | None]:
    """Pure. Initiative id to the `land_at:` in its `initiative.md` text, None when the key is absent or not a string."""
    fields = {p.parent.name: route.parse_frontmatter(text)[0] for p, text in initiative_files.items()}
    return {name: f["land_at"] if isinstance(f.get("land_at"), str) else None for name, f in fields.items()}


def _phase_land_at(phase_record: Mapping, profile: dict, work_dir: Path, initiative: str) -> str:
    """Edge. The phase record's `land_at` when its run wrote one, else what the chair plans the land with: the
    initiative's `land_at:`, else the team cartridge's, else ticket. The run store's phase manifests carry no
    `land_at`, so reading the record alone gave ticket while the chair planned at level: the chair asked for a
    phase land, the land deferred it as ticket and exited 0 with nothing done, and the chair backed off."""
    if phase_record.get("land_at") in land.LAND_AT:
        return land.granularity(phase_record)
    from core.landing import resolve_land_at  # lazy: core sits beside agent_tools in one venv

    path = work_dir / initiative / "initiative.md"
    text = _read_text_or_none(path)
    values = _initiative_land_at({path: text}) if text is not None else {}
    try:
        return resolve_land_at(_team_cartridge(profile)[0], values.get(initiative))
    except ValueError:
        return land.granularity(phase_record)


def _chair_reported_repos_path(runs_dir: Path) -> Path:
    """Beside `chair.lease.json`: the persisted set of repo paths already reported missing."""
    return runs_dir / "chair.reported_repos.json"


def _read_reported_repos(runs_dir: Path) -> set[str]:
    raw = _json_or(_read_text_or_none(_chair_reported_repos_path(runs_dir)), [])
    return set(raw) if isinstance(raw, list) else set()


def _write_reported_repos(runs_dir: Path, repos: list[str]) -> None:
    _chair_reported_repos_path(runs_dir).write_text(json.dumps(sorted(set(repos))), encoding="utf-8")


def _live_by_host(lanes: Sequence[run_store.Lane], local: str) -> dict[str, int]:
    """Live lanes per host name; a lane with no host, or on `local`, counts under the empty name."""
    names = [("" if lane.host in (None, local) else lane.host) for lane in lanes]
    return {name: names.count(name) for name in dict.fromkeys(names)}


def _dispatch_counts(lanes: Sequence[run_store.Lane], local: str, listed: Sequence[str], pidfile_live: int) -> dict[str, int]:
    """`lanes` are the store lanes no local pidfile names. Live pidfile runs and lanes on a host the profile does not list count as local, so the total is what `busy_lanes` counted."""
    by_host = _live_by_host(lanes, local)
    return {"": pidfile_live + sum(n for name, n in by_host.items() if name not in listed), **{name: by_host.get(name, 0) for name in listed}}


def _live_by_host_name(lanes: Sequence[run_store.Lane], local: str) -> dict[str, int]:
    """Live lanes per hosts-table row name. `_live_by_host` files this machine's lanes under "", but `cox host beat` names
    this machine's row `local`, so the "" count is re-keyed to `local`. Every live lane counts: the pidfile filter
    `_dispatch_counts` applies exists only because it adds the pidfile runs back, and a host list adds nothing back."""
    return {(local if name == "" else name): n for name, n in _live_by_host(lanes, local).items()}


def _record_beat_wanted(dry_run: bool, lost: str) -> bool:
    """The record file is beaten only by a live run whose lease renewal came back without a refusal."""
    if dry_run:
        return False
    return lost == ""


def _beat_record(runs_dir: Path, session: str, pid: int, host: str, now: datetime.datetime) -> None:
    """Edge. One loop beat of chair.json under the lock: moves `heartbeat_at` and, through the record's window, `until`."""
    with chair.locked(runs_dir):
        record = chair.read(runs_dir)
        moved, _ = chair.beat(record, session, pid, host, now, extend_window=True)
        # `renew_lease` returning no refusal already proves this session, pid and host
        # hold the store lease -- including right after a take_lease reclaim, which
        # rewrites only the lease sidecar (agent_tools/chair_exec.py `_lease`) and never
        # chair.json. `chair.beat` then refuses on a record that still names the prior
        # holder, so `moved` reads None though the lease is genuinely ours; adopt the
        # record here rather than leave heartbeat_at frozen behind a lease we hold.
        if moved is None:
            stamp = now.isoformat()
            moved = {
                "session": session, "pid": pid, "host": host,
                "taken_at": stamp, "heartbeat_at": stamp,
                "runs": [], "claude_session": chair.claude_session_from_env(os.environ),
            }
        chair.write(runs_dir, moved)


def _landed_commits(result: chair_exec.Result) -> list[tuple[str, str, str]]:
    """(task id, commit, land output) for every commit a result put on main. A partial_land lists one per task land in
    `landed_commits`, whatever its own status (an earlier task can land before a later one fails); any other land is
    one landed result with a commit."""
    if "landed_commits" in result:
        return [(c["task_id"], c["commit"], c["reason"]) for c in result["landed_commits"] if c.get("commit")]
    if result["status"] == "landed" and result.get("commit"):
        return [(result["action"].get("task_id", ""), result["commit"], result["reason"])]
    return []


def _land_watches(results: list[chair_exec.Result], tickets: Callable[[], Sequence[Mapping]]) -> list[dict]:
    """One watch per landed commit: one per landed result, or one per task land of a partial_land. A land_phase or
    partial_land names its phase; a plain land takes it from its ticket. The tickets are read only when a land needs them."""
    landed = [(r["action"], *c) for r in results for c in _landed_commits(r)]
    if not landed:
        return []
    phase_of = {(t.get("initiative", ""), t.get("id", "")): t.get("phase", "") for t in tickets()}
    return [
        {
            "initiative": action.get("initiative", ""),
            "phase": action.get("phase") or phase_of.get((action.get("initiative", ""), task_id), ""),
            "repo": action.get("repo", ""),
            "pr": chair_exec._pr_from_reason(reason),
            "commit": commit,
        }
        for action, task_id, commit, reason in landed
    ]


def _run_argv_output(argv: list[str]) -> tuple[int, str]:
    code, out, err = _run_argv(argv)
    return code, out or err


def _rebuild_dash_after_land(
    watches: Sequence[Mapping], dash_dir: str | None, bin_dir: str, toolchain_found: bool,
    run: Callable[[list[str]], tuple[int, str]], echo: Callable[[str], None],
) -> None:
    """Edge. Pull, build and link towpath once per distinct landed repo that is the dash checkout. A failure is one
    echoed line: it never raises, holds no land and does not touch the smoke."""
    for repo in dict.fromkeys(w.get("repo", "") for w in watches):
        try:
            argvs = chair_dash_sync.rebuild_argvs(repo, dash_dir, bin_dir, toolchain_found)
            code = chair_dash_sync.run_argvs(argvs, run) if argvs else 0
            if code != 0:
                echo(f"chair run: towpath rebuild failed (exit {code}) for {repo}")
        except Exception as exc:
            with contextlib.suppress(Exception):
                echo(f"chair run: towpath rebuild failed for {repo}: {exc}")


def _chair_perform_with_smoke(
    runs_dir: Path, perform: chair_run.Perform,
    watch: Callable[[Mapping], None] = lambda _w: None, held: Callable[[], None] = lambda: None,
    tickets: Callable[[], Sequence[Mapping]] = lambda: [],
    *, dash_dir: str | None = None, bin_dir: str = "", toolchain_found: bool = False,
    run: Callable[[list[str]], tuple[int, str]] = _run_argv_output, echo: Callable[[str], None] = lambda _l: None,
) -> chair_run.Perform:
    """Wraps `perform` with the post-land watch and smoke. A live run records a watch for every landed commit, then a
    landed coxswain (or coxswain-tools/-graphs) PR runs the fixed smoke commands once against the first triggering land's repo.
    A failure holds a `HoldRecord` and opens no revert PR: the hold makes `landed_main` report smoke failed for that
    commit, so the next tick's plan emits `revert_land`. A pass clears an existing hold. `held` runs last, to resolve
    the watches whose main went green. A dry run calls neither `watch` nor `held`. With a `dash_dir`, a live run also
    rebuilds towpath after a land in that checkout; the default `dash_dir` of None plans nothing.
    """

    def wrapped(
        actions: list[chair_exec.Action], deps: chair_exec.Deps, current_epoch: Callable[[], int], dry_run_: bool,
    ) -> list[chair_exec.Result]:
        results = perform(actions, deps, current_epoch, dry_run_)
        if not dry_run_:
            watches = _land_watches(results, tickets)
            for w in watches:
                watch(w)
            _rebuild_dash_after_land(watches, dash_dir, bin_dir, toolchain_found, run, echo)
        triggers = chair_exec.smoke_targets(results)
        if triggers:
            land = triggers[0]
            ok, failing = chair_smoke.smoke_verdict(chair_smoke.run_smoke_commands(land["repo"]))
            if not ok:
                chair_smoke.write_hold(str(runs_dir), chair_smoke.hold_record(land, failing))
            elif chair_smoke.read_hold(str(runs_dir)) is not None:
                chair_smoke.clear_hold(str(runs_dir))
        if not dry_run_:
            held()
        return results

    return wrapped


def _gh_repo(repo: str) -> str:
    """`gh --repo` needs owner/name; a land watch carries a checkout path. An unresolvable value passes through."""
    try:
        return route_sync_gh._repo_of(repo)
    except route_sync_gh.RepoError:
        return repo


def _main_ci_reader(runner: Callable[[list[str]], tuple[int, str]]) -> Callable[[str, str], tuple[str, str]]:
    """`read(repo, commit)` is `main_ci` that never raises: a gh failure reads as pending with the error text.
    Results cache per (repo, commit) in `read.cache`, which the caller clears each tick."""
    cache: dict[tuple[str, str], tuple[str, str]] = {}

    def read(repo: str, commit: str) -> tuple[str, str]:
        key = (repo, commit)
        if key not in cache:
            try:
                cache[key] = chair_revert_ports.main_ci(runner, _gh_repo(repo), commit)
            except Exception as exc:  # any gh or parse failure must not escape gather_facts
                cache[key] = ("pending", f"{type(exc).__name__}: {exc}")
        return cache[key]

    read.cache = cache  # type: ignore[attr-defined]
    return read


def _green_since_reader(runner: Callable[[list[str]], tuple[int, str]]) -> Callable[[str, str], bool]:
    """`read(repo, commit)` is `main_green_since` that never raises. Results cache per (repo, commit) in `read.cache`,
    which the caller clears each tick."""
    cache: dict[tuple[str, str], bool] = {}

    def read(repo: str, commit: str) -> bool:
        key = (repo, commit)
        if key not in cache:
            try:
                cache[key] = chair_revert_green.main_green_since(runner, repo, _gh_repo(repo), commit)
            except Exception:  # any gh or git failure must not escape gather_facts
                cache[key] = False
        return cache[key]

    read.cache = cache  # type: ignore[attr-defined]
    return read


class _SlugForge:
    """A `GhForgePort` whose `open_pr` takes the watch's checkout path and hands gh its owner/name."""

    def __init__(self, inner: chair_revert_ports.GhForgePort) -> None:
        self._inner = inner

    def open_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        return self._inner.open_pr(_gh_repo(repo), branch, title, body)

    def wait_checks(self, pr: str) -> list[str]:
        return self._inner.wait_checks(pr)

    def merge(self, pr: str) -> None:
        self._inner.merge(pr)


def _revert_ports(
    runner: Callable[[list[str]], tuple[int, str]],
) -> Callable[[chair_exec.Action], tuple[chair_revert_ports.GitRevertPort, _SlugForge]]:
    return lambda action: (
        chair_revert_ports.GitRevertPort(runner), _SlugForge(chair_revert_ports.GhForgePort(runner)),
    )


def _quarantine_phase_with(
    items: Callable[[], list[dict]], set_state: Callable[[str, str, str, str], object], by: str,
) -> Callable[[str, str, str, str], None]:
    """Quarantines every task of the phase. The reason rides the needs_chair `_revert_land` raises, not the store."""

    def quarantine(initiative: str, phase: str, cause: str, reason: str) -> None:
        for item in items():
            if item.get("initiative") == initiative and item.get("phase") == phase:
                set_state(initiative, item["id"], "quarantined", by)

    return quarantine


def _resolve_land_with(
    read: Callable[[], Sequence[Mapping]], write: Callable[[Mapping], None],
) -> Callable[[str, str, str], None]:
    """`resolve_watch` is keyed by commit; a phase's land is found through its pending watches."""

    def resolve(initiative: str, phase: str, outcome: str) -> None:
        for w in chair_revert_watch.pending_watches(read):
            if w["initiative"] == initiative and w["phase"] == phase:
                chair_revert_watch.resolve_watch(write, w["commit"], outcome)

    return resolve


def _initiative_intakes(initiative_texts: Mapping[Path, str]) -> dict[str, str]:
    """Initiative id (its directory name) to the intake path its `initiative.md` names; one naming none is left out."""
    named = {p.parent.name: intake_state.intake_field(text) for p, text in initiative_texts.items()}
    return {initiative: intake for initiative, intake in named.items() if intake}


def _newest_run_ids(run_ids_by_initiative: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Initiative id to its newest decompose run id. Each list is oldest first, as `read_decompose_runs` returns it."""
    return {initiative: run_ids[-1] for initiative, run_ids in run_ids_by_initiative.items() if run_ids}


def _decompose_initiative_id(intake_id: str, rows: Sequence[Mapping]) -> str:
    """The initiative a decompose of this intake runs under. Once the fresh `id-N` initiative exists the newest live one wins, since `fresh_initiative_id` alone would step past it to `id-N+1`."""
    fresh = route_guard.fresh_initiative_id(intake_id, rows)
    if fresh == intake_id:
        return intake_id
    pattern = re.compile(re.escape(intake_id) + r"-(\d+)")
    live = [
        (int(m.group(1)), row["task_id"])
        for row in rows
        if row.get("kind") == "initiative" and row.get("state") != "dropped"
        for m in [pattern.fullmatch(str(row.get("task_id")))]
        if m
    ]
    return max(live)[1] if live else fresh


def _task_item_counts(tickets: Sequence[Mapping], initiatives: Collection[str]) -> dict[str, int]:
    """Initiative id to its task count, a row with no `kind` being a task. Each id in `initiatives` carries an explicit 0, since a missing id reads as non-empty."""
    counts = collections.Counter(str(t["initiative"]) for t in tickets if t.get("kind", "task") == "task")
    return {**dict.fromkeys(initiatives, 0), **counts}


def _chair_git(args: list[str]) -> str | None:
    """Edge. Stdout of `git <args>`, or None on a non-zero exit, a timeout or a missing git."""
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout if done.returncode == 0 else None


class _CheckoutReports:
    """Owned by one chair process: which (repo, kind) checkout notes were already printed."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def first(self, repo: str, kind: str) -> bool:
        """True the first time (repo, kind) is asked, False every time after."""
        fresh = (repo, kind) not in self._seen
        self._seen.add((repo, kind))
        return fresh


def _checkout_note(repo: Path, run: Callable[[Path, Sequence[str]], tuple[int, str]]) -> tuple[str, str | None]:
    """Edge. Fetch origin's default branch, then sync the checkout: (kind, one-line report or None when silent)."""
    code, out = run(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    default = out.strip().removeprefix("origin/") if code == 0 and out.strip() else "main"
    code, out = run(repo, ["fetch", "--quiet", "origin", f"+refs/heads/{default}:refs/remotes/origin/{default}"])
    if code != 0:
        reason = next((line.strip() for line in out.splitlines() if line.strip()), f"exit code {code}")
        return "fetch_failed", f"checkout not fast-forwarded: fetch of origin/{default} failed: {reason}"
    verdict, line = checkout_sync.sync_checkout(repo, default, run)
    if line is None:
        return verdict, None
    return ("fast_forward_failed" if verdict == "fast_forward" else verdict), line


def _sync_chair_checkouts(
    repos: Sequence[str], reports: _CheckoutReports, echo: Callable[[str], None],
    run: Callable[[Path, Sequence[str]], tuple[int, str]] | None = None,
) -> None:
    """Edge, once per repo per tick. Silent for fast_forward and already_current; any other note prints once per
    (repo, kind) for the life of `reports`. Nothing raised here leaves the tick."""
    runner = run or checkout_sync.git_runner
    for repo in dict.fromkeys(repos):
        try:
            kind, line = _checkout_note(Path(repo), runner)
        except Exception as exc:
            kind, line = "error", f"checkout sync of {repo} failed: {exc}"
        if line is not None and reports.first(repo, kind):
            with contextlib.suppress(Exception):
                echo(f"chair run: {repo}: {line}")


def _chair_float(table: Mapping[str, object], key: str, default: float) -> float:
    """`table[key]` when it is a number of zero or more (a bool is not one), else `default`; zero is a real setting."""
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return default
    return float(value)


def _chair_cache_paths(runs_dir: Path) -> tuple[Path, Path, Path]:
    """The work-file, store-row and decompose-log cache files; `save_cache` writes one cache per path."""
    return runs_dir / "chair-cache.json", runs_dir / "chair-cache-store.json", runs_dir / "chair-cache-decompose.json"


CHAIR_WAKE_POLL_SECONDS = 5.0  # how often the between-tick wait re-reads the bus for a run_exited entry

# Bounds of the process-lifetime read caches, sized from the work they hold so a day of ticks cannot grow memory.
CHAIR_FILES_CACHE_MAX = 4096  # one entry per work file: a few thousand
CHAIR_STORE_CACHE_MAX = 8  # one entry per store read shape: the work rows and the queue rows
CHAIR_DECOMPOSE_CACHE_MAX = 256  # one intake's exit rows, task count and run logs: a few hundred runs at most
CHAIR_DECOMPOSE_INTAKES_MAX = 64  # intakes holding a decompose cache at once; the least recently used is dropped


def _takes_cache(read: Callable[..., list[dict]], cache: object) -> Callable[..., list[dict]]:
    """Edge. `read` with `cache=` bound when it has that parameter, else `read` itself, called as before."""
    try:
        takes = "cache" in inspect.signature(read).parameters
    except (TypeError, ValueError):
        takes = False
    return (lambda *args, **kwargs: read(*args, cache=cache, **kwargs)) if takes else read


def _chair_run_deps(
    runs_dir: Path, profile: dict, session: str, pid: int, host: str, dry_run: bool, echo: Callable[[str], None],
    profile_path: Path, mode: str,
) -> chair_run.RunDeps:
    """Every source has an edge reader; `_ChairUnwired` stays for a source that loses one. A dry run never beats the lease."""
    holder = chair.lease_holder(session, pid, host)
    ws = runs_dir.parent
    home = os.path.expanduser("~")
    harness_dir = profile.get("harness_dir", "")
    harness_python = str(install_root.harness_python(harness_dir)) if harness_dir else "python"

    def now() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    def now_text() -> str:
        return now().strftime("%Y-%m-%dT%H:%M:%SZ")

    snapshot: list[dict] = []  # edge state: `beat` empties it, so one tick's three docket readers share one read
    warned_hosts: list[frozenset[str]] = [frozenset()]  # edge state: the hosts `beat` has already warned are overloaded

    def warn_overloaded_hosts() -> None:
        # A failed hosts read must not break the beat: warn nothing and keep the warned set.
        try:
            warned_hosts[0] = chair_load_warn.warn_overloaded(run_store.hosts(runs_dir), echo, warned_hosts[0])
        except (*run_store._DB_ERRORS, OSError, RuntimeError, ValueError, TypeError):
            pass

    runner = chair_revert_ports.bounded(chair_exec.run_argv)  # gh and git of the revert path never wait past the bound

    main_ci = _main_ci_reader(runner)  # edge state: `beat` clears its cache, one gh read per land per tick
    green_since = _green_since_reader(runner)  # edge state: `beat` clears its cache, one read per land per tick
    # edge state for the whole process: each open land PR's check bodies, kept a few minutes so a round's `gh` reads are mostly hits
    lands_checks = chair_read_idle_lands.cached_checks(chair_read_idle_lands.gh_checks, {})

    # edge state: `beat` empties it, so one tick's window, weekly and source readers share one meter read and verdict
    meter_snapshot: list[tuple[usage_meter.Meter | None, str]] = []

    # edge state: `beat` empties it, so the orchestration fallback notice prints once per tick
    fallback_noticed: list[bool] = []

    # edge state: `beat` empties it, so one tick reads each work file, the store's work rows, the attempts and the
    # stranded inputs once and every reader that needs one shares it
    tick_reads: dict[str, object] = {}

    def once(key: str, read: Callable[[], object]) -> object:
        if key not in tick_reads:
            tick_reads[key] = read()
        return tick_reads[key]

    # One windowed chair_actions read per tick, shared by the gather, held() and resolve_land. The window reaches back
    # `chair.stale_days` at least; revert_watch rows are kept at any age by `_KEPT_KINDS`, so an open watch is never lost.
    actions_window = max(
        chair_read_stale.ACTIONS_WINDOW_DAYS, chair_facts.resolve_stale_days((profile.get("chair") or {}).get("stale_days"))
    )
    actions = lambda: once(  # noqa: E731 -- the one reader `FactsDeps.actions` and the watch readers share
        "chair_actions", lambda: chair_read_stale.read_chair_actions(runs_dir, now(), actions_window)
    )

    # Process-lifetime, unlike `tick_reads`; `load_cache` fills them from the previous chair's stop.
    files_path, store_path, decompose_path = _chair_cache_paths(runs_dir)
    files_cache = chair_read_cache.StampCache(CHAIR_FILES_CACHE_MAX)
    store_cache = chair_read_cache.StampCache(CHAIR_STORE_CACHE_MAX)
    queue_cache = chair_read_cache.StampCache(CHAIR_STORE_CACHE_MAX)  # not saved: queue rows carry claims
    if not dry_run:
        chair_read_cache.load_cache(files_cache, files_path)
        chair_read_cache.load_cache(store_cache, store_path)
    # `read_decompose_runs` drops every entry its call did not touch, so each intake gets its own cache.
    decompose_caches: collections.OrderedDict[str, object] = collections.OrderedDict()

    def decompose_cache_for(intake_id: str):
        if intake_id not in decompose_caches:
            cache = chair_read_cache.StampCache(CHAIR_DECOMPOSE_CACHE_MAX)
            if not dry_run:
                chair_read_cache.load_cache(cache, decompose_path)  # the first call for this intake prunes what it does not use
            decompose_caches[intake_id] = cache
            while len(decompose_caches) > CHAIR_DECOMPOSE_INTAKES_MAX:
                decompose_caches.popitem(last=False)
        decompose_caches.move_to_end(intake_id)
        return decompose_caches[intake_id]

    def save_caches() -> None:
        chair_read_cache.save_cache(files_cache, files_path)
        chair_read_cache.save_cache(store_cache, store_path)
        if decompose_caches:  # none read this run: leave the saved file for the next chair
            merged = chair_read_cache.StampCache(CHAIR_DECOMPOSE_CACHE_MAX * len(decompose_caches))
            for cache in decompose_caches.values():
                chair_read_cache.load_entries(merged, chair_read_cache.dump_entries(cache))  # type: ignore[arg-type]
            chair_read_cache.save_cache(merged, decompose_path)

    def work_files() -> Mapping[Path, str]:
        return once("files", lambda: chair_read_quarantined.read_work_files(ws, files_cache))  # type: ignore[return-value]

    def store_rows() -> list:
        return once("store_rows", lambda: run_store.work_items(runs_dir, None, store_cache) if mode == "store" else [])  # type: ignore[return-value]

    def attempts() -> list[dict]:
        return once("attempts", lambda: chair_read_attempts.read_attempts(ws, work_files()))  # type: ignore[return-value]

    def stranded_inputs() -> tuple[list[dict], list[dict]]:
        return once("stranded", lambda: _chair_stranded_inputs(ws, mode, work_files(), store_rows()))  # type: ignore[return-value]

    def work_rows() -> list[dict]:
        # `store_rows` (run_store.work_items) carries no `kind` column; the queue read does. Store mode only, once per tick.
        return once("work_rows", lambda: _takes_cache(run_store.read_queue, queue_cache)(runs_dir) if mode == "store" else [])  # type: ignore[return-value]

    def ticket_items() -> list[dict]:
        return once("tickets", lambda: chair_read_docket._work_items(ws, mode, work_files(), store_rows()))  # type: ignore[return-value]

    def decompose_runs_of(path: str, initiative: str = "") -> list:
        iid = chair_exec.decompose_id(path, chair_exec.read_intake_id(ws, path))
        if isinstance(iid, chair_exec.Refusal):
            return []
        # The launch decomposes under a fresh id when a dropped initiative holds the intake id. The dropped one asking
        # for itself (`initiative == iid`) keeps its own runs.
        under = iid if initiative == iid else _decompose_initiative_id(iid, work_rows())
        return chair_decompose_runs.read_decompose_runs(Path(runs_dir), under, decompose_cache_for(under))

    def initiative_intakes() -> dict[str, str]:
        return _initiative_intakes(chair_read_quarantined.initiative_files(ws, work_files()))

    def item_counts() -> dict[str, int]:
        # An initiative naming an intake is `decomposed` (or `landed`, which holds items) by `intake_state.intake_group`.
        return _task_item_counts(ticket_items(), initiative_intakes())

    def initiative_states() -> dict[str, str]:
        # The store row of an initiative is keyed by its own id with no phase; the rows are the tick's cached read.
        return {r["initiative"]: r["state"] for r in store_rows() if r["task_id"] == r["initiative"] and not r["phase"]}

    def decomposed_intake() -> dict[str, str]:
        # Only the initiatives with zero task items can be reported by `empty_decompose_facts`, so only their decompose
        # runs are read: no run read for an initiative that has tasks. Keyed by the initiative's directory name, the key
        # tickets and live runs use; the run list is read under the intake's own decompose id, which may differ from it.
        intakes = initiative_intakes()
        empty = [initiative for initiative, count in item_counts().items() if count == 0]
        return _newest_run_ids({i: [r.run_id for r in decompose_runs_of(intakes[i], i)] for i in empty})

    def land_at_map() -> dict[str, str]:
        # Initiative id to its resolved land_at; an initiative with no value reads ticket.
        return once(  # type: ignore[return-value]
            "land_at",
            lambda: chair_facts.land_at_facts(
                _team_cartridge(profile)[0],
                _initiative_land_at(chair_read_quarantined.initiative_files(ws, work_files())),
                sorted({t["initiative"] for t in ticket_items()}),
            ),
        )

    def docket() -> dict:
        if not snapshot:
            busy = chair_read_docket._busy_lanes(ws / "runs", now_text())
            snapshot.append(
                chair_facts.docket_by_land_at(ticket_items(), busy, _local_max_in_flight(runs_dir, profile, host), land_at_map())
            )
        return snapshot[0]

    def beat() -> object:
        snapshot.clear()
        tick_reads.clear()
        main_ci.cache.clear()
        green_since.cache.clear()
        meter_snapshot.clear()
        fallback_noticed.clear()
        lost = "" if dry_run else chair.renew_lease(runs_dir, session, pid, host)
        if _record_beat_wanted(dry_run, lost):
            _beat_record(runs_dir, session, pid, host, now())
        warn_overloaded_hosts()
        return lost

    def live_initiatives() -> list[str]:
        return chair_read_live.read_live_initiatives(runs_dir, [row["id"] for row in docket()["initiatives"]], now_text())

    def approved() -> list:
        # The land's runs come from `land_records`: a code or ticket cause hides a record from the stranded report,
        # but an approved item still lands from the run holding its approval.
        records, items = stranded_inputs()
        unsettled = chair_read_approved.read_unsettled_runs(runs_dir, records)
        stranded = chair_read_stranded.read_stranded(chair_read_approved.land_records(records, unsettled), items)
        facts = chair_read_approved.read_fetch_facts(runs_dir, stranded)
        return chair_read_approved.with_runs(
            chair_read_approved.read_approved(ws, mode, work_files(), store_rows()), stranded, facts,
        )

    last_missing_repos: list[str] = []  # edge state: the current tick's `missing_repos()` result, read back by `reported_repos()`

    def missing_repos() -> list[str]:
        last_missing_repos[:] = chair_read_stranded.read_missing_repos(*stranded_inputs())
        return last_missing_repos

    def reported_repos() -> set[str]:
        """Returns the set reported as of the *previous* tick, then persists this tick's `missing_repos()`
        as the new reported set, so a still-missing repo is not reported again and one that reappears
        as present is dropped. A dry run never persists, matching `beat`'s own rule."""
        previous = _read_reported_repos(runs_dir)
        if not dry_run:
            _write_reported_repos(runs_dir, last_missing_repos)
        return previous

    def epoch() -> int:
        lease = chair._read_lease(runs_dir, holder)
        return lease["epoch"] if lease is not None else -1  # no lease matches no action's epoch, so all are fenced

    record = chair_read_record.recorder(runs_dir, epoch, now_text, store=store_cli.runner(runs_dir), holder=holder)

    def has_patch(initiative: str, task: str) -> bool:
        """`read_has_patch` needs a run id: the newest attempt on this task names it. No attempt has no record."""
        run = chair_read_patch.newest_runs(attempts()).get((initiative, task))
        return run is not None and chair_read_patch.read_has_patch(runs_dir, str(run), task)

    profile_text = _read_text_or_none(profile_path) or ""  # the `profile` dict never carries lane_hosts
    parsed_hosts = _profile_lane_hosts(profile_text)
    if isinstance(parsed_hosts, lane_hosts.LaneHostError):
        echo(f"chair run: lane_hosts ignored: {parsed_hosts.message}")
    startup_rows = run_store.hosts(runs_dir)
    startup_hosts = _lane_host_candidates(profile_text, startup_rows)
    unreachable = [] if isinstance(startup_hosts, lane_hosts.LaneHostError) else host_cmd.dispatchable(startup_hosts)[1]
    # The local row is never truly unreachable: its own workspace_dir is the routing profile's, resolved here
    # instead of refused even when its own `cox host beat` never recorded one.
    local_resolved = host_cmd.local_workspace_dir(startup_rows, host, str(profile.get("workspace_dir", "")))
    unreachable = [name for name in unreachable if not (name == host and local_resolved)]
    if unreachable:
        echo(f"chair run: not dispatching to {', '.join(unreachable)}: no workspace_dir; run `cox host beat` on each")
    shadow = _shadowed_note(profile_text, [str(r["name"]) for r in startup_rows])
    if shadow:
        echo(f"chair run: {shadow}")
    if not dry_run and host_cmd.local_host_missing(startup_rows, host):
        register_argv = host_cmd.local_host_add_argv(host, _chair_max_in_flight(runs_dir, profile), session)
        register_code, register_output = store_cli.runner(runs_dir)(register_argv)
        if register_code != 0:
            # A write failure (missing harness interpreter, unreachable store) must not stop the tick: the
            # loop falls back to the cartridge cap exactly as it does when the table carries no local row.
            echo(f"chair run: could not register this host in the table: store_cli exit {register_code}: {register_output.strip()}")

    def dispatch(row: dict) -> chair_facts.DispatchFacts:
        stamp = now_text()
        pidfile_live, pidfiled = chair_read_docket.local_runs(runs_dir, stamp)
        lanes = run_store.remote_lanes(run_store.live_lanes(runs_dir, stamp), pidfiled)
        host_rows = run_store.hosts(runs_dir)  # per tick: a drain takes effect without a restart
        candidates = _lane_host_candidates(profile_text, host_rows, host)
        # a host with no workspace_dir is refused by `route launch --on`, so the loop must not plan launches onto it
        host_names = [] if isinstance(candidates, lane_hosts.LaneHostError) else host_cmd.dispatchable(candidates)[0]
        configured = chair_capacity.chair_local_lanes(profile)
        effective = chair_orchestrate.effective_local_lanes(configured, host_names)  # no usable host: build locally this tick
        if configured == "decompose" and effective == "any" and not fallback_noticed:
            fallback_noticed.append(True)
            echo(f"chair run: {chair_orchestrate.FALLBACK_NOTICE}")
        return chair_facts.dispatch_facts(
            row, host_names, _dispatch_counts(lanes, host, host_names, pidfile_live), host_cmd.row_capacities(host_rows),
            weight=host_cmd.row_weights(host_rows), capabilities=host_cmd.row_capabilities(host_rows), local=host,
            local_lanes=effective,
        )

    weekly_reset = usage_window.parse_weekly_reset(profile.get("weekly_reset"))

    def meter_fresh_now() -> tuple[usage_meter.Meter | None, str]:
        """One meter read and one source verdict per tick, shared by `window`, `weekly` and both source
        labels, so a label always names the side its figure was built from."""
        if not meter_snapshot:
            chosen = _chair_meter(usage_meter.read(), now(), runs_dir)
            _reconcile_ceilings(*chosen, runs_dir)  # once per tick, before `window` or `weekly` reads a ceiling
            meter_snapshot.append(chosen)
        return meter_snapshot[0]

    def window() -> pacing.Window:
        meter, source = meter_fresh_now()
        now_ = now()
        if meter is not None and meter.five_hour is None:
            return usage_meter.unlimited_window(now_)  # no five-hour limit on this plan; the label still names the meter
        ceiling = usage_meter.implied_ceiling("five_hour", now_)
        if ceiling is None:
            ceiling = profile.get("window_ceiling_usd")
        estimate_window = usage_window.gather(runs_dir, now_, ceiling_usd=ceiling)
        usable = (
            usage_meter.usable_percentage("five_hour", meter.five_hour, meter.observed_at, now_, Path(runs_dir), ceiling)
            if meter is not None else None
        )
        meter_window = (
            usage_meter.as_window(
                dataclasses.replace(meter.five_hour, used_percentage=usable), now_, datetime.timedelta(hours=5),
            )
            if usable is not None else None
        )
        if source == "meter":
            usage_meter.record_implied_ceiling("five_hour", meter.five_hour, Path(runs_dir), now_)
        return usage_meter.prefer(meter_window, estimate_window)

    def window_source() -> str:
        return meter_fresh_now()[1]

    def weekly() -> pacing.Window:
        meter, source = meter_fresh_now()
        now_ = now()
        ceiling = usage_meter.implied_ceiling("weekly", now_)
        if ceiling is None:
            ceiling = profile.get("weekly_ceiling_usd")
        estimate_weekly = usage_window.gather_weekly(
            runs_dir, now_, ceiling,
            store_spend=lambda since: run_store.cost_since(runs_dir, since),
            reset=weekly_reset,
        )
        usable = (
            usage_meter.usable_percentage("weekly", meter.seven_day, meter.observed_at, now_, Path(runs_dir), ceiling)
            if meter is not None else None
        )
        meter_weekly = (
            usage_meter.as_window(
                dataclasses.replace(meter.seven_day, used_percentage=usable), now_, datetime.timedelta(days=7),
            )
            if usable is not None else None
        )
        if source == "meter":
            usage_meter.record_implied_ceiling("weekly", meter.seven_day, Path(runs_dir), now_)
        return usage_meter.prefer(meter_weekly, estimate_weekly)

    def weekly_source() -> str:
        return meter_fresh_now()[1]

    def round_intake() -> Sequence[str]:
        return once(  # type: ignore[return-value]
            "intake", lambda: chair_read_intake.read_intake(ws, chair_read_quarantined.initiative_files(ws, work_files()))
        )

    def round_live_initiatives() -> list[str]:
        return once("live_initiatives", live_initiatives)  # type: ignore[return-value]

    def idle_live(probe: Sequence[str]) -> list[str]:
        """Live ids among `probe`: the round's docket read answers for its initiatives, only the rest are probed."""
        known = {row["id"] for row in docket()["initiatives"]}
        wanted = set(probe)
        extra = sorted(wanted - known)
        found = wanted & set(round_live_initiatives())
        return sorted(found | set(chair_read_live.read_live_initiatives(runs_dir, extra, now_text()) if extra else []))

    def idle_stall(n: datetime.datetime) -> chair_facts.IdleStallInputs:
        return chair_facts.idle_stall_inputs(
            chair_read_idle_backlog.read_idle_backlog(
                ws, mode, _local_max_in_flight(runs_dir, profile, host), n, work_files(), store_rows(), actions(),
                queued=round_intake(), items=ticket_items(), live=idle_live, land_at=land_at_map(),
            ),
            chair_read_idle_hosts.read_idle_hosts(runs_dir, startup_hosts),
            chair_read_idle_lands.read_lands_waiting(runs_dir, n, lands_checks),
            chair_read_idle_open.read_idle_open(ws, actions()),
            chair_facts.resolve_idle_stall_minutes((profile.get("chair") or {}).get("idle_stall_minutes")),
        )

    sources_configured, profile_stale = chair_read_intake.sources_configured_keeping_last_good(profile_path)
    repo_for = _repo_for(ws, home)
    lane_capacity = _check_capacity(runs_dir, profile, host)

    def wrongly_released() -> list:
        files, at = work_files(), land_at_map()
        bodies = [  # the work items carry frontmatter fields only, so the note is read from the file text
            {**i, "body": route.parse_frontmatter(files.get(ws / "work" / str(i["initiative"]) / str(i["file"]), ""))[1]}
            for i in ticket_items() if i.get("state") == "ready"
        ]
        merged = _wrongly_released_merged(
            _approved_branch_merged(runner, repo_for, runs_dir), runs_dir, lambda: _live_runs(runs_dir, now_text()), at
        )
        return chair_facts.wrongly_released_facts(bodies, merged, at)

    facts_deps = chair_facts.FactsDeps(
        approved_branch_merged=_approved_branch_merged(runner, repo_for, runs_dir),
        wrongly_released=wrongly_released,
        lease=lambda: chair_read_lease.read_lease(runs_dir, now()),
        window=window,
        weekly=weekly,
        window_source=window_source,
        weekly_source=weekly_source,
        policy=lambda: _resolved_pacing_policy(runs_dir),
        month=lambda: _profile_month(runs_dir, profile, now(), _resolved_pacing_policy(runs_dir).min_elapsed_fraction),
        docket=docket,
        approved=approved,
        phase_tasks=lambda: chair_read_approved.read_phase_tasks(ws, mode, work_files(), store_rows()),
        phase_records=lambda tasks: chair_read_phase_records.read_phase_records(runs_dir, tasks),
        quarantined=lambda: chair_read_quarantined.read_quarantined(ws, mode, work_files(), store_rows()),
        held_files=lambda: queue_export.read_held_record(ws),
        stranded=lambda: chair_read_stranded.read_stranded(*stranded_inputs()),
        queue=work_rows if mode == "store" else None,
        stranded_records=(lambda: stranded_inputs()[0]) if mode == "store" else None,
        missing_repos=missing_repos,
        reported_repos=reported_repos,
        run_exited=lambda: chair_read_exits.exits(runs_dir),
        attempts=attempts,
        has_patch=has_patch,
        live_initiatives=live_initiatives,
        intake=round_intake,
        work_store_ready=lambda: chair_read_docket.work_store_ready(docket()),
        sources_configured=sources_configured,
        profile_stale=profile_stale,
        session=session, pid=pid, host=host, dispatch=dispatch,
        drafts=lambda: draft_list.count_drafts(
            draft_list.read_drafts(ws / "work", now_text(), chair_read_quarantined.initiative_files(ws, work_files()))
        ),
        remote_unfetched=lambda: chair_read_remote_unfetched.read_remote_unfetched(
            runs_dir, [row["id"] for row in docket()["initiatives"]]
        ),
        lost_runs=lambda: chair_read_lost.read_lost_runs(runs_dir, now_text()),
        tickets=ticket_items,
        landing_records=lambda: _landing_records(runs_dir),
        decomposed_intake=decomposed_intake,
        item_counts=item_counts,
        initiative_states=initiative_states,
        work_rows=work_rows,
        repos=lambda: _initiative_repos(chair_read_quarantined.initiative_files(ws, work_files()), home),
        cartridge=lambda: _team_cartridge(profile)[0],  # {} when unreadable: land_at then reads the default
        land_at_values=lambda: _initiative_land_at(chair_read_quarantined.initiative_files(ws, work_files())),
        actions=actions,
        land_watches=lambda: chair_revert_watch.pending_watches(actions),
        read_main_ci=main_ci,
        green_since=green_since,
        land_hold=lambda: chair_smoke.read_hold(str(runs_dir)),
        land_outcomes=lambda: chair_revert_watch.outcomes(actions),
        decompose_runs=decompose_runs_of,
        newest_run_host=lambda: run_store.newest_run_hosts(runs_dir, [row["id"] for row in docket()["initiatives"]]),
        history=lambda: chair_read_housekeeping.read_last_housekeeping(runs_dir),
        housekeeping_hours=lambda: (profile.get("chair") or {}).get("housekeeping_hours"),
        stale_days=lambda: (profile.get("chair") or {}).get("stale_days"),
        max_launches_per_hour=lambda: (profile.get("chair") or {}).get("max_launches_per_hour"),
        merge_queue=lambda: profile.get("merge_queue"),
        shared_file_limit=lambda: (profile.get("chair") or {}).get("max_shared_file_initiatives"),
        serial_files=lambda: profile.get("serial_files"),
        host_load_ceiling=lambda: (profile.get("chair") or {}).get("host_load_ceiling"),
        stale_candidates=lambda n: chair_read_stale.read_stale_candidates(ws, n, work_files(), attempts(), actions()),
        stall_candidates=lambda n: chair_read_stall.read_stall_candidates(runs_dir, startup_hosts, n),
        idle_stall=idle_stall,
        hosts=lambda: run_store.hosts(runs_dir),
        pid_probe=lambda: chair_pid_probe.read_pid_probe(
            runs_dir, now_text(), chair_exec.fenced_fetch(_pid_probe_ssh, dry_run), () if isinstance(parsed_hosts, lane_hosts.LaneHostError) else parsed_hosts, host
        ),
    )
    exec_deps = chair_exec.edge_deps(
        runs_dir, ws, session, pid,
        run_id=chair_read_run_id.make_run_id(runs_dir), repo_for=repo_for,
        record=record,
        host=host,
        harness_python=harness_python,
        log_retention_days=run_logs.retention_days(profile),
        ids_mode=profile.get("ids", "slug"),
        dry_run=dry_run,
        lane_capacity=lane_capacity,
    )
    exec_deps = dataclasses.replace(
        exec_deps,
        label=session,
        revert_ports=_revert_ports(runner),
        quarantine_phase=_quarantine_phase_with(
            lambda: chair_facts.read_ticket_items(ws, mode),
            lambda i, t, s, b: store_cli.set_state(runs_dir, i, t, s, b),
            holder,
        ),
        resolve_land=_resolve_land_with(actions, record),
        rerun_main=lambda repo, commit: chair_revert_ports.rerun_infra(runner, _gh_repo(repo), commit),
        launch_stagger_s=chair_exec.stagger_seconds((profile.get("chair") or {}).get("launch_stagger_s")),
    )
    def held() -> None:
        """Resolves each pending watch the smoke hold does not name, when its commit is green on main or main went green after it."""
        for w in chair_revert_watch.pending_watches(actions):
            (ci, _) = main_ci(w["repo"], w["commit"])
            hold = chair_smoke.read_hold(str(runs_dir))
            unheld = hold is None or hold["land"]["commit"] != w["commit"]
            if unheld and (ci == "green" or green_since(w["repo"], w["commit"])):
                chair_revert_watch.resolve_watch(record, w["commit"], "held")

    stop_lands = chair_run.no_lands_to_stop
    lands_in_flight = chair_run.no_lands_in_flight
    if not dry_run:
        # Lands run behind the tick on the land worker (tools #1287), which beats only the chair lease while one runs:
        # the tick's own `beat` also clears this tick's fact caches, which a worker thread must never touch.
        sink = chair_run.land_sink(lambda: chair.renew_lease(runs_dir, session, pid, host), released=lambda: chair.honour_release_request(runs_dir, session, pid, host))
        from agent_tools import chair_land_sink

        def spawn_land(argv: list[str]) -> int:
            return subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            ).pid

        lands = chair_land_sink.ProcessLands(
            runs_dir / "chair-lands", sink,
            lambda action, code, output: chair_exec.land_outcome(action, exec_deps, code, output),
            land_limit.repo_land_limit, spawn_land, chair.pid_alive, host, time.time,
        )
        exec_deps = dataclasses.replace(exec_deps, lands=lands)
        facts_deps = dataclasses.replace(facts_deps, landing=lands.landing)
        stop_lands = sink.stop  # the thread sink only: a land process is never signalled
        # Land processes outlive the chair and a new chair adopts them from the state dir; only thread lands die with it.
        lands_in_flight = lambda: bool(lands.held_here())  # noqa: E731
    # A dry run never releases a real lease, so it passes no release check.
    release_requested = None if dry_run else (lambda: chair.honour_release_request(runs_dir, session, pid, host))
    checkout_state = lambda: chair_restart.read_checkout_state(chair_restart.checkout_root(), _chair_git)  # noqa: E731
    checkout_reports = _CheckoutReports()  # edge state for the whole process: each note prints once per repo
    changed_files = lambda old, new: chair_restart.changed_paths(chair_restart.checkout_root(), old, new, _chair_git)  # noqa: E731
    chair_table = profile.get("chair") or {}
    restart_path = chair_restart_log.record_path(runs_dir)

    def record_restart(reason: str) -> None:
        previous = chair_restart_log.read_record(restart_path)
        chair_restart_log.write_record(restart_path, chair_restart_log.bump(previous, reason, now()))

    restart_note = "" if dry_run else chair_restart_log.restart_note(chair_restart_log.read_record(restart_path), now())

    git_workspace = workspace_git_on(profile)

    def pull_and_sync() -> str:
        """The live tick's pull (when the workspace is in git), then the checkout sync of each repo the initiatives
        name; the sync never raises."""
        try:
            return workspace_sync.pull_workspace(ws) if git_workspace else ""
        finally:
            try:
                repos = _initiative_repos(chair_read_quarantined.initiative_files(ws, work_files()), home).values()
                _sync_chair_checkouts([r for r in repos if Path(r).is_dir()], checkout_reports, echo)
            except Exception as exc:
                if checkout_reports.first("", "repos"):
                    with contextlib.suppress(Exception):
                        echo(f"chair run: checkout sync skipped: {exc}")

    def sync_lane_checkouts_now() -> list[dict]:
        """The needs_chair actions for lane host checkouts left alone; the repos are the initiatives' that exist here."""
        repos = _initiative_repos(chair_read_quarantined.initiative_files(ws, work_files()), home).values()
        return lane_checkout_sync.sync_lane_checkouts(parsed_hosts, [r for r in repos if Path(r).is_dir()], str(ws), _lane_sync_ssh)

    has_lane_hosts = not isinstance(parsed_hosts, lane_hosts.LaneHostError) and bool(parsed_hosts)

    started_checkout = checkout_state()
    dev_install = started_checkout is not None and started_checkout.on_branch  # the condition that arms the restart rule
    towpath = shutil.which("towpath")
    dash_wiring = {
        "dash_dir": str(chair_restart.checkout_root().parent / "coxswain-dash"),
        "bin_dir": os.path.dirname(towpath) if towpath else os.path.join(home, ".local", "bin"),
        "toolchain_found": shutil.which("cargo") is not None,
    } if dev_install else {}

    def read_bus() -> list[dict]:
        return courier.entries(_read_text_or_none(ws / "courier.jsonl") or "")  # a missing file is an empty bus

    # A dry run and a missing workspace keep the plain sleep, so polling is unchanged there.
    wake = None if dry_run or not ws.is_dir() else chair_wake.wait_for_run_exit(read_bus, time.sleep, time.monotonic, CHAIR_WAKE_POLL_SECONDS)

    chat = None if dry_run or not ws.is_dir() else _chat_deps(ws)

    return chair_run.RunDeps(
        chat=chat,
        wait=wake,
        checkout_state=checkout_state, started_checkout=started_checkout, lands_in_flight=lands_in_flight,
        stop_lands=stop_lands, facts_deps=facts_deps, gather=_chair_gather(ws), exec_deps=exec_deps, report_deps=chair_report.Deps(echo=echo),
        beat=beat, current_epoch=epoch,
        lease_beat=lambda: chair.renew_lease(runs_dir, session, pid, host),
        holds=lambda: chair._read_lease(runs_dir, holder) is not None,
        release=lambda: chair.release_lease(runs_dir, session, pid, host), release_requested=release_requested,
        sleep=time.sleep, now=now,
        perform=_chair_perform_with_smoke(
            runs_dir, chair_exec.perform,
            watch=lambda w: chair_revert_watch.record_watch(record, w), held=held,
            tickets=lambda: chair_facts.read_ticket_items(ws, mode), echo=echo, **dash_wiring,
        ),
        export_rows=(lambda ids: _chair_export_hook(runs_dir, ids, git_workspace)) if mode == "store" and not dry_run else None,
        notify_config=notify_core.config_from_profile(profile), workspace=ws,
        pull_workspace=None if dry_run else pull_and_sync,
        lane_checkout_actions=sync_lane_checkouts_now if has_lane_hosts and not dry_run else None,
        changed_files=changed_files,
        restart_window_s=_chair_float(chair_table, "restart_window_s", 120.0),
        restart_max_defer_s=_chair_float(chair_table, "restart_max_defer_s", 600.0),
        restart_note=restart_note,
        record_restart=None if dry_run else record_restart,
        save_cache=None if dry_run else save_caches,
    )


def _chair_run(a: argparse.Namespace) -> int:
    """Refuses before any beat while a fact source or recorder is unwired, so a chair that cannot act never holds the lease."""
    as_holder = bool(getattr(a, "as_holder", False))
    if as_holder and not a.dry_run:
        print("chair run: refusing to start, no lease taken; --as-holder needs --dry-run")
        return 2
    profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    last: collections.deque[str] = collections.deque([""], maxlen=1)

    def echo(line: str) -> None:
        chair_report.echo_line(line)
        last.append(line)

    provider, problem = _lake_provider(a)
    if problem:  # an unreadable provider profile would read as mode "files" and relaunch landed work
        print(f"chair run: refusing to start, no lease taken; lake: {problem}")
        return 2
    deps = _chair_run_deps(
        runs_dir, profile, _holder_label(a), os.getpid(), socket.gethostname(), a.dry_run, echo,
        _profile_path(a), work_state.work_state_mode(provider),
    )
    deps = dataclasses.replace(deps, force_holder=as_holder)
    missing = _chair_unwired_sources(deps)
    if missing:
        print(f"chair run: refusing to start, no lease taken; not wired: {', '.join(missing)}")
        return 2
    chair_run.run(a.once, a.interval, a.dry_run, deps, a.max_overruns)
    if a.once and deps.holds():  # a one-shot process exits here, so its lease must not outlive it
        deps.release()
    if not a.once and deps.restart_state.moved is not None:
        return chair_restart.RESTART_EXIT_CODE
    if not a.once and chair_run.overrun_limit_hit(deps.overrun_state.consecutive, a.max_overruns):
        print(f"chair run: stopping after {deps.overrun_state.consecutive} facts-stage overruns in a row; exit 3 for the supervisor")
        return 3
    return _chair_once_exit(last[0]) if a.once else 0


def _chair_default_environment_file() -> str | None:
    garage = Path("~/.config/agent-tools/garage.env").expanduser()
    return str(garage) if garage.is_file() else None


def _chair_service_environment_file(a: argparse.Namespace) -> str | None:
    if a.environment_file:
        return os.path.abspath(Path(a.environment_file).expanduser())
    return _chair_default_environment_file()


@dataclasses.dataclass(frozen=True)
class _ChairUnitRefusal:
    """Why no chair unit text could be built; `message` is printed as is."""

    message: str


@dataclasses.dataclass(frozen=True)
class _ChairUnitInputs:
    """The absolute, systemd-safe values a chair unit is built from."""

    cox: str
    workspace_dir: str
    profile: str
    path: str
    env_file: str | None


def _chair_unit_inputs(label: str | None, env_file: str | None, profile_path: Path) -> _ChairUnitInputs | _ChairUnitRefusal:
    """Edge-light. Resolves cox, the profile, PATH and the env file, or says why not; writes and prints nothing.
    A `label` of None means the unit has none, so there is no label to check."""
    cox = shutil.which("cox")
    if cox is None:
        return _ChairUnitRefusal("chair service: cox is not on PATH")
    text = _read_text_or_none(profile_path)
    if text is None:
        return _ChairUnitRefusal(f"routing: no profile at {profile_path}")
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return _ChairUnitRefusal(f"routing: profile unreadable: {exc}")
    if not profile.get("workspace_dir", ""):
        return _ChairUnitRefusal(f"routing: workspace_dir not set in profile {profile_path}")
    harness_dir = profile.get("harness_dir")
    # The tools venv: the coxswain checkout's, else (an unmigrated host) the tools checkout's own.
    tools_root = install_root.checkout_root(Path(__file__).parent) or Path(__file__).resolve().parents[1]
    unit_path_value = chair_service.path_value(
        os.path.abspath(cox),
        str(install_root.venv_python(tools_root).parent),
        str(install_root.harness_python(harness_dir).parent) if harness_dir else "",
        os.environ.get("PATH", ""),
    )
    # systemd does not expand `~/` in WorkingDirectory=, and the unit does not inherit this shell's
    # $AGENT_TOOLS_PROFILE, so every path is absolute and the profile is pinned in ExecStart.
    values = {
        "cox": os.path.abspath(cox),
        "workspace_dir": os.path.abspath(Path(profile["workspace_dir"]).expanduser()),
        **({"--label": label} if label is not None else {}),
        "--profile": os.path.abspath(profile_path),
        **({"--environment-file": env_file} if env_file is not None else {}),
        "PATH": unit_path_value,
    }
    unsafe = chair_service.unsafe_values(values)
    if unsafe:
        return _ChairUnitRefusal(
            f"chair service: systemd would split or expand {', '.join(unsafe)}; use only letters, digits and . _ @ + : , / = -"
        )
    exports = chair_service.export_lines(_read_text_or_none(Path(env_file)) or "") if env_file is not None else []
    if exports:
        numbers = ", ".join(str(n) for n in exports)
        return _ChairUnitRefusal(
            f"chair service: {env_file} has export lines ({numbers}) that EnvironmentFile= may skip; pass --environment-file with NAME=value lines"
        )
    return _ChairUnitInputs(values["cox"], values["workspace_dir"], values["--profile"], unit_path_value, env_file)


def _chair_unit_text(label: str, interval: int | str, env_file: str | None, profile_path: Path) -> str | _ChairUnitRefusal:
    """Edge-light. The unit text for these arguments, or the refusal; install and `setup migrate` both build it here."""
    resolved = _chair_unit_inputs(label, env_file, profile_path)
    if isinstance(resolved, _ChairUnitRefusal):
        return resolved
    return chair_service.unit_text(
        resolved.cox, resolved.workspace_dir, label, interval, env_file, resolved.profile, resolved.path,
    )


def _lander_unit_text(a: argparse.Namespace, ws: Path) -> str | None:
    """Edge-light. The lander's unit text, built from the same resolved cox, PATH and profile as the chair's; None after
    printing why when systemd would read one of them differently from this shell."""
    from agent_tools import lander_cli

    env_file = _chair_service_environment_file(a)
    resolved = _chair_unit_inputs("lander", env_file, _profile_path(a))
    if isinstance(resolved, _ChairUnitRefusal):
        print(resolved.message)
        return None
    return lander_cli.unit_text(resolved.cox, str(ws), int(a.interval), resolved.profile, resolved.path, env_file)


def _chair_service_install(a: argparse.Namespace, path: Path) -> int:
    """Refuses before any write when systemd would read a value differently from the shell that ran the install."""
    text = _chair_unit_text(_holder_label(a), a.interval, _chair_service_environment_file(a), _profile_path(a))
    if isinstance(text, _ChairUnitRefusal):
        print(text.message)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(path)
    print("\n".join(chair_service.systemctl_lines()))
    return 0


def _chair_service_host_steps(a: argparse.Namespace, profile: Mapping, runs_dir: Path) -> list | int:
    """The install steps for `a.host`, or an exit code after printing why there are none."""
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.host), None)
    if row is None:
        print(f"unknown host: {a.host}")
        return 2
    root = str(Path(profile["workspace_dir"]).expanduser().parent)
    workspace_remote = _origin_url(str(Path(profile["workspace_dir"]).expanduser())) or ""
    return chair_service_host.install_steps(
        str(row["ssh"]), root, _chair_service_host_repos(profile), workspace_remote, _holder_label(a), _chair_heads(profile),
    )


def _chair_service_host_print(steps: list) -> int:
    for argv, run in steps:
        line = shlex.join(argv)
        print(line if run else f"{line}  # run by hand")
    return 0


def _chair_service_host_apply(steps: list) -> int:
    """Runs the runnable steps in order; stops at the first non-zero exit and prints its output, then
    prints every step still left (runnable or not) as a line for a person to run by hand."""
    for i, (argv, run) in enumerate(steps):
        if not run:
            print(f"{shlex.join(argv)}  # run by hand")
            continue
        done = subprocess.run(argv, capture_output=True, text=True)
        if done.returncode != 0:
            print((done.stdout + done.stderr).strip())
            for left_argv, _left_run in steps[i + 1 :]:
                print(f"{shlex.join(left_argv)}  # run by hand")
            return done.returncode
    return 0


def _chair_service_host_install(a: argparse.Namespace, profile: Mapping, runs_dir: Path) -> int:
    steps = _chair_service_host_steps(a, profile, runs_dir)
    if isinstance(steps, int):
        return steps
    if not getattr(a, "apply", False):
        return _chair_service_host_print(steps)
    return _chair_service_host_apply(steps)


def _chair_service_host_ssh_line(ssh: str, cmd: str) -> str | None:
    """The stdout of `ssh ssh cmd`, or None on a non-zero exit or a 30s timeout. Never raises."""
    try:
        done = subprocess.run(["ssh", ssh, cmd], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    return done.stdout if done.returncode == 0 else None


def _chair_service_host_status(a: argparse.Namespace, runs_dir: Path) -> int:
    """Prints `chair_service_host.status_line` for `a.host`; an unknown name prints one line and exits 2."""
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.host), None)
    if row is None:
        print(f"unknown host: {a.host}")
        return 2
    ssh = str(row["ssh"])
    unit = _chair_service_host_ssh_line(ssh, "systemctl --user is-active coxswain-chair.service")
    unit_state = unit.strip() if unit is not None else None
    runs_top = _chair_service_host_ssh_line(ssh, "cox runs top --once")
    chair_line = runs_top.splitlines()[0] if runs_top else None
    tick = _chair_service_host_ssh_line(ssh, "tail -1 repos/workspace/runs/chair-loop.log")
    last_tick = tick.strip() if tick else None
    print(chair_service_host.status_line(a.host, unit_state, chair_line, last_tick))
    return 0


def _chair_service(a: argparse.Namespace) -> int:
    """Edge for the unit text in `chair_service`. Never runs systemctl; `--install` wins when both flags are given."""
    path = chair_service.unit_path(Path.home())
    host = getattr(a, "host", None)
    if a.install and host:
        profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
        if refuse_rc is not None:
            return refuse_rc
        return _chair_service_host_install(a, profile, runs_dir)
    if a.install:
        return _chair_service_install(a, path)
    if a.status and host:
        _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
        if refuse_rc is not None:
            return refuse_rc
        return _chair_service_host_status(a, runs_dir)
    if a.status:
        text = _read_text_or_none(path)
        if text is None:
            print(f"no unit at {path}")
        else:
            print(text, end="")
        return 0
    with contextlib.suppress(SystemExit):
        build_parser().parse_args(["chair", "service", "--help"])
    return 0


_DECISION_PREFIX = "coxswain://decision/"


def _decision_bus(a: argparse.Namespace) -> tuple[Path, str, list[dict], str] | None:
    """The courier path, its raw text, its latest-per-id entries and the profile's operator; None when no workspace resolves."""
    profile = _courier_profile(a)
    if profile is None: return None
    path = Path(profile["workspace_dir"]).expanduser() / "courier.jsonl"
    blob = _read_text_or_none(path) or ""
    return path, blob, courier.entries(blob), settings_model.operator_name(profile)


def _new_decision_id(taken: set[str]) -> str | None:
    """Up to five draws; None when every one collides with a taken ref."""
    drawn = (f"d-{secrets.token_hex(4)}" for _ in range(5))
    return next((d for d in drawn if f"{_DECISION_PREFIX}{d}" not in taken), None)


def _decision_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _chair_ask(a: argparse.Namespace) -> int:
    bus = _decision_bus(a)
    if bus is None: return 2
    path, blob, entries, operator = bus
    problem = decisions.validate_options(a.option)
    if problem: print(f"chair ask: {problem}"); return 2
    decision_id = _new_decision_id({e["ref"] for e in entries})
    if decision_id is None: print("chair ask: could not draw an unused decision id"); return 2
    entry = decisions.make_ask_entry(
        a.question, a.option, a.context, _holder_label(a), decision_id, _decision_now(), operator=operator
    )
    path.write_text(courier.append_line(blob, entry), encoding="utf-8")
    print(decision_id)
    return 0


def _chair_answer(a: argparse.Namespace) -> int:
    bus = _decision_bus(a)
    if bus is None: return 2
    path, blob, entries, operator = bus
    asks, _ = decisions.inbox_split(entries, operator)
    ask_entry = next((e for e in asks if e["ref"] == f"{_DECISION_PREFIX}{a.id}"), None)
    problem = decisions.validate_answer(ask_entry, a.option)
    if problem: print(f"chair answer: {problem}"); return 2
    answer = decisions.make_answer_entry(
        ask_entry, a.option, answered_at=_decision_now(), message_id=uuid.uuid4().hex, operator=operator
    )
    path.write_text(courier.ack(courier.append_line(blob, answer), ask_entry["id"]), encoding="utf-8")
    return 0


def _decision_line(d: dict) -> str:
    answer = f" -> {d['answer']}" if d["answer"] is not None else ""
    return f"{d['id']}  {d['asked_at']}  {d['question']}  [{' | '.join(d['options'])}]{answer}"


def _chair_decisions(a: argparse.Namespace) -> int:
    bus = _decision_bus(a)
    if bus is None: return 2
    _, _, entries, operator = bus
    asks, answers = decisions.inbox_split(entries, operator)
    listed = decisions.filter_decisions(decisions.merge_decisions(asks, answers), open_only=a.open, answered_only=a.answered)
    if a.json: print(json.dumps(listed))
    else:
        for d in listed: print(_decision_line(d))
    return 0


def _chair_lease_row(runs_dir: Path) -> dict | None:
    """Edge, read only (`run_store._open` is `mode=ro`). The whole chair lease row, `status` included; None with no store or no row.

    A store error is raised, not read as no lease: `read_lease` and `read_chair_rows` fold it into None, and a false "no lease"
    sends the operator to take a held lease. `_chair_attach` turns the raise into its one-line reason."""
    opened = run_store._open(runs_dir)
    if opened is None:
        return None
    conn, mark = opened
    try:
        row = conn.execute(f"SELECT * FROM leases WHERE name = {mark}", (chair.LEASE_NAME,)).fetchone()
    finally:
        conn.close()
    return None if row is None else dict(row)


def _attach_time(value: object) -> datetime.datetime | None:
    """Pure. An aware datetime from a datetime or ISO text, naive read as UTC; None when absent or unparseable."""
    try:
        at = value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=datetime.UTC)


def _attach_inputs(row: Mapping) -> tuple[object, datetime.datetime | None] | None:
    """Pure. `(HolderLease, last_tick)` from a lease row; None when the holder is not `label@host:pid` or the expiry does not parse.

    Only the chair loop writes a `tick_at` into the `status` cell (`chair_tick_status`), and a Claude session holder is
    `label@host:pid` too, so a tick marks a loop. A row with no `status` column cannot say, so it counts as a loop and
    the pid and tick checks decide; a store that old must not answer "not a loop" for a live one."""
    label, _, rest = str(row.get("holder") or "").partition("@")
    host, _, pid = rest.rpartition(":")
    expires_at = _attach_time(row.get("expires_at"))
    if not (label and host and pid.isdigit() and expires_at):
        return None
    doc = run_store._json_cell(row.get("status")) if row.get("status") else None
    last_tick = _attach_time(doc.get("tick_at")) if isinstance(doc, dict) else None
    return chair_holder.HolderLease(label, int(pid), host, expires_at, "status" not in row or last_tick is not None), last_tick


def _is_chair_run_command(command: str) -> bool:
    """Pure. True when the argv has `chair` directly followed by `run`, whatever the executable or the holder label."""
    return " chair run " in f" {' '.join(command.split())} "


def _pid_command(pid: int) -> str:
    """Edge. The command line of `pid` from `ps`; empty when it cannot be read."""
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _is_service_cgroup(text: str) -> bool:
    """Pure. True when the cgroup text of a process names the coxswain chair service unit."""
    return chair_service.UNIT_NAME in text


def _pid_cgroup(pid: int) -> str:
    """Edge. The cgroup text of `pid` from proc; empty when it cannot be read (no proc on macOS, or the pid is gone)."""
    try:
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _chair_attach_lend(a: argparse.Namespace, on: bool, clock: Callable[[], datetime.datetime] | None = None) -> int:
    """Edge. Lend or recall the routine ticks on chair.json; only the live holder may, anyone else is refused with exit 1."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    host = socket.gethostname()
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return 1
        now = (clock or (lambda: datetime.datetime.now(datetime.UTC)))()
        state = chair.liveness(record, _leader_pid_alive(record), now, host, _leader_heartbeat_minutes())
        if record is None or state != "live" or not chair.is_caller_holder(record, os.environ, host):
            print(f"chair: not lent: this session is not the live holder (lease {state})")
            return 1
        chair.write(runs_dir, chair.lend_routine(record, on))
    print("chair: routine ticks lent" if on else "chair: routine ticks recalled")
    return 0


def _chair_attach(
    a: argparse.Namespace,
    read_row: Callable[[Path], dict | None] | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    clock: Callable[[], datetime.datetime] | None = None,
    pid_command: Callable[[int], str] | None = None,
) -> int:
    """The explicit holder check. Reads the lease, this host, the clock and the pid; `chair_holder_verdict` decides.

    Exit 0 and the holder line when a loop is live; exit 1 and the reason when none is. It writes only under
    `--lend-routine` and `--recall-routine`, through `_chair_attach_lend`."""
    lend, recall = getattr(a, "lend_routine", False), getattr(a, "recall_routine", False)
    if lend or recall:
        return _chair_attach_lend(a, bool(lend), clock)
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    try:
        row = (read_row or _chair_lease_row)(runs_dir)
    except (*run_store._DB_ERRORS, RuntimeError, OSError, ValueError) as exc:  # an unreachable Postgres, psycopg absent, a bad store
        print(f"no live chair loop: store unreadable ({type(exc).__name__}: {' '.join(str(exc).split())})")
        return 1
    held = None if row is None or not row.get("holder") else _attach_inputs(row)
    if held is None:
        print("no live chair loop: " + ("no lease" if row is None or not row.get("holder") else "holder unreadable"))
        return 1
    lease, last_tick = held
    this_host = socket.gethostname()
    alive = (pid_alive or chair.pid_alive)(lease.pid) if lease.host == this_host else None
    if not lease.is_loop and alive and _is_chair_run_command((pid_command or _pid_command)(lease.pid)):
        lease = dataclasses.replace(lease, is_loop=True)  # a loop whose tick never landed: the process says what it is, not the label
    verdict = chair_holder.chair_holder_verdict(
        lease, this_host, (clock or (lambda: datetime.datetime.now(datetime.UTC)))(), last_tick, alive, 3 * _CHAIR_RUN_INTERVAL
    )
    print(chair_holder.format_holder_verdict(verdict))
    return 1 if verdict.kind == "none" else 0


def _watchdog_paths(platform: str, home: Path) -> list[Path]:
    """The files a watchdog timer lives in on this platform."""
    if platform == "darwin":
        return [home / "Library" / "LaunchAgents" / chair_service.WATCHDOG_PLIST_NAME]
    unit_dir = chair_service.unit_path(home).parent
    return [unit_dir / chair_service.WATCHDOG_SERVICE_NAME, unit_dir / chair_service.WATCHDOG_TIMER_NAME]


def _watchdog_install_plan(platform: str, home: Path, resolved: _ChairUnitInputs, minutes: int) -> list[tuple[Path, str]]:
    """Each file to write, in `_watchdog_paths` order, with its text."""
    if platform == "darwin":
        texts = [chair_service.watchdog_plist_text(resolved.cox, resolved.profile, resolved.path, minutes)]
    else:
        texts = [
            chair_service.watchdog_service_text(resolved.cox, resolved.profile, resolved.path, resolved.env_file),
            chair_service.watchdog_timer_text(minutes),
        ]
    return list(zip(_watchdog_paths(platform, home), texts, strict=True))


def _watchdog_activation_lines(platform: str, plist: Path) -> list[str]:
    """Text for the user to run; nothing here is executed."""
    if platform == "darwin":
        return [f"launchctl bootstrap gui/$(id -u) {plist}"]
    return ["systemctl --user daemon-reload", f"systemctl --user enable --now {chair_service.WATCHDOG_TIMER_NAME}"]


def _chair_watchdog_install(a: argparse.Namespace) -> int:
    """Edge. Writes the watchdog timer files and prints the lines that activate them; runs no launchd or systemd tool."""
    if a.interval_minutes < 1:
        print("refusing: --interval-minutes must be at least 1")
        return 2
    resolved = _chair_unit_inputs(None, _chair_default_environment_file(), _profile_path(a))
    if isinstance(resolved, _ChairUnitRefusal):
        print(resolved.message)
        return 2
    plan = _watchdog_install_plan(sys.platform, Path.home(), resolved, a.interval_minutes)
    for path, text in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(path)
    print("\n".join(_watchdog_activation_lines(sys.platform, plan[0][0])))
    return 0


def _chair_watchdog_uninstall() -> int:
    """Edge. Removes the watchdog timer files that exist; runs no launchd or systemd tool."""
    # The platform's own paths, so uninstall undoes exactly what install wrote on this machine.
    present = [p for p in _watchdog_paths(sys.platform, Path.home()) if p.exists()]
    for path in present:
        path.unlink()
        print(f"removed {path}")
    if not present:
        print("no watchdog timer installed")
    return 0


def _chair_watchdog(a: argparse.Namespace) -> int:
    """Edge. One check of the chair lease and status rows; the notify state file is the one `chair run` uses."""
    if a.install:
        return _chair_watchdog_install(a)
    if a.uninstall:
        return _chair_watchdog_uninstall()
    profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    return chair_watchdog.run_watchdog(
        runs_dir, notify_config=notify_core.config_from_profile(profile), state_path=runs_dir.parent / "notify-sent.json",
        idle_minutes=a.idle_minutes, stale_minutes=a.stale_minutes, dry_run=a.dry_run,
    )


def _chair_freeze_lands(a: argparse.Namespace) -> int:
    """Edge. Prints the entry's text; exit 1 when that text is the usage line."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    text = chair_land_freeze.run_freeze(runs_dir, datetime.datetime.now(datetime.UTC), a.minutes, a.reason, a.off)
    print(text)
    return 1 if text.endswith(chair_land_freeze.USAGE) else 0


def _landing_records(runs_dir: Path) -> list[dict]:
    """Edge. The store's landing records for the chair's facts; empty, with a note, when they cannot be read."""
    from agent_tools import lander_ports
    return lander_ports.read_records(runs_dir, lambda line: print(line, file=sys.stderr))


def _lander_once(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli._once(a)


def _lander_run(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli._run(a)


def _lander_status(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli._status(a)


def _lander_adopt(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli._adopt(a)


def _lander_backlog(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli._backlog(a)


def _lander_service(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli._service(a)


_PROFILE_ARG = commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"})
LANDER_GROUP = commands.Group(
    name="lander", help="the landing worker: merges built phases and lands whole initiatives",
    description=(
        "The landing worker. It runs beside the chair and owns the land queue: a built phase is merged into its "
        "initiative branch, and a whole initiative goes to its parent as one pull request."
    ),
    epilog="examples:\n  cox lander status\n  cox lander adopt\n  cox lander adopt --apply\n  cox lander once\n  cox lander run --interval 30\n  cox lander service --install",
)
LANDER_COMMANDS = [
    commands.Command(
        "once", "lander", "one pass: queue what is ready, then merge and land within the caps",
        (_PROFILE_ARG,), "agent_tools.cli:_lander_once", False, (), defaults={"profile": None},
    ),
    commands.Command(
        "run", "lander", "pass after pass until interrupted",
        (commands.Arg(("--interval",), {"type": float, "default": 30.0, "help": "seconds between passes (default: 30)"}), _PROFILE_ARG),
        "agent_tools.cli:_lander_run", False, (), defaults={"profile": None},
    ),
    commands.Command(
        "status", "lander", "every initiative in flight with its phases, and the land queue with how long each item has waited",
        (commands.Arg(("--json",), {"action": "store_true", "help": "print the status as JSON"}), _PROFILE_ARG),
        "agent_tools.cli:_lander_status", False, (), defaults={"profile": None},
    ),
    commands.Command(
        "adopt", "lander", "bring initiatives that were in flight before the landing record under it; a dry run unless --apply",
        (
            commands.Arg(("initiative",), {"nargs": "*", "help": "initiative ids (default: every initiative with no landing record)"}),
            commands.Arg(("--apply",), {"action": "store_true", "help": "do it; without this flag only the report is printed"}),
            _PROFILE_ARG,
        ),
        "agent_tools.cli:_lander_adopt", False, (), defaults={"profile": None},
        description=(
            "For each initiative with no landing record: work already on the parent is marked done, an approval off "
            "its phase branch is carried onto it, one that cannot be found goes back to ready, the initiative branch "
            "is kept or cut, and every built phase is merged into it.\nNothing is deleted, reset or rebased, and an "
            "initiative that has a record is skipped, so it is safe to run twice."
        ),
    ),
    commands.Command(
        "backlog", "lander", "mark the tasks a hand-folded pull request carried as done, once it has merged; a dry run unless --apply",
        (
            commands.Arg(("--manifest",), {"required": True, "help": "the backlog manifest (prereq-backlog-manifest.json)"}),
            commands.Arg(("--pr",), {"required": True, "help": "the pull request that carried the manifest's tasks (URL)"}),
            commands.Arg(("--apply",), {"action": "store_true", "help": "do it; refused until the forge says the pull request merged"}),
            _PROFILE_ARG,
        ),
        "agent_tools.cli:_lander_backlog", False, (), defaults={"profile": None},
        description=(
            "Each task the manifest lists as applied moves from approved to done.\nA task already done, or in any other "
            "state, is skipped, so it is safe to run twice."
        ),
    ),
    commands.Command(
        "service", "lander", "write or show the systemd user unit; never runs systemctl",
        (
            commands.Arg(("--install",), {"action": "store_true", "help": "write the unit and print the systemctl lines to run"}),
            commands.Arg(("--status",), {"action": "store_true", "help": "print the unit as it is on disk"}),
            commands.Arg(("--interval",), {"type": int, "default": 30, "help": "seconds between passes (default: 30)"}),
            commands.Arg(("--environment-file",), {"help": "an EnvironmentFile for the unit (default: the chair's)"}),
            _PROFILE_ARG,
        ),
        "agent_tools.cli:_lander_service", False, (), defaults={"profile": None},
    ),
]

CHAIR_GROUP = commands.Group(
    name="chair", help="the chair loop that runs the landing and launch ticks itself",
    description="The chair loop that runs the landing and launch ticks itself.",
    epilog='examples:\n  cox chair run --once --dry-run\n  cox chair run --interval 30\n  cox chair service --install\n  cox chair ask "ship it?" --option yes --option no',
)
CHAIR_COMMANDS = [
    commands.Command(
        "run", "chair", "beat, gather, plan, perform and report every tick until interrupted",
        (
            commands.Arg(("--once",), {"action": "store_true", "help": "run one tick, release the lease, exit 1 if the tick errored"}),
            commands.Arg(("--interval",), {"type": float, "default": _CHAIR_RUN_INTERVAL, "help": "seconds between ticks (default: 60)"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "plan and report each tick without taking the lease or performing any action"}),
            commands.Arg(("--as-holder",), {"action": "store_true", "help": "with --dry-run, plan as the lease holder would while the loop holds the lease; takes no lease and performs nothing"}),
            commands.Arg(("--max-overruns",), {"type": int, "default": 3, "help": "exit 3 after this many facts-stage overruns in a row (default: 3; 0 disables)"}),
            commands.Arg(("--label",), {"help": "the chair's holder label (default: $COX_SESSION_LABEL, else unlabeled)"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        "agent_tools.cli:_chair_run", False, (),
        defaults={"profile": None},
        description=(
            "Each tick beats the chair lease, gathers facts, plans, performs and reports one status line.\n"
            "It yields to `cox route chair take --steal`: actions planned under a lost lease are fenced.\n"
            "It hard-stops at the profile's weekly_hard_stop_fraction of the weekly ceiling."
        ),
    ),
    commands.Command(
        "service", "chair", "write or show the systemd user unit that keeps `cox chair run` alive",
        (
            commands.Arg(("--install",), {"action": "store_true", "help": "write the unit file and print the systemctl lines to run; runs nothing itself"}),
            commands.Arg(("--status",), {"action": "store_true", "help": "print the unit file, or say there is none"}),
            commands.Arg(("--label",), {"help": "the chair's holder label (default: $COX_SESSION_LABEL, else unlabeled)"}),
            commands.Arg(("--interval",), {"type": int, "default": 60, "help": "seconds between ticks (default: 60)"}),
            commands.Arg(("--environment-file",), {"help": "env file the unit loads (default: ~/.config/agent-tools/garage.env when it exists)"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming workspace_dir, pinned in the unit's ExecStart (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--host",), {"help": "a hosts-table name: install or report on that lane host over ssh"}),
            commands.Arg(("--apply",), {"action": "store_true", "help": "with --install --host, run the steps instead of printing them"}),
        ),
        "agent_tools.cli:_chair_service", False, (),
        description=(
            "--install writes ~/.config/systemd/user/coxswain-chair.service and prints the systemctl lines.\n"
            "It does not run systemctl."
        ),
    ),
    commands.Command(
        "ask", "chair", "put a question with its options to the operator on the courier bus",
        (
            commands.Arg(("question",)),
            commands.Arg(("--option",), {"action": "append", "default": [], "help": "an answer the operator may give; repeat for each, at least two"}),
            commands.Arg(("--context",), {"default": "", "help": "text or a URL that helps the operator decide"}),
            commands.Arg(("--label",), {"help": "the chair's label (default: $COX_SESSION_LABEL, else unlabeled)"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_chair_ask", False, (),
    ),
    commands.Command(
        "attach", "chair", "say whether a live chair loop holds the lease; exit 0 if so, 1 if not",
        (
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--lend-routine",), {"action": "store_true", "help": "mark the routine ticks lent in chair.json; the live holder only"}),
            commands.Arg(("--recall-routine",), {"action": "store_true", "help": "remove the lent mark from chair.json; the live holder only"}),
        ),
        "agent_tools.cli:_chair_attach", False, (),
        description=(
            "Reads the chair lease and the loop's last tick.\n"
            "It writes only under --lend-routine and --recall-routine, which set or clear routine_lent in chair.json for the live holder.\n"
            "It never takes, steals or releases the lease."
        ),
        exclusive=("--lend-routine", "--recall-routine"),
    ),
    commands.Command(
        "answer", "chair", "answer an open decision with one of its options",
        (commands.Arg(("id",)), commands.Arg(("option",)), commands.Arg(("--profile",))),
        "agent_tools.cli:_chair_answer", False, (),
    ),
    commands.Command(
        "decisions", "chair", "list decisions asked of the operator and their answers",
        (
            commands.Arg(("--open",), {"action": "store_true", "help": "only decisions without an answer"}),
            commands.Arg(("--answered",), {"action": "store_true", "help": "only decisions with an answer"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_chair_decisions", False, (),
        exclusive=("--open", "--answered"),
    ),
    commands.Command(
        "watchdog", "chair", "one-shot loop check for a five-minute timer; reads only the store",
        (
            commands.Arg(("--idle-minutes",), {"type": int, "default": 30, "help": "minutes of idle status lines before an alert (default: 30)"}),
            commands.Arg(("--stale-minutes",), {"type": int, "default": 10, "help": "minutes without a lease beat before the loop is down (default: 10)"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print each alert and push nothing"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--install",), {"action": "store_true", "help": "write the timer files and print the lines that activate them; runs nothing itself"}),
            commands.Arg(("--uninstall",), {"action": "store_true", "help": "remove the timer files this platform's install wrote"}),
            commands.Arg(("--interval-minutes",), {"type": int, "default": 5, "help": "minutes between checks in the installed timer (default: 5)"}),
        ),
        "agent_tools.cli:_chair_watchdog", False, (),
        exclusive=("--install", "--uninstall"),
        description=(
            "A one-shot check meant for a five-minute timer.\n"
            "It reads only the store and exits 0 whether or not it pushed.\n"
            "It exits 1 only when the store cannot be read.\n"
            "--install and --uninstall only write or remove files: no check runs and no launchd or systemd tool does."
        ),
    ),
    commands.Command(
        "freeze-lands", "chair", "hold every land for a release cut, show the freeze, or lift it",
        (
            commands.Arg(("--for",), {"type": int, "dest": "minutes", "metavar": "MINUTES", "help": "minutes the freeze lasts; needs --reason"}),
            commands.Arg(("--reason",), {"metavar": "TEXT", "help": "why lands are frozen; needs --for"}),
            commands.Arg(("--off",), {"action": "store_true", "help": "lift the freeze now; not with --for or --reason"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        "agent_tools.cli:_chair_freeze_lands", False, (),
        defaults={"profile": None},
        description=(
            "With --for and --reason, freeze lands until the minutes pass.\n"
            "With --off, lift the freeze.\n"
            "With neither, show whether lands are frozen.\n"
            "A freeze that outlives its minutes lifts itself."
        ),
    ),
]

ROUTER_GROUP = commands.Group(
    name="router", help="the routing-profile flag and the tier decision it gates",
    description="The routing-profile flag and the tier decision it gates.",
    epilog="examples:\n  cox router select --role build\n  cox router select --role build --json",
)
ROUTER_COMMANDS = [
    commands.Command(
        "select", "router", "the effective tier for a role under the profile's router: off|shadow|on flag",
        (
            commands.Arg(("--role",), {"required": True}),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile and the router flag (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_router_select", False, (),
    ),
]

STEWARD_GROUP = commands.Group(
    name="steward", help="ceiling-change candidates from stats.db, proposed as intake files",
    description="Ceiling-change candidates from stats.db, proposed as intake files.",
    epilog="examples:\n  cox steward propose\n  cox steward propose --json\n  cox steward draft --json",
)
STEWARD_COMMANDS = [
    commands.Command(
        "propose", "steward", "write each candidate clearing the evidence bar as a new intake file; never edits a provider profile",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_steward_propose", False, (),
    ),
    commands.Command(
        "draft", "steward", "write each grounded proposal in intake as a draft initiative under work/, list those that exist or could not be grounded",
        (
            commands.Arg(("--profile",), {"help": "the routing profile naming the workspace (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_steward_draft", False, (),
    ),
]


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


def _profile_checkout(profile: Mapping) -> str | None:
    """Pure. The one checkout of a monorepo profile: `umbrella_dir` when `harness_dir` lies inside it; None for a
    profile of the component layout, whose umbrella held no code."""
    umbrella, harness = profile.get("umbrella_dir"), profile.get("harness_dir")
    if not umbrella or not harness:
        return None
    root, inner = Path(umbrella).expanduser(), Path(harness).expanduser()
    return str(root) if inner.is_relative_to(root) and inner != root else None


def _cartridges_checkout(profile: Mapping) -> str:
    """The checkout that holds `provider_profile`; `cartridges_dir` when the profile names no provider profile."""
    provider, cartridges = profile.get("provider_profile"), profile.get("cartridges_dir")
    if provider:
        return str(Path(provider).expanduser().parent)
    return str(Path(cartridges).expanduser()) if cartridges else ""


def _host_repos(profile: Mapping) -> list[str]:
    """Edge: the harness, cartridges and tools checkouts, the three paths the chair itself runs from; on a migrated
    host they are folders of one coxswain checkout, named once, at its root."""
    harness = str(Path(profile["harness_dir"]).expanduser()) if profile.get("harness_dir") else ""
    paths = [p for p in (harness, _cartridges_checkout(profile), str(Path(__file__).resolve().parents[1])) if p]
    return install_root.collapse_to_checkouts(paths, {p: install_root.checkout_root(p) for p in paths})


def _repo_is_detached(repo: Path) -> bool:
    """Edge: True when `git symbolic-ref -q HEAD` fails; a path that is no checkout reads as detached too."""
    return subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 0


def _install_roots(profile: Mapping) -> list[str]:
    """Edge: the checkouts the chair runs from: `umbrella_dir`, the monorepo checkout, and the host repos."""
    roots = [profile.get("umbrella_dir"), _profile_checkout(profile), *_host_repos(profile)]
    return [str(r) for r in roots if r]


def _origin_url(path: str) -> str | None:
    """Edge: `git -C path remote get-url origin`, or None when there is no such remote or no checkout there."""
    done = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True)
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


def _parse_login_ok(text: str) -> bool | None:
    """`true`, `false` or `none`, any case; the one place the login verb's tri-state is read."""
    value = {"true": True, "false": False, "none": None}
    if text.strip().lower() not in value:
        raise argparse.ArgumentTypeError(f"login_ok must be true, false or none, not {text!r}")
    return value[text.strip().lower()]


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


HOST_GROUP = commands.Group(
    name="host", help="the hosts table: lane hosts, their capacity, versions and login",
    description="The hosts table: lane hosts, their capacity, versions and login.",
    epilog="examples:\n  cox host add host-a --ssh host-a --capacity 8\n  cox host list\n  cox host beat\n  cox host sync host-a",
    args=(commands.Arg(("--profile",)),),
)
HOST_COMMANDS = [
    commands.Command(
        "add", "host", "add or update a host in the table",
        (
            commands.Arg(("name",)), commands.Arg(("--ssh",), {"required": True}),
            commands.Arg(("--capacity",), {"type": int, "required": True, "help": "lanes the host may run at once"}),
            commands.Arg(("--weight",), {"type": int, "default": 1, "help": "share of new placements"}),
            commands.Arg(("--capabilities",), {"default": "", "help": "comma-separated, e.g. go,rust"}),
        ),
        "agent_tools.cli:_host_add", False, (),
    ),
    commands.Command("list", "host", "one line per host: state, capacity, beat age, login", (), "agent_tools.cli:_host_list", False, ()),
    commands.Command("drain", "host", "stop launching on a host; its live lanes finish", (commands.Arg(("name",)),), "agent_tools.cli:_host_drain", False, ()),
    commands.Command("deploy-only", "host", "receive releases but never lanes", (commands.Arg(("name",)),), "agent_tools.cli:_host_deploy_only", False, ()),
    commands.Command(
        "remove", "host", "delete a drained idle host from the hosts table",
        (commands.Arg(("name",)), commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the row that would be removed; write nothing"})),
        "agent_tools.cli:_host_remove", False, (),
    ),
    commands.Command("activate", "host", "make a host a lane host again", (commands.Arg(("name",)),), "agent_tools.cli:_host_activate", False, ()),
    commands.Command(
        "orchestrate", "host", "show, or turn on or off, orchestration on this machine",
        (commands.Arg(("mode",), {"nargs": "?", "choices": ["on", "off"], "help": "omit to show the current mode"}),),
        "agent_tools.cli:_host_orchestrate", False, (),
    ),
    commands.Command(
        "capacity", "host", "change only a host's capacity, leaving ssh, weight and capabilities as they are",
        (
            commands.Arg(("name",)),
            commands.Arg(("n",), {"type": int, "help": "lanes the host may run at once; 0 suspends new placement"}),
            commands.Arg(("--json",), {"action": "store_true", "help": "print the row as JSON"}),
        ),
        "agent_tools.cli:_host_capacity", False, (),
    ),
    commands.Command(
        "beat", "host", "record this machine's versions and claude login in the table",
        (commands.Arg(("name",), {"nargs": "?", "help": "default: this machine's hostname"}),), "agent_tools.cli:_host_beat", False, (),
    ),
    commands.Command(
        "login", "host", "store a host's login state without touching its beat time",
        (
            commands.Arg(("name",)),
            commands.Arg(("login_ok",), {"type": _parse_login_ok, "metavar": "{true,false,none}"}),
            commands.Arg(("--reason",), {"default": None, "help": "why the check failed"}),
            commands.Arg(("--failures",), {"type": int, "default": 0, "help": "consecutive failed checks"}),
        ),
        "agent_tools.cli:_host_login", False, (),
    ),
    commands.Command("doctor", "host", "the table's line for a host, then its doctor over ssh", (commands.Arg(("name",)),), "agent_tools.cli:_host_doctor", False, ()),
    commands.Command(
        "sync", "host", "bring a host's checkouts to the commits this machine runs (fetch, then fast-forward or check out)",
        (commands.Arg(("name",)),), "agent_tools.cli:_host_sync", False, (),
    ),
]


def _dispatch_fn(target: str | None) -> _LazyHandler | None:
    return None if target is None else _LazyHandler(target)


def _dispatch_row(row: commands.Command) -> commands.Command:
    """The row with its `module:function` handler string, and its children's, bound to a `_LazyHandler`."""
    return dataclasses.replace(row, handler=_dispatch_fn(row.handler), subcommands=tuple(_dispatch_row(c) for c in row.subcommands))


def _dispatch_table(
    table: list[tuple[commands.Group, list[commands.Command]]],
) -> list[tuple[commands.Group, list[commands.Command]]]:
    """Each row and leaf group names its handler as `"dotted.module:function"` data; this binds each one
    to a `_LazyHandler`, so `cox --help` builds the parser without importing any handler's module."""
    return [(dataclasses.replace(group, fn=_dispatch_fn(group.fn)), [_dispatch_row(row) for row in rows]) for group, rows in table]


COMMAND_TABLE: list[tuple[commands.Group, list[commands.Command]]] = _dispatch_table([
    (RUNS_GROUP, RUNS_COMMANDS),
    (COURIER_GROUP, COURIER_COMMANDS),
    (VERSIONS_GROUP, []),
    (CONSOLE_GROUP, []),
    (DASH_GROUP, []),
    (SETTINGS_GROUP, SETTINGS_COMMANDS),
    (INSTALL_GROUP, []),
    (UPGRADE_GROUP, []),
    (HOME_GROUP, []),
    (SESSION_GROUP, []),
    (STATS_GROUP, STATS_COMMANDS),
    (LAKE_GROUP, LAKE_COMMANDS),
    (USAGE_GROUP, USAGE_COMMANDS),
    (PLAN_GROUP, PLAN_COMMANDS),
    (EPIC_GROUP, EPIC_COMMANDS),
    (PR_GROUP, PR_COMMANDS),
    (ROUTE_GROUP, ROUTE_COMMANDS),
    (CHAIR_GROUP, CHAIR_COMMANDS),
    (LANDER_GROUP, LANDER_COMMANDS),
    (HOST_GROUP, HOST_COMMANDS),
    (ROUTER_GROUP, ROUTER_COMMANDS),
    (STEWARD_GROUP, STEWARD_COMMANDS),
    (SETUP_GROUP, SETUP_COMMANDS),
])


def _table_entry(name: str) -> tuple[commands.Group, list[commands.Command]]:
    """`COMMAND_TABLE`'s (Group, rows) pair for `name`, so `build_parser` can
    register each migrated group at its own original position -- interleaved
    among the groups still hand-built -- instead of moving every migrated
    group's registration to wherever the table is folded, which would reorder
    `cox --help`'s top-level listing."""
    return next((group, rows) for group, rows in COMMAND_TABLE if group.name == name)


def _session_launch(launcher_profile: str | None, no_plugin: bool, extra_args: list[str]) -> tuple[list[str], str, str | None] | int:
    """The one place the coxswain session's argv is built, shared by bare `cox`
    and `cox session` so the two cannot drift. Resolves the profile the same
    way `setup doctor` does, via `_profile_path`. Returns (argv, cwd, warning),
    or an exit code. A missing profile is the usual help, exit 2 — this
    launcher needs a real workspace to run in, not a one-liner; an unreadable
    one or a missing `workspace_dir` gets the same one-line reason `route`'s
    own commands print, not swallowed into that help text."""
    profile_path = _profile_path(argparse.Namespace(profile=launcher_profile))
    text = _read_text_or_none(profile_path)
    if text is None:
        build_parser().print_help()
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    if shutil.which("claude") is None:
        print("claude not found on PATH")
        return 2
    skills_roots = profile.get("skills_roots") or []
    plugin_root = None if no_plugin else _plugin_root(skills_roots)
    argv, warning = _launcher_argv(plugin_root, skills_roots, no_plugin, extra_args)
    return argv, str(Path(workspace).expanduser()), warning


def _aware_or_none(value: object) -> datetime.datetime | None:
    """An aware datetime from a datetime or ISO text; naive reads as UTC, anything unparseable as None."""
    try:
        at = value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=datetime.UTC)


def _holder_from_row(row: Mapping[str, object] | None) -> tuple[object, datetime.datetime | None] | None:
    """The chair lease row as (HolderLease, last tick), or None when nobody holds it.

    Only a chair loop writes a `tick_at` into the row's `status` cell, so a holder with one is the loop and a Claude
    session holder is not. A row with no `status` column cannot say, so it counts as a loop, as `_attach_inputs` reads it.
    An expiry that does not parse reads as already expired."""
    holder = str((row or {}).get("holder") or "")
    label, _, rest = holder.partition("@")
    host, _, pid_text = rest.rpartition(":")
    if row is None or not label or not host or not pid_text.isdigit():
        return None
    doc = run_store._json_cell(row.get("status")) if row.get("status") else None
    tick = _aware_or_none(doc.get("tick_at")) if isinstance(doc, dict) and doc.get("tick_at") else None
    expires = _aware_or_none(row.get("expires_at")) or datetime.datetime.fromtimestamp(0, datetime.UTC)
    return chair_holder.HolderLease(label, int(pid_text), host, expires, "status" not in row or tick is not None), tick


def _read_holder_inputs(runs_dir: Path):
    """Edge. (HolderLease, last tick, pid_alive) for the chair lease row; None with no store, no row, or a store error.

    `pid_alive` is None for a holder on another host: this host cannot probe that pid."""
    try:
        opened = run_store._open(runs_dir)
        if opened is None:
            return None
        conn, token = opened
        try:
            row = conn.execute(run_store._sql("SELECT * FROM leases WHERE name = {p}", token), (chair.LEASE_NAME,)).fetchone()
        finally:
            conn.close()
    except (*run_store._DB_ERRORS, RuntimeError, OSError, ValueError):
        return None
    held = _holder_from_row(None if row is None else dict(row))
    if held is None:
        return None
    lease, tick = held
    return lease, tick, chair.pid_alive(lease.pid) if lease.host == socket.gethostname() else None


def _loop_holder(label: str, pid: int, host: str) -> chair_resolve.SessionHolder:
    """Edge. A live chair loop holder; the service form when its cgroup names the chair service (readable only on this host)."""
    return chair_resolve.SessionHolder("loop", label, host == socket.gethostname() and _is_service_cgroup(_pid_cgroup(pid)))


def _live_loop_holder(runs_dir: Path) -> chair_resolve.SessionHolder | None:
    """The live chair loop that holds the chair, or None when the chair may be taken.

    The lease row names a loop by its tick. A service whose row has no tick yet, or one only the runs chair record names,
    is still a loop when its pid is alive on this host and its command line is a chair run command."""
    this_host = socket.gethostname()
    inputs = _read_holder_inputs(runs_dir)
    if inputs is not None:
        lease, tick, alive = inputs
        if not lease.is_loop and alive and _is_chair_run_command(_pid_command(lease.pid)):
            lease = dataclasses.replace(lease, is_loop=True)
        verdict = chair_holder.chair_holder_verdict(
            lease, this_host, datetime.datetime.now(datetime.UTC), tick, alive, float(chair.DEFAULT_LEASE_TTL_SECONDS)
        )
        if verdict.kind != "none":
            return _loop_holder(lease.label, lease.pid, lease.host)
    try:
        record = chair.read(runs_dir)
    except (OSError, ValueError):
        return None
    pid = None if record is None else record.get("pid")
    if isinstance(pid, int) and record is not None and record.get("host") == this_host and chair.pid_alive(pid) and _is_chair_run_command(_pid_command(pid)):
        return _loop_holder(str(record.get("session") or "?"), pid, this_host)
    return None


def _launcher_session_takes(profile_arg: str | None) -> str:
    """The profile's `chair: session_takes:` policy; when_free when the profile cannot be read or does not say."""
    text = _read_text_or_none(_profile_path(argparse.Namespace(profile=profile_arg)))
    try:
        chair_keys = (route.parse_profile(text) if text is not None else {}).get("chair")
    except route.ProfileError:
        chair_keys = None
    return chair_resolve.resolve_session_takes(chair_keys.get("session_takes") if isinstance(chair_keys, dict) else None)


def _launcher(a: argparse.Namespace, extra_args: list[str]) -> int:
    """Bare `cox` (spec §7) and `cox session`: a real Claude Code session with
    the coxswain plugin loaded and the profile's workspace as cwd, off the
    dedicated `launcher_profile` dest. The argv comes from `_session_launch`."""
    launch = _session_launch(a.launcher_profile, a.no_plugin, extra_args)
    if isinstance(launch, int):
        return launch
    argv, workspace, warning = launch
    cwd = workspace
    if warning:
        print(warning)
    runs_dir = Path(workspace).expanduser() / "runs"
    host = socket.gethostname()
    now = datetime.datetime.now(datetime.UTC)
    try:
        record = chair.read(runs_dir)
    except (OSError, ValueError):
        record = None
    state = chair.liveness(record, _leader_pid_alive(record), now, host, _leader_heartbeat_minutes())

    def _transcript_exists(session_id: str, record_cwd: str | None) -> bool:
        resume_cwd = record_cwd if record_cwd and Path(record_cwd).is_dir() else workspace  # where claude --resume will run
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return any(chair_resolve.transcript_path(config, c, session_id).is_file() for c in {resume_cwd, str(Path(resume_cwd).resolve())})

    plan = chair_resolve.resolve_bare(record, state, host, now, _transcript_exists)
    if plan.note:
        print(plan.note, file=sys.stderr if a.print_argv else sys.stdout)
    live = _live_loop_holder(runs_dir)
    policy = _launcher_session_takes(a.launcher_profile)
    holder = live or chair_resolve.SessionHolder({"take": "free", "resume": "stale"}.get(plan.kind, "session"))
    decision = chair_resolve.session_take_decision(policy, holder, False)
    offered = plan.kind == "offer" and live is None  # a live non-loop holder: the offer flow, unless the policy decides
    if live is not None or (decision.action == "observer" and not offered):
        action, notice = "observer", decision.line
    elif offered:
        action = {"always": "takeover", "never": "observer"}.get(policy, plan.default)
        notice = chair_resolve.holder_line(record) if policy == "never" else None
    else:
        action, notice = {"take": "take", "resume": "resume"}.get(plan.kind, plan.default), None
    sid = None  # a take learns its session id from the SessionStart status call; claude's argv stays as it was
    label = f"chair-{now:%Y-%m-%d}"
    if offered and policy == "when_free":
        print(chair_resolve.holder_line(record), file=sys.stderr if a.print_argv else sys.stdout)
        print(chair_resolve.offer_line(plan), file=sys.stderr if a.print_argv else sys.stdout)
        if not a.print_argv:
            action = chair_resolve.choose(_read_key(), plan.default)
    if action == "cancel":
        print("chair: cancelled")
        return 0
    if action in ("resume", "attach"):
        argv = chair_resolve.with_session(argv, "--resume", plan.session_id, extra_args)
        cwd = plan.cwd if plan.cwd and Path(plan.cwd).is_dir() else cwd
        label, sid = plan.label or label, plan.session_id
    if a.print_argv:
        print(argv)
        print(cwd)
        print(chair_resolve.describe(action, plan), file=sys.stderr)  # stdout stays the argv and cwd alone
        return 0
    if notice:
        print(notice)  # a live chair loop holds the chair, or the policy never takes it: this session takes, steals and releases nothing
    if action in ("observer", "attach"):
        os.chdir(cwd)
        os.execvp(argv[0], argv)
        return 0
    # steal=True: chair.take only lets `steal` override a stale/crashed record
    # (agent_tools/chair.py `take`), never a live one, so a prior bare-`cox`
    # session's own now-unbeaten lock is retaken rather than refused on every
    # later invocation, while a LIVE foreign holder is still only printed.
    chair_a = argparse.Namespace(
        profile=a.launcher_profile,
        label=label,
        pid=os.getpid(),
        steal=True,
        cwd=cwd,
        claude_session=sid,
    )
    if action == "takeover":
        took, line = chair_resolve.takeover(runs_dir, label, chair_a.pid, host, sid, cwd, heartbeat_minutes=_leader_heartbeat_minutes())
        print(line)
    else:
        took = _route_chair_take(chair_a) == 0
        if took and action == "resume":
            with chair.locked(runs_dir):  # the SessionStart line `chair: resumed <label>` reads this stamp
                taken = chair.read(runs_dir)
                if taken is not None:
                    chair.write(runs_dir, {**taken, "resumed": True})
    if took:
        runs_dir = Path(workspace).expanduser() / "runs"
        beater_argv = [
            sys.executable, "-m", "agent_tools.chair", "beat-loop",
            "--label", chair_a.label, "--pid", str(chair_a.pid), "--runs-dir", str(runs_dir),
        ]
        try:
            beater = _spawn(beater_argv)
        except OSError as exc:
            print(f"chair: beater failed to start: {exc}")
            chair.release_at_exit(runs_dir, chair_a.label, chair_a.pid, host)  # only what will keep the chair may hold it
            print("chair: released; a non-chair session is starting")
            took = False
        else:
            print(f"chair: beating from pid {beater.pid}")
        # chair_a.label is the label `_route_chair_take` just wrote as the holder above;
        # it is the holder, not a re-read of the file this same call just produced.
        if took:
            _print_inbox(_read_text_or_none(Path(workspace).expanduser() / "courier.jsonl") or "", chair_a.label, chair_a.label)
    os.chdir(cwd)
    os.execvp(argv[0], argv)
    return 0


_TOP_OPTIONS_WITH_VALUE = ("--profile",)
_TOP_FLAGS = ("--no-plugin", "--print-argv")


def _bare_launcher_split(args: list[str]):
    """Splits a bare-`cox` invocation's `-- EXTRA...` tail off before the real
    parser sees it: the top-level subparsers action is a PARSER-style
    positional and would otherwise try to consume the first extra token as an
    (invalid) subcommand name. Returns (head, tail) only when `args`, up to
    any `--`, holds nothing but the launcher's own top-level options; returns
    None the moment a subcommand token (or anything else) appears, so `main`
    leaves that invocation, `--` and all, to the ordinary parser untouched —
    an existing subcommand's own `--` semantics (e.g. `route file -- -title`)
    are not this function's to change."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return args[:i], args[i + 1 :]
        if tok in _TOP_OPTIONS_WITH_VALUE:
            i += 1
            if i >= len(args):
                return None
        elif tok not in _TOP_FLAGS:
            return None
        i += 1
    return args, []


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args and not sys.stdout.isatty():
        return _route_status(argparse.Namespace(profile=None, json=False, all=False))
    split = _bare_launcher_split(args)
    head, tail = split if split is not None else (args, [])
    a = build_parser().parse_args(head)
    if not hasattr(a, "fn"):
        return _launcher(a, tail)
    return a.fn(a)


if __name__ == "__main__":
    # Under `python -m`, this module is `__main__`, not `agent_tools.cli`; the table's "agent_tools.cli:..."
    # handlers would otherwise import a second copy of this file at dispatch.
    sys.modules.setdefault("agent_tools.cli", sys.modules[__name__])
    sys.exit(main())
