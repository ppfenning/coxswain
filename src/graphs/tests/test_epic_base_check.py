"""A phase base is checked for the merged work its tasks need before any build, rebuilt, or the phase stops."""

# ruff: noqa: F811  (cart and repo are imported fixtures that each test takes by name)

from __future__ import annotations

import subprocess

import pytest

from harness.store_migrate import open_store
from harness.store_write import Store
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    cart,
    drive,
    git,
    initiative,
    is_ancestor,
    new_file_patch,
    repo,
)

DEPENDENT = "t3-cutover"
NEED = "t1-probe"
PHASE_BRANCH = "epic/demo-initiative/p2-rollout"
MISSING = "0" * 40


class Watching(Runner):
    """Records the phase branch head at each build call, and the build calls themselves."""

    def __init__(self, repo, patches) -> None:
        super().__init__(patches)
        self.repo = repo
        self.heads: list[str] = []

    def run(self, *, role, **kw):
        if role == "build":
            self.heads.append(git("rev-parse", PHASE_BRANCH, cwd=self.repo))
        return super().run(role=role, **kw)

    def builds(self) -> list[dict]:
        return [c for c in self.calls if c["role"] == "build"]


def _work(commit: str, need_state: str = "approved") -> dict:
    """The need is merged under `commit`; the second task of its phase is approved with no commit of its own."""
    work = initiative()
    for item in work["items"]:
        if item["id"] == NEED:
            item.update({"state": need_state, "merged": True, "merged_commit": commit})
        elif item["phase"] == "p1-foundations":
            item.update({"state": "approved", "merged": True})
    return work


def _side_commit(repo, path: str, text: str) -> str:
    """A commit on a side branch off main, which no phase branch holds. The checkout returns to main on any exit."""
    git("checkout", "-q", "-b", f"side-{repo.parent.name}-{path}", cwd=repo)
    try:
        (repo / path).write_text(text, encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-qm", "side work", cwd=repo)
        return git("rev-parse", "HEAD", cwd=repo)
    finally:
        git("checkout", "-q", "main", cwd=repo)


def _attempts(store: Store) -> int:
    return store.conn.query_one(f"SELECT count(*) FROM attempts WHERE task_id = '{DEPENDENT}'")[0]


@pytest.fixture
def memory_store():
    """One in-memory store per test, closed when the test ends."""
    store = Store(open_store("sqlite:///:memory:", "2026-11-06T00:00:00+00:00"))
    try:
        yield store
    finally:
        store.conn.close()


def _drive(repo, cart, tmp_path, work, runner, store):
    # p1 has nothing to build, so the harness cuts its branch on a thread that p2 can outrun; cut it here instead.
    git("branch", "epic/demo-initiative/p1-foundations", "main", cwd=repo)
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, store=store, run_id=f"epic-{tmp_path.name}")
    return result, {p["phase"]: p for p in result["phases"]}["p2-rollout"]


def test_missing_is_not_a_commit_in_the_tests_repo(repo) -> None:
    probe = subprocess.run(["git", "cat-file", "-e", f"{MISSING}^{{commit}}"], cwd=repo, capture_output=True)
    assert probe.returncode != 0


def test_a_failed_side_commit_leaves_the_checkout_on_main(repo) -> None:
    with pytest.raises(FileNotFoundError):
        _side_commit(repo, "no-such-dir/needed.txt", "ok\n")
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"


@pytest.mark.usefixtures("at_task")
def test_a_base_missing_a_needed_commit_is_rebuilt_and_the_build_runs(repo, cart, tmp_path, memory_store) -> None:
    commit = _side_commit(repo, "needed.txt", "ok\n")
    runner = Watching(repo, {DEPENDENT: new_file_patch(f"{DEPENDENT}.txt")})

    result, phase = _drive(repo, cart, tmp_path, _work(commit), runner, memory_store)

    assert phase["base_rebuilt"] == [NEED]
    assert len(runner.builds()) == 1
    assert is_ancestor(repo, commit, runner.heads[0])
    assert phase["status"] == "complete"
    assert {t["id"]: t["state"] for t in result["tasks"]}[DEPENDENT] == "approved"
    assert result["quarantined"] == []


@pytest.mark.usefixtures("at_task")
@pytest.mark.parametrize("conflicting", [True, False], ids=["conflict", "unknown-commit"])
def test_a_base_that_cannot_be_assembled_stops_the_phase_before_any_build(
    repo, cart, tmp_path, memory_store, conflicting
) -> None:
    store = memory_store
    commit = MISSING
    if conflicting:
        commit = _side_commit(repo, "README.md", "# side\n")
        (repo / "README.md").write_text("# main\n", encoding="utf-8")
        git("commit", "-qam", "main moves", cwd=repo)
    runner = Watching(repo, {DEPENDENT: new_file_patch(f"{DEPENDENT}.txt")})

    result, phase = _drive(repo, cart, tmp_path, _work(commit), runner, store)

    assert phase["status"] == "blocked"
    assert phase["reason"].startswith(f"{NEED}: the build base lacks the merged work of {NEED}")
    assert result["quarantined"] == [
        {"id": NEED, "phase": "p2-rollout", "grain": "phase", "kind": "stale_base", "reason": phase["reason"]}
    ]
    assert "base_rebuilt" not in phase
    assert runner.builds() == []
    assert _attempts(store) == 0
    assert DEPENDENT not in {t["id"] for t in result["tasks"]}
    state = store.conn.query_one(f"SELECT state FROM work_items WHERE task_id = '{DEPENDENT}'")
    assert state is None or state[0] == "ready"


def test_a_base_that_holds_every_needed_commit_builds_unchanged(repo, cart, tmp_path, memory_store) -> None:
    head = git("rev-parse", "main", cwd=repo)
    runner = Watching(repo, {DEPENDENT: new_file_patch(f"{DEPENDENT}.txt")})

    _, phase = _drive(repo, cart, tmp_path, _work(head), runner, memory_store)

    assert runner.heads == [head]
    assert "base_rebuilt" not in phase
    assert phase["status"] == "complete"


@pytest.mark.usefixtures("at_task")
@pytest.mark.parametrize(("need_state", "blocked"), [("done", False), ("approved", True)])
def test_only_an_unlanded_need_is_checked_against_the_base(
    repo, cart, tmp_path, memory_store, need_state, blocked
) -> None:
    """A need done in the store landed, perhaps on another machine; its commit is not expected on the phase base."""
    runner = Watching(repo, {DEPENDENT: new_file_patch(f"{DEPENDENT}.txt")})

    result, phase = _drive(repo, cart, tmp_path, _work(MISSING, need_state), runner, memory_store)

    assert (phase["status"] == "blocked") is blocked
    assert (len(runner.builds()) == 0) is blocked
    assert any(q.get("kind") == "stale_base" for q in result["quarantined"]) is blocked
