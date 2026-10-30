import json
import shlex
import signal
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_tools import chair_exec, chair_housekeeping, chair_pid_probe, cli, courier, remote_lane, run_store
from agent_tools.chair_exec import (
    Deps,
    Refusal,
    Run,
    already_landed,
    argv_for,
    decompose_id,
    delete_branches_with,
    failed_reason,
    land_commit,
    land_refusal,
    launch_verdict,
    perform,
    phase_task_states,
    run_argv,
    run_lane_host,
    smoke_targets,
    tail,
)
from agent_tools.chair_facts import STRANDED_CAUSE
from agent_tools.chair_report import format_status
from agent_tools.draft_apply import plan_approve
from agent_tools.remote_argv import ssh_argv, sync_argv
from agent_tools.store_url import TracesRoot

LANDED = "merge: ok\nmark_done: ok\n"


def _deps(calls: list, output: str = LANDED, code: int = 0, deleted: tuple[str, ...] = ("epic/alpha/t1",)) -> Deps:
    def run(argv):
        calls.append(("run", argv))
        return code, output

    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: calls.append(("delete", repo, pattern)) or (list(deleted), ""),
        acquire_lease=lambda holder, host: calls.append(("lease", holder)) or "",
        record=lambda action: calls.append(("record", action["kind"])),
        run_id=lambda action: "run-1",
        repo_for=lambda action: action.get("repo", "r"),
        rev_parse=lambda repo, ref: None,
    )


def _touched(calls: list) -> list:
    """The calls that reached cox, git or the lease: everything but the record."""
    return [c for c in calls if c[0] != "record"]


WIDEN = {
    "kind": "widen_ticket", "initiative": "i", "task_id": "t1",
    "paths": ["src/app.rs"], "additions": ["accessor"], "reason": "handoff", "epoch": 1,
}


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ({"kind": "fetch", "run": "r-1"}, ["cox", "runs", "fetch", "r-1"]),
        ({"kind": "fetch_exit", "run": "r-1", "initiative": "i"}, ["cox", "runs", "fetch", "r-1"]),
        (
            {"kind": "land_phase", "run": "r-1", "phase": "p1", "repo": "r"},
            ["cox", "runs", "land", "r-1", "--phase", "p1", "--repo", "r", "--apply", "--no-claim"],
        ),
        ({"kind": "land_phase", "run": "r-1", "repo": "r"}, None),
        (WIDEN, None),
    ],
    ids=["fetch", "fetch_exit", "land_phase", "land_phase_without_phase", "widen_ticket"],
)
def test_argv_for_fetch_land_phase_and_in_process_kinds(action, expected) -> None:
    assert argv_for(action) == expected


@pytest.mark.parametrize(
    ("code", "output", "status", "reason"),
    [
        (0, "", "done", "fetched r-1; approved: t1, t2"),
        (1, "boom", "failed", "boom"),
        (2, "fetch: refuse: r-1 is still live on host-a: the lane has not ended", "skipped", "fetch: refuse: r-1 is still live on host-a: the lane has not ended"),
        (2, "fetch: ssh: connection refused", "failed", "fetch: ssh: connection refused"),
    ],
    ids=["exit_zero", "exit_nonzero", "live_refusal", "fetch_error"],
)
def test_a_fetch_exit_applies_approvals_only_on_exit_zero_and_keeps_the_fetch_reason(tmp_path, monkeypatch, code, output, status, reason) -> None:
    remote_lane.remote_record_path(tmp_path, "r-1").write_text(
        json.dumps({"host": "host-a", "launched_at": "2026-09-26T00:00:00+00:00"}), encoding="utf-8"
    )
    calls: list = []
    monkeypatch.setattr(chair_exec.chair_apply_fetch, "apply_fetched_approvals", lambda *a: calls.append(a) or ["t1", "t2"])
    deps = replace(_deps([], code=code, output=output), runs_dir=tmp_path, work_dir=tmp_path)
    results = perform([{"kind": "fetch_exit", "run": "r-1", "initiative": "i", "epoch": 1}], deps, lambda: 1, False)
    assert calls == ([(tmp_path, tmp_path, "r-1", "i")] if code == 0 else [])
    assert (results[0]["status"], results[0]["reason"]) == (status, reason)
    if status == "done":
        assert (results[0]["run"], results[0]["host"]) == ("r-1", "host-a")
    else:
        assert "run" not in results[0] and "host" not in results[0]


VERDICT = "fetch: verify: r-1 has no approved tasks in the store"
LAST_BRANCH = "epic/alpha/t20 -> epic/alpha/t20"
GIT_STDERR = "".join(f" * [new branch]      epic/alpha/t{n:02d} -> epic/alpha/t{n:02d}\n" for n in range(1, 21))


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("", "one\ntwo\n", "one\ntwo"),
        (f"\n{VERDICT}\nsecond line\n", "", VERDICT),
        ("", "", ""),
    ],
    ids=["stderr_tail", "stdout_verdict", "both_empty"],
)
def test_failed_reason_is_the_stdout_verdict_else_the_stderr_tail(stdout, stderr, expected) -> None:
    assert failed_reason(stdout, stderr) == expected


@pytest.mark.parametrize("stdout", [VERDICT + "\n", ""], ids=["with_verdict", "stderr_only"])
def test_failed_reason_cuts_long_stderr_to_600_chars_keeping_a_leading_verdict_and_the_last_line(stdout) -> None:
    reason = failed_reason(stdout, GIT_STDERR)
    assert len(GIT_STDERR) > 600
    assert reason.startswith(stdout)
    assert reason.endswith(LAST_BRANCH)
    assert len(reason) <= 600


FETCH_ARGV = ["cox", "runs", "fetch", "r-1"]
LAUNCH_ARGV = ["cox", "route", "launch", "epic", "--initiative", "work/i", "--no-claim", "--on", "host-a"]
HELD_WARNINGS = "".join(f"warning: held export work/other-{n:03d}/export.json blocks nothing here\n" for n in range(50))
USAGE_HOLD = "routing: usage hold: weekly 91% of the ceiling"


@pytest.mark.parametrize(
    ("argv", "stdout", "stderr", "start", "end"),
    [
        (FETCH_ARGV, VERDICT + "\n", GIT_STDERR, "fetch: verify:", LAST_BRANCH),
        (LAUNCH_ARGV, "routing: X\n", HELD_WARNINGS, "routing: X\n", "blocks nothing here"),
        (
            LAUNCH_ARGV,
            "export: skipped 1 contentless row(s): x\nmerged: t1 + t2 -> t1 (shared a.py)\noverride: launching i despite blocked t0\n"
            f"{USAGE_HOLD}\nrouting: advisory: usage go_degraded: window 80%\nrouting: i-3 is already running (pid 42)\n",
            HELD_WARNINGS,
            "routing: i-3 is already running (pid 42)\n",
            "",
        ),
        (
            LAUNCH_ARGV,
            f"{USAGE_HOLD}\nrouting: /home/user/repo has uncommitted changes:\n M agent_tools/x.py\n?? notes.md\n\n",
            HELD_WARNINGS,
            "routing: /home/user/repo has uncommitted changes: M agent_tools/x.py; ?? notes.md\n",
            "",
        ),
        (
            LAUNCH_ARGV,
            "export: skipped 1 contentless row(s): x\nrefusing: the landing loop is held by s-1 (live); pass --force to override\n",
            "",
            "refusing: the landing loop is held by s-1",
            "",
        ),
    ],
    ids=["fetch", "launch_verdict_before_warnings", "launch_refusal_after_notes", "launch_dirty_repo", "launch_leader_guard"],
)
def test_a_failed_command_records_its_stdout_verdict_before_its_stderr_noise(monkeypatch, argv, stdout, stderr, start, end) -> None:
    done = chair_exec.ArgvResult(2, stdout, stderr, stdout + stderr)
    monkeypatch.setattr(chair_exec, "run_argv_split", lambda *a, **k: done)
    code, output = run_argv(argv)
    reason = chair_exec._recorded({"action": {"kind": "fetch_exit", "run": "r-1"}, "status": "failed", "reason": output})["reason"]
    assert code == 2
    assert reason.startswith(start)
    assert reason.endswith(end)
    assert len(reason) <= 600


def test_a_launch_verdict_is_the_last_routing_line_with_what_follows_else_the_last_line() -> None:
    assert launch_verdict(f"{USAGE_HOLD}\nrouting: launch on host-a failed at sync: exit 23\n") == "routing: launch on host-a failed at sync: exit 23"
    assert launch_verdict("routing: unknown lane host: host-b\nconfigured: host-a\n") == "routing: unknown lane host: host-b configured: host-a"
    assert launch_verdict("merged: a + b -> a (shared x)\nlaunch: refused\n") == "launch: refused"
    assert launch_verdict("\n") == ""


@pytest.mark.parametrize("code", [0, 124, 127])
def test_a_launch_that_succeeds_times_out_or_cannot_run_keeps_the_mixed_text(monkeypatch, code) -> None:
    done = chair_exec.ArgvResult(code, f"{USAGE_HOLD}\nrun i-3\n", "warn\n", f"{USAGE_HOLD}\nrun i-3\nwarn\n")
    monkeypatch.setattr(chair_exec, "run_argv_split", lambda *a, **k: done)
    assert run_argv(LAUNCH_ARGV) == (code, done.mixed)


def test_a_failed_land_keeps_the_mixed_text_so_the_conflict_line_survives(monkeypatch) -> None:
    done = chair_exec.ArgvResult(1, "merge: ok\n", "error: could not apply abc\n", "merge: ok\nerror: could not apply abc\n")
    monkeypatch.setattr(chair_exec, "run_argv_split", lambda *a, **k: done)
    assert run_argv(["cox", "runs", "land", "r-1"]) == (1, done.mixed)


def _widen_ticket_file(tmp_path: Path) -> Path:
    ticket = tmp_path / "work" / "i" / "p1" / "t1.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("---\nid: t1\nstate: blocked\nsurfaces: [agent_tools/a.py]\n---\nBody.\n", encoding="utf-8")
    return ticket


def test_a_widen_ticket_widens_in_process_mirrors_ready_to_the_store_and_records_the_task_and_path(tmp_path, monkeypatch) -> None:
    ticket = _widen_ticket_file(tmp_path)
    mirrored: list = []
    monkeypatch.setattr(chair_exec.store_cli, "mirror_state", lambda *a: mirrored.append(a) or None)
    calls: list = []
    rows: list = []
    deps = replace(_deps(calls), runs_dir=tmp_path / "runs", work_dir=tmp_path, record=rows.append)
    results = perform([WIDEN], deps, lambda: 1, False)
    assert _touched(calls) == []
    assert mirrored == [(tmp_path / "runs", "i", "t1", "ready", "chair")]
    assert (results[0]["status"], results[0]["reason"]) == ("done", "widened t1: src/app.rs")
    assert "state: ready" in ticket.read_text(encoding="utf-8")
    assert [(r["status"], r["reason"]) for r in rows] == [("done", "widened t1: src/app.rs")]


@pytest.mark.parametrize(
    ("with_ticket", "mirror_result", "status", "reason"),
    [
        (True, "warning: store did not record i/t1 as ready", "done", "widened t1: src/app.rs; warning: store did not record i/t1 as ready"),
        (False, None, "failed", "ticket not found"),
    ],
    ids=["mirror_warning", "no_ticket"],
)
def test_a_widen_ticket_carries_a_mirror_warning_and_fails_without_a_ticket(tmp_path, monkeypatch, with_ticket, mirror_result, status, reason) -> None:
    if with_ticket:
        _widen_ticket_file(tmp_path)
    mirrored: list = []
    monkeypatch.setattr(chair_exec.store_cli, "mirror_state", lambda *a: mirrored.append(a) or mirror_result)
    results = perform([WIDEN], replace(_deps([]), work_dir=tmp_path), lambda: 1, False)
    assert (results[0]["status"], results[0]["reason"]) == (status, reason)
    assert len(mirrored) == (1 if with_ticket else 0)


def _close(initiative: str, state: str | None) -> dict:
    return {"kind": "close_initiative", "initiative": initiative, **({} if state is None else {"state": state}), "epoch": 1}


def _closing(monkeypatch, tmp_path, written: list, rows: list, result=None) -> Deps:
    stored = chair_exec.store_cli.StateSet({}) if result is None else result
    monkeypatch.setattr(chair_exec.store_cli, "set_state", lambda *a, **k: written.append(a) or stored)
    return replace(_deps([]), runs_dir=tmp_path / "runs", record=rows.append)


@pytest.mark.parametrize(("initiative", "state"), [("alpha", "done"), ("beta", "dropped")])
def test_a_close_initiative_writes_its_state_by_chair_loop_and_is_recorded_like_its_neighbours(tmp_path, monkeypatch, initiative, state) -> None:
    written: list = []
    rows: list = []
    results = perform([_close(initiative, state)], _closing(monkeypatch, tmp_path, written, rows), lambda: 1, False)
    assert written == [(tmp_path / "runs", initiative, initiative, state, "chair-loop")]
    assert (results[0]["status"], results[0]["reason"]) == ("done", f"closed {initiative} as {state}")
    assert rows == [{**_close(initiative, state), "status": "done", "reason": f"closed {initiative} as {state}"}]


