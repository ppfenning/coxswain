"""`cox runs recover` records an escalated task as merged into its phase branch, the way the epic records its own merges.

The decisions are pure and tested on example values. The command runs end to end on a real git repository in a temp
dir and a throwaway sqlite store, reached only through its real API, in process: nothing in the command path is replaced.
"""

import json
import shlex
import subprocess as sp

import pytest

from agent_tools import cli, land, run_store, store_cli
from harness.epic import unmerged_approved
from harness.landing_ops import off_branch
from harness.store_migrate import open_store
from harness.store_read import work_items

_SHA = "0e12" + "a" * 36
_TASK, _RUN, _PHASE_BRANCH, _SOURCE = "seams-task", "epic-x-5", "epic/x/seams", "agents/epic-x-5/seams-task"


# --- pure ---

@pytest.mark.parametrize("row, reopen, landed, refused", [
    (("approved", None), False, False, None),
    (("approved", None), True, False, None),
    (("done", None), True, False, None),
    (None, False, False, "the store has no row for it"),
    (("ready", None), False, False, "its state is 'ready', not approved"),
    (("done", None), False, False, "pass --reopen"),
    (("done", "abc123"), True, False, "merge commit abc123 recorded, so it landed"),
    (("done", None), True, True, "already on the default branch, so it landed; it stays done"),
    (("approved", None), False, True, "already on the default branch"),
])
def test_recover_gate(row, reopen, landed, refused):
    reason = land.recover_gate(row, reopen=reopen, landed=landed)
    assert reason is None if refused is None else refused in reason


def test_recover_contained_refusal_turns_away_an_attempt_that_built_nothing():
    assert land.recover_contained_refusal(1, "agents/r-1/t", "main") is None
    assert land.recover_contained_refusal(0, "agents/r-1/t", "main") == (
        "agents/r-1/t has no commit of its own off main: it built nothing, or its work is on main already")


def _front(**fields):
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n\nBody.\n"


def test_recovered_text_sets_the_three_lines_and_keeps_every_other_one():
    text = _front(id="t", state="done", hold_reason="'kept as written'")
    new = land.recovered_text(text, _SHA)
    assert new == _front(id="t", state="approved", hold_reason="'kept as written'", merged="true", merged_commit=f'"{_SHA}"')
    assert land.recovered_text(new, _SHA) is None
    assert land.recovered_text("no front matter\n", _SHA) is None


# --- end to end ---

def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def ws(tmp_path):
    """A workspace with a real store, in process, under its own runs directory."""
    (tmp_path / "runs").mkdir()
    open_store(run_store.store_url_for(tmp_path / "runs"), "2026-01-01T00:00:00Z").close()
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "f").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    _git(root, "checkout", "-qb", _SOURCE)
    (root / "g").write_text("y")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "Add seams module")
    _git(root, "checkout", "-q", "main")
    return root


def _task(ws, state="approved", merged_commit=None, item_state="approved"):
    """The task record, the work item and the store row, each as the harness and the board leave them."""
    record = ws / "runs" / _RUN / "tasks" / "seams" / f"{_TASK}.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"run_id": f"{_RUN}:seams:seams-t", "ticket": _TASK, "review": {"verdict": "approve"}}))
    item = ws / "work" / "x" / "seams" / f"{_TASK}.md"
    item.parent.mkdir(parents=True)
    item.write_text(f"---\nid: {_TASK}\nstate: {item_state}\n---\n\nBody.\n", encoding="utf-8")
    if state is not None:
        assert run_store.upsert_row(ws / "runs", {
            "kind": "task", "initiative": "x", "task_id": _TASK, "phase": "seams", "state": "approved",
            "needs": [], "surfaces": [], "title": _TASK, "body": "", "extra": {}})
        if merged_commit:
            assert isinstance(store_cli.mark_merged(ws / "runs", "x", _TASK, merged_commit, "t"), store_cli.MergeRecorded)
        if state != "approved":
            assert isinstance(store_cli.set_state(ws / "runs", "x", _TASK, state, "t"), store_cli.StateSet)
    return record, item


