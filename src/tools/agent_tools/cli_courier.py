"""`cox courier` and `cox mcp`: the inbox and the MCP server."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from agent_tools.cli import _profile_path, _read_epics, chair, chair_attach, courier
from agent_tools.cli_route import _courier_profile


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
    courier.append_file(path, entry)
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
        courier.read_text(workspace / "courier.jsonl"), a.label, holder,
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


def _mcp_serve(a: argparse.Namespace) -> int:
    """Edge. Serve the chair's MCP tools for the profile's workspace: on stdin and stdout, or over HTTP with
    --http and a tokens file. Only protocol messages are written to stdout; everything else goes to stderr."""
    # Imported here: cli loads a command module only when it runs.
    from agent_tools import mcp_http, mcp_ports, mcp_server

    with contextlib.redirect_stdout(sys.stderr):  # a profile problem is said on stderr, never on the protocol stream
        profile = _courier_profile(a)
    if profile is None:
        return 2
    workspace = Path(profile["workspace_dir"]).expanduser()
    ports = mcp_ports.build(profile, _profile_path(a), lambda: sorted(_read_epics(workspace)[0]))
    if a.http is None:
        def write(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()

        # The attached session's config carries its conversation id here, so its entries stay in one conversation.
        caller = a.label or os.environ.get(chair_attach.CONVERSATION_ENV) or f"stdio:{getpass.getuser()}"
        return mcp_server.serve_stdio(sys.stdin, write, mcp_server.make_handler(ports, caller))
    host, _, port = a.http.rpartition(":")
    if not host or not port.isdigit() or not a.tokens_file:
        print("mcp: --http takes HOST:PORT with a literal address, and needs --token-file", file=sys.stderr)
        return 2
    try:
        tokens = mcp_http.parse_tokens(Path(a.tokens_file).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"mcp: the tokens file cannot be used: {exc}", file=sys.stderr)
        return 2
    return mcp_http.serve(host.strip("[]"), int(port), lambda label: mcp_server.make_handler(ports, label), tokens)
