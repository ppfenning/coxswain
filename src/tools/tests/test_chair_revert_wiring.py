"""The revert pieces wired into the chair's real deps: slug resolution, the CI reader, quarantine and resolve."""

from __future__ import annotations

import json
from pathlib import Path

from agent_tools import cli, commands  # noqa: F401  (cli imports commands first; keep the import order the suite uses)

GREEN = json.dumps([{"databaseId": 7, "status": "completed", "conclusion": "success", "headSha": "abc"}])


def _checkout(tmp_path: Path, url: str = "git@github.com:o/r.git") -> str:
    (tmp_path / "co" / ".git").mkdir(parents=True)
    (tmp_path / "co" / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    return str(tmp_path / "co")


def test_gh_repo_resolves_a_checkout_and_passes_a_slug_through(tmp_path) -> None:
    assert cli._gh_repo(_checkout(tmp_path)) == "o/r"
    assert cli._gh_repo("o/r") == "o/r"


def test_main_ci_reader_reads_green() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, GREEN

    assert cli._main_ci_reader(runner)("o/r", "abc") == ("green", "")
    assert calls[0][calls[0].index("--repo") + 1] == "o/r"


def test_main_ci_reader_turns_a_gh_failure_into_pending() -> None:
    state, output = cli._main_ci_reader(lambda argv: (1, "boom"))("o/r", "abc")
    assert state == "pending"
    assert output.startswith("RuntimeError: ")


def test_main_ci_reader_resolves_a_checkout_path(tmp_path) -> None:
    calls: list[list[str]] = []
    cli._main_ci_reader(lambda argv: (calls.append(argv), (0, GREEN))[1])(_checkout(tmp_path), "abc")
    assert calls[0][calls[0].index("--repo") + 1] == "o/r"


def test_main_ci_reader_caches_per_repo_and_commit_until_cleared() -> None:
    calls: list[list[str]] = []
    read = cli._main_ci_reader(lambda argv: (calls.append(argv), (0, GREEN))[1])
    read("o/r", "abc")
    read("o/r", "abc")
    assert len(calls) == 1
    read("o/r", "def")
    assert len(calls) == 2
    read.cache.clear()
    read("o/r", "abc")
    assert len(calls) == 3


def test_green_since_reader_reads_a_descendant_green_run_as_true() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return (0, json.dumps([{"status": "completed", "conclusion": "success", "headSha": "later"}])) if argv[0] == "gh" else (0, "")

    assert cli._green_since_reader(runner)("o/r", "abc") is True
    assert calls[0][calls[0].index("--repo") + 1] == "o/r"
    assert calls[1:] == [["git", "-C", "o/r", "merge-base", "--is-ancestor", "abc", c] for c in ("abc", "later")]


def test_green_since_reader_reads_a_runner_exit_1_as_false() -> None:
    assert cli._green_since_reader(lambda argv: (1, "boom"))("o/r", "abc") is False


def test_green_since_reader_never_raises() -> None:
    def runner(argv: list[str]) -> tuple[int, str]:
        raise OSError("no gh")

    assert cli._green_since_reader(runner)("o/r", "abc") is False


def test_green_since_reader_caches_per_repo_and_commit_until_cleared() -> None:
    calls: list[list[str]] = []
    read = cli._green_since_reader(lambda argv: (calls.append(argv), (0, GREEN))[1])
    assert read("o/r", "abc") is True
    assert read("o/r", "abc") is True
    assert len(calls) == 1
    read("o/r", "def")
    assert len(calls) == 4  # gh, then git: is `def` there at all, then is it under the green run
    read.cache.clear()
    read("o/r", "abc")
    assert len(calls) == 5


def test_slug_forge_open_pr_hands_gh_the_slug(tmp_path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "https://github.com/o/r/pull/9\n"

    forge = cli._SlugForge(cli.chair_revert_ports.GhForgePort(runner))
    assert forge.open_pr(_checkout(tmp_path), "revert-x", "t", "b") == "https://github.com/o/r/pull/9"
    assert calls[0][calls[0].index("--repo") + 1] == "o/r"


def test_slug_forge_delegates_wait_checks_and_merge_unchanged() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "ci\tpass\t1s\thttp://x\n"

    forge = cli._SlugForge(cli.chair_revert_ports.GhForgePort(runner))
    assert forge.wait_checks("http://pr") == []
    forge.merge("http://pr")
    assert calls == [["gh", "pr", "checks", "http://pr", "--watch"], ["gh", "pr", "merge", "http://pr", "--squash"]]


def test_revert_ports_gives_a_git_port_and_a_slug_forge_for_any_action() -> None:
    git, forge = cli._revert_ports(lambda argv: (0, ""))({})
    assert isinstance(git, cli.chair_revert_ports.GitRevertPort)
    assert isinstance(forge, cli._SlugForge)


def _task(task_id: str, phase: str, state: str, initiative: str = "i1") -> dict:
    return {"kind": "task", "initiative": initiative, "task_id": task_id, "phase": phase, "state": state, "extra": {"k": "v"}}


def _quarantine(rows: list[dict], answers: dict | None = None, refused_marks: dict | None = None, attempts: dict | None = None):
    moved: list[tuple[str, str, str]] = []
    marked: list[tuple[str, str, dict]] = []

    def move(initiative: str, task: str, expected: str):
        moved.append((initiative, task, expected))
        return (answers or {}).get(task, cli.store_cli.StateSet({"state": "quarantined"}))

    def mark(initiative: str, task: str, extra: dict) -> str:
        marked.append((initiative, task, extra))
        return (refused_marks or {}).get(task, "")

    quarantine = cli._quarantine_phase_with(
        lambda i: [r for r in rows if r["initiative"] == i],
        lambda: ["i1-2", "i1-7", "i2-9"],
        lambda i, p, t: (attempts or {}).get(t, []),
        move,
        mark,
    )
    return quarantine("i1", "p1", "main_red", "why"), moved, marked


def _mark(run: str) -> dict:
    return {"k": "v", "quarantine_mark": {"cause": "main_red", "reason": "why", "run": run}}


def test_quarantine_phase_moves_only_the_tasks_its_land_carried_each_on_the_state_read() -> None:
    rows = [
        _task("a", "p1", "approved"), _task("b", "p1", "done"), _task("c", "p1", "dropped"), _task("d", "p1", "in_progress"),
        _task("e", "p1", "ready"), _task("f", "p2", "approved"), _task("g", "p1", "approved", initiative="i2"),
    ]
    done, moved, marked = _quarantine(rows, attempts={"b": ["i1-9"]})
    assert done == cli.chair_phase_quarantine.Quarantined(("a", "b"), ())
    assert moved == [("i1", "a", "approved"), ("i1", "b", "done")]
    # b's own attempt in i1-9 is newer than any recorded run, so its mark names that run and the attempt never outdates it.
    assert marked == [("i1", "a", _mark("i1-7")), ("i1", "b", _mark("i1-9"))]


def test_quarantine_phase_answers_one_line_per_write_the_store_refused_and_marks_no_row_it_did_not_move() -> None:
    rows = [_task("a", "p1", "approved"), _task("b", "p1", "approved"), _task("c", "p1", "done")]
    refused = {"a": cli.store_cli.StateRefused('{"actual": "in_progress", "expected": "approved"}', None)}
    done, _, marked = _quarantine(rows, answers=refused, refused_marks={"c": "exit 1"})
    assert done.moved == ("b", "c")
    assert done.failures == (
        'warning: store did not record i1/a as quarantined: refused: {"actual": "in_progress", "expected": "approved"}',
        "warning: store did not record the main_red cause on i1/c: exit 1",
    )
    assert [t for _, t, _ in marked] == ["b", "c"]


def _watch_row(initiative: str, phase: str, commit: str) -> dict:
    return {"kind": "revert_watch", "initiative": initiative, "phase": phase, "repo": "o/r", "pr": 1, "commit": commit}


def test_resolve_land_writes_one_row_per_pending_watch_of_that_phase() -> None:
    rows = [
        _watch_row("i1", "p1", "c1"),
        _watch_row("i1", "p1", "c2"),
        _watch_row("i1", "p1", "c3"),
        _watch_row("i1", "p2", "c4"),
        _watch_row("i2", "p1", "c5"),
        {"kind": "revert_watch_resolved", "commit": "c3", "outcome": "held"},
    ]
    written: list[dict] = []
    cli._resolve_land_with(lambda: rows, written.append)("i1", "p1", "reverted")
    assert [(w["kind"], w["commit"], w["outcome"]) for w in written] == [
        ("revert_watch_resolved", "c1", "reverted"),
        ("revert_watch_resolved", "c2", "reverted"),
    ]


def test_chair_run_deps_wires_the_revert_deps(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.exec_deps.revert_ports is not None
    assert deps.exec_deps.quarantine_phase is not None
    assert deps.exec_deps.resolve_land is not None
    assert callable(deps.facts_deps.read_main_ci)


def test_chair_run_deps_wires_green_since_and_rerun_main(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.green_since is not None
    assert deps.exec_deps.rerun_main is not None


def test_rerun_main_hands_rerun_infra_the_slug(tmp_path, monkeypatch) -> None:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(cli.chair_revert_ports, "rerun_infra", lambda runner, repo, commit: (seen.append((repo, commit)), ("pending", ""))[1])
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.exec_deps.rerun_main(_checkout(tmp_path), "abc") == ("pending", "")
    assert seen == [("o/r", "abc")]