@pytest.mark.parametrize(
    ("state", "refused", "writes"),
    [("todo", False, 0), ("ready", False, 0), ("", False, 0), (None, False, 0), ("done", True, 1)],
    ids=["todo", "ready", "empty", "missing", "store_refuses"],
)
def test_a_close_initiative_that_is_invalid_or_refused_by_the_store_is_recorded_failed(tmp_path, monkeypatch, state, refused, writes) -> None:
    written: list = []
    rows: list = []
    result = chair_exec.store_cli.StateRefused("no such row") if refused else None
    results = perform([_close("alpha", state)], _closing(monkeypatch, tmp_path, written, rows, result), lambda: 1, False)
    assert len(written) == writes
    assert results[0]["status"] == "failed"
    assert [(r["kind"], r["status"]) for r in rows] == [("close_initiative", "failed")]


@pytest.mark.parametrize(
    ("state", "problem"),
    [
        ("done", ""),
        ("dropped", ""),
        ("todo", "close_initiative state must be done or dropped, not 'todo'"),
    ],
)
def test_the_close_state_problem_is_empty_only_for_done_and_dropped(state, problem) -> None:
    assert chair_exec._close_state_problem(state) == problem


def _land(task: str, repo: str) -> dict:
    return {"kind": "land", "task_id": task, "repo": repo, "run": "run-1", "epoch": 1}


def _land_phase(phase: str, repo: str) -> dict:
    return {"kind": "land_phase", "phase": phase, "repo": repo, "run": "run-1", "epoch": 1}


def _clear(initiative: str) -> dict:
    return {"kind": "clear_branches", "initiative": initiative, "epoch": 1}


def _carry(epoch: int = 1) -> dict:
    pick = {"task": "t1", "run": "run-1", "host": "h", "branch": "agents/run-1/t1", "commit": "abc", "needs": [], "run_seq": 1}
    return {"kind": "carry_phase", "initiative": "alpha", "phase": "p1", "pr_branch": "carry/alpha-p1", "picks": [pick], "epoch": epoch}


def _rebase(epoch: int = 1) -> dict:
    return {"kind": "rebase_phase", "initiative": "alpha", "phase": "p1", "branch": "epic/alpha/p1", "tip": "a" * 40, "base": "main", "epoch": epoch}


def _wired(calls: list, monkeypatch) -> Deps:
    def carry(action, git, forge, store):
        calls.append(("carry", action["phase"]))
        return {"action": action, "status": "done", "reason": ""}

    def rebase(action, port):
        calls.append(("rebase", action["branch"]))
        return {"action": action, "status": "done", "reason": ""}

    monkeypatch.setattr(chair_exec, "perform_carry", carry)
    monkeypatch.setattr(chair_exec, "perform_rebase", rebase)
    return replace(
        _deps(calls),
        carry_ports=lambda action: calls.append(("ports",)) or (None, None, None),
        rebase_port=lambda action: calls.append(("port",)),
    )


@pytest.mark.parametrize(
    ("actions", "epoch", "dry_run", "status", "calls"),
    [
        ([{"kind": "pull", "epoch": 1}], 2, False, "fenced", [("record", "pull")]),
        ([_carry(), _rebase()], 2, False, "fenced", [("record", "carry_phase"), ("record", "rebase_phase")]),
        ([_carry(), _rebase()], 1, True, "dry_run", []),
    ],
    ids=["fenced_pull", "fenced_carry_and_rebase", "dry_run_carry_and_rebase"],
)
def test_a_fenced_or_dry_run_action_touches_nothing_and_only_a_fenced_one_is_recorded(monkeypatch, actions, epoch, dry_run, status, calls) -> None:
    seen: list = []
    results = perform(actions, _wired(seen, monkeypatch), lambda: epoch, dry_run)
    assert [r["status"] for r in results] == [status] * len(actions)
    assert seen == calls


@pytest.mark.parametrize(
    ("action", "calls"),
    [
        (_carry(), [("ports",), ("carry", "p1"), ("record", "carry_phase")]),
        (_rebase(), [("port",), ("rebase", "epic/alpha/p1"), ("record", "rebase_phase")]),
    ],
    ids=["carry_phase", "rebase_phase"],
)
def test_a_carry_phase_and_a_rebase_phase_reach_their_executor_and_are_recorded_once(monkeypatch, action, calls) -> None:
    seen: list = []
    results = perform([action], _wired(seen, monkeypatch), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    assert seen == calls


def test_a_carry_conflict_is_surfaced_as_needs_chair_and_marks_nothing_landed() -> None:
    calls: list = []

    class Git:
        def fetch_branch(self, host, branch): calls.append("fetch")
        def create_branch_from_main(self, name): calls.append("branch")
        def already_on_main(self, commit): return False
        def cherry_pick(self, commit): return ["a.py"]

    class Store:
        def mark_landed(self, task, run): calls.append("mark_landed")
        def set_done(self, task): calls.append("set_done")

    deps = replace(_deps(calls), carry_ports=lambda action: (Git(), None, Store()))
    results = perform([_carry()], deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "recorded"]
    assert results[0]["needs_chair"]["kind"] == "needs_chair"
    assert "mark_landed" not in calls and "set_done" not in calls
    assert [c for c in calls if c[0] == "record"] == [("record", "carry_phase"), ("record", "needs_chair")]


def _store_edge(tmp_path, monkeypatch, pr_url: str) -> tuple[list, chair_exec._StoreEdge]:
    argvs: list = []

    def fake_run(build, run=None):
        argvs.append(build("py"))
        return 0, "{}"

    monkeypatch.setattr(chair_exec.store_cli, "_run", fake_run)
    monkeypatch.setattr(chair_exec.store_cli, "_store_url", lambda runs_dir: None)
    forge = chair_exec._ForgeEdge(None)
    forge.pr_url = pr_url
    return argvs, chair_exec._StoreEdge(tmp_path, tmp_path, _carry(), forge, lambda: "2026-10-05T00:00:00+00:00")


@pytest.mark.parametrize(
    ("pr_url", "expected"),
    [
        (
            "https://github.com/o/r/pull/7",
            [[
                "py", "-m", "harness.store_cli", "mark-landed", "run-1", "p1", "t1",
                "--pr", "https://github.com/o/r/pull/7", "--at", "2026-10-05T00:00:00+00:00",
            ]],
        ),
        ("", []),
    ],
    ids=["pr_opened", "no_pr"],
)
def test_the_store_edge_stamps_the_pr_url_open_pr_returned_and_writes_nothing_without_one(tmp_path, monkeypatch, pr_url, expected) -> None:
    argvs, store = _store_edge(tmp_path, monkeypatch, pr_url)
    store.mark_landed("t1", "run-1")
    assert argvs == expected


def test_the_forge_edge_keeps_only_the_url_gh_printed(monkeypatch) -> None:
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: (0, ""))
    monkeypatch.setattr(chair_exec.forge_auto, "open_pr", lambda *a, **k: (True, "Creating pull request\nhttps://github.com/o/r/pull/7\n"))
    forge = chair_exec._ForgeEdge(chair_exec._GitEdge(Path("."), Path("."), "r"))
    assert forge.open_pr("carry/alpha-p1", "t", "b") == "https://github.com/o/r/pull/7"
    assert forge.pr_url == "https://github.com/o/r/pull/7"


def test_the_rebase_edge_fetches_origin_then_pushes_origin_main_as_the_phase_tip(monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (0, ""))
    chair_exec._RebaseEdge("/r").recreate_branch("epic/alpha/p1", "main")
    assert argvs == [
        ["git", "-C", "/r", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "-C", "/r", "push", "--force", "origin", "origin/main:refs/heads/epic/alpha/p1"],
    ]


