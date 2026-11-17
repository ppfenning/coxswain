"""Bare `cox`: the coxswain session, attached to a live chair or launched in the workspace."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import shutil
import socket
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from agent_tools.cli import build_parser
from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_courier import _launcher_argv, _plugin_root, _print_inbox, _spawn
from agent_tools.cli_land import _leader_pid_alive
from agent_tools.cli_lazy import chair, chair_attach, chair_resolve, courier, route
from agent_tools.cli_leader import _launcher_session_takes, _live_loop_holder, _read_key, _route_chair_take
from agent_tools.cli_runs import _leader_heartbeat_minutes


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


def _conversation_file() -> Path:
    """Edge. Where the attached session's conversation id is stored on this machine."""
    return chair_attach.conversation_path(os.environ.get("XDG_STATE_HOME"), Path.home())


def _attach_over_mcp(a: argparse.Namespace, argv: list[str], extra_args: list[str], mint: Callable[[], str]) -> int:
    """Edge. Start (or print) the session attached to the live chair over MCP: the chair's MCP server configured, the
    operator's stored conversation id resumed, the current directory kept. Reads and writes no lease, takes nothing,
    starts no beater and prints no inbox. `argv` is the workspace session's, read only for the plugin directory."""
    path = _conversation_file()
    conversation = chair_attach.read_conversation(_read_text_or_none(path))
    if conversation is None:
        conversation = mint()
        with contextlib.suppress(OSError):  # an id that cannot be stored still names this session's conversation
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(chair_attach.conversation_text(conversation), encoding="utf-8")
    plugin_root = argv[argv.index("--plugin-dir") + 1] if "--plugin-dir" in argv else None
    attached = chair_attach.attach_argv(plugin_root, chair_attach.mcp_config("cox", conversation), extra_args)
    if a.print_argv:
        print(attached)
        print(Path.cwd())
        print("chair: a live chair service holds the chair; this session is attached to it over MCP", file=sys.stderr)
        return 0
    if a.launcher_profile:  # the server the session starts reads the same profile this launch did
        os.environ["AGENT_TOOLS_PROFILE"] = str(Path(a.launcher_profile).expanduser())
    print("chair: a live chair service holds the chair; attaching to it over MCP (cox --workspace forces the workspace session)")
    os.execvp(attached[0], attached)
    return 0


def _launcher(a: argparse.Namespace, extra_args: list[str], mint: Callable[[], str] | None = None) -> int:
    """Bare `cox` (spec §7) and `cox session`: a real Claude Code session with
    the coxswain plugin loaded and the profile's workspace as cwd, off the
    dedicated `launcher_profile` dest. The argv comes from `_session_launch`.

    With a live chair loop under another label the session attaches to it over MCP instead (`_attach_over_mcp`),
    unless `--workspace` forces the workspace session. `mint` makes a new conversation id; a test fixes it."""
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
    # Before anything that reads the chair record to take, offer or resume: a live chair loop is attached to, not joined.
    live = _live_loop_holder(runs_dir)
    if chair_attach.attach_mode(live, f"chair-{now:%Y-%m-%d}", bool(getattr(a, "workspace_session", False))) == "mcp":
        return _attach_over_mcp(a, argv, extra_args, mint or (lambda: uuid.uuid4().hex))
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
            _print_inbox(courier.read_text(Path(workspace).expanduser() / "courier.jsonl"), chair_a.label, chair_a.label)
    os.chdir(cwd)
    os.execvp(argv[0], argv)
    return 0
