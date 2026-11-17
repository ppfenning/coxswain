"""Before a run builds, its initiative branch takes its parent forward, so no phase builds on an old base (H4.2).

Sixteen initiative branches were 18 to 51 commits behind `main`, six of them in conflict: the parent was resolved once,
when the branch was cut, and only the lander took it forward, at land time. Now each run merges the parent into the
initiative branch first. A reused phase branch then takes the initiative tip (`_refresh_level`), and a new one is cut
from it. When the merge conflicts, the run files a rectify task in the `integrate` phase, on a branch holding the
conflicted merge, and every other phase waits until it has merged.

Every run here is a real `run_epic` on a real repository and one throwaway sqlite store; only the model is scripted.
"""

from __future__ import annotations

from pathlib import Path

from harness import epic, landing_store, store_queue
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

INITIATIVE = "demo-initiative"
INIT = f"init/{INITIATIVE}"
P1 = f"epic/{INITIATIVE}/p1-foundations"
INTEGRATE = f"epic/{INITIATIVE}/integrate"
RECTIFY = "integrate-rectify"


def _store() -> Store:
    """One store across a test's runs, as a deployment has: the landing record outlives a run."""
    return Store(open_store("sqlite:///:memory:", "2026-11-17T00:00:00+00:00"))


def _commit_on_main(repo: Path, name: str, text: str) -> None:  # noqa: F811
    (repo / name).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", f"another initiative lands {name}", cwd=repo)


def _first_then(t2_state: str = "ready") -> tuple[dict, dict]:
    """The work of the first run (t1-probe alone) and of the next (t1-probe approved and merged, t2-bench `t2_state`)."""
    one = initiative(two_phases=False)
    t1, t2 = one["items"]
    return {**one, "items": [t1]}, {**one, "items": [{**t1, "state": "approved", "merged": True}, {**t2, "state": t2_state}]}


class Resolves(Runner):
    """Scripted as `Runner`, and the integrate task's builder resolves the conflict: it reads the conflicted file from
    the integrate branch, as a builder reads its tree, and replaces it, markers and all, with `ok`."""

    def __init__(self, repo: Path, patches: dict[str, str]) -> None:  # noqa: F811
        super().__init__(patches)
        self.repo = repo

    def run(self, *, role, tier=None, hints=None, schema, prompt, context=(), thread=None, budget_usd=None, task=None,
            wait_if_paused=None):
        if role == "build" and task == RECTIFY:
            with self.lock:
                self.calls.append({"role": role, "prompt": prompt, "task": task})
            lines = git("show", f"{INTEGRATE}:t1-probe.txt", cwd=self.repo).splitlines()
            patch = (
                "diff --git a/t1-probe.txt b/t1-probe.txt\n--- a/t1-probe.txt\n+++ b/t1-probe.txt\n"
                f"@@ -1,{len(lines)} +1 @@\n" + "".join(f"-{line}\n" for line in lines) + "+ok\n"
            )
            return {"patch": patch, "summary": "resolved", "files_touched": ["t1-probe.txt"], "commands_run": []}
        return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread,
                           budget_usd=budget_usd, task=task)


def _builds(runner: Runner) -> list[dict]:
    return [c for c in runner.calls if c["role"] == "build"]


