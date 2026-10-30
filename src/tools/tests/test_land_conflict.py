import subprocess

from agent_tools import cli, land


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_a_real_cherry_pick_conflict_yields_the_reason_the_task_is_marked_with(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "cli.py").write_text("a\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "task")
    (tmp_path / "cli.py").write_text("task\n")
    _git(tmp_path, "commit", "-qam", "task change")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "cli.py").write_text("main\n")
    _git(tmp_path, "commit", "-qam", "landed first")
    ok, detail = cli._cherry_pick_generated(tmp_path, sha, None, echo=lambda _: None)
    assert not ok and detail.startswith(f"cherry-pick of {sha[:8]} conflicts in: cli.py")
    assert land.land_conflict_reason("cherry_pick", detail, "main@abc12345") == "land conflict with main@abc12345 in cli.py"


def test_the_reason_is_none_for_a_stop_that_is_not_a_cherry_pick_conflict():
    assert land.land_conflict_reason("checks", "cherry-pick of abc conflicts in: a.py", "main@1") is None
    assert land.land_conflict_reason("cherry_pick", "expected exactly one commit ahead of main", "main@1") is None


def test_conflict_to_ready_rewrites_only_the_state_line_and_adds_the_reason():
    text = "---\nstate: approved\ntitle: T\n---\n\nstate: approved\n"
    assert land.conflict_to_ready(text, "land conflict with main@1 in a.py") == (
        "---\nstate: ready\nland_conflict: land conflict with main@1 in a.py\ntitle: T\n---\n\nstate: approved\n"
        "\nLand conflict with main@1 in a.py; rebuilding on current main.\n"
    )
    assert land.conflict_to_ready("---\nstate: ready\n---\n", "r") is None
