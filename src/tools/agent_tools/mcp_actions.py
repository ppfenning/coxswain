"""The edge behind the MCP server's plain writes and its cartridge reader, for a server that runs beside the store
and on the machine that holds the cartridge.

Each write is the `cox` command a person would type, run as its own process with its output captured: the same
code, the same refusals, one place that knows how. Nothing here decides anything; `argv_for` only spells the command.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

_DETAIL_CHARS = 1500  # a diff or a refusal is short; a long one is cut at the end


class Refused(ValueError):
    """The request names something no command is run for."""


def argv_for(tool: str, args: Mapping[str, object]) -> list[str]:
    """Pure. The `cox` command line for one tool call. Raises Refused for a tool or kind with no command."""
    def text(key: str) -> str:
        return str(args.get(key) or "").strip()

    if tool == "file_intake":
        stage = text("stage") or "draft"
        optional = [(flag, text(key)) for flag, key in (("--body", "body"), ("--epic", "epic"), ("--priority", "priority"),
                                                         ("--target-release", "target_release"))]
        return ["route", "file", "--intake", "--repo", text("repo"), "--title", text("title"), f"--{stage}",
                *(part for flag, value in optional if value for part in (flag, value))]
    if tool == "set_stage":
        return ["route", text("stage"), text("initiative")]
    if tool == "set_priority":
        return ["route", "priority", text("initiative"), "--set", text("priority")]
    if tool == "send_directive" and text("kind") == "hold":
        return ["route", "hold", text("initiative"), "--reason", text("reason"), "--by", text("caller") or "mcp"]
    if tool == "send_directive" and text("kind") == "release":
        return ["route", "release", text("initiative")]
    if tool == "set_setting":
        return ["settings", "set", "cartridge", text("key"), text("value"), *(["--dry-run"] if args.get("dry_run") else ["--commit"])]
    raise Refused(f"no command carries out {tool}{' ' + text('kind') if text('kind') else ''}")


def operator_only(key: str) -> bool:
    """Edge-free lookup: whether the settings registry marks this cartridge key as the operator's alone."""
    from agent_tools import settings_model

    found = settings_model.setting_for("cartridge", key)
    return bool(found is not None and found.operator_only)


def run_cox(argv: list[str]) -> tuple[int, str]:
    """Edge. Run one `cox` command as its own process and return (exit code, everything it printed).
    Its own process, so nothing the command or a program it starts prints can land on the server's protocol stream."""
    import subprocess
    import sys

    try:
        done = subprocess.run([sys.executable, "-m", "agent_tools.cli", *argv], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return done.returncode, (done.stdout + done.stderr)


def local_actor(profile_path: Path, run: Callable[[list[str]], tuple[int, str]] | None = None) -> Callable[[str, Mapping[str, object]], tuple[bool, str]]:
    """Edge. The `act` port: run the command for a tool call against the profile at `profile_path` and return
    (exit 0, what it printed). An operator-only setting is refused before anything runs."""

    def act(tool: str, args: Mapping[str, object]) -> tuple[bool, str]:
        if tool == "set_setting" and operator_only(str(args.get("key") or "")):
            return False, f"{args.get('key')} is an operator-only setting: change it with `cox settings set` yourself"
        try:
            argv = [*argv_for(tool, args), "--profile", str(profile_path)]
        except Refused as exc:
            return False, str(exc)
        try:
            code, said = (run or run_cox)(argv)
        except Exception as exc:  # a command that cannot run is a refusal the caller can read, not a dead server
            return False, f"{type(exc).__name__}: {exc}"
        return code == 0, said.strip()[-_DETAIL_CHARS:]

    return act


def settings_reader(profile_path: Path) -> Callable[[], dict]:
    """Edge. The `settings` read port: what `cox settings get --json` prints, as data."""

    def read() -> dict:
        from agent_tools import cox_settings

        return cox_settings.to_json(cox_settings.read_grouped(Path(profile_path)))

    return read
