"""Handlers behind `cox setup token`. Plain functions; the cli task wires the edges."""

from __future__ import annotations

import getpass
import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping

from agent_tools.claude_token import (
    TOKEN_ENV,
    claude_env,
    parse_token_text,
    status_report,
    write_token,
)

_TAIL = 200
_AUTH_ARGV = ["claude", "auth", "status"]
_PROBE_ARGV = ["claude", "-p", "ok", "--max-turns", "1", "--model", "haiku"]


def resolve_source(flag: str | None, file_text: str | None, prompted: str | None) -> str | None:
    """The first source that is given wins. A given source that does not parse is None, not a fall-through."""
    for given in (flag, file_text, prompted):
        if given is not None:
            return parse_token_text(given)
    return None


def prompt_secret(prompt: str) -> str:
    return getpass.getpass(prompt)


def read_file_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def run_command(argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
    """Returns stdout, so stderr noise never lands in front of JSON. stderr stands in only when stdout is empty."""
    done = subprocess.run(argv, env=dict(env), capture_output=True, text=True, check=False)
    return done.returncode, done.stdout if done.stdout.strip() else done.stderr


def store(
    flag: str | None,
    from_file: str | None,
    *,
    path: str,
    interactive: bool,
    ask_secret: Callable[[str], str],
    read_text: Callable[[str], str | None],
    write: Callable[[str, str], None] = write_token,
) -> int:
    file_text = read_text(from_file) if flag is None and from_file else None
    if flag is None and from_file and file_text is None:
        print(f"claude token: could not read {from_file}")
        return 2
    prompted = None
    if flag is None and file_text is None and interactive:
        prompted = ask_secret("Claude token (input hidden): ")
    if flag is None and file_text is None and prompted is None:
        print(
            "claude token: no token given. Run `claude setup-token`, then pass the result with --token or --from-file."
        )
        return 2
    token = resolve_source(flag, file_text, prompted)
    if token is None:
        print("claude token: the given text is not a single token or CLAUDE_CODE_OAUTH_TOKEN=value line")
        return 2
    write(path, token)
    print(f"claude token: stored at {path} (private, mode 600)")
    return 0


def _attempt(
    run: Callable[[list[str], Mapping[str, str]], tuple[int, str]],
    argv: list[str],
    env: Mapping[str, str],
    token: str,
) -> tuple[int, str]:
    """Runs one command. OSError becomes a failure. The token is redacted from the whole output before any cut."""
    try:
        code, out = run(argv, env)
    except OSError as exc:
        code, out = 1, str(exc)
    return code, out.replace(token, "***")


def _json_object(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def status(
    probe: bool,
    *,
    path: str,
    read_token: Callable[[str], str | None],
    run: Callable[[list[str], Mapping[str, str]], tuple[int, str]],
    environ: Mapping[str, str] | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    token = read_token(path)
    source = "file"
    if token is None:
        # A token the operator keeps in the environment (an env file the shell or the service unit loads) is a
        # stored token too: the file is only one place to keep it. Nothing is copied; the value is never printed.
        token, source = (environment.get(TOKEN_ENV) or "").strip() or None, "environment"
    if token is None:
        report = status_report(path, False, "", None, "")
    else:
        env = claude_env(environment, token)
        auth_exit, auth_out = _attempt(run, _AUTH_ARGV, env, token)
        auth = _json_object(auth_out)
        if auth is None and auth_exit != 0:
            probe_exit: int | None = auth_exit
            tail = auth_out[-_TAIL:]
        elif probe and auth is not None and auth.get("loggedIn") is True:
            probe_exit, out = _attempt(run, _PROBE_ARGV, env, token)
            tail = out[-_TAIL:]
        else:
            probe_exit, tail = None, ""
        report = status_report(path, True, auth_out, probe_exit, tail)
        if source == "environment":
            report = {**report, "tokenSource": TOKEN_ENV}
    print(json.dumps(report))
    return 0 if report["loggedIn"] is True else 1


def token_fact(path: str, environ: Mapping[str, str] | None = None) -> dict:
    """The mapping doctor judges. mode is the permission bits as an int, None when absent.

    A file that exists but cannot be read is present and not empty: empty is claimed only from text actually read.
    With no file, a token in the environment counts: the fact then carries `source` set to the variable's name, and
    no mode, because there is no file to protect.
    """
    if not os.path.exists(path) and ((os.environ if environ is None else environ).get(TOKEN_ENV) or "").strip():
        return {"path": path, "present": True, "empty": False, "mode": None, "source": TOKEN_ENV}
    try:
        with open(path, encoding="utf-8") as fh:
            mode = stat.S_IMODE(os.fstat(fh.fileno()).st_mode)
            empty = not fh.read().strip()
    except FileNotFoundError:
        return {"path": path, "present": False, "empty": False, "mode": None}
    except OSError:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            return {"path": path, "present": False, "empty": False, "mode": None}
        return {"path": path, "present": True, "empty": False, "mode": mode}
    return {"path": path, "present": True, "empty": empty, "mode": mode}
