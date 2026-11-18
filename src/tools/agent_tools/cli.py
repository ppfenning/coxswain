"""cox — the coxswain's operator tools (alias: agent-tools, removed next release)."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
import sqlite3
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from agent_tools import commands
from core import module_facade


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


# H9 (docs/hardening.md): the handlers live in the part modules below, one command group or stage each. This module
# keeps the command table, the parser and `main`, and answers for every name the parts define (core.module_facade):
# the first read of one imports the parts, so `cox --help` imports none of them, and a write (a test's monkeypatch)
# reaches every part that holds the name.
_PARTS = (
    "agent_tools.cli_runs_fetch",
    "agent_tools.cli_route_sync",
    "agent_tools.cli_land_steps",
    "agent_tools.cli_land_facts",
    "agent_tools.cli_runs",
    "agent_tools.cli_land",
    "agent_tools.cli_board",
    "agent_tools.cli_route",
    "agent_tools.cli_route_launch",
    "agent_tools.cli_courier",
    "agent_tools.cli_leader",
    "agent_tools.cli_launcher",
    "agent_tools.cli_views",
    "agent_tools.cli_host",
    "agent_tools.cli_chair",
    "agent_tools.cli_chair_service",
    "agent_tools.cli_install",
    "agent_tools.cli_setup_store",
    "agent_tools.cli_doctor",
    "agent_tools.cli_migrate",
)


def __getattr__(name: str) -> Any:
    return module_facade.attribute(__name__, name)


# Names the code kept here calls, defined in a part: each imports its part on the first call.
_launcher = _LazyHandler("agent_tools.cli_launcher:_launcher")
_release = _LazyHandler("agent_tools.cli_install:_release")
_route_status = _LazyHandler("agent_tools.cli_route:_route_status")
_setup_tui = _LazyHandler("agent_tools.cli_views:_setup_tui")


card_keys = _LazyModule("agent_tools.card_keys")
bounded_run = _LazyModule("agent_tools.bounded_run")
chair = _LazyModule("agent_tools.chair")
chair_capacity = _LazyModule("agent_tools.chair_capacity")
chair_chat = _LazyModule("agent_tools.chair_chat")
chair_dash_sync = _LazyModule("agent_tools.chair_dash_sync")
chair_decompose_runs = _LazyModule("agent_tools.chair_decompose_runs")
chair_exec = _LazyModule("agent_tools.chair_exec")
chair_facts = _LazyModule("agent_tools.chair_facts")
chair_holder = _LazyModule("agent_tools.chair_holder")
chair_resolve = _LazyModule("agent_tools.chair_resolve")
chair_attach = _LazyModule("agent_tools.chair_attach")
chair_land_freeze = _LazyModule("agent_tools.chair_land_freeze")
chair_load_warn = _LazyModule("agent_tools.chair_load_warn")
chair_login_check = _LazyModule("agent_tools.chair_login_check")
chair_orchestrate = _LazyModule("agent_tools.chair_orchestrate")
chair_pid_probe = _LazyModule("agent_tools.chair_pid_probe")
chair_read_approved = _LazyModule("agent_tools.chair_read_approved")
chair_read_attempts = _LazyModule("agent_tools.chair_read_attempts")
chair_read_branch_merged = _LazyModule("agent_tools.chair_read_branch_merged")
chair_read_cache = _LazyModule("agent_tools.chair_read_cache")
chair_tick_cache = _LazyModule("agent_tools.chair_tick_cache")
chair_read_decompose_miss = _LazyModule("agent_tools.chair_read_decompose_miss")
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
chair_read_run_records = _LazyModule("agent_tools.chair_read_run_records")
chair_read_stale = _LazyModule("agent_tools.chair_read_stale")
chair_read_stall = _LazyModule("agent_tools.chair_read_stall")
chair_read_run_outcomes = _LazyModule("agent_tools.chair_read_run_outcomes")
chair_read_stranded = _LazyModule("agent_tools.chair_read_stranded")
chair_report = _LazyModule("agent_tools.chair_report")
chair_restart = _LazyModule("agent_tools.chair_restart")
chair_restart_log = _LazyModule("agent_tools.chair_restart_log")
chair_revert_green = _LazyModule("agent_tools.chair_revert_green")
chair_revert_ports = _LazyModule("agent_tools.chair_revert_ports")
chair_revert_watch = _LazyModule("agent_tools.chair_revert_watch")
chair_phase_quarantine = _LazyModule("agent_tools.chair_phase_quarantine")
chair_run = _LazyModule("agent_tools.chair_run")
chair_service = _LazyModule("agent_tools.chair_service")
unit_env = _LazyModule("agent_tools.unit_env")
chair_service_host = _LazyModule("agent_tools.chair_service_host")
chair_smoke = _LazyModule("agent_tools.chair_smoke")
chair_unit = _LazyModule("agent_tools.chair_unit")
chair_wake = _LazyModule("agent_tools.chair_wake")
chair_watchdog = _LazyModule("agent_tools.chair_watchdog")
checkout = _LazyModule("agent_tools.checkout")
checkout_sync = _LazyModule("agent_tools.checkout_sync")
cleanup = _LazyModule("agent_tools.cleanup")
completions = _LazyModule("agent_tools.completions")
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
dash_history_store = _LazyModule("agent_tools.dash_history_store")
decisions = _LazyModule("agent_tools.decisions")
doctor = _LazyModule("agent_tools.doctor")
draft_apply = _LazyModule("agent_tools.draft_apply")
draft_list = _LazyModule("agent_tools.draft_list")
epic = _LazyModule("agent_tools.epic")
forge = _LazyModule("agent_tools.forge")
forge_auto = _LazyModule("agent_tools.forge_auto")
forge_github = _LazyModule("agent_tools.forge_github")
generate = _LazyModule("agent_tools.generate")
home_paths = _LazyModule("agent_tools.home")
host_cmd = _LazyModule("agent_tools.host_cmd")
host_load = _LazyModule("agent_tools.host_load")
install = _LazyModule("agent_tools.install")
install_exec = _LazyModule("agent_tools.install_exec")
install_root = _LazyModule("agent_tools.install_root")
intake_fields = _LazyModule("agent_tools.intake_fields")
intake_pull_map = _LazyModule("agent_tools.intake_pull_map")
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
log_doctor = _LazyModule("agent_tools.log_doctor")
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
upgrade_edge = _LazyModule("agent_tools.upgrade_edge")
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
    """Edge. Today's (UTC day of `now`) cost, landed count, completions, cost per completion and first-try share; None
    with no store or one that fails to answer.

    The one place both the docket's `efficiency:` line and the dash feed's `spend.today` read from. `landed` counts the
    tasks the land log shows reaching `mark_done` today. `completions` counts tasks the way the feed's spend_split
    windows do (`completions.day_completions`): a task done, or approved and merged into its phase or PR branch, or
    landed on main, at its earliest such stamp, if that falls today; a logged land the store holds no record of counts
    too. `cost_per_completion` is today's spend over `completions`, and `per_landed` is its deprecated alias, the same
    value, kept one release. The first-try share is still measured over the land log's tasks."""
    today = now[:10]
    day = datetime.date.fromisoformat(today)
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)
    tomorrow = (day + datetime.timedelta(days=1)).isoformat()
    try:
        spend = run_store.cost_since(runs_dir, today, tomorrow)
        if spend is None:
            return None
        landed = route.landed_today(_read_text_or_none(runs_dir / "land.jsonl") or "", today)
        counts = run_store.build_counts(runs_dir, [t for _, t in landed if t is not None], [r for r, _ in landed if r is not None])
        records = dash_history_store.read_task_records_since(runs_dir, today)
    except (*run_store._DB_ERRORS, OSError, RuntimeError, ValueError, TypeError):
        return None
    rate, measured = route.first_try(landed, counts)
    done = completions.day_completions(records, landed, start, start + datetime.timedelta(days=1))
    per_completion = spend / len(done) if done else None
    return {
        "date": today,
        "cost": spend,
        "landed": len(landed),
        "per_landed": per_completion,  # deprecated alias of cost_per_completion, the same value
        "completions": len(done),
        "cost_per_completion": per_completion,
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


# Commands that run before a store is configured, or configure it. Every other command needs the one store.
_STORE_FREE = frozenset({"setup", "install", "upgrade", "versions", "settings", "dev", "release", "home", "plan"})
# Commands in a board group that read only the files they are pointed at, so a machine with no store can run them.
_STORE_FREE_COMMANDS = frozenset({("route", "lint")})
STORE_DOUBLE_ENV = "COXSWAIN_STORE_DOUBLE"  # the suites' sqlite stand-in; the harness reads the same name


def store_refusal(head: Sequence[str], storage: object, environ: Mapping[str, str]) -> str | None:
    """Pure. Why the command line `head` may not run against the store URL `storage`; None when it may.
    The one store is Postgres. No URL, or any other kind, is refused for every command that touches the board."""
    words = [w for w in head if not w.startswith("-")]
    if not words or words[0] in _STORE_FREE or tuple(words[:2]) in _STORE_FREE_COMMANDS or environ.get(STORE_DOUBLE_ENV) == "sqlite":
        return None
    if not isinstance(storage, str) or not storage:
        return "no store is configured: the provider profile needs a storage_url naming a postgresql:// store. Run `cox setup store --store shared`."
    try:
        shared = store_dialect.is_postgres(storage)
    except ValueError:
        shared = False
    return None if shared else (
        "the configured store is not Postgres: sqlite and file-only work state are retired. "
        "Run `cox setup store --store shared`, then `cox setup store switch shared` to carry the old rows over."
    )


def _store_refusal(a: argparse.Namespace, head: Sequence[str]) -> str | None:
    """Edge. `store_refusal` for the profile in use. A machine with no routing profile yet is not refused: it has
    nothing configured to be wrong, and `cox setup` is how it gets one."""
    text = _read_text_or_none(_profile_path(a))
    if text is None:
        return None
    try:
        named = route.parse_profile(text).get("provider_profile")
    except route.ProfileError:
        return None  # the command reports the unreadable profile itself
    provider = store_url.read_provider_profile(named) if named else {}
    return store_refusal(head, provider.get("storage_url"), os.environ)


_EXPORT_STAMP = ".export-stamp"
# Commands that read the board from this machine's files. `chair` is not here: its tick exports for itself.
_BOARD_READERS = frozenset({"route", "runs", "lander", "epic", "console", "dash"})
_BOARD_WRITERS = frozenset({("route", "export"), ("route", "import"), ("route", "store-fill")})


def _note_export_stamp(runs_dir: Path, stamp: object) -> None:
    """Edge: remember the store version this machine's files were last written from. A failure only costs a re-export."""
    if stamp is None:
        return
    with contextlib.suppress(OSError):
        (runs_dir / _EXPORT_STAMP).write_text(json.dumps(stamp) + "\n", encoding="utf-8")


def reads_board(head: Sequence[str]) -> bool:
    """Pure: whether the command line `head` runs a command that reads the board files and does not write them itself."""
    words = [w for w in head if not w.startswith("-")]
    return bool(words) and words[0] in _BOARD_READERS and tuple(words[:2]) not in _BOARD_WRITERS


def _refresh_board_files(a: argparse.Namespace) -> None:
    """Edge. With the workspace out of git, bring this machine's work/ and intake/ files up to the store before a
    command reads them: one cheap version probe, and an export only when the store changed since the last one.
    Never raises and never prints on success: a store that cannot be reached leaves the files as they are."""
    try:
        text = _read_text_or_none(_profile_path(a))
        profile = route.parse_profile(text) if text else {}
        if not profile.get("workspace_dir") or workspace_git_on(profile):
            return
        workspace = Path(profile["workspace_dir"]).expanduser()
        runs_dir = workspace / "runs"
        stamp = run_store.queue_stamp(runs_dir)
        if stamp is None:
            return
        last = _read_text_or_none(runs_dir / _EXPORT_STAMP)
        if last is not None and last.strip() == json.dumps(stamp):
            return
        _export_board(runs_dir, workspace, prune_stale=True)
        _note_export_stamp(runs_dir, stamp)
    except Exception as exc:  # a refresh is a convenience; the command still runs on the files it has
        print(f"export: could not refresh this machine's board files from the store: {type(exc).__name__}: {exc}", file=sys.stderr)


def _leader_takeover_hours() -> float:
    """`policy.leader.takeover_hours`, default 3; answers the default for the same reason `_leader_heartbeat_minutes` does."""
    return 3.0


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


def _export_board(runs_dir: Path, workspace: Path, prune_stale: bool = False) -> list[str]:
    """Edge. Write the whole store board under `workspace` and return the ids of the contentless rows skipped.

    The whole board, never one initiative: `export_files` deletes every intake/ and work/ file its rows do not name.
    So it fills the store from the files first: a decompose writes its tickets as files, and an export with no fill
    deleted them, on the chair's tick and on an epic launch alike (2026-10-06). A fill that cannot read the store
    raises before anything is written or deleted. `prune_stale` is for a workspace out of git: see `fill_workspace`."""
    from agent_tools import store_fill  # imported here: cli loads a command module only when it runs

    # The argument is passed only when set, so a caller's stand-in for the plain fill keeps working.
    store_fill.fill_workspace(workspace, prune_stale=True) if prune_stale else store_fill.fill_workspace(workspace)
    # A file with no row is not necessarily orphaned: it may be a live decompose's, whose rows are not written yet.
    live = chair_decompose_runs.live_decompose_initiatives(runs_dir)
    return queue_export.export_files(workspace, run_store.read_queue(runs_dir), hashes=queue_export.store_hashes(runs_dir), protected=live).skipped


def workspace_git_on(profile: Mapping) -> bool:
    """Whether the chair keeps the workspace in git: `chair.workspace_git`, on unless the profile says false.

    Off, the store is the only shared copy of the board. The work/ and intake/ trees are then this machine's own
    files, written from the store so everything that reads them still can: the chair neither pulls the workspace
    nor commits and pushes what it exports, and any machine refreshes its copy with `cox route export`."""
    chair = profile.get("chair")
    return not (isinstance(chair, Mapping) and chair.get("workspace_git") is False)


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
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the merge and the record that would be made; writes no branch, row or file, but fetches origin's default branch first"}),
            commands.Arg(("--reopen",), {"action": "store_true", "help": "put a task an earlier recover closed as done (no merge commit recorded) back to approved, then record the merge"}),
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
    name="upgrade", help="fetch and check out newer pinned versions; refuses dirty checkouts",
    description=(
        "On the stable channel (the default) the checkout moves to the newest release tag.\n"
        "On the edge channel (profile upgrade_channel: edge) it moves to the newest commit on main whose CI passed,\n"
        "restarts the running chair and lander units, runs the doctor, and moves back when any of that fails."
    ),
    epilog="examples:\n  cox upgrade\n  cox upgrade --to 2026.11.8\n  cox upgrade --install-timer",
    args=(
        commands.Arg(("--root",), {"help": "the installs folder; defaults to the home's installs folder"}),
        commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
        commands.Arg(("--manifest",)),
        commands.Arg(("--provider",), {"default": "claude-code"}),
        commands.Arg(("--with",), {"action": "append", "default": None, "dest": "with_", "metavar": "FLAG"}),
        commands.Arg(("--team",)),
        commands.Arg(("--workspace",)),
        commands.Arg(("--to",), {"help": "a release version or a commit; skips the edge channel's CI lookup"}),
        commands.Arg(("--dry-run",), {"action": "store_true"}),
        commands.Arg(("--profile",), {"help": "the routing profile that holds upgrade_channel (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        commands.Arg(("--scheduled",), {"action": "store_true", "help": "what the upgrade timer runs: does nothing unless this machine's channel is edge"}),
        commands.Arg(("--install-timer",), {"action": "store_true", "help": "write the schedule that runs the scheduled upgrade (a systemd user timer on Linux, a launchd agent on macOS) and print the lines that activate it; runs nothing itself"}),
        commands.Arg(("--uninstall-timer",), {"action": "store_true", "help": "remove the schedule files --install-timer wrote on this platform"}),
        commands.Arg(("--interval-minutes",), {"type": int, "default": 30, "help": "minutes between scheduled upgrades in the installed timer (default: 30)"}),
        commands.Arg(("--environment-file",), {"help": "with --install-timer: the NAME=value file the timer's service loads (default: ~/.config/agent-tools/garage.env)"}),
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
        commands.Arg(("--workspace",), {"action": "store_true", "dest": "session_workspace", "help": "force the workspace session that can act as the chair"}),
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
            "  bare cox opens the coxswain Claude session: attached over MCP to a live chair service, from any\n"
            "    directory and without touching its lease; with no live chair, the workspace session that can act\n"
            "    as the chair. cox --workspace forces the workspace session; cox --print-argv shows which would run\n"
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
    p.add_argument("--workspace", action="store_true", dest="workspace_session", help="bare cox: force the workspace session that can act as the chair")
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

    group, rows = _table_entry("mcp")
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


MCP_GROUP = commands.Group(
    name="mcp", help="serve the chair's tools to MCP clients",
    description=(
        "Serve the board, the queue and the cartridge settings to an MCP client. `cox mcp` and `cox mcp serve` are the same.\n"
        "Run it on the machine that runs the chair and holds the cartridge.\n"
        "Reads answer from the board as it is now. Plain writes (file an intake, set a stage or a priority, hold, "
        "release, change a cartridge setting) run the cox command a person would type and return its outcome.\n"
        "A release cut, a scope change and a feature switch are sent to the chair as requests.\n"
        "With no --http it speaks MCP on stdin and stdout, which is what a local client launches. "
        "--http binds only a loopback, private, tailnet or link-local address and has no TLS."
    ),
    epilog="examples:\n  cox mcp\n  cox mcp serve --http 127.0.0.1:8765 --token-file tokens",
    args=(
        commands.Arg(("mode",), {"nargs": "?", "choices": ("serve",), "help": "optional; serving is all this command does"}),
        commands.Arg(("--http",), {"metavar": "HOST:PORT", "help": "serve over HTTP on this literal private address instead of stdin and stdout"}),
        commands.Arg(("--token-file", "--tokens-file"), {"metavar": "PATH", "dest": "tokens_file", "help": "with --http: lines of 'label token'; a token is at least 24 characters"}),
        commands.Arg(("--label",), {"help": "who the stdio client is, on every entry it sends; default $COXSWAIN_MCP_CONVERSATION, else stdio:<user>. Over HTTP the token's label is used"}),
        commands.Arg(("--profile",)),
    ),
    fn="agent_tools.cli:_mcp_serve",
)


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
        "cartridges", "route", "publish this machine's cartridges to the store, or write a published snapshot here",
        (
            commands.Arg(("action",), {"choices": ("publish", "fetch")}),
            commands.Arg(("--sha",), {"help": "the snapshot to fetch"}),
            commands.Arg(("--replace",), {"action": "store_true", "help": "fetch over cartridges that no fetch put there"}),
            commands.Arg(("--into",), {"help": "the directory to publish from or fetch into; default the profile's cartridges_dir"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_cartridges", False, (),
        description=(
            "Cartridges are written and versioned where their owner keeps them. The store only carries a copy of "
            "the exact files a launch used, under the digest of those files, so a lane host can read them.\n"
            "publish prints the sha of the profile's cartridges_dir and stores its files once.\n"
            "fetch makes cartridges_dir hold exactly that snapshot. It refuses to write over files a person put "
            "there or edited, unless --replace is given."
        ),
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
            commands.Arg(("--initiative",), {"default": None, "help": "task ticket only: the initiative holding the task, so an id repeated across initiatives picks this one"}),
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
            commands.Arg(("--fix",), {"action": "store_true", "help": "move damaged log lines to a .damaged file beside the log, then check again"}),
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
        "store", "setup", "configure the shared Postgres store for run records",
        (
            commands.Arg(("--store",), {"help": "shared; the files store is retired and is refused by name"}),
            *_STORE_FLAGS,
            commands.Arg(("--allow-unreachable",), {"action": "store_true", "help": "exit 0 even when a probe fails"}),
        ),
        "agent_tools.cli:_setup_store", False, (),
        subcommands=(
            commands.Command(
                "switch", "setup", "copy an old local run store and its traces into the shared store, then record store.mode",
                (commands.Arg(("target",), {"choices": ("files", "shared")}), *_suppressed(_STORE_FLAGS)),
                "agent_tools.cli:_setup_store_switch", False, (),
            ),
        ),
        sub_dest="store_cmd", sub_required=False,
    ),
    commands.Command(
        "unit-env", "setup", "write the one env file the chair, lander and upgrade units read, with the stored token",
        (
            commands.Arg(("--environment-file",), {"help": "the file to write (default: ~/.config/agent-tools/garage.env, the one the units read)"}),
        ),
        "agent_tools.cli:_setup_unit_env", False, (),
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


def _parse_login_ok(text: str) -> bool | None:
    """`true`, `false` or `none`, any case; the one place the login verb's tri-state is read."""
    value = {"true": True, "false": False, "none": None}
    if text.strip().lower() not in value:
        raise argparse.ArgumentTypeError(f"login_ok must be true, false or none, not {text!r}")
    return value[text.strip().lower()]


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
    (MCP_GROUP, []),
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
    refused = _store_refusal(a, head)
    if refused is not None:
        print(f"cox: {refused}", file=sys.stderr)
        return 2
    if reads_board(head):
        _refresh_board_files(a)
    return a.fn(a)


module_facade.install(__name__, _PARTS)


if __name__ == "__main__":
    # Under `python -m`, this module is `__main__`, not `agent_tools.cli`; the table's "agent_tools.cli:..."
    # handlers would otherwise import a second copy of this file at dispatch.
    sys.modules.setdefault("agent_tools.cli", sys.modules[__name__])
    sys.exit(main())