def test_the_git_edge_fetches_origin_first_and_cuts_the_carry_branch_from_origin_main(tmp_path, monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (0, ""))
    monkeypatch.setattr(chair_exec.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    git = chair_exec._GitEdge(tmp_path, tmp_path, "/r")
    git.create_branch_from_main("carry/alpha-p1")
    assert argvs[0] == ["git", "-C", "/r", "fetch", "origin"]
    assert argvs[-1] == ["git", "-C", "/r", "worktree", "add", "-B", "carry/alpha-p1", str(tmp_path), "origin/main"]


def test_the_git_edge_checks_a_pick_against_origin_main(monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (0, ""))
    assert chair_exec._GitEdge(Path("."), Path("."), "/r").already_on_main("abc") is True
    assert argvs[-1] == ["git", "-C", "/r", "merge-base", "--is-ancestor", "abc", "origin/main"]


def test_a_repo_with_a_base_setting_cuts_its_phase_branch_from_origin_develop(tmp_path, monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (0, ""))
    monkeypatch.setattr(chair_exec.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    git = chair_exec._GitEdge(tmp_path, tmp_path, "/r", None, "develop")
    git.create_branch_from_main("carry/alpha-p1")
    assert argvs[-1] == ["git", "-C", "/r", "worktree", "add", "-B", "carry/alpha-p1", str(tmp_path), "origin/develop"]
    assert git.default_branch() == "develop"


def test_a_repo_with_a_base_setting_carries_forward_onto_origin_develop() -> None:
    argvs: list = []
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n\n{_P_WORKTREE}\n", {"epic/i/p"})
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app", repo_base=lambda repo: "develop")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    assert perform([action], deps, lambda: 1, False)[0]["status"] == "done"
    assert any(a[-3:] == ["merge", "--no-edit", "origin/develop"] for a in argvs)
    assert not any(a[-1] == "origin/main" for a in argvs)


def test_a_carry_whose_checkout_lacks_origin_base_fails_without_merging_a_local_branch() -> None:
    argvs: list = []
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n\n{_P_WORKTREE}\n", {"epic/i/p"}, remote_branches=frozenset({"origin/main"}))
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app", repo_base=lambda repo: "develop")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    result = perform([action], deps, lambda: 1, False)[0]
    assert result["status"] == "failed" and "runs land: origin/develop not found in /w/app" in result["reason"]
    assert not any("merge" in a or "add" in a for a in argvs)


def test_a_carry_whose_fetch_of_origin_fails_stops_before_resolving_or_merging() -> None:
    argvs: list = []
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n\n{_P_WORKTREE}\n", {"epic/i/p"}, fail=("fetch", "origin"))
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    result = perform([action], deps, lambda: 1, False)[0]
    assert result["status"] == "failed" and "git fetch origin in /w/app" in result["reason"]
    assert not any("merge" in a or "symbolic-ref" in a for a in argvs)


def test_a_landed_develop_repo_records_the_commit_of_origin_develop(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda argv: subprocess.run(argv, cwd=repo, capture_output=True, text=True, check=True)  # noqa: E731
    run(["git", "init", "-q", "-b", "develop"])
    run(["git", "config", "user.email", "a@b.c"])
    run(["git", "config", "user.name", "tester"])
    (repo / "f.txt").write_text("one", encoding="utf-8")
    run(["git", "add", "f.txt"])
    run(["git", "commit", "-q", "-m", "first"])
    run(["git", "update-ref", "refs/remotes/origin/develop", "HEAD"])
    run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop"])
    expected = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    assert land_commit(str(repo)) == expected
    result = chair_exec._land_result({"kind": "land"}, str(repo), 0, "merge: ok\nmark_done: t\n")
    assert result["status"] == "landed" and result["commit"] == expected
    run(["git", "update-ref", "refs/remotes/origin/staging", "HEAD"])
    assert land_commit(str(repo), "staging") == expected


def test_a_failed_fetch_of_origin_fails_the_carry_before_any_worktree_is_added(monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (1, "no route"))
    with pytest.raises(RuntimeError, match="git fetch origin: no route"):
        chair_exec._GitEdge(Path("."), Path("."), "/r").create_branch_from_main("carry/alpha-p1")
    assert argvs == [["git", "-C", "/r", "fetch", "origin"]]


def test_land_refusal_is_none_on_a_clean_exit() -> None:
    assert land_refusal(_land("t1", "r"), 0, LANDED) is None


def test_land_refusal_names_a_conflict_from_the_output() -> None:
    # git cherry-pick's own conflict text, verbatim from a real conflict through `cli._execute_land_step`:
    # its "CONFLICT (content): ..." line goes to stdout, which that step drops whenever stderr is non-empty,
    # so the substring this rule reads is stderr's "error: could not apply ...", not "CONFLICT".
    output = (
        "error: could not apply a1b2c3d... Add seams module\n"
        "hint: After resolving the conflicts, mark them with\n"
        'hint: "git add/rm <pathspec>", then run\n'
        'hint: "git cherry-pick --continue".\n'
    )
    refusal = land_refusal(_land("t1", "r"), 1, output)
    assert refusal["cause"] == "conflict"


def test_land_refusal_names_failing_checks_from_the_output() -> None:
    refusal = land_refusal(_land("t1", "r"), 1, "failing checks: lint")
    assert refusal["cause"] == "checks"


def test_land_refusal_names_a_missing_branch_from_the_output() -> None:
    refusal = land_refusal(_land("t1", "r"), 1, "no candidate branch found for t1: tried agents/run-1/t1")
    assert refusal["cause"] == "missing_branch"


def test_land_refusal_falls_back_to_land_for_any_other_nonzero_exit() -> None:
    refusal = land_refusal(_land("t1", "r"), 1, "boom, something else broke")
    assert refusal["cause"] == "land"


def test_land_refusal_carries_the_action_s_initiative_and_task_id() -> None:
    action = {"kind": "land", "task_id": "t9", "initiative": "alpha", "repo": "r", "run": "run-1", "epoch": 3}
    assert land_refusal(action, 1, "boom") == {
        "kind": "needs_chair", "initiative": "alpha", "task_id": "t9", "cause": "land", "epoch": 3,
    }


def test_a_land_counts_only_when_both_markers_appear() -> None:
    results = perform([_land("t1", "r")], _deps([]), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed"]


def test_a_land_with_a_nonzero_exit_is_refused_and_escalated_with_its_cause() -> None:
    calls: list = []
    results = perform([_land("t1", "r")], _deps(calls, code=1), lambda: 1, False)
    assert [(r["action"]["kind"], r["status"]) for r in results] == [("land", "refused"), ("needs_chair", "escalated")]
    assert results[1]["action"]["cause"] == "land"
    assert [c for c in calls if c[0] == "record"] == [("record", "land"), ("record", "needs_chair")]


def test_a_refusing_land_blocks_its_repo_siblings_this_tick() -> None:
    calls: list = []
    actions = [_land("t1", "r"), _land("t2", "r")]
    output = "error: could not apply a1b2c3d... Add seams module\n"
    results = perform(actions, _deps(calls, output, code=1), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "skipped", "escalated"]
    assert [r["action"].get("cause") for r in results if r["status"] == "escalated"] == ["conflict", STRANDED_CAUSE]
    assert _touched(calls) == [("run", ["cox", "runs", "land", "run-1", "--task", "t1", "--repo", "r", "--apply", "--no-claim"])]


def test_a_land_refused_before_it_runs_blocks_nothing() -> None:
    calls: list = []
    actions = [{"kind": "land", "task_id": "t1", "repo": "r", "epoch": 1}, _land("t2", "r")]
    results = perform(actions, _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "landed"]
    assert [c[1][5] for c in calls if c[0] == "run"] == ["t2"]


def test_the_status_line_names_a_refused_land_s_cause() -> None:
    facts = {
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "five_hour_fraction": None},
        "dispatch": {"max_in_flight": 4, "live_runs": 0},
    }
    land = {**_land("t1", "r"), "initiative": "alpha"}
    results = perform([land], _deps([], "failing checks: lint", code=1), lambda: 1, False)
    line = format_status(facts, [], results, datetime(2026, 9, 27, tzinfo=UTC))
    assert line.endswith("needs chair: alpha:checks")


def test_a_land_refused_by_the_repo_lease_is_busy_and_never_escalated() -> None:
    calls: list = []
    output = "land: refusing, pid 1 on host is landing in /repo"
    results = perform([_land("t1", "r")], _deps(calls, output), lambda: 1, False)
    assert [r["status"] for r in results] == ["busy"]
    assert ("record", "needs_chair") not in calls


def test_a_repo_lease_refusal_that_exits_non_zero_is_still_busy_for_a_land_and_a_land_phase() -> None:
    # `cox runs land` exits 2 when another land holds the repository: busy, never refused or escalated.
    output = "land: refusing, chair-loop@chair-host:77598 is landing in /repo"
    for action in (_land("t1", "r"), _land_phase("p", "r")):
        calls: list = []
        results = perform([action], _deps(calls, output, code=2), lambda: 1, False)
        assert [r["status"] for r in results] == ["busy"]
        assert ("record", "needs_chair") not in calls


def test_a_land_refused_for_any_other_reason_still_escalates_as_stranded() -> None:
    recorded: list = []
    deps = replace(_deps([], "land: refusing, /repo is dirty"), record=recorded.append)
    results = perform([_land("t1", "r")], deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["not_landed", "escalated"]
    assert [(a["kind"], a.get("cause")) for a in recorded] == [("land", None), ("needs_chair", "stranded")]


def test_a_land_with_only_merge_is_not_counted_and_skips_its_repo_siblings() -> None:
    calls: list = []
    actions = [_land("t1", "r"), _land("t2", "r"), _clear("alpha"), _land("t3", "other")]
    results = perform(actions, _deps(calls, "merge: ok\n"), lambda: 1, False)
    assert [r["status"] for r in results if r["status"] != "escalated"] == ["not_landed", "skipped", "skipped", "not_landed"]
    assert "t1" in results[2]["reason"]
    assert [c[1][5] for c in calls if c[0] == "run"] == ["t1", "t3"]


def test_a_land_missing_its_repo_is_refused_without_running() -> None:
    calls: list = []
    results = perform([{"kind": "land", "task_id": "t1", "epoch": 1}], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated"]
    assert calls == [("record", "land"), ("record", "needs_chair")]


def test_a_land_phase_counts_only_when_both_markers_appear() -> None:
    results = perform([_land_phase("p1", "r")], _deps([]), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed"]


def test_a_land_phase_runs_cox_runs_land_with_phase_repo_apply_no_claim() -> None:
    calls: list = []
    perform([_land_phase("p1", "r")], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [("run", ["cox", "runs", "land", "run-1", "--phase", "p1", "--repo", "r", "--apply", "--no-claim"])]


def _recorded_land_phase_rows(tip: str | None) -> tuple[list[dict], list]:
    """The rows `perform` records for one land_phase whose phase branch tip is `tip`, and the calls in order."""
    calls: list = []
    rows: list[dict] = []

    def stub(repo: str, ref: str) -> str | None:
        calls.append(("rev_parse", repo, ref))
        return tip

    deps = replace(_deps(calls), rev_parse=stub, record=rows.append)
    perform([{**_land_phase("p1", "r"), "initiative": "alpha"}], deps, lambda: 1, False)
    return rows, calls


def test_a_land_phase_records_the_phase_branch_head() -> None:
    rows, calls = _recorded_land_phase_rows("aaa")
    assert rows[0]["head"] == "aaa"
    assert ("rev_parse", "r", "epic/alpha/p1") in calls


def test_a_land_phase_reads_the_head_before_the_land_command_runs() -> None:
    _, calls = _recorded_land_phase_rows("aaa")
    assert [c[0] for c in calls] == ["rev_parse", "run"]


def test_a_land_phase_with_an_unresolvable_branch_records_no_head_key() -> None:
    rows, _ = _recorded_land_phase_rows(None)
    assert "head" not in rows[0]


def test_a_land_phase_with_an_empty_tip_records_no_head_key() -> None:
    rows, _ = _recorded_land_phase_rows("")
    assert "head" not in rows[0]


def test_a_land_phase_with_only_merge_is_not_counted_and_skips_its_repo_siblings() -> None:
    calls: list = []
    actions = [_land_phase("p1", "r"), _land("t2", "r")]
    results = perform(actions, _deps(calls, "merge: ok\n"), lambda: 1, False)
    assert [r["status"] for r in results if r["status"] != "escalated"] == ["not_landed", "skipped"]
    assert _touched(calls) == [("run", ["cox", "runs", "land", "run-1", "--phase", "p1", "--repo", "r", "--apply", "--no-claim"])]


def test_an_uncounted_land_phase_skips_a_later_land_phase_in_its_repo() -> None:
    calls: list = []
    actions = [_land_phase("p1", "r"), _land_phase("p2", "r")]
    results = perform(actions, _deps(calls, "merge: ok\n"), lambda: 1, False)
    assert [r["status"] for r in results] == ["not_landed", "escalated", "skipped", "escalated"]
    assert results[2]["reason"] == "an earlier land in r (phase p1) was not counted"
    assert [c[1][5] for c in calls if c[0] == "run"] == ["p1"]


def _stopped_at(step: str) -> str:
    return f"merge: ok\nland: stopped at {step}: checks not green\n"


def test_a_land_phase_stopped_at_wait_checks_does_not_skip_the_next_land_for_that_phase() -> None:
    calls: list = []
    land = {**_land_phase("p1", "r"), "initiative": "A"}
    results = perform([land, land], _deps(calls, _stopped_at("wait_checks"), code=1), lambda: 1, False)
    assert "skipped" not in [r["status"] for r in results]
    assert [c[1][5] for c in calls if c[0] == "run"] == ["p1", "p1"]


def test_a_land_phase_stopped_at_squash_phase_still_skips_the_next_land_for_that_phase() -> None:
    calls: list = []
    land = {**_land_phase("p1", "r"), "initiative": "A"}
    results = perform([land, land], _deps(calls, _stopped_at("squash_phase"), code=1), lambda: 1, False)
    skipped = [r for r in results if r["status"] == "skipped"]
    assert [r["reason"] for r in skipped] == ["an earlier land in r (phase p1) was not counted"]
    assert [c[1][5] for c in calls if c[0] == "run"] == ["p1"]


def test_a_wait_checks_stop_in_another_phase_still_skips_the_next_land() -> None:
    calls: list = []
    first, second = ({**_land_phase(p, "r"), "initiative": "A"} for p in ("p1", "p2"))
    results = perform([first, second], _deps(calls, _stopped_at("wait_checks"), code=1), lambda: 1, False)
    skipped = [r for r in results if r["status"] == "skipped"]
    assert [r["reason"] for r in skipped] == ["an earlier land in r (phase p1) was not counted"]
    assert [c[1][5] for c in calls if c[0] == "run"] == ["p1"]


def test_a_refused_land_phase_for_one_initiative_still_runs_another_initiative_s_relaunch_and_land_phase(tmp_path: Path) -> None:
    calls: list = []
    refused = {**_land_phase("p1", "r"), "initiative": "A"}
    actions = [
        refused,
        {"kind": "relaunch", "initiative": "B", "repo": "r", "epoch": 1},
        {**_land_phase("p2", "r"), "initiative": "B"},
    ]
    results = perform(actions, replace(_deps(calls, "failing checks: lint", code=1), work_dir=tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "failed", "refused", "escalated"]
    assert [c[1][:3] for c in calls if c[0] == "run"] == [["cox", "runs", "land"], ["cox", "route", "launch"], ["cox", "runs", "land"]]
    assert [c[1][5] for c in calls if c[0] == "run" and c[1][2] == "land"] == ["p1", "p2"]


def test_a_refused_land_phase_skips_a_later_action_for_the_same_initiative_in_another_repo() -> None:
    calls: list = []
    refused = {**_land_phase("p1", "r"), "initiative": "A"}
    actions = [refused, {**_land("t2", "r2"), "initiative": "A"}]
    results = perform(actions, _deps(calls, "failing checks: lint", code=1), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "skipped", "escalated"]
    assert results[2]["reason"] == "an earlier land in r2 (phase p1) was not counted"
    assert [c[1][5] for c in calls if c[0] == "run"] == ["p1"]


def test_a_refused_land_phase_still_records_a_later_needs_chair_for_its_initiative() -> None:
    refused = {**_land_phase("p1", "r"), "initiative": "A"}
    actions = [refused, {"kind": "needs_chair", "initiative": "A", "epoch": 1}]
    results = perform(actions, _deps([], "failing checks: lint", code=1), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "recorded"]


def test_a_refused_land_phase_escalates_naming_its_phase() -> None:
    facts = {
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "five_hour_fraction": None},
        "dispatch": {"max_in_flight": 4, "live_runs": 0},
    }
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    results = perform([action], _deps([], "failing checks: lint", code=1), lambda: 1, False)
    assert results[1]["action"] == {"kind": "needs_chair", "initiative": "alpha", "task_id": "", "cause": "checks", "epoch": 1, "phase": "p1"}
    assert results[1]["reason"] == "land phase p1 refused"
    line = format_status(facts, [], results, datetime(2026, 9, 27, tzinfo=UTC))
    assert line.endswith("needs chair: alpha/p1:checks")


def test_an_unclassified_land_phase_escalates_as_stranded_naming_its_phase() -> None:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    results = perform([action], _deps([], "merge: ok\n"), lambda: 1, False)
    assert results[1]["action"] == {"kind": "needs_chair", "initiative": "alpha", "task_id": "", "cause": STRANDED_CAUSE, "epoch": 1, "phase": "p1"}
    assert results[1]["reason"] == "land phase p1 not_landed"


def test_a_squash_phase_stop_escalates_as_land_conflict_with_its_detail_once() -> None:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    output = "merge: ok\nland: stopped at squash_phase: CONFLICT (content) in src/a.py\n"
    results = perform([action], _deps([], output, code=1), lambda: 1, False)
    assert results[0]["status"] == "refused"
    raised = [r for r in results if r["action"]["kind"] == "needs_chair"]
    assert len(raised) == 1
    assert raised[0]["action"]["cause"] == "land_conflict"
    assert raised[0]["reason"] == (
        "land phase p1 stopped at squash_phase: CONFLICT (content) in src/a.py. Rebase the phase branch onto main, or re-run the phase."
    )
    assert raised[0]["reason"].count("src/a.py") == 1


_GONE = "land: stopped at squash_phase: fatal: invalid reference: epic/alpha/p1\n"


def _gone_land(output: str, states: list[str]) -> list:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    deps = replace(_deps([], output, code=1), phase_states=lambda initiative, phase: states)
    return perform([action], deps, lambda: 1, False)


def test_already_landed_needs_a_squash_phase_missing_branch_and_every_task_done() -> None:
    gone = "fatal: invalid reference: epic/alpha/p1"
    assert already_landed("squash_phase", gone, ["done", "done"])
    assert not already_landed("squash_phase", gone, ["done", "approved"])
    assert not already_landed("squash_phase", gone, [])
    assert not already_landed("squash_phase", "CONFLICT (content) in src/a.py", ["done"])
    assert not already_landed("wait_checks", gone, ["done"])


def test_a_missing_phase_branch_with_every_task_done_is_recorded_as_already_landed() -> None:
    results = _gone_land(_GONE, ["done", "done"])
    assert [r["status"] for r in results] == ["recorded"]
    assert results[0]["reason"] == "land phase p1 already landed: fatal: invalid reference: epic/alpha/p1"
    assert "needs_chair" not in results[0]
    assert not [r for r in results if r["action"]["kind"] == "needs_chair" or r["action"].get("cause") == "land_conflict"]


def test_a_missing_phase_branch_with_a_task_not_done_still_escalates_as_land_conflict() -> None:
    results = _gone_land(_GONE, ["done", "approved"])
    raised = [r for r in results if r["action"]["kind"] == "needs_chair"]
    assert [r["action"]["cause"] for r in raised] == ["land_conflict"]


def test_another_squash_phase_failure_with_every_task_done_still_escalates_as_land_conflict() -> None:
    results = _gone_land("land: stopped at squash_phase: CONFLICT (content) in src/a.py\n", ["done", "done"])
    raised = [r for r in results if r["action"]["kind"] == "needs_chair"]
    assert [r["action"]["cause"] for r in raised] == ["land_conflict"]


def test_a_missing_branch_fatal_on_a_later_line_of_git_s_stderr_is_still_already_landed() -> None:
    output = "land: stopped at squash_phase: Preparing worktree (new branch 'pr')\nfatal: invalid reference: epic/alpha/p1\n"
    assert [r["status"] for r in _gone_land(output, ["done"])] == ["recorded"]


def test_phase_task_states_keeps_only_the_rows_of_the_phase() -> None:
    rows = [{"phase": "p1", "state": "done"}, {"phase": "p2", "state": "approved"}, {"phase": "p1", "state": "approved"}]
    assert phase_task_states(rows, "p1") == ["done", "approved"]
    assert phase_task_states(rows, "p3") == []


class _Sink:
    """A LandSink that runs the land at submit and hands its result back at the next collect, as the chair's sink does."""

    def __init__(self, finished: list | None = None) -> None:
        self.finished: list = finished or []

    def pending(self, repo: str) -> bool:
        return False

    def landing(self) -> list:
        return []

    def submit(self, action: dict, work) -> dict:
        self.finished.append(work())
        return {"action": action, "status": "in_progress", "reason": ""}

    def collect(self) -> list:
        done, self.finished = self.finished, []
        return done


def _through_sink(output: str, states: list[str]) -> list:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    sink = _Sink()
    deps = replace(_deps([], output, code=1), phase_states=lambda initiative, phase: states, lands=sink)
    perform([action], deps, lambda: 1, False)
    return perform([], deps, lambda: 1, False)


def test_an_already_landed_phase_collected_from_the_land_sink_raises_nothing() -> None:
    results = _through_sink(_GONE, ["done", "done"])
    assert [r["status"] for r in results] == ["recorded"]
    assert not [r for r in results if r["action"]["kind"] == "needs_chair"]


def test_a_missing_branch_with_a_task_not_done_collected_from_the_land_sink_still_escalates() -> None:
    results = _through_sink(_GONE, ["done", "approved"])
    assert [r["action"].get("cause") for r in results if r["action"]["kind"] == "needs_chair"] == ["land_conflict"]


def test_another_recorded_land_collected_from_the_land_sink_still_escalates() -> None:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    sink = _Sink([{"action": action, "status": "recorded", "reason": "no approved task of the phase has a known run yet"}])
    results = perform([], replace(_deps([], LANDED), lands=sink), lambda: 1, False)
    assert [r["status"] for r in results] == ["recorded", "escalated"]


def test_a_wait_checks_stop_escalates_as_stranded_with_its_step_and_detail() -> None:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    output = "wait_checks: failing checks: lint\nland: stopped at wait_checks: checks still pending\n"
    results = perform([action], _deps([], output, code=1), lambda: 1, False)
    assert results[1]["action"]["cause"] == STRANDED_CAUSE
    assert results[1]["reason"] == "land phase p1 stopped at wait_checks: checks still pending"


def test_a_squash_phase_stop_with_a_clean_exit_is_also_a_land_conflict() -> None:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    results = perform([action], _deps([], "land: stopped at squash_phase: CONFLICT in src/a.py\n"), lambda: 1, False)
    assert results[0]["status"] == "not_landed"
    assert results[1]["action"]["cause"] == "land_conflict"


def test_a_land_phase_output_without_a_stop_line_keeps_its_reason() -> None:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    results = perform([action], _deps([], "merge: ok\nsome other line\n"), lambda: 1, False)
    assert results[1]["action"]["cause"] == STRANDED_CAUSE
    assert results[1]["reason"] == "land phase p1 not_landed"


def test_a_land_action_still_works_unchanged() -> None:
    calls: list = []
    results = perform([_land("t1", "r")], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed"]
    assert _touched(calls) == [("run", ["cox", "runs", "land", "run-1", "--task", "t1", "--repo", "r", "--apply", "--no-claim"])]


def test_clear_branches_refuses_a_foreign_glob() -> None:
    calls: list = []
    results = perform([_clear("*")], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused"]
    assert _touched(calls) == []


def test_clear_branches_deletes_its_own_glob_in_the_resolved_repo() -> None:
    calls: list = []
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("delete", "/w/app", "epic/alpha/*"),
    ]
    assert results[0]["status"] == "done"
    assert "epic/alpha/t1" in results[0]["reason"]


def test_clear_branches_that_finds_nothing_to_delete_is_done() -> None:
    results = perform([_clear("alpha")], _deps([], deleted=()), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    assert results[0]["reason"].startswith("nothing to clear")


def test_delete_branches_runs_git_in_the_named_repo_and_reports_what_it_deleted() -> None:
    argvs: list = []

    def run(argv):
        argvs.append(argv)
        return (0, "epic/alpha/t1\nepic/alphabet/x\n") if "for-each-ref" in argv else (0, "")

    assert delete_branches_with(run, "/w/app", "epic/alpha/*") == (["epic/alpha/t1"], "")
    assert all(a[:3] == ["git", "-C", "/w/app"] for a in argvs)
    assert argvs[1] == ["git", "-C", "/w/app", "branch", "-D", "epic/alpha/t1"]


SSH_BOUND = ["ssh", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
_MAIN_WORKTREE = "worktree /w/app\nHEAD aaa\nbranch refs/heads/main"
_ALPHA_WORKTREE = "worktree /w/app/.worktrees/alpha-build\nHEAD bbb\nbranch refs/heads/epic/alpha/build"


def _porcelain_run(calls: list, listing: str, fail_remove_path: str = "") -> Run:
    """A fake git runner: `worktree list` returns `listing`; `worktree remove` on `fail_remove_path` fails."""

    def run(argv):
        calls.append(("run", argv))
        if "list" in argv:
            return 0, listing
        if "remove" in argv and fail_remove_path and fail_remove_path in argv:
            return 1, "boom"
        return 0, ""

    return run


def test_clear_branches_prunes_the_matching_worktree_then_deletes_its_branch() -> None:
    """The sweep finds nothing left after the prune deleted the only match; the clear is still done."""
    calls: list = []
    listing = f"{_MAIN_WORKTREE}\n\n{_ALPHA_WORKTREE}\n"
    deps = replace(_deps(calls, deleted=()), run=_porcelain_run(calls, listing), repo_for=lambda action: "/w/app")
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], deps, lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("run", ["git", "-C", "/w/app", "worktree", "remove", "--force", "/w/app/.worktrees/alpha-build"]),
        ("run", ["git", "-C", "/w/app", "branch", "-D", "epic/alpha/build"]),
        ("delete", "/w/app", "epic/alpha/*"),
    ]
    assert results[0]["status"] == "done"


def test_clear_branches_with_no_matching_worktree_still_sweeps_the_pattern() -> None:
    """No live worktree to prune, so chair_plan_prune builds no argv; the pattern sweep clear_branches already makes today still runs."""
    calls: list = []
    listing = f"{_MAIN_WORKTREE}\n"
    deps = replace(_deps(calls), run=_porcelain_run(calls, listing), repo_for=lambda action: "/w/app")
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], deps, lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("delete", "/w/app", "epic/alpha/*"),
    ]
    assert results[0]["status"] == "done"


def test_clear_branches_stops_after_a_failing_worktree_remove() -> None:
    calls: list = []
    listing = f"{_MAIN_WORKTREE}\n\n{_ALPHA_WORKTREE}\n"
    deps = replace(
        _deps(calls),
        run=_porcelain_run(calls, listing, fail_remove_path="/w/app/.worktrees/alpha-build"),
        repo_for=lambda action: "/w/app",
    )
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], deps, lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("run", ["git", "-C", "/w/app", "worktree", "remove", "--force", "/w/app/.worktrees/alpha-build"]),
    ]
    assert results[0]["status"] == "failed"


def test_a_failed_clear_branches_holds_back_its_relaunch_this_tick() -> None:
    calls: list = []
    listing = f"{_MAIN_WORKTREE}\n\n{_ALPHA_WORKTREE}\n"
    deps = replace(
        _deps(calls),
        run=_porcelain_run(calls, listing, fail_remove_path="/w/app/.worktrees/alpha-build"),
        repo_for=lambda action: "/w/app",
    )
    actions = [{**_clear("alpha"), "repo": "/w/app"}, {"kind": "relaunch", "initiative": "alpha", "epoch": 1}]
    results = perform(actions, deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["failed", "skipped"]
    assert not any(c[1][0] == "cox" for c in _touched(calls) if c[0] == "run")


def _git_repo(argvs: list, listing: str, branches: set) -> Run:
    """A stateful fake git: `branch -D` removes from `branches`, and `for-each-ref` lists what is left."""

    def run(argv):
        argvs.append(argv)
        if "list" in argv:
            return 0, listing
        if "for-each-ref" in argv:
            prefix = argv[-1].removeprefix("refs/heads/").removesuffix("*")
            return 0, "\n".join(sorted(b for b in branches if b.startswith(prefix)))
        if "-D" in argv:
            branches.discard(argv[-1])
        return 0, ""

    return run


def test_a_cleared_single_worktree_is_done_through_the_real_sweep_and_its_relaunch_runs() -> None:
    argvs: list = []
    branches = {"main", "epic/alpha/build"}
    run = _git_repo(argvs, f"{_MAIN_WORKTREE}\n\n{_ALPHA_WORKTREE}\n", branches)
    deps = replace(
        _deps([]), run=run, delete_branches=lambda repo, pattern: delete_branches_with(run, repo, pattern),
        repo_for=lambda action: "/w/app",
    )
    actions = [_clear("alpha"), {"kind": "relaunch", "initiative": "alpha", "epoch": 1}]
    results = perform(actions, deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["done", "done"]
    assert branches == {"main"}
    assert [a[3] if a[0] == "git" else a[0] for a in argvs] == ["worktree", "worktree", "branch", "for-each-ref", "cox"]


_P_WORKTREE = "worktree /w/app/.worktrees/i-p\nHEAD ccc\nbranch refs/heads/epic/i/p"


def _carry_run(
    argvs: list, listing: str, branches: set, conflict_branches: frozenset = frozenset(), fail: tuple = (),
    remote_branches: frozenset = frozenset({"origin/main", "origin/develop"}),
) -> Run:
    """A stateful fake git: worktree list/add/remove, for-each-ref/branch -D, and a carried phase's merge/diff/abort.

    `fail`, when given, names the argv tokens (e.g. `("worktree", "add")`) whose exact command exits 1.
    """
    tmp_branch: dict[str, str] = {}

    def run(argv):
        argvs.append(argv)
        if fail and all(tok in argv for tok in fail):
            return 1, f"boom: {' '.join(fail)}"
        if "worktree" in argv and "list" in argv:
            return 0, listing
        if "rev-parse" in argv:
            if argv[-1].removeprefix("refs/remotes/") in remote_branches:
                return 0, ""
            return (0, "") if argv[-1].removeprefix("refs/heads/") in branches else (1, "")
        if argv[0] == "mktemp":
            return 0, "/tmp/cox-carry-abc\n"
        if "worktree" in argv and "add" in argv:
            tmp_branch[argv[-2]] = argv[-1]
            return 0, ""
        if "worktree" in argv and "remove" in argv:
            return 0, ""
        if "for-each-ref" in argv:
            prefix = argv[-1].removeprefix("refs/heads/").removesuffix("*")
            return 0, "\n".join(sorted(b for b in branches if b.startswith(prefix)))
        if "branch" in argv and "-D" in argv:
            branches.discard(argv[-1])
            return 0, ""
        if "merge" in argv and "--no-edit" in argv:
            return (1, "CONFLICT (content): merge conflict") if tmp_branch.get(argv[2]) in conflict_branches else (0, "merge ok")
        if "diff" in argv and "--diff-filter=U" in argv:
            return 0, "src/app.py\n"
        if "merge" in argv and "--abort" in argv:
            return 0, ""
        return 0, ""

    return run


def test_a_carried_phase_keeps_its_worktree_removed_branch_kept_then_merges_in_a_throwaway_worktree() -> None:
    argvs: list = []
    branches = {"epic/i/p"}
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n\n{_P_WORKTREE}\n", branches)
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    results = perform([action], deps, lambda: 1, False)
    assert results[0]["status"] == "done"
    assert branches == {"epic/i/p"}
    assert not any("-D" in a for a in argvs)
    assert argvs[0] == ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]
    assert argvs[1] == ["git", "-C", "/w/app", "worktree", "remove", "--force", "/w/app/.worktrees/i-p"]
    assert argvs[2] == ["git", "-C", "/w/app", "for-each-ref", "--format=%(refname:short)", "refs/heads/epic/i/*"]
    assert argvs[3] == ["git", "-C", "/w/app", "fetch", "origin"]
    assert argvs[4][:5] == ["git", "-C", "/w/app", "symbolic-ref", "--short"]
    assert argvs[5] == ["git", "-C", "/w/app", "remote", "get-url", "origin"]
    assert argvs[6] == ["git", "-C", "/w/app", "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"]
    add = argvs[7]
    tmp = add[-2]
    assert add == ["git", "-C", "/w/app", "worktree", "add", tmp, "epic/i/p"]
    assert argvs[8] == ["git", "-C", tmp, "merge", "--no-edit", "origin/main"]
    assert argvs[9] == ["git", "-C", "/w/app", "worktree", "remove", "--force", tmp]
    assert len(argvs) == 10
    assert not any(a[0] == "ssh" for a in argvs)
    assert not any("checkout" in a or "switch" in a for a in argvs)


def test_a_carried_sweep_leaves_the_phase_branch_but_deletes_its_task_and_sibling_branches() -> None:
    argvs: list = []
    branches = {"epic/i/p", "epic/i/p--t", "epic/i/q"}
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n", branches)
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    results = perform([action], deps, lambda: 1, False)
    assert results[0]["status"] == "done"
    assert branches == {"epic/i/p"}


def test_a_conflicting_carry_merge_aborts_and_raises_one_needs_chair() -> None:
    argvs: list = []
    branches: set = set()
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n", branches, conflict_branches=frozenset({"epic/i/p"}))
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    results = perform([action], deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["done", "recorded"]
    needs = results[1]
    assert needs["action"]["kind"] == "needs_chair" and needs["action"]["initiative"] == "i"
    assert "p" in needs["reason"] and "src/app.py" in needs["reason"]
    add = next(a for a in argvs if "worktree" in a and "add" in a)
    tmp = add[-2]
    assert ["git", "-C", tmp, "diff", "--name-only", "--diff-filter=U"] in argvs
    assert ["git", "-C", tmp, "merge", "--abort"] in argvs
    assert ["git", "-C", "/w/app", "worktree", "remove", "--force", tmp] in argvs
    assert not any("checkout" in a or "switch" in a for a in argvs)


def test_no_carry_key_runs_exactly_todays_argv_with_no_worktree_add_or_merge() -> None:
    calls: list = []
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("delete", "/w/app", "epic/alpha/*"),
    ]
    assert results[0]["status"] == "done"
    assert not any(c[0] == "run" and ("add" in c[1] or "merge" in c[1]) for c in calls)


def test_a_failed_worktree_add_fails_the_clear_and_holds_its_relaunch() -> None:
    argvs: list = []
    branches: set = set()
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n", branches, fail=("worktree", "add"))
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    actions = [{**_clear("i"), "carry": ["p"], "repo": "/w/app"}, {"kind": "relaunch", "initiative": "i", "epoch": 1}]
    results = perform(actions, deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["failed", "skipped"]
    assert "worktree add" in results[0]["reason"] and "epic/i/p" in results[0]["reason"]


def _host_carry_run(
    argvs: list,
    host_branches: set,
    conflict_branches: frozenset = frozenset(),
    host_listing: str = f"{_MAIN_WORKTREE}\n",
    chair_branches: frozenset = frozenset(),
    fail: tuple = (),
) -> Run:
    """A fake whose ssh argvs run against a host's own fake git and whose bare argvs run against the chair's."""
    chair = _carry_run([], f"{_MAIN_WORKTREE}\n", set(chair_branches))
    host = _carry_run([], host_listing, host_branches, conflict_branches, fail)

    def run(argv):
        argvs.append(argv)
        return host(shlex.split(argv[-1])) if argv[0] == "ssh" else chair(argv)

    return run


def _host_clear(run: Run, ssh: str = "me@host-a", repo: str = "/w/app", relaunch: bool = False) -> list:
    deps = replace(_deps([]), run=run, repo_for=lambda action: repo, ssh_for=lambda host: ssh)
    clear = {**_clear("i"), "carry": ["p"], "repo": repo, "host": "host-a"}
    tail_actions = [{"kind": "relaunch", "initiative": "i", "host": "host-a", "epoch": 1}] if relaunch else []
    return perform([clear, *tail_actions], deps, lambda: 1, False)


def test_a_host_homed_carry_runs_its_git_steps_on_that_host() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, {"epic/i/p"}))
    assert results[0]["status"] == "done"
    assert ssh_argv("me@host-a", sync_argv("/w/app")) in argvs
    assert [*SSH_BOUND, "me@host-a", "git -C /tmp/cox-carry-abc merge --no-edit origin/main"] in argvs
    assert [*SSH_BOUND, "me@host-a", "git -C /w/app fetch origin"] in argvs
    assert [*SSH_BOUND, "me@host-a", "git -C /w/app worktree remove --force /tmp/cox-carry-abc"] in argvs
    assert not any(a[0] == "git" and ("merge" in a or "add" in a) for a in argvs)


def test_a_phase_branch_present_only_on_the_host_is_carried_not_failed() -> None:
    results = _host_clear(_host_carry_run([], {"epic/i/p"}))
    assert results[0]["status"] == "done"
    assert results[0]["reason"] == "carried ['p'] past main on host-a"


def test_a_host_worktree_holding_the_carried_branch_is_removed_on_the_host_before_the_add() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, {"epic/i/p", "epic/i/p--t"}, host_listing=f"{_MAIN_WORKTREE}\n\n{_P_WORKTREE}\n"))
    assert results[0]["status"] == "done"
    assert results[0]["reason"] == "deleted ['epic/i/p--t'] in /w/app on host-a; carried ['p'] past main on host-a"
    remove = [*SSH_BOUND, "me@host-a", "git -C /w/app worktree remove --force /w/app/.worktrees/i-p"]
    add = [*SSH_BOUND, "me@host-a", "git -C /w/app worktree add /tmp/cox-carry-abc epic/i/p"]
    assert argvs.index(remove) < argvs.index(add)
    assert [*SSH_BOUND, "me@host-a", "git -C /w/app branch -D epic/i/p"] not in argvs


def test_a_phase_branch_only_the_chair_holds_is_pushed_to_the_host_then_carried_there() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, set(), chair_branches=frozenset({"epic/i/p"})))
    assert results[0]["reason"] == "carried ['p'] past main on host-a"
    push = ["git", "-C", "/w/app", "push", "me@host-a:/w/app", "refs/heads/epic/i/p:refs/heads/epic/i/p"]
    assert argvs.index(push) < argvs.index([*SSH_BOUND, "me@host-a", "git -C /w/app worktree add /tmp/cox-carry-abc epic/i/p"])


