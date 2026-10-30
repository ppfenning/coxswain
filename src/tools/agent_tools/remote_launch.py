"""Edge: copy an initiative to a lane host, then start the lane there. `run` takes an argv and returns its exit code."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from agent_tools import base_branch, lane_toolchain, repo_path
from agent_tools.host_cmd import ChairHead, sync_argvs
from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import (
    agent_refspec,
    base_sync_argv,
    branch_push_argv,
    epic_refspec,
    launch_argv,
    rsync_push_argv,
    ssh_argv,
    sync_argv,
)
from agent_tools.remote_doctor import env_verdict
from agent_tools.remote_lane import remote_record

__all__ = ["LaunchError", "Relaunch", "env_preflight", "launch_on_host", "launch_plan"]


@dataclass(frozen=True)
class LaunchError:
    step: str
    message: str


@dataclass(frozen=True)
class Relaunch:
    """A relocated relaunch: `tasks` are the approved task ids whose branches `prior_run` left under agents/<prior_run>/."""

    prior_run: str
    tasks: tuple[str, ...] = ()
    local_repo: str | None = None  # None: the chair's checkout is `repo` expanded against the chair's home


def _relaunch_branches(initiative: str, relaunch: Relaunch) -> list[tuple[str, str]]:
    """(branch, refspec) pairs, the epic glob first, then one per task in `relaunch.tasks` order."""
    return [
        (f"epic/{initiative}/*", epic_refspec(initiative)),
        *((f"agents/{relaunch.prior_run}/{task}", agent_refspec(relaunch.prior_run, task)) for task in relaunch.tasks),
    ]


def _checkout(path: str, host_checkouts: Sequence[str]) -> str | None:
    """The first host checkout whose basename equals the basename of `path`; a checkout of the same repo may sit under another directory there."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return next((c for c in host_checkouts if c.rstrip("/").rsplit("/", 1)[-1] == name), None)


def _git_out(path: str, *args: str) -> str:
    done = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def _local_head(path: str) -> ChairHead | None:
    """Edge. The commit the chair's checkout at `path` runs, its branch None when detached; None when git cannot read it."""
    try:
        sha = _git_out(path, "rev-parse", "HEAD")
        return ChairHead(sha, _git_out(path, "branch", "--show-current") or None) if sha else None
    except OSError:
        return None


_UNRESOLVED = "repo at unknown, chair at unknown"


class ChairBase(NamedTuple):
    """The chair repo's `origin/<branch>` commit, resolved after a fetch: the one commit every lane must build on."""

    branch: str
    sha: str


def _chair_base(path: str) -> ChairBase | LaunchError | None:
    """Edge. Fetch the chair's checkout at `path`, then read `origin/<base>` (origin's default branch, else main).
    None only when `path` is no git checkout or has no origin. A failed fetch or an unresolved `origin/<base>` is a
    LaunchError: either would send the lane to a commit the chair did not choose."""
    try:
        if not _git_out(path, "rev-parse", "--show-toplevel") or not _git_out(path, "remote", "get-url", "origin"):
            return None
        fetched = subprocess.run(
            ["git", "-C", path, "fetch", "--tags", "origin"], capture_output=True, text=True, check=False, timeout=120
        )
        branch = base_branch.base_name(path)
        sha = _git_out(path, "rev-parse", "--verify", "-q", f"origin/{branch}")
    except (OSError, subprocess.SubprocessError):
        return LaunchError("sync", f"{_UNRESOLVED}; sync failed: the chair cannot run git in {path}")
    if fetched.returncode != 0:
        why = f"the chair's git fetch origin in {path} exited {fetched.returncode}: {fetched.stderr.strip()[:120]}"
        return LaunchError("sync", f"{_UNRESOLVED}; sync failed: {why}")
    if not sha:
        return LaunchError("sync", f"{_UNRESOLVED}; sync failed: origin/{branch} does not resolve in the chair's checkout {path}")
    return ChairBase(branch, sha)


def _probe(argv: list[str]) -> tuple[int, str]:
    """Edge. Run `argv`; its exit code and stdout then stderr."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"sync: {argv[0]}: {exc}"
    return done.returncode, done.stdout + done.stderr


def _tagged(output: str, prefix: str) -> str | None:
    """The text after `prefix` on the last output line that starts with it; a login shell may print noise around it."""
    found = [line[len(prefix):].strip() for line in output.splitlines() if line.startswith(prefix)]
    return found[-1] if found else None


def base_refusal(host: str, chair_sha: str, code: int, output: str, what: str = "repo") -> str | None:
    """None when the sync output shows the host's `what` (repo or harness) at `chair_sha`, else the refusal line naming both commits."""
    at = _tagged(output, "head ")
    if code == 0 and at == chair_sha:
        return None
    reason = _tagged(output, "sync: ") or ("head is not the chair's commit" if code == 0 else f"exited {code}")
    return f"{host}: {what} at {at or 'unknown'}, chair at {chair_sha}; sync failed: {reason}"


