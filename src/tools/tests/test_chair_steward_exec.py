import subprocess
from pathlib import Path

import pytest

from agent_tools.chair_steward_exec import StewardPorts, chair_steward_exec, outcome

EPIC = "e1"
TICKET = "ticket/e1"
LEVEL = "epic/e1/phase-a"
FIELDS = {"epic": EPIC, "level": "0"}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout


def run_git(args: list[str]) -> tuple[int, str]:
    done = subprocess.run(["git", *args], capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def commit_file(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"{name}: {text.strip()}")


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.invalid")
    commit_file(repo, "base.txt", "base\n")
    git(repo, "branch", LEVEL)
    git(repo, "checkout", "-q", "-b", TICKET)
    commit_file(repo, "ticket.txt", "ticket\n")
    return repo


def ports(repo: Path, gate=lambda repo, ticket, checks: (True, "ok")) -> StewardPorts:
    return StewardPorts(
        git=run_git,
        repo_of=lambda epic: str(repo),
        ticket_branch_of=lambda epic: TICKET,
        phase_of=lambda epic, level: ["phase-a"][level],
        run_gate_checks=gate,
    )


def merge_action() -> dict:
    return {"kind": "merge_level", "epic": EPIC, "level": 0, "fields": dict(FIELDS)}


def gate_action() -> dict:
    return {"kind": "run_gate", "epic": EPIC, "level": 0, "fields": dict(FIELDS)}


def refs(repo: Path) -> str:
    return git(repo, "for-each-ref")


def test_merge_moves_ticket_branch_to_a_two_parent_commit(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", LEVEL)
    commit_file(repo, "level.txt", "level\n")
    git(repo, "checkout", "-q", TICKET)
    old, tip = git(repo, "rev-parse", TICKET).strip(), git(repo, "rev-parse", LEVEL).strip()
    result = chair_steward_exec(merge_action(), [], ports(repo))
    assert result["event"] == "level_merged"
    assert result["fields"] == FIELDS
    new = git(repo, "rev-parse", TICKET).strip()
    assert git(repo, "rev-list", "--parents", "-n1", TICKET).split() == [new, old, tip]
    assert git(repo, "rev-parse", LEVEL).strip() == tip
    assert (git(repo, "show", f"{TICKET}:level.txt"), git(repo, "show", f"{TICKET}:ticket.txt")) == (
        "level\n",
        "ticket\n",
    )


def test_already_merged_level_moves_no_ref(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", LEVEL)
    commit_file(repo, "level.txt", "level\n")
    git(repo, "checkout", "-q", TICKET)
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", LEVEL)
    before = refs(repo)
    result = chair_steward_exec(merge_action(), [], ports(repo))
    assert (result["event"], result["reason"]) == ("level_merged", "already merged")
    assert refs(repo) == before


def test_conflicting_level_fails_and_moves_no_ref(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", LEVEL)
    commit_file(repo, "shared.txt", "level side\n")
    git(repo, "checkout", "-q", TICKET)
    commit_file(repo, "shared.txt", "ticket side\n")
    before = refs(repo)
    result = chair_steward_exec(merge_action(), [], ports(repo))
    assert result["event"] == "merge_failed"
    assert result["reason"].startswith("merge-tree:")
    assert result["fields"] == FIELDS
    assert refs(repo) == before


def test_missing_level_branch_fails(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "branch", "-D", LEVEL)
    before = refs(repo)
    assert chair_steward_exec(merge_action(), [], ports(repo))["event"] == "merge_failed"
    assert refs(repo) == before


def test_passing_gate_reports_its_detail(tmp_path: Path):
    seen = []
    result = chair_steward_exec(
        gate_action(), ["ruff"], ports(tmp_path, lambda r, t, c: (seen.append((r, t, tuple(c))) or True, "all green"))
    )
    assert result == {"event": "gate_passed", "fields": FIELDS, "reason": "all green"}
    assert seen == [(str(tmp_path), TICKET, ("ruff",))]


def test_failing_gate_reports_its_detail(tmp_path: Path):
    result = chair_steward_exec(gate_action(), ["ruff"], ports(tmp_path, lambda r, t, c: (False, "ruff failed")))
    assert result == {"event": "gate_failed", "fields": FIELDS, "reason": "ruff failed"}


def boom(*_args):
    raise OSError("disk gone")


def test_raising_gate_port_is_a_gate_failure(tmp_path: Path):
    result = chair_steward_exec(gate_action(), [], ports(tmp_path, boom))
    assert result == {"event": "gate_failed", "fields": FIELDS, "reason": "OSError: disk gone"}


def test_raising_git_port_is_a_merge_failure(tmp_path: Path):
    raising = StewardPorts(
        git=boom,
        repo_of=lambda e: "r",
        ticket_branch_of=lambda e: TICKET,
        phase_of=lambda e, n: "phase-a",
        run_gate_checks=boom,
    )
    result = chair_steward_exec(merge_action(), [], raising)
    assert result == {"event": "merge_failed", "fields": FIELDS, "reason": "OSError: disk gone"}


def test_raising_phase_port_is_a_merge_failure(tmp_path: Path):
    bad_phase = StewardPorts(
        git=run_git,
        repo_of=lambda e: "r",
        ticket_branch_of=lambda e: TICKET,
        phase_of=lambda e, n: [][n],
        run_gate_checks=boom,
    )
    assert chair_steward_exec(merge_action(), [], bad_phase)["event"] == "merge_failed"


def test_other_kinds_raise_value_error(tmp_path: Path):
    with pytest.raises(ValueError):
        chair_steward_exec({"kind": "launch", "epic": EPIC}, [], ports(tmp_path))


def test_outcome_copies_the_action_fields():
    action = {"fields": {"a": "1"}}
    result = outcome("gate_passed", action, "ok")
    assert result == {"event": "gate_passed", "fields": {"a": "1"}, "reason": "ok"}
    assert result["fields"] is not action["fields"]