def test_a_conflict_on_the_host_raises_carry_conflict() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, {"epic/i/p"}, frozenset({"epic/i/p"})))
    assert [r["status"] for r in results] == ["done", "recorded"]
    assert results[1]["action"]["cause"] == "carry_conflict"
    assert "src/app.py" in results[1]["reason"]
    assert [*SSH_BOUND, "me@host-a", "git -C /tmp/cox-carry-abc merge --abort"] in argvs


def test_a_phase_branch_missing_on_both_machines_is_nothing_to_carry() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, set()))
    assert results[0]["status"] == "done"
    assert results[0]["reason"] == "nothing to clear: no branch matched epic/i/* in /w/app"
    assert not any(a[0] == "ssh" and "worktree add" in a[-1] for a in argvs)
    assert not any("push" in a for a in argvs)


def test_a_host_sync_failure_fails_the_clear_and_holds_the_relaunch_it_would_also_fail() -> None:
    results = _host_clear(_host_carry_run([], {"epic/i/p"}, fail=("bash",)), relaunch=True)
    assert [r["status"] for r in results] == ["failed", "skipped"]
    assert results[0]["reason"] == "updating /w/app on host-a before the carry: boom: bash"


def test_a_host_with_no_ssh_destination_fails_the_clear_instead_of_carrying_locally() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, {"epic/i/p"}), ssh="")
    assert results[0]["status"] == "failed"
    assert "host-a" in results[0]["reason"]
    assert argvs == []


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _commit(checkout: str, name: str) -> None:
    (Path(checkout) / name).write_text(name, encoding="utf-8")
    _git("-C", checkout, "add", name)
    _git("-C", checkout, "commit", "-q", "-m", name)


