"""The ticket land executes pr_branch_cut, merge_phase and the pr verbs: a two-phase plan over a temporary git repo and a fake forge."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_tools import cli, forge_local, land, run_store, store_cli
from core import workstore

REAL_MARK_MERGED, REAL_MARK_PHASE_DONE = store_cli.mark_merged, store_cli.mark_phase_done
PHASES = ["seams", "wiring"]
TASKS = ("a", "b")
RUN = {"run": "epic-x-5", "initiative": "x"}


class FakeForge:
    """Holds the PRs it opened by head; `behind` makes the first merge-state read BEHIND."""

    def __init__(self, behind=False, fail=()):
        self.prs, self.calls, self.behind, self.fail = {}, [], behind, set(fail)

    def _do(self, verb):
        self.calls.append(verb)
        return verb not in self.fail

    def find_open_prs(self, repo, branch):
        return [p["number"] for h, p in self.prs.items() if h == branch]

    def push(self, repo, branch):
        subprocess.run(["git", "-C", str(repo), "push", "-q", "-f", "origin", branch], check=True, capture_output=True)
        return True, f"pushed {branch}"

    def open_pr(self, repo, title, body, *, head=None, base=None, draft=False):
        if not self._do("open_pr"):
            return False, "open refused"
        self.prs[head] = {"number": len(self.prs) + 1, "draft": draft, "title": title}
        return True, f"https://forge.test/pull/{self.prs[head]['number']}"

    def update_pr(self, repo, title, body, *, head):
        if not self._do("update_pr"):
            return False, "update refused"
        self.prs[head]["title"] = title
        return True, f"https://forge.test/pull/{self.prs[head]['number']}"

    def mark_ready(self, repo, *, head):
        if not self._do("mark_ready"):
            return False, "ready refused"
        self.prs[head]["draft"] = False
        return True, f"https://forge.test/pull/{self.prs[head]['number']}"

    def wait_checks(self, repo, timeout_s, *, ref="HEAD"):
        return True, "checks green"

    def merge_state(self, pr, *, repo=None):
        self.calls.append("merge_state")
        return "BEHIND" if self.behind and "update_branch" not in self.calls else "CLEAN"

    def update_branch(self, pr, *, repo=None):
        self.calls.append("update_branch")

    def merge(self, repo, step):
        self.calls.append("merge")
        return True, "merged pr/x"


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.test")
    git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("base\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    for phase in PHASES:
        git(path, "checkout", "-q", "-b", f"epic/x/{phase}", "main")
        (path / f"{phase}.txt").write_text(phase + "\n")
        git(path, "add", ".")
        git(path, "commit", "-qm", phase)
    git(path, "checkout", "-q", "main")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git(path, "remote", "add", "origin", str(origin))
    return path


@pytest.fixture(autouse=True)
def store(monkeypatch):
    """Store plumbing is recorded, never run: the scripts it shells out to are exercised against a real sqlite store below."""
    calls = []
    monkeypatch.setattr(store_cli, "mark_merged", lambda *a: calls.append(("merged", a[1], a[2], a[3])) or store_cli.MergeRecorded(1))
    monkeypatch.setattr(store_cli, "mark_phase_done", lambda *a: store_cli.NotAvailable())
    monkeypatch.setattr(store_cli, "mark_landed", lambda runs_dir, run, phase, task, pr, at: calls.append(("landed", task, pr)) or store_cli.NotInStore())
    monkeypatch.setattr(store_cli, "mirror_state", lambda *a, **k: None)
    return calls


def plan(phase, repo, **kw):
    items = [{"id": t, "status": "approved"} for t in TASKS]
    records = [{"task": t, "title": t, "repo": "wire", "status": "approved", "review": {"verdict": "approve"}} for t in TASKS]
    steps = land.land_plan({**RUN, "phase": phase}, kw.pop("branches", {f"epic/x/{phase}": ["c"]}), "main", items=items,
                           task_records=records, repo="wire", ticket_tasks=TASKS, ticket_present=(), phases=PHASES, **kw)
    return [s for s in steps if s["kind"] not in ("checks", "route_sync", "fetch")]


def world(root, steps):
    """Work item files, task records and the phase manifest of the phase `steps` land, and `steps` enriched against them."""
    phase = next((x["phase"] for x in steps if x["kind"] == "phase_done"), None)
    kept = [x for x in steps if x["kind"] != "clean_phase"]
    if phase is None:
        return kept
    runs, paths, items = root / "runs", {}, {}
    runs.mkdir(exist_ok=True)
    (runs / f"epic-x-5:{phase}.json").write_text(json.dumps({"run_id": f"epic-x-5:{phase}", "landed": True}))
    for t in TASKS:
        items[t] = str(root / "work" / "x" / phase / f"{t}.md")
        workstore.write_item({"id": t, "phase": phase, "state": "approved", "title": t, "body": "b\n"}, items[t])
        paths[t] = str(runs / "epic-x-5" / "tasks" / phase / f"{t}.json")
        Path(paths[t]).parent.mkdir(parents=True, exist_ok=True)
        Path(paths[t]).write_text(json.dumps({"task": t, "run": "epic-x-5"}))
    return cli._land_enrich(kept, path="", worktree_root=str(root / "wt"), task_paths=paths, task_items=items, store_runs_dir=str(runs))


def item(repo, phase, task):
    return workstore.read_item(repo.parent / "work" / "x" / phase / f"{task}.md")


def record(repo, phase, task):
    return json.loads((repo.parent / "runs" / "epic-x-5" / "tasks" / phase / f"{task}.json").read_text())


def walk(repo, steps, forge, planned=None, pr="", level="full"):
    steps = land.gate_steps(world(repo.parent, steps), level)
    phase = next((s["phase"] for s in steps if s["kind"] == "phase_done"), "x")
    planned = [x for x in planned if x["kind"] != "clean_phase"] if planned else steps
    return cli._land_walk(repo, steps, planned, {**RUN, "run_id": f"epic-x-5:{phase}", "phase": phase}, None, level, False, forge, [], pr=pr)


def test_phase_one_cuts_merges_and_opens_one_draft_pr(repo, capsys, store):
    forge = FakeForge()
    code, reached, pr = walk(repo, plan("seams", repo), forge)
    assert (code, pr) == (0, "https://forge.test/pull/1")
    assert "mark_merged" in reached and "mark_done" not in reached
    for task in TASKS:
        assert (item(repo, "seams", task)["state"], item(repo, "seams", task)["merged"]) == ("approved", True)
        assert record(repo, "seams", task)["merged"] is True and "landed" not in record(repo, "seams", task)
    assert [c[1:3] for c in store if c[0] == "merged"] == [("x", "a"), ("x", "b")]
    manifest = json.loads((repo.parent / "runs" / "epic-x-5:seams.json").read_text())
    assert (manifest["status"], manifest["merged_into"], "landed" in manifest) == ("done", "pr/x", False)
    assert reached[:2] == ["pr_branch_cut", "merge_phase"]
    assert forge.prs["pr/x"]["draft"] is True and len(forge.prs) == 1
    assert git(repo, "show", "--format=%p", "-s", "pr/x").count(" ") == 1
    assert git(repo, "ls-tree", "--name-only", "pr/x").split() == ["f.txt", "seams.txt"]


def test_phase_two_updates_the_same_pr_and_carries_it_to_the_tasks(repo, capsys, store):
    forge = FakeForge(behind=True)
    walk(repo, plan("seams", repo), forge, level="phase")
    assert git(repo, "rev-parse", "pr/x") != git(repo, "rev-parse", "main")
    steps = plan("wiring", repo, phase_pr={"local_tip": "t", "remote_tip": "t", "pr": 1, "pr_head": "t"})
    code, reached, pr = walk(repo, steps, forge, planned=steps)
    assert pr == "https://forge.test/pull/1" and len(forge.prs) == 1 and forge.prs["pr/x"]["draft"] is False
    assert "pr_create" not in reached and "pr_branch_cut" not in reached
    assert forge.calls.index("update_branch") < forge.calls.index("merge") and forge.calls[-1] == "merge"
    assert {c for c in store if c[0] == "landed"} == {("landed", "a", pr), ("landed", "b", pr)}
    for task in TASKS:
        assert item(repo, "wiring", task)["state"] == "done" and record(repo, "wiring", task)["landed"] is True


def test_phase_two_with_no_pr_in_the_plan_finds_the_open_pr_by_head(repo, capsys):
    forge = FakeForge()
    forge.prs["pr/x"] = {"number": 4, "draft": True, "title": "t"}
    git(repo, "branch", "pr/x", "main")
    steps = plan("wiring", repo)
    steps = [s for s in steps if s["kind"] != "pr_update" and s["kind"] != "pr_ready"]
    code, _, pr = walk(repo, steps, forge, level="phase")
    assert pr == "#4"


def test_a_gated_phase_two_names_the_pr_in_the_gate_line(repo, capsys):
    forge = FakeForge()
    walk(repo, plan("seams", repo), forge, level="phase")
    steps = plan("wiring", repo, phase_pr={"local_tip": "t", "remote_tip": "t", "pr": 1, "pr_head": "t"})
    code, _, pr = walk(repo, steps, forge, planned=steps, level="ticket")
    out = capsys.readouterr().out
    assert code == 3 and "pull request https://forge.test/pull/1 left open" in out and "(none opened)" not in out
    assert "merge" not in forge.calls


def test_a_failed_pr_update_is_reported_and_tasks_are_still_marked_merged(repo, capsys, store):
    forge = FakeForge(fail=("update_pr",))
    walk(repo, plan("seams", repo), forge, level="phase")
    store.clear()
    steps = plan("wiring", repo, phase_pr={"local_tip": "t", "remote_tip": "t", "pr": 1, "pr_head": "t"})
    code, reached, _ = walk(repo, steps, forge)
    out = capsys.readouterr().out
    assert code == 1 and "pr_update failed after the push" in out
    assert "mark_merged" in reached and "phase_done" in reached
    assert [item(repo, "wiring", t)["merged"] for t in TASKS] == [True, True]
    assert "merge" not in forge.calls and "mark_done" not in reached


def test_a_conflicting_phase_merges_nothing_and_leaves_no_half_merge(repo):
    git(repo, "checkout", "-q", "-b", "pr/x", "main")
    (repo / "seams.txt").write_text("other\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "clash")
    git(repo, "checkout", "-q", "main")
    tip = git(repo, "rev-parse", "pr/x")
    ok, detail = cli._merge_phase(repo, "epic/x/seams", "pr/x", "epic x: merge phase seams")
    assert not ok and detail
    assert git(repo, "rev-parse", "pr/x") == tip
    assert not cli._land_worktree(repo, "pr/x").exists()


def test_pr_branch_cut_never_moves_an_existing_branch(repo):
    assert cli._cut_pr_branch(repo, "pr/x", "main")[0]
    git(repo, "worktree", "remove", "--force", str(cli._land_worktree(repo, "pr/x")))
    ok, _ = cli._merge_phase(repo, "epic/x/seams", "pr/x", "m")
    tip = git(repo, "rev-parse", "pr/x")
    assert ok and cli._cut_pr_branch(repo, "pr/x", "main") == (True, "reusing pr/x")
    assert git(repo, "rev-parse", "pr/x") == tip


def test_a_phase_whose_plan_refused_merges_nothing(repo):
    steps = [{"kind": "refuse", "reason": "no branch is ahead of main"}]
    code, reached, _ = walk(repo, steps, FakeForge())
    assert code == 2 and reached == []
    assert git(repo, "branch", "--list", "pr/x") == ""


def test_mark_merged_sets_the_flag_on_the_item_and_record_and_keeps_approved(repo, tmp_path, store):
    from core import workstore
    item = tmp_path / "work" / "x" / "seams" / "a.md"
    workstore.write_item({"id": "a", "phase": "seams", "state": "approved", "title": "a", "body": "b\n"}, item)
    record = tmp_path / "runs" / "epic-x-5" / "tasks" / "seams" / "a.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"task": "a"}))
    git(repo, "branch", "pr/x", "main")
    step = {"kind": "mark_merged", "task": "a", "merged": True, "pr_branch": "pr/x", "path": str(record), "item": str(item),
            "runs_dir": str(tmp_path / "runs")}
    assert cli._mark_merged(repo, step)[0]
    found = workstore.read_item(item)
    assert (found["state"], found["merged"]) == ("approved", True)
    assert json.loads(record.read_text())["merged"] is True and "landed" not in json.loads(record.read_text())
    assert store[0][1:3] == ("x", "a")


def test_phase_done_writes_done_and_merged_into_the_pr_branch_not_landed(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "epic-x-5:seams.json").write_text(json.dumps({"run_id": "epic-x-5:seams", "landed": True}))
    step = {"kind": "phase_done", "initiative": "x", "phase": "seams", "pr_branch": "pr/x", "runs_dir": str(runs), "phase_record_id": "epic-x-5:seams"}
    assert cli._phase_done_record(step)[0]
    written = json.loads((runs / "epic-x-5:seams.json").read_text())
    assert (written["status"], written["merged_into"]) == ("done", "pr/x") and "landed" not in written
    assert run_store.phase_manifests(runs, "epic-x-5")[0]["merged_into"] == "pr/x"


def test_a_two_phase_land_on_the_local_forge_merges_into_main_and_marks_every_task_done(repo, capsys, store):
    code, _, pr = walk(repo, plan("seams", repo), forge_local)
    assert (code, pr) == (0, "") and git(repo, "branch", "--list", "pr/x") != ""
    assert git(repo, "ls-tree", "--name-only", "main").split() == ["f.txt"]
    steps = plan("wiring", repo, phase_pr={"local_tip": "t", "remote_tip": "t", "pr": None, "pr_head": "t"})
    code, reached, pr = walk(repo, steps, forge_local, planned=steps)
    assert code == 0 and "merge" in reached and pr == ""
    assert git(repo, "ls-tree", "--name-only", "main").split() == ["f.txt", "seams.txt", "wiring.txt"]
    assert [item(repo, "wiring", t)["state"] for t in TASKS] == ["done", "done"]


def test_a_failed_pr_create_in_an_ordinary_land_stops_at_once_without_the_pr_branch_warning(repo, capsys):
    steps = [{"kind": "pr_create", "title": "t", "body": "b", "head": "h", "base": "main"}, {"kind": "wait_checks"}]
    code, reached, _ = cli._land_walk(repo, steps, steps, None, None, "full", False, FakeForge(fail=("open_pr",)), [])
    out = capsys.readouterr().out
    assert code == 1 and reached == ["pr_create"] and "warning" not in out and out.count("open refused") == 2


def test_the_store_writes_run_for_real_and_a_store_only_phase_is_recorded(tmp_path, monkeypatch):
    from harness.store_migrate import open_store
    from harness.store_write import Store, upsert_work_item
    url = f"sqlite:///{tmp_path}/cox.db"
    now = "2026-11-06T00:00:00+00:00"
    conn = open_store(url, now)
    upsert_work_item(conn, "x", "a", "seams", "approved", [], now, "t")
    Store(conn).record_phase({"run_id": "epic-x-5:seams", "ts": now, "manifest_record": {"run_id": "epic-x-5:seams", "landed": True}})
    conn.close()
    monkeypatch.setattr(store_cli, "_harness_python", lambda: Path(sys.executable))
    monkeypatch.setattr(store_cli, "_store_url", lambda runs_dir: url)
    monkeypatch.setattr(store_cli, "mark_merged", REAL_MARK_MERGED)
    monkeypatch.setattr(store_cli, "mark_phase_done", REAL_MARK_PHASE_DONE)
    assert store_cli.mark_merged(tmp_path, "x", "a", "abc123", "t") == store_cli.MergeRecorded(1)
    assert isinstance(store_cli.mark_phase_done(tmp_path, "epic-x-5", "nope", "pr/x"), store_cli.StateRefused)
    step = {"kind": "phase_done", "phase": "seams", "pr_branch": "pr/x", "runs_dir": str(tmp_path), "phase_record_id": "epic-x-5:seams"}
    assert cli._phase_done_record(step) == (True, "phase seams done, merged into pr/x (store)")
    assert cli._phase_done_record({**step, "phase_record_id": "epic-x-5:nope"})[0] is False
    db = sqlite3.connect(tmp_path / "cox.db")
    assert db.execute("SELECT state, merged_commit FROM work_items").fetchone() == ("approved", "abc123")
    manifest = json.loads(db.execute("SELECT record_json FROM phases").fetchone()[0])["manifest_record"]
    assert (manifest["status"], manifest["merged_into"], "landed" in manifest) == ("done", "pr/x", False)
