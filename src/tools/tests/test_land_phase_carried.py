"""A phase branch whose work main already carries is landed: tasks marked done, reported as `landed via PR #n`."""
import subprocess
from types import SimpleNamespace

# The store-mode land harness: `_land` drives `cli._runs_land` against a temporary repo and store.
from test_land_from_store import _land, _own_tempdir, repo, store  # noqa: F401

from agent_tools import cli, land

ITEMS = [{"id": "a", "status": "approved"}, {"id": "b", "status": "approved"}]
PHASE = {"run": "r", "phase": "p", "initiative": "i"}
RECORDS = [{"run": "r", "task": t, "phase": "p", "initiative": "i", "status": "approved",
            "review": {"verdict": "approve"}, "build": {"patch": "x", "files_touched": [f"{t}.txt"]}} for t in ("a", "b")]
NO_PRS: list[dict] = []


def git(where, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(where), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _commit(where, name, text, subject=None):
    (where / name).write_text(text + "\n")
    git(where, "add", name)
    git(where, "commit", "-qm", subject or text)


def _repo(tmp_path, main_subject="phase work (#42)", follow_up=False, own_commits=True):
    """A clone whose main holds the squash of epic/i/p (a.txt, b.txt) under `main_subject`.
    `follow_up` adds a commit main lacks; `own_commits` False leaves epic/i/p level with main."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    git(clone, "remote", "add", "origin", str(origin))
    _commit(clone, "base.txt", "base")
    git(clone, "checkout", "-qb", "epic/i/p")
    if own_commits:
        _commit(clone, "a.txt", "a")
        _commit(clone, "b.txt", "b")
    if follow_up:
        _commit(clone, "c.txt", "follow-up")
    git(clone, "checkout", "-q", "main")
    if own_commits:
        _commit(clone, "a.txt", "a", main_subject)
        _commit(clone, "b.txt", "b", main_subject)
    git(clone, "push", "-q", "origin", "main")
    return clone


def _carried(clone, prs=NO_PRS, parent=None):
    by_branch = {"pr/i--p": prs, "pr/i--q": [{"number": 7, "state": "merged"}]}
    forge = SimpleNamespace(find_branch_prs=lambda _where, branch: by_branch[branch])
    return cli._phase_merged_pr(clone, "i", "p", "main", forge, cli._carried_files(ITEMS, RECORDS), parent)


def test_carried_files_are_the_approved_tasks_touched_files():
    items = [*ITEMS, {"id": "c", "status": "done"}]
    records = [*RECORDS, {"task": "c", "build": {"files_touched": ["c.txt"]}}]
    assert cli._carried_files(items, records) == ["a.txt", "b.txt"]


def test_a_branch_main_carries_names_the_pr_in_the_subject_of_the_commit_that_touched_its_files(tmp_path):
    assert cli._carried_pr(_carried(_repo(tmp_path))) == "#42"


def test_a_carried_phase_plans_mark_done_per_task_and_reports_landed_via(tmp_path):
    merged = _carried(_repo(tmp_path))
    steps = land.land_plan(PHASE, {}, "main", items=ITEMS, task_records=RECORDS, already_merged=merged is not None)
    assert [s["kind"] for s in steps] == ["clean_phase", "mark_done", "mark_done"]
    assert cli._merged_line(merged) == "landed via PR #42"


def test_an_own_pr_merged_phase_keeps_the_already_merged_line():
    assert cli._merged_line({"number": 535, "state": "merged"}) == "merge: already merged (PR #535)"
    assert cli._carried_pr({"number": 535, "state": "merged"}) == ""


def test_the_parent_phase_merged_pr_names_the_carrier_when_no_subject_does(tmp_path):
    assert _carried(_repo(tmp_path, main_subject="phase work"), parent="q")["number"] == 7


def test_with_no_carrying_pr_number_the_phase_is_not_carried(tmp_path):
    assert _carried(_repo(tmp_path, main_subject="phase work")) is None


def test_a_branch_with_changes_over_main_is_not_carried_and_still_plans_a_squash(tmp_path):
    assert _carried(_repo(tmp_path, follow_up=True)) is None
    steps = land.land_plan(PHASE, {}, "main", items=ITEMS, task_records=RECORDS, already_merged=False)
    assert "squash_phase" in [s["kind"] for s in steps]


def _fetched(clone):
    git(clone, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    return clone


def test_a_later_commit_on_the_files_does_not_lend_its_pr_number(tmp_path):
    clone = _repo(tmp_path)
    _commit(clone, "a.txt", "changed again", "unrelated change (#99)")
    git(clone, "push", "-q", "origin", "main")
    subjects = cli._main_subjects_touching(_fetched(clone), "main", "epic/i/p", ["a.txt", "b.txt"])
    assert (subjects, land.carrying_pr(subjects, None)) == (["phase work (#42)"], 42)


def test_the_commits_that_left_the_files_as_the_branch_holds_them_name_the_pr(tmp_path):
    subjects = cli._main_subjects_touching(_fetched(_repo(tmp_path)), "main", "epic/i/p", ["a.txt", "b.txt"])
    assert subjects == ["phase work (#42)"]


PHASE_OF = {"a1": "p1", "a2": "p1", "b1": "p2", "c1": "p3"}


def test_the_parent_phase_is_the_one_phase_whose_tasks_the_phase_needs():
    assert cli._sole_parent(PHASE_OF, {"c1": ["a1", "a2"]}, "p3") == "p1"


def test_a_phase_needing_no_other_phase_has_no_parent():
    assert cli._sole_parent(PHASE_OF, {"c1": [], "b1": ["a1"]}, "p3") is None


def test_a_phase_needing_two_phases_has_no_single_parent():
    assert cli._sole_parent(PHASE_OF, {"c1": ["a1", "b1"]}, "p3") is None


def test_a_need_inside_the_phase_is_not_a_parent():
    assert cli._sole_parent(PHASE_OF, {"a2": ["a1"]}, "p1") is None


def test_the_parent_phase_is_read_from_the_tickets_needs_even_when_the_needed_task_is_done(tmp_path):
    for phase, task, extra in (("p1", "t1", "state: done\n"), ("p2", "t2", "needs: [t1]\n"), ("p3", "t3", "")):
        (tmp_path / "i" / phase).mkdir(parents=True)
        (tmp_path / "i" / phase / f"{task}.md").write_text(f"---\nid: {task}\n{extra}---\nbody\n")
    assert [cli._parent_phase(tmp_path, "i", p) for p in ("p1", "p2", "p3")] == [None, "p1", None]


def test_a_branch_level_with_main_is_not_carried(tmp_path):
    assert _carried(_repo(tmp_path, own_commits=False), parent="q") is None


def test_a_phase_with_an_open_pr_of_its_own_is_not_carried(tmp_path):
    assert _carried(_repo(tmp_path), prs=[{"number": 9, "state": "open"}]) is None


def test_an_unreadable_pr_list_is_not_carried(tmp_path):
    assert _carried(_repo(tmp_path), prs="could not list pull requests for pr/i--p: HTTP 502") is None


def test_the_walk_records_the_carrying_pr_on_mark_done(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_execute_land_step", lambda _where, step, _forge: (seen.append(step), (True, "done"))[1])
    path = tmp_path / "runs" / "r" / "tasks" / "p" / "a.json"
    step = {"kind": "mark_done", "task": "a", "path": str(path)}
    rc, reached, pr = cli._land_walk(tmp_path, [step], [step], None, None, "task", False, None, [], pr="#42")
    assert (rc, reached, pr, seen[0]["pr"]) == (0, ["mark_done"], "#42", "#42")


def test_a_phase_land_main_carries_prints_landed_via_and_records_the_pr_on_mark_done(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "_phase_merged_pr", lambda *args: {"number": 42, "state": "merged", "carried": True})
    recorded = []
    facts = cli._mark_done_facts
    monkeypatch.setattr(cli, "_mark_done_facts", lambda step, pr, at: recorded.append(pr) or facts(step, pr, at))
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    lines = capsys.readouterr().out.splitlines()
    assert (rc, ran) == (0, ["clean_phase", "mark_done"])
    assert lines.count("landed via PR #42") == 1
    assert not any(line.startswith("merge: already merged") for line in lines)
    assert recorded == ["#42"]