def _sync_argvs(path: str, head: ChairHead | None, *, detached_only: bool = False) -> list[list[str]]:
    """`sync_argvs` for a known chair head, else the old default-branch `sync_argv`. With `detached_only`, a head on a
    branch also keeps `sync_argv`: the repo moves to the chair's commit only at a release tag, since a branch or commit
    the chair has not pushed is not on the host's origin."""
    use_head = head is not None and not (detached_only and head.branch)
    return sync_argvs(path, head) if use_head else [sync_argv(path)]


def _sync_steps(ssh: str, path: str, head: ChairHead | None, *, detached_only: bool = False) -> list[list[str]]:
    return [ssh_argv(ssh, argv) for argv in _sync_argvs(path, head, detached_only=detached_only)]


def _sync_path(path: str, host_checkouts: Sequence[str], home: str | None) -> str:
    """The path to sync on the lane host: its matching checkout, else the home-relative form when `home` is given, else `path`."""
    found = _checkout(path, host_checkouts)
    if found is not None:
        return found
    return path if home is None else repo_path.host_form(path, home)


def launch_plan(
    host: LaneHost,
    initiative: str,
    run_id: str,
    label: str,
    locate: Callable[[str], str] | None = None,
    repo: str | None = None,
    harness_dir: str | None = None,
    relaunch: Relaunch | None = None,
    *,
    home: str | None = None,
    host_checkouts: Sequence[str] = (),
    harness_head: ChairHead | None = None,
    repo_head: ChairHead | None = None,
    base: ChairBase | None = None,
    harness_base: ChairBase | None = None,
) -> list[list[str]]:
    """The argv that `launch_on_host` runs in order: the rsync push, then, when `harness_dir` is
    given, the sync of the harness on the lane host, then, when `repo` is given, the sync of `repo`
    there, then, for a `relaunch` with a `repo`, a force push of the epic branches and of each task
    branch of `relaunch.prior_run` to the lane host, then the launch ssh command.

    rsync needs the parent of the destination to exist; the copy keeps the local work/<id> layout.
    `route launch` reads --initiative as a path from its cwd, and an ssh command starts in the home directory.
    `repo` may be tilde-prefixed or recorded under another machine's home, so with `home` given it is mapped
    per use: home-relative for the ssh sync, tilde form for the push destination, expanded for the local side.
    Without `home` it is passed through unchanged. `harness_dir` is the git checkout holding the harness (on a
    migrated host the coxswain checkout, whose `src/graphs` is the profile's harness_dir). It and `repo` each sync at the entry of
    `host_checkouts` with the same basename when there is one, and the repo is pushed to that entry too.
    The harness goes to `harness_base` and the repo to `base`, each the chair's `origin/<base>` commit for that checkout, in
    one `base_sync_argv` step that also checks the result. With no `harness_base` the harness follows `harness_head`
    (`sync_argvs`); with no `base` the repo follows `repo_head` only when detached (see `_sync_argvs`)."""
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    src = f"work/{initiative}"
    remote_dir = f"{host.workspace_dir.rstrip('/')}/{src}"
    # The local work/<initiative> is the source: a ticket moved or dropped here must not linger on the lane host,
    # where a second copy of one id fails the run's DAG check.
    rsync_step = rsync_push_argv(src, place(remote_dir), mirror=True)
    launch_step = ssh_argv(host.ssh, launch_argv(remote_dir, run_id, label))
    harness_steps = (
        []
        if harness_dir is None
        else [ssh_argv(host.ssh, base_sync_argv(_sync_path(harness_dir, host_checkouts, home), harness_base.branch, harness_base.sha))]
        if harness_base is not None
        else _sync_steps(host.ssh, _sync_path(harness_dir, host_checkouts, home), harness_head)
    )
    checkout = None if repo is None else _checkout(repo, host_checkouts)
    push_dest = checkout or (None if repo is None or home is None else repo_path.contract(repo, home))
    local_default = None if repo is None or home is None else repo_path.expand(repo, home)
    repo_steps = (
        []
        if repo is None
        else [ssh_argv(host.ssh, base_sync_argv(_sync_path(repo, host_checkouts, home), base.branch, base.sha))]
        if base is not None
        else _sync_steps(host.ssh, _sync_path(repo, host_checkouts, home), repo_head, detached_only=True)
    )
    push_steps = (
        []
        if relaunch is None or repo is None
        else [
            branch_push_argv(
                relaunch.local_repo or local_default or repo,
                host.ssh,
                push_dest or repo,
                refspec,
            )
            for _, refspec in _relaunch_branches(initiative, relaunch)
        ]
    )
    return [rsync_step, *harness_steps, *repo_steps, *push_steps, launch_step]


def env_preflight(names: tuple[str, ...], probes: Mapping[str, tuple[int, str]]) -> str | None:
    """None when every name's probe (printenv exit code, text) is a non-empty value. Exit 1 is unset; any other non-zero is ssh failing."""
    if not names:
        return "no auth_env or endpoint_env configured: nothing to check on the host"
    broken = next((probes[name][1] for name in names if probes[name][0] not in (0, 1)), None)
    if broken is not None:
        return f"env var check failed on the host: {broken.strip()[:80]}"
    verdict = env_verdict(names, lambda name: probes[name][1] if probes[name][0] == 0 else "")
    return None if verdict is None else f"{verdict} (set it there)"