def _configured(checkout: str) -> None:
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git("-C", checkout, "config", key, value)


def test_a_host_carry_runs_through_a_real_shell_against_real_git(tmp_path) -> None:
    """Each ssh argv runs as `bash -c` would on the host. The host's previous run still holds the phase branch in
    a worktree, and origin has a landed commit the host's main has not fetched yet."""
    origin, app, other, held = (str(tmp_path.resolve() / n) for n in ("origin.git", "app", "other", "held"))
    _git("init", "-q", "--bare", "-b", "main", origin)
    _git("init", "-q", "-b", "main", app)
    _configured(app)
    _commit(app, "a.txt")
    _git("-C", app, "remote", "add", "origin", origin)
    _git("-C", app, "push", "-q", "origin", "main")
    _git("-C", app, "worktree", "add", "-q", "-b", "epic/i/p", held)
    _commit(held, "b.txt")
    _git("clone", "-q", origin, other)
    _configured(other)
    _commit(other, "c.txt")
    _git("-C", other, "push", "-q", "origin", "main")
    chair = _carry_run([], f"{_MAIN_WORKTREE}\n", set())

    def run(argv):
        return run_argv(["bash", "-c", argv[-1]]) if argv[0] == "ssh" else chair(argv)

    results = _host_clear(run, repo=app)
    assert results[0]["status"] == "done", results[0]["reason"]
    assert results[0]["reason"] == "carried ['p'] past main on host-a"
    assert f"worktree {held}\n" not in _git("-C", app, "worktree", "list", "--porcelain")
    assert _git("-C", app, "show", "epic/i/p:b.txt") == "b.txt"
    assert _git("-C", app, "show", "epic/i/p:c.txt") == "c.txt"


def test_a_missing_binary_is_an_exit_code_not_an_exception() -> None:
    code, output = run_argv(["cox-binary-that-does-not-exist"])
    assert code == 127
    assert "cox-binary-that-does-not-exist" in output


