import subprocess
from pathlib import Path

import pytest

from agent_tools import cli


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, Path]:
    """A workspace cloned from a bare upstream, with one pushed commit holding work/seed.md."""
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "clone", "-q", str(bare), str(workspace)], check=True, capture_output=True)
    _identify(workspace)
    (workspace / "work").mkdir()
    (workspace / "work" / "seed.md").write_text("seed\n")
    (workspace / "runs").mkdir()
    _git(workspace, "add", "work")
    _git(workspace, "commit", "-q", "-m", "seed")
    _git(workspace, "push", "-q", "-u", "origin", "HEAD:main")
    return bare, workspace


def _stub_export(monkeypatch: pytest.MonkeyPatch, text: str | None) -> None:
    def export(runs_dir: Path, workspace: Path, prune_stale: bool = False) -> list[str]:
        if text is not None:
            (workspace / "work" / "ticket.md").write_text(text)
        return []

    monkeypatch.setattr(cli, "_export_board", export)


def test_a_committing_export_pushes_the_commit_to_the_upstream(clone, monkeypatch) -> None:
    bare, workspace = clone
    _stub_export(monkeypatch, "changed\n")
    assert cli._chair_export_hook(workspace / "runs", ("t1",)) == "export: committed 1 row(s)"
    assert _git(bare, "rev-parse", "main") == _git(workspace, "rev-parse", "HEAD")
    assert _git(bare, "log", "-1", "--format=%s", "main") == "chair: export 1 changed row(s)"


def test_a_rejected_push_is_a_note_and_does_not_raise(clone, monkeypatch, tmp_path) -> None:
    bare, workspace = clone
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    _identify(other)
    (other / "work" / "elsewhere.md").write_text("e\n")
    _git(other, "add", "work")
    _git(other, "commit", "-q", "-m", "elsewhere")
    _git(other, "push", "-q", "origin", "HEAD:main")
    advanced = _git(bare, "rev-parse", "main")
    _stub_export(monkeypatch, "changed\n")
    note = cli._chair_export_hook(workspace / "runs", ("t1",))
    assert note.startswith("export: committed 1 row(s); workspace push failed:")
    assert _git(bare, "rev-parse", "main") == advanced


def test_an_export_with_no_file_changes_does_not_push(clone, monkeypatch) -> None:
    bare, workspace = clone
    before = _git(bare, "rev-parse", "main")
    _stub_export(monkeypatch, None)
    assert cli._chair_export_hook(workspace / "runs", ("t1",)) == "export: no file changes"
    assert _git(bare, "rev-parse", "main") == before


def test_a_checkout_with_no_upstream_leaves_the_note_unchanged(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "solo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    _identify(workspace)
    (workspace / "work").mkdir()
    (workspace / "runs").mkdir()
    _stub_export(monkeypatch, "changed\n")
    assert cli._chair_export_hook(workspace / "runs", ("t1",)) == "export: committed 1 row(s)"


def test_a_live_run_sets_pull_workspace_and_a_dry_run_leaves_it_none(clone) -> None:
    _, workspace = clone
    args = (workspace / "runs", {}, "chair", 1, "h")
    live = cli._chair_run_deps(*args, False, print, workspace / "p.yaml", "files")
    dry = cli._chair_run_deps(*args, True, print, workspace / "p.yaml", "files")
    assert live.pull_workspace is not None
    assert live.pull_workspace() == ""
    assert dry.pull_workspace is None


def test_with_workspace_git_off_the_export_writes_the_files_and_commits_and_pushes_nothing(clone, monkeypatch) -> None:
    """The store is the shared copy; the tree is this machine's own, written so readers can still read it."""
    bare, workspace = clone
    head, upstream = _git(workspace, "rev-parse", "HEAD"), _git(bare, "rev-parse", "main")
    _stub_export(monkeypatch, "changed\n")
    note = cli._chair_export_hook(workspace / "runs", ("t1", "t2"), False)
    assert note == "export: wrote the files for 2 changed row(s); the workspace is not in git"
    assert (workspace / "work" / "ticket.md").read_text() == "changed\n"
    assert _git(workspace, "rev-parse", "HEAD") == head and _git(bare, "rev-parse", "main") == upstream
    assert _git(workspace, "diff", "--cached", "--name-only") == "", "nothing is staged either"


def test_with_workspace_git_off_a_workspace_that_is_no_git_checkout_exports_all_the_same(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "plain"
    (workspace / "work").mkdir(parents=True)
    (workspace / "runs").mkdir()
    monkeypatch.setattr(cli, "_export_board", lambda runs_dir, ws, prune_stale=False: ["empty-row"])
    assert cli._chair_export_hook(workspace / "runs", ("t1",), False) == (
        "export: wrote the files for 1 changed row(s); the workspace is not in git; skipped 1 contentless row(s): empty-row"
    )


@pytest.mark.parametrize(
    ("profile", "on"),
    [({}, True), ({"chair": {}}, True), ({"chair": {"workspace_git": True}}, True), ({"chair": {"workspace_git": False}}, False),
     ({"chair": "broken"}, True)],
)
def test_the_workspace_stays_in_git_unless_the_profile_says_false(profile, on) -> None:
    assert cli.workspace_git_on(profile) is on


def test_the_profile_parser_reads_chair_workspace_git_as_a_boolean() -> None:
    from agent_tools import route

    assert route.parse_profile("workspace_dir: /srv/ws\nchair:\n  workspace_git: false\n")["chair"]["workspace_git"] is False
    assert route.parse_profile("workspace_dir: /srv/ws\nchair:\n  workspace_git: yes\n")["chair"]["workspace_git"] is True
    with pytest.raises(route.ProfileError):
        route.parse_profile("workspace_dir: /srv/ws\nchair:\n  workspace_git: sometimes\n")


def test_route_export_is_a_command_and_writes_the_files_from_the_store(tmp_path, monkeypatch, capsys) -> None:
    import argparse

    group, rows = cli._table_entry("route")
    assert "export" in [row.name for row in rows]
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n")
    (tmp_path / "ws" / "runs").mkdir(parents=True)
    called: list = []
    monkeypatch.setattr(cli, "_export_board", lambda runs_dir, ws, prune_stale=False: called.append(ws) or [])
    assert cli._route_export(argparse.Namespace(profile=str(profile))) == 0
    assert called == [tmp_path / "ws"] and "matches the store" in capsys.readouterr().out


def test_a_detached_decompose_is_rerun_in_the_background_without_the_flag_and_with_its_run_id() -> None:
    """So a lane host's decompose writes its plan to the store as rows, as a local one does: no file is carried back."""
    argv = ["cox", "route", "launch", "decompose", "--local", "--detach", "--idea", "intake/x.md", "--initiative-id", "x",
            "--run-id", "x-1", "--label", "lane-a", "--no-claim"]
    assert cli.detached_rows_argv(argv, "x-1") == [
        "cox", "route", "launch", "decompose", "--local", "--idea", "intake/x.md", "--initiative-id", "x",
        "--run-id", "x-1", "--label", "lane-a", "--no-claim",
    ]
    bare = ["cox", "route", "launch", "decompose", "--detach", "--idea", "intake/x.md", "--initiative-id", "x"]
    assert cli.detached_rows_argv(bare, "x-7")[-2:] == ["--run-id", "x-7"]
    assert cli.detached_rows_argv(["cox", "route", "launch", "decompose", "--idea", "i.md"], "x-1") is None
    assert cli.detached_rows_argv(["pytest", "-q"], "x-1") is None, "called some other way: the caller launches the harness itself"
