"""Keep one repository's checkout on a lane host at origin's default tip, or say why it was left alone.

`verdict_from_facts`, `needs_chair_action` and `parse_facts` are pure. `sync_host_checkout` is the edge: one ssh
invocation fetches origin's default branch and reads the five facts, and a second ssh invocation runs
`git merge --ff-only` only after the verdict is fast_forward. The verdict is decided here, by
`checkout_sync.decide`, never in the remote shell.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from agent_tools import checkout_sync, remote_argv, remote_fetch
from agent_tools.lane_hosts import LaneHost

LaneVerdict = checkout_sync.Verdict | Literal["unreachable"]
Runner = Callable[[Sequence[str]], tuple[int, str]]  # (argv) -> (exit code, output)
Facts = tuple[bool, str | None, str, bool, bool]  # clean, branch, default, head_is_ancestor, at_tip

UNREACHABLE: LaneVerdict = "unreachable"
_RAISED_CODE = 255

# Any read git cannot answer ends the script nonzero, so a failed read is never taken for a clean tree.
_READ_SCRIPT = """\
cd "$1" || exit 2
d=$(git symbolic-ref --short -q refs/remotes/origin/HEAD) || exit 3
d=${d#origin/}
[ -n "$d" ] || exit 3
git fetch --quiet origin "$d" || exit 4
o=origin/$d
s=$(git status --porcelain) || exit 5
h=$(git rev-parse HEAD) || exit 6
t=$(git rev-parse "$o") || exit 6
b=$(git symbolic-ref --short -q HEAD || true)
if [ -z "$s" ]; then echo clean=1; else echo clean=0; fi
echo "branch=$b"
echo "default=$d"
if git merge-base --is-ancestor HEAD "$o"; then echo ancestor=1; else echo ancestor=0; fi
if [ "$h" = "$t" ]; then echo at_tip=1; else echo at_tip=0; fi
"""
_MERGE_SCRIPT = 'cd "$1" && git merge --ff-only "origin/$2"'


def verdict_from_facts(
    clean: bool, branch: str | None, default: str, head_is_ancestor: bool, at_tip: bool
) -> checkout_sync.Verdict:
    return checkout_sync.decide(clean, branch, default, head_is_ancestor, at_tip)


def needs_chair_action(
    host_name: str,
    host_path: str,
    verdict: LaneVerdict,
    branch: str | None = None,
    default: str = "",
    detail: str = "",
) -> dict[str, str] | None:
    """None for fast_forward and already_current. `detail` is the reason an unreachable host gives."""
    if verdict in ("fast_forward", "already_current"):
        return None
    why = (detail or "no reason given") if verdict == UNREACHABLE else checkout_sync.report(verdict, branch, default)
    one_line = " ".join(f"lane host {host_name}, checkout {host_path}: {why}".split())
    return {"kind": "needs_chair", "host": host_name, "cause": f"lane_checkout_{verdict}", "reason": one_line}


def parse_facts(output: str) -> Facts | None:
    """None unless the read script printed all five facts; an empty branch is a detached HEAD."""
    pairs = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    flags = [pairs.get(key) for key in ("clean", "ancestor", "at_tip")]
    default = pairs.get("default", "").strip()
    if any(flag not in ("0", "1") for flag in flags) or not default or "branch" not in pairs:
        return None
    clean, ancestor, at_tip = (flag == "1" for flag in flags)
    return clean, pairs["branch"].strip() or None, default, ancestor, at_tip


def _read_argv(host: LaneHost, host_path: str) -> list[str]:
    return remote_argv.ssh_argv(host.ssh, ["sh", "-c", _READ_SCRIPT, "lane-sync", host_path])


def _merge_argv(host: LaneHost, host_path: str, default: str) -> list[str]:
    return remote_argv.ssh_argv(host.ssh, ["sh", "-c", _MERGE_SCRIPT, "lane-sync", host_path, default])


def _first_line(output: str, code: int) -> str:
    return next((line.strip() for line in output.splitlines() if line.strip()), f"exit code {code}")


def _call(run: Runner, argv: Sequence[str]) -> tuple[int, str]:
    # The runner is injected and may be anything; a raise from it is an unreachable host, not a crash of the tick.
    try:
        return run(argv)
    except Exception as exc:
        return _RAISED_CODE, f"{type(exc).__name__}: {exc}"


def sync_host_checkout(host: LaneHost, chair_repo: str, chair_workspace: str, run: Runner) -> dict[str, str] | None:
    """Edge. Fetch and read, decide, and fast-forward only on fast_forward.

    Unreadable facts, an unreachable host and a fast-forward git refuses are all the unreachable cause: the
    checkout is left as it was and nothing is retried another way.
    """
    host_path = remote_fetch.host_repo_path(chair_workspace, host.workspace_dir, chair_repo)
    code, out = _call(run, _read_argv(host, host_path))
    facts = parse_facts(out) if code == 0 else None
    if facts is None:
        return needs_chair_action(host.name, host_path, UNREACHABLE, detail=_first_line(out, code))
    clean, branch, default, ancestor, at_tip = facts
    verdict = verdict_from_facts(clean, branch, default, ancestor, at_tip)
    if verdict != "fast_forward":
        return needs_chair_action(host.name, host_path, verdict, branch, default)
    merge_code, merge_out = _call(run, _merge_argv(host, host_path, default))
    if merge_code == 0:
        return None
    detail = f"fast-forward to origin/{default} failed: {_first_line(merge_out, merge_code)}"
    return needs_chair_action(host.name, host_path, UNREACHABLE, detail=detail)


def sync_lane_checkouts(
    hosts: Sequence[LaneHost], repos: Sequence[str], chair_workspace: str, run: Runner
) -> list[dict[str, str]]:
    """Every repo on every host; the needs_chair actions for the checkouts left alone, empty when all are current."""
    return [
        action
        for host in hosts
        for repo in repos
        if (action := sync_host_checkout(host, repo, chair_workspace, run)) is not None
    ]
