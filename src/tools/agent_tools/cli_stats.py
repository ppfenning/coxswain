"""`cox stats`, `cox router` and `cox steward`: what the ledger says, the router's selection and the steward's proposals."""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

import yaml

from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_lazy import (
    chair,
    jsonl_log,
    queue_export,
    route,
    router,
    run_store,
    stats_causes,
    stats_chair,
    stats_efficiency,
    stats_examples,
    stats_flaky,
    stats_gates,
    stats_ingest,
    stats_lanes,
    stats_models_cmd,
    stats_query,
    stats_schema,
    stats_shadow_tier,
    stats_system_one,
    stats_tiers_cmd,
    steward,
    steward_draft,
)


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