def _recover(ws, repo, capsys, *flags):
    rc = cli.main(["runs", "recover", _RUN, _TASK, "--repo", str(repo), "--runs-dir", str(ws / "runs"), *flags])
    return rc, capsys.readouterr().out


def _epic_view(ws, repo):
    """What the epic reads: the harness's own store rows, the tasks `_merge_into_initiative` holds the phase back for,
    and the approved tasks whose merge commit the phase tip lacks."""
    conn = open_store(run_store.store_url_for(ws / "runs"), "2026-01-01T00:00:00Z")
    try:
        rows = [{**r, "id": r["task_id"]} for r in work_items(conn, "x")]
    finally:
        conn.close()
    return unmerged_approved(rows), off_branch(repo, _PHASE_BRANCH, rows, "seams")


def _merged_text(commit):
    return f'---\nid: {_TASK}\nstate: approved\nmerged: true\nmerged_commit: "{commit}"\n---\n\nBody.\n'


def test_end_to_end_recover_on_a_real_repo_records_the_merge_so_the_epic_s_phase_check_sees_it(ws, repo, capsys):
    _git(repo, "branch", _PHASE_BRANCH, "main")
    record, item = _task(ws)
    assert _epic_view(ws, repo) == ([_TASK], [])
    rc, out = _recover(ws, repo, capsys)
    assert rc == 0, out
    tip = _git(repo, "rev-parse", _PHASE_BRANCH)
    assert _git(repo, "merge-base", "--is-ancestor", _SOURCE, _PHASE_BRANCH) == ""
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("approved", tip)
    assert json.loads(record.read_text())["merged_commit"] == tip
    assert item.read_text(encoding="utf-8") == _merged_text(tip)
    assert _epic_view(ws, repo) == ([], [])


def test_a_phase_that_already_holds_the_commit_records_the_task_s_own_commit(ws, repo, capsys):
    _git(repo, "branch", _PHASE_BRANCH, _SOURCE)
    _task(ws)
    rc, out = _recover(ws, repo, capsys)
    assert rc == 0, out
    assert "nothing to merge" in out
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("approved", _git(repo, "rev-parse", _SOURCE))


def test_a_dry_run_writes_nothing(ws, repo, capsys):
    _git(repo, "branch", _PHASE_BRANCH, "main")
    record, item = _task(ws)
    before = (_git(repo, "rev-parse", _PHASE_BRANCH), record.read_text(), item.read_text())
    rc, out = _recover(ws, repo, capsys, "--dry-run")
    assert rc == 0, out
    assert f"would merge {_SOURCE} into {_PHASE_BRANCH}" in out and '"kind": "mark_merged"' in out
    assert (_git(repo, "rev-parse", _PHASE_BRANCH), record.read_text(), item.read_text()) == before
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("approved", None)


@pytest.mark.parametrize("state, flags, said", [
    (None, (), "the store has no row for it"),
    ("ready", (), "its state is 'ready', not approved"),
    ("done", (), "pass --reopen"),
], ids=["no-store-row", "ready-row", "done-row-without-reopen"])
def test_a_row_recover_may_not_record_is_refused_before_the_merge_and_nothing_moves(ws, repo, capsys, state, flags, said):
    _git(repo, "branch", _PHASE_BRANCH, "main")
    record, item = _task(ws, state=state)
    before = (_git(repo, "rev-parse", _PHASE_BRANCH), record.read_text(), item.read_text())
    rc, out = _recover(ws, repo, capsys, *flags)
    assert rc == 2, out
    assert said in out
    assert (_git(repo, "rev-parse", _PHASE_BRANCH), record.read_text(), item.read_text()) == before