def launch_on_host(
    host: LaneHost,
    initiative: str,
    run_id: str,
    label: str,
    launched_at: str,
    run: Callable[[list[str]], int],
    locate: Callable[[str], str] | None = None,
    repo: str | None = None,
    harness_dir: str | None = None,
    relaunch: Relaunch | None = None,
    *,
    preflight: str | None = None,
    home: str | None = None,
    host_checkouts: Sequence[str] = (),
    chair_head: Callable[[str], ChairHead | None] = _local_head,
    chair_base: Callable[[str], ChairBase | LaunchError | None] = _chair_base,
    probe: Callable[[list[str]], tuple[int, str]] = _probe,
    tools: Sequence[str] = (),
) -> dict | LaunchError:
    """`preflight` is a refusal line from a check made before anything is copied; None lets the launch go on.
    `tools` are the executables the repo's checks run: one `probe` of the host's lane PATH, before the copy, refuses
    the launch naming the first one it lacks.
    `chair_base` fetches a chair checkout and reads its `origin/<base>` commit; the host's harness and repo are each
    brought to it and the launch is refused, naming both commits, unless each is there (`probe` runs that step).
    A chair path with no git checkout or no origin has no base: it keeps the old unguarded sync and records no `base_sha`.
    An origin whose `origin/<base>` does not resolve refuses. The harness now follows origin/<base>, not the commit the
    chair runs (`chair_head`), so a chair on a tagged harness can differ from its lane's harness."""
    if preflight is not None:
        return LaunchError("auth", preflight)
    if relaunch is not None and repo is None:
        return LaunchError(
            "push", f"a relocated relaunch to {host.name} has no repo to push epic/{initiative}/* into: nothing was run"
        )
    missing = lane_toolchain.refusals(host.name, tools, ssh_argv(host.ssh, lane_toolchain.lane_argv(tools)), probe)
    if missing:
        return LaunchError("toolchain", missing[0])
    given_local = None if relaunch is None else relaunch.local_repo
    chair_repo = None if repo is None else given_local or (repo if home is None else repo_path.expand(repo, home))
    harness_head = None if harness_dir is None else chair_head(harness_dir)
    repo_head = None if chair_repo is None else chair_head(chair_repo)
    base = None if chair_repo is None else chair_base(chair_repo)
    harness_base = None if harness_dir is None else chair_base(harness_dir)
    for resolved in (base, harness_base):
        if isinstance(resolved, LaunchError):
            return LaunchError(resolved.step, f"{host.name}: {resolved.message}")
    plan = launch_plan(
        host, initiative, run_id, label, locate, repo, harness_dir, relaunch,
        home=home, host_checkouts=host_checkouts, harness_head=harness_head, repo_head=repo_head, base=base,
        harness_base=harness_base,
    )
    rsync_argv, ssh_cmd = plan[0], plan[-1]
    pushed = run(rsync_argv)
    if pushed != 0:
        return LaunchError("rsync", f"rsync of work/{initiative} to {host.name} exited {pushed}")
    next_step = 1
    if harness_dir is not None and harness_base is not None:
        refused = base_refusal(host.name, harness_base.sha, *probe(plan[next_step]), what="harness")
        next_step += 1
        if refused is not None:
            return LaunchError("harness", f"{refused}: the lane would run a harness other than the chair's origin/{harness_base.branch}")
    elif harness_dir is not None:
        steps = len(_sync_argvs(harness_dir, harness_head))
        updated = next((code for code in map(run, plan[next_step : next_step + steps]) if code != 0), 0)
        next_step += steps
        if updated != 0:
            return LaunchError(
                "harness",
                f"updating the harness at {_sync_path(harness_dir, host_checkouts, home)} on {host.name} exited {updated}: "
                "the lane would run an older harness than the store",
            )
    if repo is not None and base is not None:
        refused = base_refusal(host.name, base.sha, *probe(plan[next_step]))
        next_step += 1
        if refused is not None:
            return LaunchError("sync", refused)
    elif repo is not None:
        steps = len(_sync_argvs(repo, repo_head, detached_only=True))
        synced = next((code for code in map(run, plan[next_step : next_step + steps]) if code != 0), 0)
        next_step += steps
        if synced != 0:
            return LaunchError(
                "sync", f"updating {repo} on {host.name} exited {synced}: the lane would build on a stale main"
            )
    branches = [] if relaunch is None else [branch for branch, _ in _relaunch_branches(initiative, relaunch)]
    for branch, push_step in zip(branches, plan[next_step:-1], strict=True):
        pushed_branch = run(push_step)
        if pushed_branch != 0:
            return LaunchError(
                "push",
                f"pushing {branch} to {host.name} exited {pushed_branch}: the lane would not have the initiative's branches",
            )
    started = run(ssh_cmd)
    if started != 0:
        return LaunchError("ssh", f"starting the lane on {host.name} exited {started}")
    return remote_record(host.name, launched_at, repo, None if base is None else base.sha)
