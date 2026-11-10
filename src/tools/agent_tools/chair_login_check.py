"""Edge: check a lane host's claude login, then record the result with `cox host beat`.

Reuses `remote_argv.ssh_argv`/`token_status_argv` for the ssh invocation. The runner returns an
`ArgvResult`-shaped reply and the host's token verb is parsed from its stdout only: the wrong belief was that
mixed stdout and stderr parse as JSON, so one stderr line made a logged-in host read as unreadable. Reuses
`host_cmd.beat_argv` for the store_cli write, so the write goes through the one path (`cox host beat`, an
injected runner) that already knows how to reach store_cli.
This module never logs a host in. It does check the loop's own machine: the wrong belief was that every
host row is reached over ssh, but the local host runs no sshd, so ssh to it always failed and recorded a
false `login_lapsed`. A host whose name equals `socket.gethostname()` runs the token verb directly.

The host's token verb reads the host's own token file and probes it with one turn, so the check depends on
neither a shell startup file nor the caller's environment. The wrong belief was that a missing token file
reads as logged out like any other failure: the reply says `tokenFile` false, which blocks the host at once.
A rejected token (`tokenFile` true, `loggedIn` false) counts toward `BLOCK_AFTER_FAILURES`. A token never
renews, so nothing here renews it.

A host running a `runner` other than `claude-code` has no `claude` CLI to ask, so its readiness check
reads `env_names` (a provider profile's `auth_env`/`endpoint_env`) off the host instead, one `printenv`
per name, over the same local/ssh split. `claude` is never invoked on that branch.
"""
from __future__ import annotations

import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agent_tools import chair_login_watch, host_cmd
from agent_tools.remote_argv import env_check_argv, json_object_in, ssh_argv, token_status_argv

BLOCK_AFTER_FAILURES = 2
REASON_OUTPUT_CHARS = 200


class Reply(Protocol):
    """What the runner hands back for one argv: `chair_exec.ArgvResult` in production."""

    @property
    def returncode(self) -> int: ...
    @property
    def stdout(self) -> str: ...
    @property
    def mixed(self) -> str: ...


SshRun = Callable[[list[str]], Reply]
CliRun = Callable[[list[str]], dict]


@dataclass(frozen=True)
class _Check:
    ok: bool
    expired: bool
    reason: str | None
    output: str
    unreachable: bool = False


def _remote_argv(host_name: str, ssh: str, argv: list[str]) -> list[str]:
    """`argv` unwrapped on the loop's own host (`host_name == socket.gethostname()`); `ssh_argv`-wrapped
    everywhere else."""
    return argv if host_name == socket.gethostname() else ssh_argv(ssh, argv)


def _stdout_verdict(stdout: str) -> bool | None:
    """True when stdout is JSON with `loggedIn` true; False for any other JSON object; None when stdout is
    not a JSON object."""
    status = json_object_in(stdout)
    return status.get("loggedIn") is True if status is not None else None


def _failure_reason(returncode: int, output: str) -> str:
    """The exit code and the first `REASON_OUTPUT_CHARS` characters of the trimmed output."""
    return f"exit {returncode}: {output.strip()[:REASON_OUTPUT_CHARS]}"


def _failure_count(versions: dict, key: str = "login_failures") -> int:
    """The stored count under `key` (`login_failures` by default); 0 when absent or not a positive int."""
    stored = versions.get(key)
    return stored if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0 else 0


def _unreachable_login(count: int, now: str, reason: str | None) -> dict:
    """The `--login-json` payload of an unreachable check. `unreachable_since` joins it once `count`
    reaches `UNREACHABLE_AFTER`. `login_ok`, `login_failures` and `beat_at` are never in it."""
    base = {chair_login_watch.UNREACHABLE_FAILURES_KEY: count, "login_checked_at": now, "reason": reason}
    return base if count < chair_login_watch.UNREACHABLE_AFTER else {**base, chair_login_watch.UNREACHABLE_SINCE_KEY: now}


def _token_check(status: dict, reply: Reply) -> _Check:
    """A passing check when `status` says logged in. A missing token file is `expired`, so it blocks the host
    at once. A rejected token fails without it and counts toward `BLOCK_AFTER_FAILURES`."""
    if status.get("loggedIn") is True:
        return _Check(True, False, None, reply.mixed)
    stated = status.get("reason")
    reason = stated if isinstance(stated, str) and stated else _failure_reason(reply.returncode, reply.mixed)
    return _Check(False, status.get("tokenFile") is False, reason, reply.mixed)


def _claude_check(host_name: str, ssh: str, ssh_run: SshRun) -> _Check:
    """The host's token verb once. An unreadable reply fails, as unreachable when `is_unreachable_reply`
    says so."""
    # The probe costs one haiku turn per check.
    reply = ssh_run(_remote_argv(host_name, ssh, token_status_argv(probe=True)))
    status = json_object_in(reply.stdout)
    if status is None:
        unreachable = chair_login_watch.is_unreachable_reply(reply.returncode, reply.mixed)
        return _Check(False, False, _failure_reason(reply.returncode, reply.mixed), reply.mixed, unreachable)
    return _token_check(status, reply)


