"""The epic harness cuts phase branches from `origin/<base>` after a fetch, never from a local branch name."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import epic as epic_module
from harness.epic import origin_ref, resolve_default_ref
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

PHASE = "epic/demo-initiative/p1-foundations"


@pytest.mark.parametrize(
    ("base", "origin_head", "known", "expected"),
    [
        pytest.param("develop", "origin/main", {"main"}, "origin/develop", id="an explicit base wins"),
        pytest.param("origin/develop", None, set(), "origin/develop", id="an origin/ prefix is dropped"),
        pytest.param(None, "origin/trunk", {"trunk", "main"}, "origin/trunk", id="origin's default"),
        pytest.param(None, None, {"main", "master"}, "origin/main", id="main"),
        pytest.param(None, None, {"master"}, "origin/master", id="master"),
        pytest.param(None, None, set(), "origin/topic", id="the checkout"),
    ],
)
def test_origin_ref_keeps_the_fallback_order_and_always_names_origin(base, origin_head, known, expected) -> None:
    assert origin_ref(base, origin_head, known, "topic") == expected


def with_identity(path: Path) -> Path:
    git("config", "user.name", "fixture", cwd=path)
    git("config", "user.email", "fixture@invalid", cwd=path)
    return path


def publish(repo: Path, tmp_path: Path) -> Path:  # noqa: F811
    """Give `repo` a bare origin carrying its `main`, and return the origin."""
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    with_identity(repo)
    git("remote", "add", "origin", str(origin), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    git("remote", "set-head", "origin", "main", cwd=repo)
    return origin


def advance_origin_develop(origin: Path, tmp_path: Path) -> str:
    """Create or move `develop` on origin from a second clone; its new tip."""
    other = tmp_path / "other"
    if not other.exists():
        git("clone", "-q", str(origin), str(other), cwd=tmp_path)
        with_identity(other)
    git("checkout", "-q", "-B", "develop", "origin/main", cwd=other)
    (other / "develop-only.txt").write_text("ok\n", encoding="utf-8")  # the fixture's check.py demands "ok" in every *.txt
    git("add", "-A", cwd=other)
    git("commit", "-qm", "develop ahead", cwd=other)
    git("push", "-q", "-f", "origin", "develop", cwd=other)
    return git("rev-parse", "HEAD", cwd=other)


def test_the_phase_branch_is_cut_from_origin_develop_not_a_stale_local_develop(repo, cart, tmp_path) -> None:  # noqa: F811
    origin = publish(repo, tmp_path)
    git("branch", "develop", "main", cwd=repo)
    git("push", "-q", "origin", "develop", cwd=repo)
    stale = git("rev-parse", "develop", cwd=repo)
    tip = advance_origin_develop(origin, tmp_path)
    assert tip != stale

    drive(repo, cart, tmp_path, base="origin/develop")

    assert git("rev-parse", "develop", cwd=repo) == stale
    assert is_ancestor(repo, tip, PHASE)


def test_a_repo_with_no_local_develop_cuts_from_origin_develop(repo, cart, tmp_path) -> None:  # noqa: F811
    origin = publish(repo, tmp_path)
    tip = advance_origin_develop(origin, tmp_path)
    assert "develop" not in git("branch", "--format=%(refname:short)", cwd=repo).split()

    drive(repo, cart, tmp_path, base="origin/develop")

    assert is_ancestor(repo, tip, PHASE)
    assert "develop" not in git("branch", "--format=%(refname:short)", cwd=repo).split()


def test_without_a_base_the_fallback_is_origin_main_after_a_fetch(repo, tmp_path) -> None:  # noqa: F811
    origin = publish(repo, tmp_path)
    advance_origin_develop(origin, tmp_path)

    assert resolve_default_ref(repo, None) == "origin/main"


def test_a_repo_with_no_origin_keeps_its_bare_local_name(repo) -> None:  # noqa: F811
    assert resolve_default_ref(repo, None) == "main"
    assert resolve_default_ref(repo, "origin/main") == "main"


def test_a_branch_origin_lacks_stays_an_origin_ref_even_when_a_local_one_exists(repo, tmp_path) -> None:  # noqa: F811
    publish(repo, tmp_path)
    git("branch", "develop", "main", cwd=repo)

    assert resolve_default_ref(repo, "develop") == "origin/develop"


def test_a_failed_fetch_raises_instead_of_falling_back_to_a_local_branch(repo, tmp_path) -> None:  # noqa: F811
    publish(repo, tmp_path)
    git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=repo)

    with pytest.raises(RuntimeError, match="git fetch origin failed"):
        resolve_default_ref(repo, "main")


def test_the_review_pr_is_opened_against_the_branch_not_the_origin_ref(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    origin = publish(repo, tmp_path)
    advance_origin_develop(origin, tmp_path)
    prs: list[str] = []

    def fake_pr(ctx, *, title, body, head, base):
        prs.append(base)
        return True, "https://forge.example/pr/1"

    monkeypatch.setattr(epic_module, "_push_review_branch", lambda ctx, task: (True, "pushed"))
    monkeypatch.setattr(epic_module, "_open_review_pr", fake_pr)
    runner = Runner({"t1-probe": new_file_patch("harness/x.py"), "t2-bench": new_file_patch("t2-bench.txt")})

    drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False), base="origin/develop")

    assert prs == ["develop"]