@pytest.mark.parametrize(
    "action",
    [
        _land("t1", "r"),
        {"kind": "pull", "epoch": 1},
        {"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1},
        {"kind": "mark_lost", "initiative": "i", "run": "r1", "epoch": 1},
        {"kind": "standby", "epoch": -1},
        {"kind": "housekeeping", "epoch": 1},
    ],
    ids=lambda action: action["kind"],
)
def test_a_dry_run_performs_and_records_nothing(action: dict) -> None:
    calls: list = []
    results = perform([action], _deps(calls), lambda: 1, True)  # type: ignore[list-item]
    assert [r["status"] for r in results] == ["dry_run"]
    assert calls == []


def test_a_rescue_launches_through_cox_route_launch_rescue() -> None:
    calls: list = []
    results = perform([{"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1}], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [("run", ["cox", "route", "launch", "rescue", "--initiative", "work/a", "--task", "t1", "--no-claim"])]
    assert [r["status"] for r in results] == ["done"]


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1},
        {"kind": "mark_lost", "initiative": "i", "run": "r1", "epoch": 1},
        {"kind": "check_login", "host": "shed", "epoch": 1},
    ],
    ids=lambda action: action["kind"],
)
def test_an_action_planned_under_a_stale_epoch_is_fenced_and_calls_nothing(action: dict) -> None:
    calls: list = []
    deps = replace(_deps(calls), check_login=lambda host: calls.append(("check_login", host)) or {})
    results = perform([action], deps, lambda: 2, False)  # type: ignore[list-item]
    assert [r["status"] for r in results] == ["fenced"]
    assert _touched(calls) == []


def test_an_unlisted_kind_is_refused() -> None:
    calls: list = []
    results = perform([{"kind": "cut_release", "epoch": 1}], _deps(calls), lambda: 1, False)  # type: ignore[list-item]
    assert [r["status"] for r in results] == ["refused"]
    assert _touched(calls) == []


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "standby", "epoch": 1},
        {"kind": "needs_chair", "epoch": 1, "initiative": "i"},
        {"kind": "mark_lost", "epoch": 1, "initiative": "i", "run": "r1"},
    ],
    ids=lambda action: action["kind"],
)
def test_a_bookkeeping_action_is_recorded_without_running_anything(action: dict) -> None:
    calls: list = []
    results = perform([action], _deps(calls), lambda: 1, False)  # type: ignore[list-item]
    assert [r["status"] for r in results] == ["recorded"]
    assert calls == [("record", action["kind"])]


def test_a_check_login_action_calls_the_injected_edge_with_its_host_and_is_recorded() -> None:
    hosts_seen: list = []

    def fake_check_login(host: str) -> dict:
        hosts_seen.append(host)
        return {"name": host, "versions_json": {"login_ok": True}}

    deps = replace(_deps([]), check_login=fake_check_login)
    results = perform([{"kind": "check_login", "host": "shed", "epoch": 1}], deps, lambda: 1, False)
    assert hosts_seen == ["shed"]
    assert [r["status"] for r in results] == ["recorded"]


def test_the_login_edge_passes_the_provider_profile_s_runner_and_env_names(monkeypatch, tmp_path) -> None:
    calls: list = []

    def fake_check_login_on_host(host, ssh, current_versions, now, ssh_run, cli_run, *, runner="claude-code", env_names=()):
        calls.append((runner, env_names))
        return {}

    monkeypatch.setattr(chair_exec.chair_login_check, "check_login_on_host", fake_check_login_on_host)
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, "chair-loop", 1, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host="host-b", harness_python="python",
        provider_profile=lambda: {"runner": "openai-compatible", "auth_env": "MY_API_KEY"},
    )
    deps.check_login("shed")
    assert calls == [("openai-compatible", ("MY_API_KEY",))]


def _edge(tmp_path, **kwargs) -> Deps:
    return chair_exec.edge_deps(
        tmp_path, tmp_path, "chair-loop", 1, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host="host-b", harness_python="python", **kwargs,
    )


def test_a_dry_run_edge_reaches_no_lane_host(monkeypatch, tmp_path) -> None:
    reached: list = []
    monkeypatch.setattr(chair_exec.subprocess, "run", lambda argv, **kw: reached.append(argv))
    deps = _edge(tmp_path, dry_run=True)
    ssh = ["ssh", "shed", "true"]
    assert deps.run(ssh) == (1, chair_exec.NOT_PROBED)
    assert deps.run(["git", "push", "shed:/repo", "main"]) == (1, chair_exec.NOT_PROBED)
    assert deps.check_login("shed") == {}
    results = perform([{"kind": "fetch_exit", "run": "x-1", "epoch": 1}], deps, lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run"]
    assert reached == []


LANE_BEAT = "2026-10-05T11:59:00Z"


def _remote_lane_store(monkeypatch) -> None:
    fresh = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # the chair reads the real clock
    host = {"name": "h", "beat_at": fresh, "ssh": "me@h", "state": "active", "versions_json": '{"workspace_dir": "/ws"}'}
    lane = run_store.Lane("alpha-2", "h", "2026-10-05T10:00:00Z", LANE_BEAT)
    monkeypatch.setattr(chair_pid_probe.run_store, "hosts", lambda runs_dir: [host])
    monkeypatch.setattr(chair_pid_probe.run_store, "live_lanes", lambda runs_dir, now: [lane])
    monkeypatch.setattr(chair_pid_probe, "local_runs", lambda runs_dir, now: (0, set()))


def _chair_deps(monkeypatch, tmp_path, dry_run: bool, fetch):
    """The real `cox chair run` bundle over a store with one remote lane, its ssh door replaced by `fetch`."""
    _remote_lane_store(monkeypatch)
    monkeypatch.setattr(cli, "_pid_probe_ssh", fetch)
    return cli._chair_run_deps(tmp_path, {}, "chair", 1, "box", dry_run, print, tmp_path / "p.yaml", "files")


def test_a_dry_run_chair_bundle_never_calls_fetch_for_a_remote_lane_and_reads_empty(monkeypatch, tmp_path) -> None:
    def fetch_must_not_run(argv):
        raise AssertionError(f"fetch called in a dry run: {argv}")

    deps = _chair_deps(monkeypatch, tmp_path, True, fetch_must_not_run)
    assert deps.facts_deps.pid_probe() == {}
    assert deps.exec_deps.run(["ssh", "shed", "true"]) == (1, chair_exec.NOT_PROBED)


def test_a_live_chair_bundle_still_calls_fetch_once_for_a_remote_lane(monkeypatch, tmp_path) -> None:
    dialled: list = []

    def fetch(argv):
        dialled.append(argv)
        return 0, "alive\n"

    deps = _chair_deps(monkeypatch, tmp_path, False, fetch)
    assert deps.facts_deps.pid_probe() == {"alpha": {"alive": True, "last_beat_at": LANE_BEAT}}
    assert len(dialled) == 1 and dialled[0][-2] == "me@h"


@pytest.mark.parametrize(
    ("action", "status", "calls"),
    [
        ({"kind": "standby", "epoch": -1, "holder": "h"}, "recorded", [("record", "standby")]),
        (
            {"kind": "take_lease", "epoch": -1, "holder": "h", "host": "x"},
            "done",
            [("lease", "h"), ("record", "take_lease")],
        ),
    ],
    ids=["standby", "take_lease"],
)
def test_an_action_planned_at_epoch_minus_one_is_performed_not_fenced(action: dict, status: str, calls: list) -> None:
    seen: list = []
    results = perform([action], _deps(seen), lambda: 3, False)  # type: ignore[list-item]
    assert [r["status"] for r in results] == [status]
    assert seen == calls


def test_every_result_is_recorded_with_its_status_and_a_reason_cut_to_600_chars() -> None:
    recorded: list = []
    deps = Deps(
        run=lambda argv: (1, "x" * 700), delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=recorded.append, run_id=lambda action: "", repo_for=lambda action: "",
    )
    actions = [{"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1}, {"kind": "pull", "epoch": 9}]
    perform(actions, deps, lambda: 1, False)
    assert [(a["kind"], a["status"]) for a in recorded] == [("rescue", "failed"), ("pull", "fenced")]
    assert recorded[0]["reason"] == "x" * 600
    assert recorded[0]["initiative"] == "a"


@pytest.mark.parametrize("kind", ["relaunch", "retry", "launch_epic"])
def test_an_epic_launch_names_the_initiative_by_its_work_path(kind: str) -> None:
    assert argv_for({"kind": kind, "initiative": "x"})[-3:] == ["--initiative", "work/x", "--no-claim"]  # type: ignore[typeddict-item,index]
    with_repo = argv_for({"kind": kind, "initiative": "x", "repo": "/r"})  # type: ignore[typeddict-item]
    assert with_repo[-5:] == ["--initiative", "work/x", "--no-claim", "--repo", "/r"]  # type: ignore[index]
    assert argv_for({"kind": kind, "initiative": "x", "host": "host-a"})[-2:] == ["--on", "host-a"]  # type: ignore[typeddict-item,index]


def test_run_argv_runs_in_the_given_directory(tmp_path) -> None:
    code, output = run_argv(["pwd"], cwd=tmp_path)
    assert (code, output.strip()) == (0, str(tmp_path.resolve()))


def test_a_take_lease_over_an_expired_takeover_steals_and_a_plain_one_does_not() -> None:
    calls: list = []
    deps = replace(_deps(calls), acquire_lease=lambda holder, host, steal=False: calls.append(("lease", steal)) or "")
    perform([{"kind": "take_lease", "epoch": 1, "reason": "takeover expired at 2026-09-26T15:00:00+00:00"}], deps, lambda: 1, False)
    perform([{"kind": "take_lease", "epoch": 1}], deps, lambda: 1, False)
    assert [c for c in calls if c[0] == "lease"] == [("lease", True), ("lease", False)]


def test_the_loop_takes_the_lease_under_its_own_host_though_the_action_names_none(monkeypatch, tmp_path) -> None:
    taken: list = []
    monkeypatch.setattr(chair_exec.chair, "acquire_lease", lambda runs_dir, session, pid, host, steal=False: taken.append((session, pid, host)) or "")
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, "chair-loop", 42, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host="host-b", harness_python="/venv/bin/python",
    )
    perform([{"kind": "take_lease", "epoch": 1, "reason": "takeover expired at 2026-09-27T23:00:00+00:00"}], deps, lambda: 1, False)
    assert taken == [("chair-loop", 42, "host-b")]


def test_a_command_past_its_timeout_is_exit_124_not_an_exception() -> None:
    assert run_argv(["sleep", "5"], timeout=0.05)[0] == 124


def test_a_lane_host_ssh_that_times_out_fails_with_its_partial_output_and_is_not_retried(monkeypatch) -> None:
    seen: list = []

    def hung(argv, **kwargs):
        seen.append(kwargs)
        raise subprocess.TimeoutExpired(argv, 30, output=b"visit https://login.example/x\n", stderr="waiting\n")

    monkeypatch.setattr(chair_exec.subprocess, "run", hung)
    code, output = run_lane_host(["ssh", "h", "true"])
    assert code == 124
    assert "https://login.example/x" in output and "waiting" in output
    assert [kwargs["timeout"] for kwargs in seen] == [30]


def test_a_hung_lane_host_child_is_cut_off_at_the_bound_not_awaited(monkeypatch) -> None:
    monkeypatch.setattr(chair_exec, "LANE_HOST_TIMEOUT_S", 0.2)
    hung = ["sh", "-c", "echo partial; exec sleep 30"]
    started = time.monotonic()
    code, output = run_lane_host(hung)
    assert code == 124
    assert "partial" in output
    assert time.monotonic() - started < 3


def test_the_real_run_door_bounds_an_ssh_argv_by_the_lane_bound_and_any_other_by_the_local_default(monkeypatch, tmp_path) -> None:
    seen: list = []
    monkeypatch.setattr(chair_exec.subprocess, "run", lambda argv, **kw: seen.append((argv[0], kw["timeout"])) or subprocess.CompletedProcess(argv, 0, "", ""))
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, "s", 1, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host="h", harness_python="/venv/bin/python",
    )
    deps.run(["ssh", "h", "true"])
    deps.run(["git", "-C", "/r", "push", "u@h:/r", "refs/heads/b:refs/heads/b"])
    deps.run(["git", "status"])
    assert seen == [("ssh", 30), ("git", 30), ("git", chair_exec.LOCAL_ARGV_TIMEOUT_S)]


def test_a_timed_out_branch_check_on_the_host_is_a_failure_not_an_absent_branch() -> None:
    remote: list = []
    local: list = []

    def at_home(argv):
        remote.append(argv)
        return 124, "ssh: timed out after 30s\nhalf a line"

    deps = replace(_deps([]), run=lambda argv: local.append(argv) or (1, ""))
    present, error = chair_exec._branch_on_host(deps, at_home, "u@h", "/r", "b")
    assert (present, len(remote), local) == (False, 1, [])
    assert "timed out after 30s" in error and "half a line" in error


@pytest.mark.parametrize(
    ("path", "intake_id", "expected"),
    [("intake/x.md", "alpha", "alpha"), ("intake/my-idea.md", "", "my-idea")],
    ids=["its_id", "its_stem"],
)
def test_an_intake_yields_its_id_else_its_stem(path: str, intake_id: str, expected: str) -> None:
    assert decompose_id(path, intake_id) == expected


@pytest.mark.parametrize("bad", ["a/b", "..", ".hidden", "a\\b"])
def test_a_path_shaped_id_is_refused(bad: str) -> None:
    assert isinstance(decompose_id("intake/x.md", bad), Refusal)


def test_a_long_traceback_keeps_its_last_lines_and_not_its_head() -> None:
    frames = "".join(f'  File "m{i}.py", line {i}\n' for i in range(50))
    kept = tail("Traceback (most recent call last):\n" + frames + "ValueError: boom", 100)
    assert kept.endswith('  File "m49.py", line 49\nValueError: boom')
    assert "Traceback" not in kept and len(kept) <= 100
    assert tail("short", 200) == "short"


_DECOMPOSE = ["cox", "route", "launch", "decompose", "--idea", "intake/x.md", "--initiative-id"]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("alpha",), [*_DECOMPOSE, "alpha", "--no-claim"]),
        (("I412", "sequence"), [*_DECOMPOSE, "I412", "--task-ids", "ordinal", "--no-claim"]),
        (("I412", "slug"), [*_DECOMPOSE, "I412", "--no-claim"]),
        ((), None),
    ],
    ids=["derived_id", "sequence_ids", "slug_ids", "no_id"],
)
def test_a_decompose_argv_keeps_the_path_as_idea_and_carries_the_id_and_ids_mode(args: tuple, expected: list | None) -> None:
    assert argv_for({"kind": "launch_decompose", "intake_ids": ["intake/x.md"]}, *args) == expected  # type: ignore[arg-type]