def _login_ok(host_name: str, ssh: str, ssh_run: Callable[[list[str]], tuple[int, str]]) -> bool | None:
    """True when the host's token verb says logged in; False when it says explicitly it is not; None when
    the reply cannot be read at all. Takes a `(exit code, text)` runner. No `--probe`: the dashboard feed
    calls this on every tick, and a probe is a model turn."""
    return _stdout_verdict(ssh_run(_remote_argv(host_name, ssh, token_status_argv()))[1])


def _env_check(host_name: str, ssh: str, ssh_run: SshRun, env_names: tuple[str, ...]) -> _Check:
    """A passing check when every name in `env_names` is present; a failing one naming the first missing
    one, `"missing <name>"`, otherwise. An unreachable reply (see `is_unreachable_reply`) fails as
    `unreachable` with the usual `_failure_reason`. Fails with a fixed reason when `env_names` is empty: a
    provider profile naming neither `auth_env` nor `endpoint_env`. `claude` is never invoked here.

    Present means exit 0 and non-empty trimmed stdout. The wrong belief is that non-empty output alone
    proves the variable is set: an ssh failure (exit 255, "Connection refused" on stderr) must not read as
    present. `printenv` exits 1 on an unset name, so the exit code separates set from missing and from
    unreachable."""
    if not env_names:
        return _Check(False, False, "no auth_env or endpoint_env configured", "")
    for name in env_names:
        reply = ssh_run(_remote_argv(host_name, ssh, env_check_argv(name)))
        if chair_login_watch.is_unreachable_reply(reply.returncode, reply.mixed):
            return _Check(False, False, _failure_reason(reply.returncode, reply.mixed), reply.mixed, True)
        if reply.returncode != 0 or not reply.stdout.strip():
            return _Check(False, False, f"missing {name}", reply.mixed)
    return _Check(True, False, None, "")


def check_login_on_host(
    host_name: str,
    ssh: str,
    current_versions: dict,
    now: str,
    ssh_run: SshRun,
    cli_run: CliRun,
    *,
    runner: str = "claude-code",
    env_names: tuple[str, ...] = (),
) -> dict:
    """Runs the readiness check, locally on the loop's own host and over ssh elsewhere, merges its
    `login_ok`, `check`, `reason` and `now` into `current_versions`, writes the merge through the reused
    `cox host beat` argv, and returns the row `cli_run` prints. `ssh_run` returns an `ArgvResult`.

    `runner == "claude-code"` (the default) asks the host's token verb (see `_claude_check`). A pass stores
    `login_ok` True and `login_failures` 0. A failure stores `reason` and adds one to `login_failures`;
    `login_ok` is False once that count reaches `BLOCK_AFTER_FAILURES` or the token file is missing, and
    None before. A failed check also stores the first https URL in its output as `login_url`; a passing
    check clears any stored one. Any other `runner` checks `env_names` instead, with no failure count;
    `claude` is never invoked on that branch.

    An unreachable check (either branch) touches neither `login_ok`, `login_failures` nor `reason`, and
    never beats, since `beat_at` is the last-seen time. It writes `unreachable_failures`,
    `login_checked_at` and `reason` through `cox host login`; at `UNREACHABLE_AFTER` in a row it also
    writes `unreachable_since` and sets the host `unreachable`. A reachable check beats as above and drops
    `unreachable_failures`."""
    check = (
        _claude_check(host_name, ssh, ssh_run)
        if runner == "claude-code"
        else _env_check(host_name, ssh, ssh_run, env_names)
    )
    if check.unreachable:
        count = _failure_count(current_versions, chair_login_watch.UNREACHABLE_FAILURES_KEY) + 1
        payload = _unreachable_login(count, now, check.reason)
        row = cli_run(["host", "login", host_name, "--login-json", json.dumps(payload, sort_keys=True)])
        if count < chair_login_watch.UNREACHABLE_AFTER:
            return row
        return cli_run(host_cmd.set_state_argv(host_name, chair_login_watch.UNREACHABLE_STATE, "chair"))
    streak_cleared = {k: v for k, v in current_versions.items() if k != chair_login_watch.UNREACHABLE_FAILURES_KEY}
    if runner == "claude-code":
        failures = 0 if check.ok else _failure_count(current_versions) + 1
        login_ok = True if check.ok else (False if check.expired or failures >= BLOCK_AFTER_FAILURES else None)
        login_url = None if check.ok else chair_login_watch.login_url_from_output(check.output)
        kept = {key: value for key, value in streak_cleared.items() if key != "reason"}
        merged = {
            **chair_login_watch.merge_login_versions(
                kept, login_ok, now, check="claude_auth", reason=check.reason, login_url=login_url
            ),
            "login_failures": failures,
        }
    else:
        merged = chair_login_watch.merge_login_versions(
            streak_cleared, check.ok, now, check="env_vars", reason=check.reason
        )
    return cli_run(host_cmd.beat_argv(host_name, merged))
