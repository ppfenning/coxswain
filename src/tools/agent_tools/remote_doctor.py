"""Edge: run `cox setup doctor` on a lane host over ssh; the caller prints what comes back."""

from collections.abc import Callable, Sequence

from agent_tools import lane_toolchain
from agent_tools.lane_hosts import LaneHost
from agent_tools.lane_toolchain import CheckTools
from agent_tools.remote_argv import doctor_argv, env_check_argv, json_object_in, ssh_argv, token_status_argv


def auth_verdict(output: str) -> str | None:
    """None when the host's token status is JSON with `loggedIn` true; otherwise the reply's own reason as the refusal line."""
    status = json_object_in(output)
    if status is None:
        return f"claude token: status unreadable: {output.strip()[:80]}"
    if status.get("loggedIn") is True:
        return None
    reason = status.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    # `cox setup token status` always sets a reason when not logged in; a reply without one is
    # `claude auth status` output, which the launch preflight in cli.py still feeds here.
    return "claude auth: not logged in on the host (run claude auth login there)"


def env_verdict(names: tuple[str, ...], run_one: Callable[[str], str]) -> str | None:
    """None when every name in `names` is non-empty on the host; otherwise names the first missing one."""
    for name in names:
        if not run_one(name).strip():
            return f"env vars: {name} is not set on the host"
    return None


def doctor_on_host(
    host: LaneHost,
    run: Callable[[list[str]], tuple[int, str]],
    runner: str = "claude-code",
    env_names: tuple[str, ...] = (),
    argv_for: Callable[[list[str]], list[str]] | None = None,
    checks: CheckTools | None = None,
    repo_checks: Sequence[tuple[CheckTools, str]] = (),
) -> tuple[int, list[str]]:
    """`run` takes an argv and returns (exit code, combined output); `argv_for` wraps each command, ssh by default.
    `checks` are the tools the repo's checks run: each one the lane PATH lacks adds the refusal line a launch would give.
    `repo_checks` pairs each repo's tools with its repo_dir; a tool in that repo's venv counts as found. Rows are de-duplicated."""
    wrap = argv_for if argv_for is not None else (lambda argv: ssh_argv(host.ssh, argv))
    code, output = run(wrap(doctor_argv()))
    if runner == "claude-code":
        _, token_output = run(wrap(token_status_argv(probe=True)))
        verdict = auth_verdict(token_output)
        row = "claude token     ok" if verdict is None else verdict
    else:
        verdict = env_verdict(env_names, lambda name: run(wrap(env_check_argv(name)))[1])
        row = "env vars      ok" if verdict is None else verdict
    wanted = checks or CheckTools((), ())
    lane_missing = lane_toolchain.refusals(host.name, wanted.found, wrap(lane_toolchain.lane_argv(wanted.found)), run)
    repo_missing = [
        line
        for tools, repo_dir in repo_checks
        for line in lane_toolchain.refusals(
            host.name, tools.found, wrap(lane_toolchain.lane_argv(tools.found, repo_dir)), run, repo_dir,
        )
    ]
    missing = list(dict.fromkeys([*lane_missing, *repo_missing]))
    unparsed_lines = dict.fromkeys([*wanted.unparsed, *(u for tools, _ in repo_checks for u in tools.unparsed)])
    unparsed = [f"checks: could not read tools from: {line}" for line in unparsed_lines]
    failed = verdict is not None or bool(missing)
    return (1 if failed and code == 0 else code), [*output.splitlines(), row, *missing, *unparsed]
