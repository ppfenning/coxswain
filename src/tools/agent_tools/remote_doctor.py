"""Edge: run `cox setup doctor` on a lane host over ssh; the caller prints what comes back."""

from collections.abc import Callable

from agent_tools import lane_toolchain
from agent_tools.lane_hosts import LaneHost
from agent_tools.lane_toolchain import CheckTools
from agent_tools.remote_argv import auth_status_argv, doctor_argv, env_check_argv, json_object_in, ssh_argv


def auth_verdict(output: str) -> str | None:
    """None when `claude auth status` output is JSON with `loggedIn` true; otherwise the refusal line."""
    status = json_object_in(output)
    if status is None:
        return f"claude auth: status unreadable: {output.strip()[:80]}"
    if status.get("loggedIn") is True:
        return None
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
) -> tuple[int, list[str]]:
    """`run` takes an argv and returns (exit code, combined output); `argv_for` wraps each command, ssh by default.
    `checks` are the tools the repo's checks run: each one the lane PATH lacks adds the refusal line a launch would give."""
    wrap = argv_for if argv_for is not None else (lambda argv: ssh_argv(host.ssh, argv))
    code, output = run(wrap(doctor_argv()))
    if runner == "claude-code":
        _, auth_output = run(wrap(auth_status_argv()))
        verdict = auth_verdict(auth_output)
        row = "claude auth      ok" if verdict is None else verdict
    else:
        verdict = env_verdict(env_names, lambda name: run(wrap(env_check_argv(name)))[1])
        row = "env vars      ok" if verdict is None else verdict
    wanted = checks or CheckTools((), ())
    missing = lane_toolchain.refusals(host.name, wanted.found, wrap(lane_toolchain.lane_argv(wanted.found)), run)
    unparsed = [f"checks: could not read tools from: {line}" for line in wanted.unparsed]
    failed = verdict is not None or bool(missing)
    return (1 if failed and code == 0 else code), [*output.splitlines(), row, *missing, *unparsed]
