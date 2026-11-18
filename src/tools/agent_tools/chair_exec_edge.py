"""The chair's executor, its edges: the git, forge, store and rebase doors, and the deps production wires."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from agent_tools import (
    chair,
    chair_apply_review,
    chair_login_check,
    chair_login_watch,
    forge_auto,
    host_cmd,
    queue_rows,
    remote_fetch,
    route,
    run_store,
    store_cli,
)
from agent_tools.chair_carry_exec import ForgePort, GitPort, StorePort
from agent_tools.chair_exec_act import PR_URL_RE, SMOKE_REPOS, current_user, phase_task_states
from agent_tools.chair_exec_land import (
    LAND_TIMEOUT_S,
    LOCAL_ARGV_TIMEOUT_S,
    NOT_PROBED,
    ArgvResult,
    Deps,
    Fetch,
    Result,
    Run,
    TargetUnreadable,
    delete_branches_with,
    initiative_row,
    local_repo_path,
    run_argv,
    run_argv_split,
    target_for,
    ticket_paths_of,
)
from agent_tools.chair_types import Action, LandTrigger
from agent_tools.land import approve_to_done
from agent_tools.remote_argv import LANE_HOST_TIMEOUT_S, incoming_ref, probe_argv


def _pr_from_reason(reason: str) -> int:
    """The PR number a land's captured output names: its `pr_create: <url>` line (`cli.py`'s `_land_walk`)
    always runs before `merge:`/`mark_done:` on a true `landed()` result, so it is already in `reason`; 0
    when no such line is there."""
    match = PR_URL_RE.search(reason)
    return int(match[1]) if match else 0


def smoke_targets(results: list[Result]) -> list[LandTrigger]:
    """A `LandTrigger` for every landed result in coxswain (or the old coxswain-tools/-graphs), for the tick wiring's
    post-land smoke; any other repo is skipped. An action's repo is a checkout
    path, not a bare name, so the match is on its last component and the trigger keeps the full path for `git -C`."""
    return [
        {"repo": r["action"].get("repo", ""), "pr": _pr_from_reason(r["reason"]), "commit": r.get("commit", "")}
        for r in results
        if r["status"] == "landed" and Path(r["action"].get("repo", "")).name in SMOKE_REPOS
    ]


def run_lane_host(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Edge. One ssh against a lane host, bounded: a hung host comes back as exit 124 carrying its partial output."""
    return run_argv(argv, cwd, timeout=LANE_HOST_TIMEOUT_S)


def reaches_lane_host(argv: list[str]) -> bool:
    """An `ssh` argv, or a `git push`, which the chair only sends to a lane host's `<ssh>:<repo>`."""
    return argv[:1] == ["ssh"] or (argv[:1] == ["git"] and "push" in argv)


def local_timeout(argv: list[str]) -> float:
    """`LAND_TIMEOUT_S` for a `cox runs land`, `LOCAL_ARGV_TIMEOUT_S` for any other local argv."""
    return LAND_TIMEOUT_S if argv[:3] == ["cox", "runs", "land"] else LOCAL_ARGV_TIMEOUT_S


def _bounded_for_ssh(cwd: Path) -> Run:
    """Edge. A door that bounds an argv reaching a lane host by `LANE_HOST_TIMEOUT_S` and any local argv by `local_timeout`."""
    def run(argv: list[str]) -> tuple[int, str]:
        return run_lane_host(argv, cwd) if reaches_lane_host(argv) else run_argv(argv, cwd, timeout=local_timeout(argv))

    return run


def _local_only(cwd: Path) -> Run:
    """Edge. A dry run's door: a local argv runs as usual, one that reaches a lane host is refused unrun with exit 1."""
    def run(argv: list[str]) -> tuple[int, str]:
        return (1, NOT_PROBED) if reaches_lane_host(argv) else run_argv(argv, cwd)

    return run


def _not_probed(argv: list[str]) -> None:
    return None