def test_a_run_on_a_branch_behind_its_parent_starts_from_the_refreshed_branch(repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store()
    first, second = _first_then()
    drive(repo, cart, tmp_path, store=store, run_id=f"{INITIATIVE}-1", work=first)
    cut_at = git("rev-parse", INIT, cwd=repo)
    _commit_on_main(repo, "moved.md", "moved\n")
    assert not is_ancestor(repo, "main", INIT) and not is_ancestor(repo, "main", P1)

    # t2-bench's patch edits the file main gained since the cut: it applies only on the refreshed branch.
    edit = "diff --git a/moved.md b/moved.md\n--- a/moved.md\n+++ b/moved.md\n@@ -1 +1 @@\n-moved\n+built on it\n"
    result, runner = drive(repo, cart, tmp_path, store=store, run_id=f"{INITIATIVE}-2", work=second, patches={"t2-bench": edit})

    (phase,) = result["phases"]
    assert phase["status"] == "complete" and result["quarantined"] == []
    assert len(_builds(runner)) == 1
    assert phase["refreshed"].startswith(f"refreshed {P1} onto {INIT}: head "), "the reused phase took the new initiative tip"
    assert is_ancestor(repo, "main", INIT) and is_ancestor(repo, cut_at, INIT), "the parent was merged forward, not rebased"
    assert git("show", f"{INIT}:moved.md", cwd=repo) == "built on it"


def test_a_parent_that_conflicts_files_an_integrate_task_and_no_other_phase_builds_until_it_merges(repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store()
    first, second = _first_then()
    drive(repo, cart, tmp_path, store=store, run_id=f"{INITIATIVE}-1", work=first)
    before = git("rev-parse", INIT, cwd=repo)
    _commit_on_main(repo, "t1-probe.txt", "main wrote this too\n")

    runner = Resolves(repo, {"t2-bench": new_file_patch("t2-bench.txt")})
    result, _ = drive(repo, cart, tmp_path, store=store, run_id=f"{INITIATIVE}-2", work=second, runner=runner)

    by = {p["phase"]: p for p in result["phases"]}
    assert by["p1-foundations"] == {**by["p1-foundations"], "status": "blocked", "reason": (
        f"phase p1-foundations waits: {INIT} is behind main, and merging main into it conflicts in t1-probe.txt; "
        f"integrate task {RECTIFY} resolves it first, so no phase builds on the old base"
    )}
    assert [c["task"] for c in _builds(runner)] == [RECTIFY], "t2-bench did not build on the old base"
    row = next(r for r in store_queue.read(store.conn, INITIATIVE) if r["task_id"] == RECTIFY)
    assert row["phase"] == "integrate" and row["title"] == "Integrate with main: resolve the conflicts"
    assert "- `t1-probe.txt`" in row["body"]
    # The integrate task built on the conflicted merge, resolved it, and its phase merged into the initiative branch.
    assert by["integrate"]["status"] == "complete" and by["integrate"]["initiative_merge"]["status"] == "merged"
    assert is_ancestor(repo, before, INIT) and is_ancestor(repo, "main", INIT)
    assert git("show", f"{INIT}:t1-probe.txt", cwd=repo) == "ok"
    assert landing_store.get_record(store.conn, INITIATIVE)["phases"]["integrate"]["state"] == "merged"

    # The next run finds the branch holding its parent, and the waiting phase builds on it.
    result, runner = drive(repo, cart, tmp_path, store=store, run_id=f"{INITIATIVE}-3", work=second)
    (phase,) = result["phases"]
    assert phase["status"] == "complete" and [c["prompt"] for c in _builds(runner) if "t2-bench" in c["prompt"]]
    assert is_ancestor(repo, "main", P1)


def test_a_conflict_while_its_integrate_task_is_still_open_files_no_second_one(repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store()
    first, second = _first_then()
    drive(repo, cart, tmp_path, store=store, run_id=f"{INITIATIVE}-1", work=first)
    _commit_on_main(repo, "t1-probe.txt", "main wrote this too\n")
    # The integrate builder is not scripted, so the task stays open (quarantined) after this run.
    drive(repo, cart, tmp_path, store=store, run_id=f"{INITIATIVE}-2", work=second)
    filed = [r for r in store_queue.read(store.conn, INITIATIVE) if r["phase"] == "integrate"]
    assert [r["task_id"] for r in filed] == [RECTIFY]

    rows = {r["task_id"]: r for r in store_queue.read(store.conn, INITIATIVE)}
    again = {**second, "items": [*second["items"], {
        "id": RECTIFY, "phase": "integrate", "state": rows[RECTIFY]["state"], "needs": [], "surfaces": [],
        "title": rows[RECTIFY]["title"], "body": rows[RECTIFY]["body"],
    }]}
    result, _ = drive(repo, cart, tmp_path, store=store, run_id=f"{INITIATIVE}-3", work=again)
    assert [r["task_id"] for r in store_queue.read(store.conn, INITIATIVE) if r["phase"] == "integrate"] == [RECTIFY]
    assert f"integrate task {RECTIFY} resolves it first" in next(p for p in result["phases"] if p["phase"] == "p1-foundations")["reason"]


def test_behind_parent_names_the_conflict_and_the_task_that_resolves_it() -> None:
    assert epic.behind_parent("up_to_date", "", (), parent="main", branch="init/x", integrate=None) is None
    assert epic.behind_parent("merged", "", (), parent="main", branch="init/x", integrate=None) is None
    assert epic.behind_parent("conflict", "", ("a.py", "b.py"), parent="main", branch="init/x", integrate="integrate-rectify") == (
        "init/x is behind main, and merging main into it conflicts in a.py, b.py; integrate task integrate-rectify resolves "
        "it first, so no phase builds on the old base"
    )
    assert "no integrate task could be filed (cannot cut)" in epic.behind_parent(
        "conflict", "", ("a.py",), parent="main", branch="init/x", integrate=None, unfiled="cannot cut",
    )
    assert epic.behind_parent("error", "lease held by lander-1", (), parent="main", branch="init/x", integrate=None) == (
        "main was not merged into init/x: lease held by lander-1; no phase builds on the old base"
    )
