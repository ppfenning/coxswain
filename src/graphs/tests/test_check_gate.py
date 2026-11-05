"""The check gate resolver and the per-repo gate lock."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from harness.check_gate import gate_checks, gate_commands, repo_gate_lock

TASK = [{"name": "unit", "cmd": "pytest -q"}]
LEVEL = [{"name": "lint", "cmd": "ruff check"}, {"name": "types", "cmd": "mypy ."}]
TICKET = [{"name": "e2e", "cmd": "make e2e"}]
WAIT = 10


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def test_resolver_returns_each_gates_list():
    areas = {"checks": TASK, "level_checks": LEVEL, "ticket_checks": TICKET}
    assert gate_checks(areas, "task") == TASK
    assert gate_checks(areas, "level") == LEVEL
    assert gate_checks(areas, "ticket") == TICKET


def test_checks_only_mapping_has_empty_level_and_ticket():
    areas = {"checks": TASK}
    assert gate_checks(areas, "task") is TASK
    assert gate_checks(areas, "level") == []
    assert gate_checks(areas, "ticket") == []


@pytest.mark.parametrize("value", [None, "make", {"name": "x"}, 3])
def test_none_and_non_list_values_are_empty(value):
    areas = {"checks": value, "level_checks": value, "ticket_checks": value}
    assert [gate_checks(areas, g) for g in ("task", "level", "ticket")] == [[], [], []]


def test_unknown_gate_is_refused():
    with pytest.raises(ValueError):
        gate_checks({}, "merge")


def test_commands_omit_empty_gates():
    areas = {"level_checks": LEVEL, "ticket_checks": TICKET}
    assert gate_commands(areas) == {"level": ["ruff check", "mypy ."], "ticket": ["make e2e"]}
    assert gate_commands({}) == {}


def test_two_threads_on_one_repo_never_overlap(tmp_path):
    repo = _repo(tmp_path / "repo")
    events: list[str] = []
    first_in = threading.Event()
    release_first = threading.Event()

    def first():
        with repo_gate_lock(repo):
            events.append("first-in")
            first_in.set()
            release_first.wait(WAIT)
            events.append("first-out")

    def second():
        with repo_gate_lock(repo):
            events.append("second-in")
            events.append("second-out")

    t1 = threading.Thread(target=first)
    t1.start()
    assert first_in.wait(WAIT)
    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(0.3)
    assert t2.is_alive()
    assert events == ["first-in"]
    release_first.set()
    t1.join(WAIT)
    t2.join(WAIT)
    assert events == ["first-in", "first-out", "second-in", "second-out"]


def test_lock_is_released_when_body_raises(tmp_path):
    repo = _repo(tmp_path / "repo")
    with pytest.raises(RuntimeError), repo_gate_lock(repo):
        raise RuntimeError("boom")
    acquired = threading.Event()

    def again():
        with repo_gate_lock(repo):
            acquired.set()

    t = threading.Thread(target=again)
    t.start()
    t.join(WAIT)
    assert acquired.is_set()


def test_different_repos_do_not_block_each_other(tmp_path):
    repo_a = _repo(tmp_path / "a")
    repo_b = _repo(tmp_path / "b")
    holding_a = threading.Event()
    release_a = threading.Event()
    got_b = threading.Event()

    def hold_a():
        with repo_gate_lock(repo_a):
            holding_a.set()
            release_a.wait(WAIT)

    def take_b():
        with repo_gate_lock(repo_b):
            got_b.set()

    t1 = threading.Thread(target=hold_a)
    t1.start()
    assert holding_a.wait(WAIT)
    t2 = threading.Thread(target=take_b)
    t2.start()
    assert got_b.wait(WAIT)
    release_a.set()
    t1.join(WAIT)
    t2.join(WAIT)
