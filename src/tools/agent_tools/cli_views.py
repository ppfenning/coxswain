"""`cox console`, `dash`, `home`, `session` and `settings`: the operator's screens."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_launcher import _launcher
from agent_tools.cli_lazy import (
    console_screen,
    dash_detail_health,
    dash_detail_initiative,
    dash_detail_machine,
    dash_detail_release,
    dash_detail_run,
    dash_detail_spend,
    dash_feed,
    dash_feed_share,
    route,
    run_store,
    towpath_launch,
    usage_window,
)
from agent_tools.cli_route import _route_context
from agent_tools.cli_route_sync import _chair_max_in_flight, _initiative_token
from agent_tools.cli_runs import _resolved_run_id
from agent_tools.cli_usage import _resolved_pacing_policy


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
        argparse.Namespace(launcher_profile=a.profile, no_plugin=a.session_no_plugin, print_argv=a.session_print_argv,
                           workspace_session=getattr(a, "session_workspace", False)),
        a.session_extra,
    )


def _setup_tui(a: argparse.Namespace) -> int:
    """Bare `cox setup`: plain towpath, which takes no settings-open argument yet."""
    launched = towpath_launch.launch(which=shutil.which, execv=os.execv, stdout=sys.stdout, stdin=sys.stdin)
    if launched:
        return 0
    print("setup: needs a terminal and towpath; use setup doctor / setup install directly")
    return 2


def _settings_get(a: argparse.Namespace) -> int:
    from agent_tools import cox_settings
    return cox_settings.run_get(_profile_path(a), a.json)


def _settings_set(a: argparse.Namespace) -> int:
    from agent_tools import cox_settings
    return cox_settings.run_set(_profile_path(a), a.scope, a.key, a.value, a.dry_run, a.commit)