def _ticket(tmp_path, initiative: str, phase: str, ticket_id: str, title: str, body: str, surfaces: list[str], state: str = "ready"):
    path = tmp_path / "work" / initiative / phase / f"{ticket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    surfaces_line = "[" + ", ".join(surfaces) + "]"
    path.write_text(
        f"---\nid: {ticket_id}\nphase: {phase}\nstate: {state}\nneeds: []\nsurfaces: {surfaces_line}\ntitle: {title}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_initiative_tickets_reads_every_ticket_file_with_its_body(tmp_path) -> None:
    _ticket(tmp_path, "alpha", "build", "t1", "First ticket", "First body.", ["agent_tools/shared.py"])
    tickets = chair_exec._initiative_tickets(tmp_path, "alpha")
    assert [(t["id"], t["state"], t["surfaces"], t["body"]) for t in tickets] == [
        ("t1", "ready", ["agent_tools/shared.py"], "First body.")
    ]


def test_write_ticket_round_trips_through_initiative_tickets(tmp_path) -> None:
    _ticket(tmp_path, "alpha", "build", "t1", "First ticket", "First body.", ["agent_tools/shared.py"])
    [item] = chair_exec._initiative_tickets(tmp_path, "alpha")
    chair_exec._write_ticket(tmp_path, {**item, "title": "Renamed", "state": "dropped", "merged_into": "t2"})
    [reread] = chair_exec._initiative_tickets(tmp_path, "alpha")
    assert (reread["title"], reread["state"], reread["merged_into"]) == ("Renamed", "dropped", "t2")


@pytest.mark.parametrize("kind", ["launch_epic", "relaunch"])
def test_an_epic_launch_merges_same_phase_tickets_sharing_a_surface_before_dispatching_the_merged_ticket(kind: str, tmp_path) -> None:
    _ticket(tmp_path, "alpha", "build", "t1", "First ticket", "First body.", ["agent_tools/shared.py"])
    _ticket(tmp_path, "alpha", "build", "t2", "Second ticket", "Second body.", ["agent_tools/shared.py"])
    calls: list = []
    deps = replace(_deps(calls), work_dir=tmp_path)
    perform([{"kind": kind, "initiative": "alpha", "epoch": 1}], deps, lambda: 1, False)  # type: ignore[list-item]
    merged_text = (tmp_path / "work" / "alpha" / "build" / "t1.md").read_text(encoding="utf-8")
    dropped_text = (tmp_path / "work" / "alpha" / "build" / "t2.md").read_text(encoding="utf-8")
    assert "title: First ticket; Second ticket" in merged_text
    assert "state: ready" in merged_text
    assert "state: dropped" in dropped_text and "merged_into: t1" in dropped_text
    assert [c for c in calls if c[0] == "run"] == [("run", ["cox", "route", "launch", "epic", "--initiative", "work/alpha", "--no-claim"])]


def test_a_decompose_with_a_path_shaped_id_records_the_refusal_and_launches_nothing() -> None:
    calls: list = []
    recorded: list = []
    deps = replace(_deps(calls), intake_id=lambda path: "../x", record=recorded.append)
    perform([{"kind": "launch_decompose", "intake_ids": ["intake/x.md"], "epoch": 1}], deps, lambda: 1, False)
    assert _touched(calls) == []
    assert (recorded[0]["status"], "path-shaped" in recorded[0]["reason"]) == ("refused", True)


@pytest.mark.parametrize(
    ("path", "overrides", "tail_argv"),
    [
        ("intake/my-idea.md", {}, ["--initiative-id", "my-idea", "--no-claim"]),
        ("intake/x.md", {"intake_id": lambda path: "I412", "ids_mode": "sequence"}, ["--initiative-id", "I412", "--task-ids", "ordinal", "--no-claim"]),
    ],
    ids=["stem_without_an_id", "sequence_ids_mode"],
)
def test_a_decompose_launches_under_its_derived_id_and_ids_mode(path: str, overrides: dict, tail_argv: list) -> None:
    calls: list = []
    perform([{"kind": "launch_decompose", "intake_ids": [path], "epoch": 1}], replace(_deps(calls), **overrides), lambda: 1, False)
    assert _touched(calls) == [("run", ["cox", "route", "launch", "decompose", "--idea", path, *tail_argv])]


def test_prune_available_probes_the_harness_python() -> None:
    seen: list = []

    def run(argv: list) -> tuple[int, str]:
        seen.append(argv)
        return (0, "")

    assert chair_exec._prune_available(run, "/venv/bin/python") is True
    assert seen[0][0] == "/venv/bin/python"


_HK_PYTHON = "/venv/bin/python"
_HK_PROBE = [_HK_PYTHON, "-m", "harness.store_backfill_traces", "prune", "--help"]
_HK_PRUNE = [_HK_PYTHON, "-m", "harness.store_backfill_traces", "prune", "t/traces", "--older-than", "7"]
_HK_SYNC = ["cox", "lake", "sync"]


def _hk_deps(monkeypatch, calls: list, fail: tuple = ()) -> Deps:
    """Fake housekeeping runner: any argv in `fail` exits 1, everything else exits 0; traces root is fixed to "t/traces"."""
    def run(argv: list) -> tuple[int, str]:
        calls.append(argv)
        return (1, f"boom: {argv[0]}") if argv in fail else (0, "ok")

    monkeypatch.setattr(chair_exec.run_store, "_traces_root", lambda runs_dir: TracesRoot("t/traces", False))
    return Deps(
        run=run, delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=lambda action: None, run_id=lambda action: "", repo_for=lambda action: "",
        harness_python=_HK_PYTHON,
    )


@pytest.mark.parametrize(
    ("fail", "status", "sync", "prune", "prune_ran"),
    [
        ((), "done", "lake sync: ok", "prune: ok", True),
        ((_HK_SYNC,), "failed", "lake sync: FAILED", "prune: ok", True),
        ((_HK_PROBE,), None, "lake sync: ok", chair_housekeeping.PRUNE_SKIPPED, False),
    ],
    ids=["all_ok", "lake_sync_fails", "prune_probe_fails"],
)
def test_a_housekeeping_action_reports_every_step_whatever_an_earlier_one_did(
    monkeypatch, fail: tuple, status: str | None, sync: str, prune: str, prune_ran: bool
) -> None:
    calls: list = []
    deps = _hk_deps(monkeypatch, calls, fail=fail)
    results = perform([{"kind": "housekeeping", "epoch": 1}], deps, lambda: 1, False)
    reason = results[0]["reason"]
    assert status is None or results[0]["status"] == status
    assert sync in reason
    assert prune in reason
    assert f"clean skipped: {chair_housekeeping.CLEAN_MISSING}" in reason
    assert (_HK_PRUNE in calls) is prune_ran


def _write_stale_initiative(tmp_path, *, draft: str = "false") -> None:
    phase = tmp_path / "work" / "demo" / "phase-1"
    phase.mkdir(parents=True)
    (tmp_path / "work" / "demo" / "initiative.md").write_text(
        f"---\nid: demo\ndraft: {draft}\n---\nProse.\n", encoding="utf-8"
    )
    (phase / "t1.md").write_text("---\nid: t1\nstate: done\n---\nTicket body.\n", encoding="utf-8")


def _stale_action(**overrides) -> dict:
    return {
        "kind": "stale_to_draft", "initiative": "demo", "stale_tasks": ["t1"],
        "reason": "idle 10 days", "since": "2026-09-01T00:00:00Z", "epoch": 1, **overrides,
    }


def _stale_deps(calls: list, tmp_path) -> Deps:
    return replace(_deps(calls), work_dir=tmp_path, runs_dir=tmp_path, now=lambda: "2026-09-27T00:00:00+00:00")


def test_a_stale_to_draft_rewrites_the_files_notes_the_courier_and_is_approvable_to_ready(tmp_path) -> None:
    _write_stale_initiative(tmp_path)
    calls: list = []
    results = perform([_stale_action()], _stale_deps(calls, tmp_path), lambda: 1, False)
    assert results[0]["status"] == "done"
    initiative_text = (tmp_path / "work" / "demo" / "initiative.md").read_text(encoding="utf-8")
    ticket_text = (tmp_path / "work" / "demo" / "phase-1" / "t1.md").read_text(encoding="utf-8")
    assert "draft: true" in initiative_text
    assert "state: todo" in ticket_text
    assert ("record", "stale_to_draft") in calls
    blob = (tmp_path / "courier.jsonl").read_text(encoding="utf-8")
    # The inbox bare `cox` prints at start: its own chair label, as holder.
    [entry] = courier.inbox(blob, "chair-2026-09-27", holder="chair-2026-09-27")
    assert (entry["ref"], entry["note"]) == ("coxswain://initiative/demo", "idle 10 days")
    plan = plan_approve(initiative_text, {"t1": ticket_text}, None, "operator", datetime(2026, 9, 27, tzinfo=UTC))
    assert plan.moves == (("t1", "todo", "ready"),)


def test_a_tick_acks_the_open_land_notice_of_a_done_task(tmp_path) -> None:
    _write_stale_initiative(tmp_path)
    phase = tmp_path / "work" / "demo" / "phase-1"
    (phase / "t2.md").write_text("---\nid: t2\nstate: todo\n---\nBody.\n", encoding="utf-8")
    lines = [
        courier.send(courier.Reference("task", task), "chair-loop", "chair", "land it", f"m-{task}")
        for task in ("t1", "t2")
    ]
    (tmp_path / "courier.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    perform([], _stale_deps([], tmp_path), lambda: 1, False)
    blob = (tmp_path / "courier.jsonl").read_text(encoding="utf-8")
    assert [e["id"] for e in courier.inbox(blob)] == ["m-t2"]
    perform([], _stale_deps([], tmp_path), lambda: 1, False)
    assert (tmp_path / "courier.jsonl").read_text(encoding="utf-8") == blob


def test_a_stale_to_draft_for_an_already_draft_initiative_writes_nothing(tmp_path) -> None:
    _write_stale_initiative(tmp_path, draft="true")
    before_initiative = (tmp_path / "work" / "demo" / "initiative.md").read_text(encoding="utf-8")
    before_ticket = (tmp_path / "work" / "demo" / "phase-1" / "t1.md").read_text(encoding="utf-8")
    results = perform([_stale_action()], _stale_deps([], tmp_path), lambda: 1, False)
    assert results[0]["status"] == "refused"
    assert (tmp_path / "work" / "demo" / "initiative.md").read_text(encoding="utf-8") == before_initiative
    assert (tmp_path / "work" / "demo" / "phase-1" / "t1.md").read_text(encoding="utf-8") == before_ticket
    assert not (tmp_path / "courier.jsonl").exists()


def _signal_deps(calls: list, tmp_path) -> Deps:
    return replace(_deps(calls), runs_dir=tmp_path, send_signal=lambda pid, sig: calls.append(("signal", pid, sig)))


def _stalled_action(kind: str) -> dict:
    return {"kind": kind, "run": "r1", "initiative": "i", "epoch": 1}


@pytest.mark.parametrize("kind, sig", [("stalled_usr1", signal.SIGUSR1), ("stalled_stop", signal.SIGTERM)])
def test_a_stalled_action_with_a_pidfile_signals_the_pid_and_is_recorded_done(tmp_path, kind: str, sig: int) -> None:
    (tmp_path / "r1.pid").write_text("4242", encoding="utf-8")
    calls: list = []
    results = perform([_stalled_action(kind)], _signal_deps(calls, tmp_path), lambda: 1, False)
    assert results[0]["status"] == "done"
    assert ("signal", 4242, sig) in calls
    assert ("record", kind) in calls


def test_a_stalled_usr1_with_no_pidfile_signals_nothing_and_fails(tmp_path) -> None:
    calls: list = []
    results = perform([_stalled_action("stalled_usr1")], _signal_deps(calls, tmp_path), lambda: 1, False)
    assert results[0]["status"] == "failed"
    assert results[0]["reason"] == "no pidfile"
    assert [c for c in calls if c[0] == "signal"] == []


def test_a_stalled_stop_whose_pid_is_gone_is_recorded_failed_and_the_tick_goes_on(tmp_path) -> None:
    (tmp_path / "r1.pid").write_text("4242", encoding="utf-8")
    calls: list = []

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError(3, "No such process")

    deps = replace(_deps(calls), runs_dir=tmp_path, send_signal=gone)
    actions = [_stalled_action("stalled_stop"), {"kind": "standby", "epoch": 1}]
    results = perform(actions, deps, lambda: 1, False)
    assert [(r["action"]["kind"], r["status"]) for r in results] == [("stalled_stop", "failed"), ("standby", "recorded")]
    assert calls == [("record", "stalled_stop"), ("record", "standby")]


def test_land_commit_matches_git_log_on_a_fixture_repo(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda argv: subprocess.run(argv, cwd=repo, capture_output=True, text=True, check=True)  # noqa: E731
    run(["git", "init", "-q"])
    run(["git", "config", "user.email", "a@b.c"])
    run(["git", "config", "user.name", "tester"])
    (repo / "f.txt").write_text("one", encoding="utf-8")
    run(["git", "add", "f.txt"])
    run(["git", "commit", "-q", "-m", "first"])
    run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"])
    expected = run(["git", "log", "-1", "--format=%H", "origin/main"]).stdout.strip()
    assert land_commit(str(repo)) == expected


def test_land_commit_is_empty_on_a_git_failure(tmp_path) -> None:
    assert land_commit(str(tmp_path / "no-such-repo")) == ""


def _landed_result(repo: str, pr_url: str, commit: str) -> dict:
    reason = f"merge: ok\npr_create: {pr_url}\nmark_done: ok\n"
    return {"action": {"kind": "land", "repo": repo}, "status": "landed", "reason": reason, "commit": commit}


# An action's repo is the initiative frontmatter's checkout path, e.g. `repo: /home/user/repos/coxswain-tools`.
@pytest.mark.parametrize(
    "repo, pr, kept",
    [
        ("/x/coxswain-tools", 42, True),
        ("/x/coxswain", 9, True),
        ("/x/coxswain-graphs/", 7, True),
        ("/x/widget", 9, False),
    ],
)
def test_smoke_targets_keeps_only_the_named_repos_and_reads_the_pr_from_the_url(repo: str, pr: int, kept: bool) -> None:
    name = repo.rstrip("/").rsplit("/", 1)[1]
    landed_result = _landed_result(repo, f"https://github.com/org/{name}/pull/{pr}", "abc123")
    assert smoke_targets([landed_result]) == ([{"repo": repo, "pr": pr, "commit": "abc123"}] if kept else [])


def test_smoke_targets_reads_the_pr_from_the_pr_create_line_alone() -> None:
    # The shape `cli._land_walk` prints: one `<kind>: <detail>` line per step, `pr_create`'s detail being gh's PR URL.
    reason = (
        "forge: github\npick_branch: agents/r/t1 (Add seams)\ncherry_pick: cherry-picked a1b2c3d4 onto pr/t1\n"
        "checks: see https://github.com/org/other/pull/5\npush: pushed pr/t1\n"
        "pr_create: https://github.com/org/coxswain-tools/pull/118\n"
        "wait_checks: green\nmerge: merged\nclean: removed agents/r/t1\nmark_done: t1 done\n"
    )
    landed_result = {"action": {"kind": "land", "repo": "/x/coxswain-tools"}, "status": "landed", "reason": reason, "commit": "c0ffee"}
    assert smoke_targets([landed_result])[0]["pr"] == 118


def test_smoke_targets_skips_results_that_did_not_land() -> None:
    not_landed = {"action": {"kind": "land", "repo": "/x/coxswain-tools"}, "status": "not_landed", "reason": "boom"}
    assert smoke_targets([not_landed]) == []


def _tune(to_lanes: int = 3, epoch: int = 1) -> dict:
    return {
        "kind": "tune_lanes", "host": "hostA", "from_lanes": 2, "to_lanes": to_lanes, "epoch": epoch,
        "reason": "ahead of pace", "evidence": {"min_lanes": 1, "max_lanes": 4},
    }


def _propose(epoch: int = 1) -> dict:
    return {"kind": "propose_tiers", "proposals": [], "body": "cox settings set profile:tier.build opus", "epoch": epoch}


def _lane_only_writer(calls: list):
    def write(key: str, value: int) -> int:
        if not key.endswith(".capacity"):
            raise AssertionError(f"not a lane key: {key}")
        calls.append(("write", key, value))
        return 0

    return write


def _tune_deps(calls: list, tmp_path) -> Deps:
    write = _lane_only_writer(calls)
    return replace(
        _deps(calls), work_dir=tmp_path, runs_dir=tmp_path, set_lanes=lambda host, n: write(f"{host}.capacity", n),
    )


def test_a_tune_lanes_calls_the_lane_writer_once_and_records_one_action(tmp_path) -> None:
    calls: list = []
    results = perform([_tune()], _tune_deps(calls, tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    assert calls == [("write", "hostA.capacity", 3), ("record", "tune_lanes")]


def test_a_tune_lanes_outside_min_or_max_writes_nothing(tmp_path) -> None:
    calls: list = []
    results = perform([_tune(0), _tune(5)], _tune_deps(calls, tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "refused"]
    assert _touched(calls) == []


def test_a_propose_tiers_sends_one_inbox_item_with_the_body_and_writes_no_setting(tmp_path) -> None:
    calls: list = []
    results = perform([_propose()], _tune_deps(calls, tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    [entry] = courier.entries((tmp_path / "courier.jsonl").read_text(encoding="utf-8"))
    assert (entry["to"], entry["note"]) == ("chair", "cox settings set profile:tier.build opus")
    assert calls == [("record", "propose_tiers")]


def _armed_deps(calls: list, tmp_path) -> Deps:
    """Deps on which every fenceable kind has something to do: a stale initiative, a pidfile and a lane writer."""
    _write_stale_initiative(tmp_path)
    (tmp_path / "r1.pid").write_text("4242", encoding="utf-8")
    return replace(
        _tune_deps(calls, tmp_path),
        now=lambda: "2026-09-27T00:00:00+00:00",
        send_signal=lambda pid, sig: calls.append(("signal", pid, sig)),
    )


_FENCEABLE = [_stale_action(), _stalled_action("stalled_usr1"), _stalled_action("stalled_stop"), _tune(), _propose()]


@pytest.mark.parametrize("action", _FENCEABLE, ids=lambda a: a["kind"])
def test_a_fenced_action_is_fenced_and_reaches_nothing(tmp_path, action: dict) -> None:
    calls: list = []
    results = perform([action], _armed_deps(calls, tmp_path), lambda: 2, False)
    assert results[0]["status"] == "fenced"
    assert _touched(calls) == []
    assert not (tmp_path / "courier.jsonl").exists()


@pytest.mark.parametrize("action", _FENCEABLE, ids=lambda a: a["kind"])
def test_a_dry_run_action_performs_and_records_nothing(tmp_path, action: dict) -> None:
    calls: list = []
    results = perform([action], _armed_deps(calls, tmp_path), lambda: 1, True)
    assert results[0]["status"] == "dry_run"
    assert calls == []
    assert not (tmp_path / "courier.jsonl").exists()


def _epic(initiative: str, host: str = "", epoch: int = 1) -> dict:
    return {"kind": "launch_epic", "initiative": initiative, "epoch": epoch, **({"host": host} if host else {})}


def _staggered(calls: list, tmp_path: Path, actions: list, **fields) -> list:
    """Runs `actions` with a sleep that records into `calls` beside the launches; no real waiting."""
    deps = replace(_deps(calls), work_dir=tmp_path, **fields)
    return perform(actions, deps, lambda: 1, False, sleep=lambda s: calls.append(("sleep", s)))


def _sleeps(calls: list) -> list:
    return [c for c in calls if c[0] == "sleep"]


@pytest.mark.parametrize(
    "hosts, order, sleeps",
    [
        (["host-a", "host-a"], ["run", "sleep", "run"], [("sleep", 20.0)]),
        (["", ""], ["run", "sleep", "run"], [("sleep", 20.0)]),
        (["host-a", "friday"], ["run", "run"], []),
        (["host-a", "friday", "host-a"], ["run", "run", "sleep", "run"], [("sleep", 20.0)]),
    ],
    ids=["same-host", "no-host-is-local", "different-hosts", "third-launch-waits"],
)
def test_launches_sleep_only_between_two_launches_on_the_same_host_and_keep_their_order(
    tmp_path: Path, hosts: list, order: list, sleeps: list
) -> None:
    calls: list = []
    actions = [_epic(name, host) for name, host in zip("abc", hosts)]
    results = _staggered(calls, tmp_path, actions)
    assert [c[0] for c in _touched(calls)] == order
    assert _sleeps(calls) == sleeps
    assert [r["action"]["initiative"] for r in results] == [a["initiative"] for a in actions]


def test_launch_stagger_s_overrides_the_default_and_zero_skips_the_sleep(tmp_path: Path) -> None:
    actions = [_epic("a", "host-a"), _epic("b", "host-a")]
    five: list = []
    _staggered(five, tmp_path, actions, launch_stagger_s=5.0)
    zero: list = []
    _staggered(zero, tmp_path, actions, launch_stagger_s=0.0)
    assert _sleeps(five) == [("sleep", 5.0)]
    assert _sleeps(zero) == []


def test_a_fenced_launch_does_not_start_a_stagger(tmp_path: Path) -> None:
    calls: list = []
    _staggered(calls, tmp_path, [_epic("a", "host-a", epoch=0), _epic("b", "host-a")])
    assert _sleeps(calls) == []


def test_stagger_seconds_reads_the_profile_value() -> None:
    values = [None, "x", "5", 0, -1, 7.5, True, float("inf")]
    assert [chair_exec.stagger_seconds(v) for v in values] == [20.0, 20.0, 5.0, 0.0, 20.0, 7.5, 20.0, 20.0]


NO_PHASE_RECORD = "land: no phase record at /home/user/ws/runs/run-1:p1.json\n"


def _phase_land_by_task_deps(calls: list, phase_code: int, phase_output: str) -> Deps:
    """The land_phase answers `phase_code`/`phase_output`; every task land lands; git lists no sibling branches."""

    def run(argv):
        calls.append(("run", argv))
        if argv[:3] == ["cox", "runs", "land"]:
            return (phase_code, phase_output) if "--phase" in argv else (0, LANDED)
        return 0, ""

    return replace(_deps([]), run=run, record=lambda action: calls.append(("record", action["kind"])))


def _phase_land_with_tasks() -> dict:
    return {**_land_phase("p1", "r"), "initiative": "A", "task_runs": {"t1": "run-1", "t2": "run-1"}}


def _cox_lands(calls: list) -> list:
    return [c[1][3:6] for c in calls if c[0] == "run" and c[1][:3] == ["cox", "runs", "land"]]


def test_a_land_phase_refused_for_a_missing_phase_record_lands_its_tasks_in_the_same_tick() -> None:
    calls: list = []
    results = perform([_phase_land_with_tasks()], _phase_land_by_task_deps(calls, 1, NO_PHASE_RECORD), lambda: 1, False)
    assert _cox_lands(calls) == [["run-1", "--phase", "p1"], ["run-1", "--task", "t1"], ["run-1", "--task", "t2"]]
    assert [(r["status"], r["action"]["kind"]) for r in results] == [("landed", "land_phase")]
    assert results[0]["reason"].startswith(NO_PHASE_RECORD.strip())
    assert ("record", "needs_chair") not in calls


@pytest.mark.parametrize(
    "code, output, with_task_runs",
    [(2, "land: refusing, /home/user/ws/app has uncommitted changes\n", True), (1, NO_PHASE_RECORD, False)],
    ids=["another-reason", "no-task-runs"],
)
def test_a_phase_land_refusal_falls_back_only_for_a_missing_record_with_task_runs(
    code: int, output: str, with_task_runs: bool
) -> None:
    calls: list = []
    land = _phase_land_with_tasks() if with_task_runs else {**_land_phase("p1", "r"), "initiative": "A"}
    results = perform([land], _phase_land_by_task_deps(calls, code, output), lambda: 1, False)
    assert _cox_lands(calls) == [["run-1", "--phase", "p1"]]
    assert [r["status"] for r in results] == ["refused", "escalated"]


def test_lacks_phase_record_reads_only_a_failed_land_with_the_refusal_line() -> None:
    assert [
        chair_exec.lacks_phase_record(code, output)
        for code, output in [(1, NO_PHASE_RECORD), (1, "fetch: ok\n" + NO_PHASE_RECORD), (0, NO_PHASE_RECORD), (1, LANDED)]
    ] == [True, True, False, False]