def fenced_fetch(fetch: Fetch, dry_run: bool) -> Fetch:
    """The door a remote-lane probe reaches a host by: `fetch` itself live, and one that never dials in a dry run."""
    return _not_probed if dry_run else fetch


def read_intake_id(workspace: Path, path: str) -> str:
    """Edge. The `id` field of the intake file at `workspace/path`; "" when unreadable or absent."""
    try:
        fields, _ = route.parse_frontmatter((workspace / path).read_text(encoding="utf-8"))
    except OSError:
        return ""
    value = fields.get("id", "")
    return value if isinstance(value, str) else ""


def _host_row(runs_dir: Path, host: str) -> dict:
    """Edge. The hosts-table row named `host`; an empty row when the table has none by that name."""
    return next((row for row in run_store.hosts(runs_dir) if str(row.get("name")) == host), {})


def _cli_run(runs_dir: Path, argv: list[str]) -> dict:
    """Edge. Runs `argv` through `store_cli.runner(runs_dir)`; `store_cli.reply_row` of what it answers."""
    return store_cli.reply_row(*store_cli.runner(runs_dir)(argv))


def _login_runner_and_env(profile: dict) -> tuple[str, tuple[str, ...]]:
    """A provider profile's check_login `runner` (its `runner` key, or today's default `"claude-code"`) and
    `env_names`: the tuple of whichever of `auth_env`, `endpoint_env` the profile sets, in that order,
    skipping an absent one."""
    runner = profile.get("runner") or "claude-code"
    env_names = tuple(v for v in (profile.get("auth_env"), profile.get("endpoint_env")) if v)
    return runner, env_names


def _check_login_edge(runs_dir: Path, ssh_run: Callable[[list[str]], ArgvResult], provider_profile: Callable[[], dict], host: str) -> dict:
    """Edge. `check_login_on_host` for `host`, its ssh and versions read from the hosts table, "now" read
    from the clock at call time -- never at `edge_deps` construction time. Its `runner` and `env_names`
    come from `provider_profile()`, resolved fresh on each call through `_login_runner_and_env`."""
    row = _host_row(runs_dir, host)
    ssh = str(row.get("ssh", ""))
    current_versions = chair_login_watch.versions_of(row)
    now = datetime.now(UTC).isoformat()
    runner, env_names = _login_runner_and_env(provider_profile())
    return chair_login_check.check_login_on_host(
        host, ssh, current_versions, now, ssh_run, partial(_cli_run, runs_dir), runner=runner, env_names=env_names,
    )


def _probe_host_edge(runs_dir: Path, ssh_run: Callable[[list[str]], ArgvResult], host: str) -> dict:
    """Edge. One `probe_argv` ssh to `host`, its ssh read from the hosts table, "now" read at call time. Always
    merges probe_checked_at through `cox host login`. Exit 0 also sets the host active and clears the unreachable
    fields; any other exit changes nothing else. Never writes beat_at. A write the store refuses is answered in place of
    the row, as `store_cli.reply_row` gives it."""
    ssh = str(_host_row(runs_dir, host).get("ssh", ""))
    now = datetime.now(UTC).isoformat()
    reachable = ssh_run(probe_argv(ssh)).returncode == 0
    login = {"probe_checked_at": now}
    if reachable:
        login = {**login, "unreachable_failures": 0, "unreachable_since": None, "host_back_at": now}
    writes = [host_cmd.set_state_argv(host, "active", "probe")] if reachable else []
    replies = [_cli_run(runs_dir, argv) for argv in [*writes, ["host", "login", host, "--login-json", json.dumps(login, sort_keys=True)]]]
    failed = next((r for r in replies if store_cli.STORE_ERROR in r), None)
    return failed if failed is not None else _host_row(runs_dir, host)


_CHECKS_TIMEOUT_S = 600.0
_CHECK_COMMAND_TIMEOUT_S = 900.0


def _must(code: int, output: str, what: str) -> str:
    if code != 0:
        raise RuntimeError(f"{what}: {output.strip()}")
    return output


