"""The epic driver publishes phase and draft branches to origin as it makes and advances them."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import harness.epic as epic
from tests.test_epic_driver import cart, drive, git, repo  # noqa: F401 -- cart and repo are fixtures

PHASE = "epic/demo-initiative/p1-foundations"
DRAFTS = [f"{PHASE}--t1-probe", f"{PHASE}--t2-bench"]


@pytest.fixture
def origin(repo, tmp_path) -> Path:  # noqa: F811
    """A bare origin that already holds main."""
    bare = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(bare), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    return bare


def _origin_heads(bare: Path) -> dict[str, str]:
    lines = git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads", cwd=bare).splitlines()
    return dict(line.split(" ") for line in lines)


def test_a_phase_that_opens_is_on_origin_before_any_task_merges(repo, cart, tmp_path, origin) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, assume="r")

    assert PHASE in _origin_heads(origin)
    assert "remote_push_failed" not in result["phases"][0]


def test_a_merged_task_advances_the_phase_head_on_origin(repo, cart, tmp_path, origin) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path)

    heads = _origin_heads(origin)
    assert heads[PHASE] == git("rev-parse", PHASE, cwd=repo)
    assert heads[PHASE] != git("rev-parse", "main", cwd=repo)
    assert "remote_push_failed" not in result["phases"][0]


def test_each_draft_branch_is_on_origin_with_the_local_commit(repo, cart, tmp_path, origin) -> None:  # noqa: F811
    drive(repo, cart, tmp_path)

    heads = _origin_heads(origin)
    for draft in DRAFTS:
        assert heads[draft] == git("rev-parse", draft, cwd=repo)


def test_a_branch_left_only_locally_by_an_earlier_run_is_published_on_reuse(repo, cart, tmp_path) -> None:  # noqa: F811
    drive(repo, cart, tmp_path, run_id="epic-1")
    bare = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(bare), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    assert PHASE not in _origin_heads(bare)

    drive(repo, cart, tmp_path, run_id="epic-2")

    assert _origin_heads(bare)[PHASE] == git("rev-parse", PHASE, cwd=repo)


def test_a_failed_push_is_a_warning_and_the_phase_keeps_running(repo, cart, tmp_path, origin, monkeypatch) -> None:  # noqa: F811
    fetch_then_vanish = epic.resolve_default_ref

    def resolve_then_remove_origin(*args, **kwargs):
        resolved = fetch_then_vanish(*args, **kwargs)
        shutil.rmtree(origin)  # the start-up fetch needs origin; every push after it does not get one
        return resolved

    monkeypatch.setattr(epic, "resolve_default_ref", resolve_then_remove_origin)

    result, _ = drive(repo, cart, tmp_path)

    p1 = result["phases"][0]
    assert p1["status"] == "complete"
    assert PHASE in p1["remote_push_failed"]
    assert result["totals"]["tasks_quarantined"] == 0
    assert git("rev-parse", "--verify", PHASE, cwd=repo)


def test_a_repo_with_no_origin_runs_unchanged(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path)

    assert [p["status"] for p in result["phases"]] == ["complete", "complete"]
    assert all("remote_push_failed" not in p for p in result["phases"])