def test_reopen_puts_a_done_row_with_no_merge_commit_back_to_approved_and_records_it(ws, repo, capsys):
    _git(repo, "branch", _PHASE_BRANCH, _SOURCE)
    _, item = _task(ws, state="done", item_state="done")
    rc, out = _recover(ws, repo, capsys, "--reopen")
    assert rc == 0, out
    commit = _git(repo, "rev-parse", _SOURCE)
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("approved", commit)
    assert item.read_text(encoding="utf-8") == _merged_text(commit)


def test_reopen_refuses_a_done_row_that_carries_a_merge_commit(ws, repo, capsys):
    _git(repo, "branch", _PHASE_BRANCH, _SOURCE)
    landed = _git(repo, "rev-parse", "main")
    _task(ws, state="done", merged_commit=landed, item_state="done")
    rc, out = _recover(ws, repo, capsys, "--reopen")
    assert rc == 2, out
    assert "so it landed; it stays done" in out
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("done", landed)


@pytest.mark.parametrize("how, said", [
    ("merged", "has no commit of its own off main: it built nothing, or its work is on main already"),
    ("cherry-picked", "its commit is already on the default branch, so it landed; it stays done"),
])
def test_reopen_refuses_a_done_task_whose_commit_is_on_main(ws, repo, capsys, how, said):
    """A ticket-gate land marks done without a merge commit: its work is on main, as the same commit or the same patch."""
    _git(repo, "branch", _PHASE_BRANCH, _SOURCE)
    if how == "merged":
        _git(repo, "merge", "-q", "--no-ff", _SOURCE, "-m", "land")
    else:
        (repo / "h").write_text("z")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "main moved on")
        _git(repo, "cherry-pick", _SOURCE)
    _task(ws, state="done", item_state="done")
    rc, out = _recover(ws, repo, capsys, "--reopen")
    assert rc == 2, out
    assert said in out
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("done", None)


def test_reopen_refuses_a_task_whose_phase_was_squash_merged_to_main(ws, repo, capsys):
    """The project lands with --squash: the task's own commit is never on main, only a squash of its phase."""
    _git(repo, "branch", _PHASE_BRANCH, "main")
    _git(repo, "checkout", "-q", _PHASE_BRANCH)
    _git(repo, "merge", "-q", "--no-ff", _SOURCE, "-m", "merge the task into its phase")
    (repo / "sibling").write_text("a sibling task's work")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a sibling task in the same phase")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", _PHASE_BRANCH)
    _git(repo, "commit", "-qm", "land the phase (squash)")
    assert sp.run(["git", "-C", str(repo), "cherry", "main", _SOURCE], capture_output=True, text=True).stdout.startswith("+")
    _task(ws, state="done", item_state="done")
    rc, out = _recover(ws, repo, capsys, "--reopen")
    assert rc == 2, out
    assert "its commit is already on the default branch, so it landed; it stays done" in out
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("done", None)


def _origin(tmp_path, repo):
    """A bare origin holding main and the task branch, set as `repo`'s origin with origin/HEAD at main."""
    bare = tmp_path / "origin.git"
    sp.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "remote", "set-head", "origin", "--auto")
    return bare


def test_a_landing_on_origin_made_elsewhere_is_seen_because_recover_fetches_first(ws, repo, capsys, tmp_path):
    _git(repo, "branch", _PHASE_BRANCH, _SOURCE)
    bare = _origin(tmp_path, repo)
    _task(ws, state="done", item_state="done")
    rc, out = _recover(ws, repo, capsys, "--reopen", "--dry-run")
    assert rc == 0, out
    other = tmp_path / "other"
    sp.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "merge", "-q", "--squash", f"origin/{_SOURCE}")
    _git(other, "commit", "-qm", "land (squash)")
    _git(other, "push", "-q", "origin", "main")
    assert _git(repo, "rev-parse", "origin/main") != _git(other, "rev-parse", "main")  # this clone has not seen it
    rc, out = _recover(ws, repo, capsys, "--reopen")
    assert rc == 2, out
    assert "its commit is already on the default branch, so it landed" in out
    assert _git(repo, "rev-parse", "origin/main") == _git(other, "rev-parse", "main")
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("done", None)