def _stdout_of(done: ArgvResult) -> tuple[int, str]:
    """The exit code and stdout alone, which `remote_fetch.settle_phase_refs` parses."""
    return done.returncode, done.stdout


class _GitEdge:
    """Edge. GitPort over one throwaway worktree of `repo`; the main checkout is never switched or written.

    Every base is `origin/<default branch>` after a `git fetch origin`; local `main` is never read, so it cannot be stale."""

    def __init__(self, runs_dir: Path, workspace: Path, repo: str, home: str | None = None, target: str | None = None,
                 capacity: int | None = None) -> None:
        self.runs_dir, self.workspace, self.repo, self.tmp, self.home, self.target = runs_dir, workspace, repo, "", home, target
        self.capacity = capacity  # the lane cap `.agent-checks` divides this host's cores by; None leaves them uncapped
        self.local_repo = local_repo_path(repo, home or "")  # every `-C` here runs on this machine; `self.repo` stays as recorded

    def _git(self, *args: str) -> tuple[int, str]:
        return run_argv(["git", "-C", self.tmp or self.local_repo, *args])

    def default_branch(self) -> str:
        """What `origin/HEAD` names; "main" when it names nothing."""
        code, out = run_argv(["git", "-C", self.local_repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        name = out.strip().removeprefix("origin/") if code == 0 else ""
        return name or "main"

    def landing_branch(self) -> str:
        """The initiative's target branch when it has one, else `default_branch()`."""
        return self.target or self.default_branch()

    def fetch_branch(self, host: str, branch: str) -> list[str]:
        """The run's branch from its host, reached as `<ssh>:<repo on the host>` the way `remote_fetch` reaches it. An empty host is here.

        The branch lands under refs/coxswain/incoming/<host>/ and `remote_fetch.settle_phase_refs` moves the local
        branch forward only, so a phase branch holding merges the host lacks is never reset (2026-11-11). The lines
        name any diverged branch and the side ref its fetched tip was kept at."""
        if not host:
            return []
        lane = next(iter(host_cmd.host_rows_to_lane_hosts([{**_host_row(self.runs_dir, host), "state": "active"}])), None)
        if lane is None or not lane.workspace_dir:
            raise RuntimeError(f"host {host} has no ssh or workspace_dir recorded")
        host_repo = remote_fetch.host_repo_path(str(self.workspace), lane.workspace_dir, self.repo, self.home)
        _must(*self._git("fetch", f"{lane.ssh}:{host_repo}", f"+refs/heads/{branch}:{incoming_ref(host, branch)}"),
              f"git fetch {branch} from {host}")
        return remote_fetch.settle_phase_refs(self.local_repo, host, lambda argv: _stdout_of(run_argv_split(argv)))

    def create_branch_from_main(self, name: str) -> None:
        """Fetches origin, then cuts `name` from `origin/<default branch>` in a new worktree; a failed fetch raises."""
        _must(*run_argv(["git", "-C", self.local_repo, "fetch", "origin"]), "git fetch origin")
        start = f"origin/{self.landing_branch()}"
        tmp = tempfile.mkdtemp(prefix="cox-carry-")
        code, out = run_argv(["git", "-C", self.local_repo, "worktree", "add", "-B", name, tmp, start])
        if code != 0:
            Path(tmp).rmdir()
            raise RuntimeError(f"git worktree add {name} from {start}: {out.strip()}")
        self.tmp = tmp

    def cherry_pick(self, commit: str) -> list[str]:
        code, out = self._git("cherry-pick", commit)
        if code == 0:
            return []
        conflicts = [line for line in self._git("diff", "--name-only", "--diff-filter=U")[1].splitlines() if line.strip()]
        self._git("cherry-pick", "--abort")
        if not conflicts:
            raise RuntimeError(f"git cherry-pick {commit}: {out.strip()}")
        return conflicts

    def push(self, branch: str) -> None:
        ok, detail = forge_auto.push(self.tmp, branch)
        if not ok:
            raise RuntimeError(f"push of {branch}: {detail}")

    def run_checks(self) -> list[str]:
        """The failing lines of the worktree's `.agent-checks`, each run through bash there; blanks and comments skipped."""
        try:
            lines = (Path(self.tmp) / ".agent-checks").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        commands = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
        from harness.lane_workers import check_env
        env = check_env(os.environ, os.cpu_count() or 1, self.capacity)  # None inherits the chair's own environment
        return [c for c in commands if run_argv(["bash", "-c", c], cwd=Path(self.tmp), timeout=_CHECK_COMMAND_TIMEOUT_S, env=env)[0] != 0]

    def already_on_main(self, commit: str) -> bool:
        return self._git("merge-base", "--is-ancestor", commit, f"origin/{self.landing_branch()}")[0] == 0

    def close(self) -> None:
        if self.tmp:
            run_argv(["git", "-C", self.local_repo, "worktree", "remove", "--force", self.tmp])


def _pr_url(detail: str) -> str:
    """The URL `gh pr create` prints as its last line; "" when the forge printed none, as the local forge does."""
    last = detail.strip().splitlines()[-1].strip() if detail.strip() else ""
    return last if last.startswith("http") else ""


class _ForgeEdge:
    """Edge. ForgePort over `forge_auto` in the git edge's worktree; the carry's PR body already carries the footer."""

    def __init__(self, git: _GitEdge) -> None:
        self.git, self.branch, self.pr_url = git, "", ""

    def open_pr(self, branch: str, title: str, body: str) -> str:
        ok, detail = forge_auto.open_pr(self.git.tmp, title, body, head=branch, base=self.git.landing_branch())
        if not ok:
            raise RuntimeError(f"open PR for {branch}: {detail}")
        self.branch, self.pr_url = branch, _pr_url(detail)
        return self.pr_url or detail

    def wait_checks(self, pr: str) -> list[str]:
        ok, detail = forge_auto.wait_checks(self.git.tmp, _CHECKS_TIMEOUT_S)
        return [] if ok else [detail]

    def merge(self, pr: str) -> None:
        ok, detail = forge_auto.merge(self.git.tmp, {"branch": self.branch, "default_branch": self.git.landing_branch()})
        if not ok:
            raise RuntimeError(f"merge of {self.branch}: {detail}")


class _StoreEdge:
    """Edge. StorePort over `store_cli`, then the ticket file, the way the review path closes a landed task."""

    def __init__(self, runs_dir: Path, work_dir: Path, action: Action, forge: _ForgeEdge, now: Callable[[], str]) -> None:
        self.runs_dir, self.work_dir, self.action, self.forge, self.now = runs_dir, work_dir, action, forge, now

    def mark_landed(self, task: str, run: str) -> None:
        """Stamps the PR url `open_pr` returned. With no PR (every pick already on main) there is nothing to stamp."""
        if not self.forge.pr_url:
            return
        result = store_cli.mark_landed(self.runs_dir, run, self.action["phase"], task, self.forge.pr_url, self.now())
        error = chair_apply_review.stamped_reason(result)
        if error:
            raise RuntimeError(error)

    def set_done(self, task: str) -> None:
        initiative = self.action["initiative"]
        error = chair_apply_review.set_done_reason(store_cli.set_state(self.runs_dir, initiative, task, "done", "chair"))
        if error:
            raise RuntimeError(error)
        path = ticket_paths_of(self.work_dir, initiative).get(task)
        if path is not None:
            new_text, _ = approve_to_done(path.read_text(encoding="utf-8"), merged=True)
            if new_text is not None:
                path.write_text(new_text, encoding="utf-8")


class _RebaseEdge:
    """Edge. RebasePort over the remote: tips are read with ls-remote and pushes never check anything out."""

    def __init__(self, repo: str) -> None:
        self.repo = local_repo_path(repo)  # the remote's tips are read from this machine's checkout

    def _git(self, *args: str) -> tuple[int, str]:
        return run_argv(["git", "-C", self.repo, *args])

    def _remote_tip(self, ref: str) -> str | None:
        out = _must(*self._git("ls-remote", "origin", f"refs/heads/{ref}"), f"git ls-remote {ref}")
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name.strip() == f"refs/heads/{ref}":
                return sha
        return None

    def branch_tip(self, branch: str) -> str | None:
        return self._remote_tip(branch)

    def create_ref(self, ref: str, sha: str) -> None:
        _must(*self._git("push", "origin", f"{sha}:refs/heads/{ref}"), f"git push backup {ref}")

    def recreate_branch(self, branch: str, base: str) -> None:
        """Fetches origin, then force-pushes `origin/<base>` (the tip the planner read, never local main) as the branch."""
        _must(*self._git("fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"), f"git fetch origin {base}")
        _must(*self._git("push", "--force", "origin", f"origin/{base}:refs/heads/{branch}"), f"git push --force {branch}")

    def ref_exists(self, ref: str) -> bool:
        return self._remote_tip(ref) is not None


def _carry_ports(
    runs_dir: Path, workspace: Path, repo_for: Callable[[Action], str], now: Callable[[], str], home: str,
    target_branch: Callable[[str], str | None] | None, action: Action, capacity: int | None = None,
) -> tuple[GitPort, ForgePort, StorePort]:
    initiative = action.get("initiative", "")
    target = target_branch(initiative) if target_branch is not None and initiative else None
    git = _GitEdge(runs_dir, workspace, action.get("repo") or repo_for(action), home, target, capacity)
    forge = _ForgeEdge(git)
    return git, forge, _StoreEdge(runs_dir, workspace, action, forge, now)


def _target_branch_edge(runs_dir: Path, workspace: Path, cartridge_target: str | None, initiative: str) -> str | None:
    """Edge. Reads the initiative's row, then its own epic's file only when the row sets no branch, then `target_for`.
    A value that cannot be told raises TargetUnreadable; None means no branch is set anywhere."""
    from core.epic_files import parse_epic

    rows = run_store.read_queue(runs_dir, initiative)
    row = initiative_row(initiative, rows)
    if row is None and (workspace / "work" / initiative / "initiative.md").is_file():
        raise TargetUnreadable(f"{initiative} has an initiative file but no queue row, so the store may be unreadable")
    epic = queue_rows.epic_of_row(row) if row is not None and queue_rows.target_branch_of_row(row) is None else None
    epics: dict[str, Mapping[str, Any]] = {}
    path = workspace / "epics" / f"{epic}.md"
    if epic is not None and path.is_file():
        try:
            epics[epic] = parse_epic(path.read_text(encoding="utf-8"), path.name)
        except (ValueError, OSError) as exc:
            raise TargetUnreadable(f"epic file {path}: {exc}") from exc
    try:
        return target_for(initiative, rows, epics, cartridge_target)
    except ValueError as exc:
        raise TargetUnreadable(str(exc)) from exc


def lease_guard(record: dict[str, Any] | None, own: tuple[str, int, str], pid_alive_: bool, now: datetime, host: str) -> str:
    """Pure. The refusal line when a foreign holder is live and unexpired, else "". A passed `until` reads live in `chair.liveness`, so it is excluded here to keep the expired takeover working."""
    if record is None or chair.same_holder(record, *own):
        return ""
    if chair.liveness(record, pid_alive_, now, host) != "live" or chair.expired(record, now):
        return ""
    return f"chair: held by {record.get('session', '?')} (live)"


def foreign_holder_gone(record: dict[str, Any] | None, own: tuple[str, int, str], pid_alive_: bool, now: datetime) -> bool:
    """Pure. A foreign holder is dead or expired: `until` passed, or its pid is dead on this host. A pid on another host says nothing."""
    if record is None or chair.same_holder(record, *own):
        return False
    return chair.expired(record, now) or (record.get("host") == own[2] and not pid_alive_)


def _lease_state(runs_dir: Path, session: str, pid: int, host: str) -> tuple[str, bool]:
    """Edge. Reads chair.json once: (refusal line, take over a gone holder). A missing, unreadable or non-object record gives ("", False)."""
    try:
        record = chair.read(runs_dir)
    except (OSError, ValueError):
        return "", False
    if not isinstance(record, dict):
        return "", False
    held_pid = record.get("pid")
    alive = isinstance(held_pid, int) and chair.pid_alive(held_pid)
    own, now = (session, pid, host), datetime.now(UTC)
    return lease_guard(record, own, alive, now, host), foreign_holder_gone(record, own, alive, now)


def edge_deps(
    runs_dir: Path,
    workspace: Path,
    session: str,
    pid: int,
    run_id: Callable[[Action], str],
    repo_for: Callable[[Action], str],
    record: Callable[[Action], None],
    host: str,
    harness_python: str,
    log_retention_days: int = 7,
    ids_mode: str = "slug",
    provider_profile: Callable[[], dict] = lambda: {},
    dry_run: bool = False,
    cartridge_target: str | None = None,
    lane_capacity: int | None = None,
) -> Deps:
    """Edge. The real bundle: subprocess for cox and git, chair.acquire_lease for the lease.

    `cartridge_target` is the cartridge's `landing_areas.target_branch`, the lowest rung of an initiative's target;
    unset, only the initiative row and its epic file name one.

    `lane_capacity` is the lane cap the carry's `.agent-checks` run under; None leaves them uncapped.

    A dry run drops every door to a lane host: `run` refuses an ssh or push argv unrun and `check_login` and `probe_host` return no row.

    Every subprocess runs in `workspace`, because `cox route launch epic` reads `work/<id>/initiative.md` from its cwd.
    A take_lease action names no holder, so the lease is always taken as this loop's own session, pid and host.
    `provider_profile` is the routing profile's `provider_profile` YAML, already resolved; unset, it names no
    runner override and `check_login` keeps today's `"claude-code"` default.
    """
    home = str(Path.home())
    run = _local_only(workspace) if dry_run else _bounded_for_ssh(workspace)
    login_run = partial(run_argv_split, cwd=workspace, timeout=LANE_HOST_TIMEOUT_S)
    live_check = partial(_check_login_edge, runs_dir, login_run, provider_profile)
    live_probe = partial(_probe_host_edge, runs_dir, login_run)

    def acquire(_holder: str, _host: str, steal: bool = False, takeback: bool = False) -> str:
        if takeback:
            return chair.acquire_lease(runs_dir, session, pid, host, steal=True)
        refusal, takeover = _lease_state(runs_dir, session, pid, host)
        return refusal or chair.acquire_lease(runs_dir, session, pid, host, steal=steal or takeover)

    target_branch = partial(_target_branch_edge, runs_dir, workspace, cartridge_target)

    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: delete_branches_with(run, local_repo_path(repo, home), pattern),
        acquire_lease=acquire,
        record=record,
        run_id=run_id,
        repo_for=repo_for,
        ssh_for=lambda name: str(_host_row(runs_dir, name).get("ssh", "")),
        intake_id=partial(read_intake_id, workspace),
        initiative_rows=partial(run_store.read_queue, runs_dir),
        runs_dir=runs_dir,
        work_dir=workspace,
        check_login=(lambda name: {}) if dry_run else live_check,
        probe_host=(lambda name: {}) if dry_run else live_probe,
        log_retention_days=log_retention_days,
        harness_python=harness_python,
        ids_mode=ids_mode,
        home=home,
        carry_ports=partial(_carry_ports, runs_dir, workspace, repo_for, lambda: datetime.now(UTC).isoformat(), home, target_branch, capacity=lane_capacity),
        target_branch=target_branch,
        rebase_port=lambda action: _RebaseEdge(action.get("repo") or repo_for(action)),
        phase_states=lambda initiative, phase: phase_task_states(run_store.work_items(runs_dir, initiative), phase),
        temp_root=Path(tempfile.gettempdir()),
        user=current_user(),
    )
