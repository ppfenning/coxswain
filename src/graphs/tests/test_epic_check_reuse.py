"""The task gate and the style pass reuse a passing check on the same tree, and write every pass to the ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_epic_driver import cart, drive, git, initiative, repo  # noqa: F401 -- fixtures

import harness.epic as epic
from harness import check_reuse
from harness.checks import checks_evidence

LINT, TOOLS = "echo lint", "echo tools"
CHECKS = [{"name": "lint", "cmd": LINT}, {"name": "tools", "cmd": TOOLS}]


class Fake:
    """Stands in for `run_checks` under `check_reuse`: records each batch and fails the commands in `failing`."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.calls: list[list[str]] = []
        self.failing = failing

    def __call__(self, worktree, checks, **kw):
        self.calls.append([c["cmd"] for c in checks])
        return [
            {
                "name": c["name"],
                "cmd": c["cmd"],
                "passed": c["cmd"] not in self.failing,
                "outcome": "failed" if c["cmd"] in self.failing else "passed",
                "exit_code": 1 if c["cmd"] in self.failing else 0,
                "counts": {"passed": 3},
                "cpu_seconds": 1.5,
            }
            for c in checks
        ]


@pytest.fixture
def fake(monkeypatch) -> Fake:
    seen = Fake()
    monkeypatch.setattr(check_reuse, "run_checks", seen)
    monkeypatch.setattr(epic, "run_checks", seen)  # the gate hands its own runner to the reuse step
    return seen


def ledger_rows(common: Path) -> list[dict]:
    path = common / check_reuse.LEDGER_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def test_a_second_gate_run_on_an_unchanged_tree_reuses_and_says_so(repo, fake) -> None:  # noqa: F811
    first = epic._gate_checks(repo, CHECKS, env=None)
    second = epic._gate_checks(repo, CHECKS, env=None)
    assert fake.calls == [[LINT, TOOLS]]
    assert epic._reused_names(first) == []
    assert epic._reused_names(second) == ["lint", "tools"]
    assert [r["cpu_seconds"] for r in second] == [0.0, 0.0]
    assert all(row["output"].endswith("\nreused: same tree and command passed earlier") for row in checks_evidence(second))
    assert not any("reused" in row["output"] for row in checks_evidence(first))


def test_a_changed_tree_reruns_every_check(repo, fake) -> None:  # noqa: F811
    epic._gate_checks(repo, CHECKS, env=None)
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    again = epic._gate_checks(repo, CHECKS, env=None)
    assert fake.calls == [[LINT, TOOLS], [LINT, TOOLS]]
    assert epic._reused_names(again) == []


def test_a_failing_result_is_rerun_and_never_written(repo, fake) -> None:  # noqa: F811
    fake.failing = frozenset({LINT})
    epic._gate_checks(repo, CHECKS, env=None)
    again = epic._gate_checks(repo, CHECKS, env=None)
    assert fake.calls == [[LINT, TOOLS], [LINT]]
    assert epic._reused_names(again) == ["tools"]
    assert [r["passed"] for r in again] == [False, True]
    assert [row["cmd"] for row in ledger_rows(repo / ".git")] == [TOOLS]


def test_the_ledger_lands_in_the_common_directory_of_a_linked_worktree(repo, fake, tmp_path) -> None:  # noqa: F811
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", "--detach", str(linked), cwd=repo)
    epic._gate_checks(linked, CHECKS, env=None)
    assert [row["cmd"] for row in ledger_rows(repo / ".git")] == [LINT, TOOLS]
    assert not (linked / ".git").is_dir()
    assert epic._ledger_dir(linked) == (repo / ".git").resolve()
    epic._gate_checks(repo, CHECKS, env=None)
    assert fake.calls == [[LINT, TOOLS]]


def test_without_a_repository_the_checks_just_run(tmp_path, monkeypatch, fake) -> None:
    monkeypatch.setattr(epic, "_ledger_dir", lambda _worktree: None)
    monkeypatch.setattr(epic, "run_checks", fake)
    epic._gate_checks(tmp_path, CHECKS, env=None)
    epic._gate_checks(tmp_path, CHECKS, env=None)
    assert fake.calls == [[LINT, TOOLS], [LINT, TOOLS]]


def test_the_epic_gate_writes_its_pass_and_records_no_reuse_the_first_time(
    repo,  # noqa: F811
    cart,  # noqa: F811
    tmp_path,
    fake,
    monkeypatch,
) -> None:
    cart["landing_areas"]["checks"] = CHECKS
    build, seen = epic._build_task, {}

    def spy_build(ctx, **kw):
        seen["record"] = build(ctx, **kw)
        return seen["record"]

    monkeypatch.setattr(epic, "_build_task", spy_build)
    work = initiative(two_phases=False)
    work["items"] = work["items"][:1]
    drive(repo, cart, tmp_path, work=work)
    assert seen["record"]["check_scope"]["reused"] == []
    assert not any("reused" in r["output"] for r in seen["record"]["evidence"] if r["check"].startswith("checks:"))
    assert {row["cmd"] for row in ledger_rows(repo / ".git")} == {LINT, TOOLS}