@pytest.mark.parametrize("break_origin, said", [
    (lambda repo: _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/pruned"), "cannot fetch origin's pruned"),
    (lambda repo: _git(repo, "remote", "set-head", "origin", "--delete"), "origin/HEAD is not set"),
], ids=["origin-head-at-a-pruned-branch", "no-origin-head"])
def test_when_git_cannot_tell_whether_the_task_landed_recover_refuses(ws, repo, capsys, tmp_path, break_origin, said):
    _git(repo, "branch", _PHASE_BRANCH, _SOURCE)
    _origin(tmp_path, repo)
    break_origin(repo)
    _task(ws, state="done", item_state="done")
    rc, out = _recover(ws, repo, capsys, "--reopen")
    assert rc == 2, out
    assert said in out
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("done", None)


def _main_moves_onto_the_task_s_file(repo):
    """main takes a different version of the file the task adds: the task's work conflicts with main, while the
    phase branch, cut before that, takes the task cleanly."""
    _git(repo, "branch", _PHASE_BRANCH, "main")
    (repo / "g").write_text("main's own version")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main moves on the same file")


def test_end_to_end_an_approved_task_that_conflicts_with_main_is_still_merged_into_its_phase_and_recorded(ws, repo, capsys):
    """The main use: a self_modification merge was escalated, and main has since moved on the same file."""
    _main_moves_onto_the_task_s_file(repo)
    _, item = _task(ws)
    rc, out = _recover(ws, repo, capsys)
    assert rc == 0, out
    tip = _git(repo, "rev-parse", _PHASE_BRANCH)
    assert _git(repo, "merge-base", "--is-ancestor", _SOURCE, _PHASE_BRANCH) == ""
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("approved", tip)
    assert item.read_text(encoding="utf-8") == _merged_text(tip)
    assert _epic_view(ws, repo) == ([], [])


@pytest.mark.parametrize("named_store", [False, True], ids=["store-in-runs-dir", "store-named-by-provider-profile"])
def test_end_to_end_a_reopen_that_conflicts_with_main_names_the_next_step_and_the_step_works(ws, repo, capsys, monkeypatch, named_store):
    if named_store:
        # The store lives where the provider profile's storage_url says, not in the runs dir: the printed command
        # must reach it through --provider-profile, or the row it moves is the wrong one.
        elsewhere = ws / "elsewhere"
        elsewhere.mkdir()
        provider = ws / "provider.yaml"
        provider.write_text(f"storage_url: sqlite:///{elsewhere / 'cox.db'}\n", encoding="utf-8")
        routing = ws / "routing-named.yaml"
        routing.write_text(f"workspace_dir: {ws}\nharness_dir: {ws / 'harness'}\nprovider_profile: {provider}\n", encoding="utf-8")
        monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(routing))
        open_store(run_store.store_url_for(ws / "runs"), "2026-01-01T00:00:00Z").close()
        assert run_store.store_url_for(ws / "runs") == f"sqlite:///{elsewhere / 'cox.db'}"
    _main_moves_onto_the_task_s_file(repo)
    _task(ws, state="done", item_state="done")
    rc, out = _recover(ws, repo, capsys, "--reopen")
    assert rc == 2, out
    assert "merging it into main conflicts" in out
    assert "so recover cannot tell whether it landed. To check, look for the task's pull request" in out
    lines = out.splitlines()
    set_state = lines[lines.index(next(line for line in lines if line.endswith("put its row back to approved:"))) + 1]
    assert ("--provider-profile" in set_state) is named_store and "--runs-dir" in set_state
    again = lines[-1]
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("done", None)
    done = sp.run(shlex.split(set_state), capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("approved", None)
    assert shlex.split(again)[:3] == ["cox", "runs", "recover"]
    rc = cli.main(shlex.split(again)[1:])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("approved", _git(repo, "rev-parse", _PHASE_BRANCH))


def test_a_done_row_with_a_merge_commit_is_refused_as_landed_even_when_its_work_conflicts_with_main(ws, repo, capsys):
    """The recorded merge commit is the definite answer: recover neither asks git nor offers a way around it."""
    _main_moves_onto_the_task_s_file(repo)
    landed = _git(repo, "rev-parse", "main")
    _task(ws, state="done", merged_commit=landed, item_state="done")
    rc, out = _recover(ws, repo, capsys, "--reopen")
    assert rc == 2, out
    assert "so it landed; it stays done" in out
    assert "conflicts" not in out and "put its row back" not in out
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("done", landed)


def test_reopen_conflict_next_step_says_how_to_check_and_names_both_commands():
    text = land.reopen_conflict_next_step("origin/main", "agents/r-1/t", "epic/x/p1",
                                          ["py", "-m", "harness.store_cli", "set-state", "x", "t", "approved"],
                                          ["cox", "runs", "recover", "r-1", "t", "--repo", "/repo dir"])
    assert text == (
        "recover: the task's work conflicts with origin/main, so recover cannot tell whether it landed. To check, look "
        "for the task's pull request, or for its change among the commits on origin/main that touch its files:\n"
        "  git log --oneline origin/main -- $(git diff --name-only origin/main...agents/r-1/t)\n"
        "If it did not land, put its row back to approved:\n  py -m harness.store_cli set-state x t approved\n"
        "then run recover again without --reopen; it merges the task into epic/x/p1 and records it:\n"
        "  cox runs recover r-1 t --repo '/repo dir'"
    )


@pytest.mark.parametrize("merge_rc, merge_out, tree_rc, tree_out, expected", [
    (0, "t1\n", 0, "t1\n", True),
    (0, "t2\n", 0, "t1\n", False),
    (1, "t2\nconflict\n", 0, "t1\n", None),
    (128, "", 0, "t1\n", None),
    (0, "t1\n", 128, "", None),
])
def test_landed_by_tree_fails_closed(merge_rc, merge_out, tree_rc, tree_out, expected):
    landed, why = land.landed_by_tree(merge_rc, merge_out, tree_rc, tree_out, "main", "agents/r-1/t")
    assert landed is expected
    assert (why == "") is (expected is not None)


def test_an_attempt_branch_with_no_commit_of_its_own_is_not_counted_as_recovered(ws, repo, capsys):
    _git(repo, "branch", "-f", _SOURCE, "main")
    _git(repo, "branch", _PHASE_BRANCH, "main")
    _task(ws)
    rc, out = _recover(ws, repo, capsys)
    assert rc == 2, out
    assert f"{_SOURCE} has no commit of its own off main: it built nothing" in out
    assert run_store.task_merge_row(ws / "runs", "x", _TASK) == ("approved", None)


@pytest.mark.parametrize("ticket, work_dirs, said", [
    ("other-task", ("x/seams",), "disagrees with its filename"),
    (_TASK, (), "no single initiative"),
    (_TASK, ("x/seams", "y/seams"), "no single initiative"),
], ids=["ticket-disagrees-with-filename", "no-initiative-holds-the-task", "two-initiatives-hold-the-task"])
def test_an_unresolvable_record_is_refused(ws, repo, capsys, ticket, work_dirs, said):
    _git(repo, "branch", _PHASE_BRANCH, "main")
    record = ws / "runs" / _RUN / "tasks" / "seams" / f"{_TASK}.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"run_id": f"{_RUN}:seams:seams-t", "ticket": ticket}))
    for d in work_dirs:
        (ws / "work" / d).mkdir(parents=True)
        (ws / "work" / d / f"{_TASK}.md").write_text(f"---\nid: {_TASK}\nstate: approved\n---\n", encoding="utf-8")
    rc, out = _recover(ws, repo, capsys, "--dry-run")
    assert rc == 2
    assert said in out
