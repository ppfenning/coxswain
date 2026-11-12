"""Each epic task lane runs its checks against its own test database and drops it with its worktree."""

from __future__ import annotations

import os
from types import SimpleNamespace

from test_epic_driver import cart, drive, initiative, repo  # noqa: F401 -- fixtures

import harness.epic as epic
from harness import check_reuse

VAR = "LANE_DB"
SUFFIX = "_test"
SETTING = {"env": VAR, "template": f"postgresql://localhost/{{lane}}{SUFFIX}"}


def _work() -> dict:
    work = initiative(two_phases=False)
    next(i for i in work["items"] if i["id"] == "t1-probe")["verify"] = ["echo verify"]
    return work


def _spy_checks(monkeypatch) -> list[tuple[str, dict | None]]:
    """Every `run_checks` call as (worktree name, env), still run for real."""
    calls: list[tuple[str, dict | None]] = []
    real = epic.run_checks

    def spy(worktree, checks, **kw):
        calls.append((worktree.name, kw.get("env")))
        return real(worktree, checks, **kw)

    monkeypatch.setattr(epic, "run_checks", spy)
    monkeypatch.setattr(check_reuse, "run_checks", spy)  # the task gate runs through the ledger
    return calls


def _fake_drop(monkeypatch) -> list[tuple[str, str | None]]:
    """The drop edge with a fake runner: every command run as (command, lane variable in its environment)."""
    seen: list[tuple[str, str | None]] = []
    real = epic.drop_lane_database

    def run(cmd, **kw):
        seen.append((cmd, kw["env"].get(VAR)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(epic, "drop_lane_database", lambda cmd, name, env: real(cmd, name, env, run=run))
    return seen


def _with(cart, **setting) -> dict:  # noqa: F811
    cart["landing_areas"]["test_database"] = {**SETTING, **setting}
    return cart


def test_two_lanes_check_against_different_databases_ending_in_the_template_suffix(
    repo, cart, tmp_path, monkeypatch  # noqa: F811
) -> None:
    calls = _spy_checks(monkeypatch)
    drive(repo, _with(cart), tmp_path, work=_work())
    by_task = {name: [env[VAR] for n, env in calls if n == name] for name in ("t1-probe", "t2-bench")}
    assert all(urls and len(set(urls)) == 1 for urls in by_task.values())
    first, second = (urls[0] for urls in by_task.values())
    assert first != second
    assert first.endswith(SUFFIX) and second.endswith(SUFFIX)


def test_the_checks_and_the_verify_call_of_one_task_share_one_database_and_the_process_environment(
    repo, cart, tmp_path, monkeypatch  # noqa: F811
) -> None:
    calls = _spy_checks(monkeypatch)
    drive(repo, _with(cart), tmp_path, work=_work())
    probe = [env for name, env in calls if name == "t1-probe"]
    assert len(probe) == 2  # the main checks, then the ticket verify
    assert probe[0][VAR] == probe[1][VAR]
    assert all(env["PATH"] == os.environ["PATH"] for env in probe)


def test_the_task_record_names_the_lane_database(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, _with(cart), tmp_path, work=_work())
    names = {t["id"]: t["test_database"] for t in result["tasks"]}
    assert set(names) == {"t1-probe", "t2-bench"}
    assert all(name.endswith(SUFFIX) and "/" not in name for name in names.values())
    assert names["t1-probe"] != names["t2-bench"]


def test_no_setting_runs_checks_with_only_the_lane_basetemp_and_leaves_the_record_alone(
    repo, cart, tmp_path, monkeypatch  # noqa: F811
) -> None:
    monkeypatch.delenv("COX_LANE_CAPACITY", raising=False)  # a lane host exports it; this test is the no-capacity case
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)  # a lane exports its own --basetemp
    calls = _spy_checks(monkeypatch)
    result, _ = drive(repo, cart, tmp_path, work=_work())
    assert calls and all("--basetemp=" in env["PYTEST_ADDOPTS"] and VAR not in env for _, env in calls)
    assert not any("test_database" in t for t in result["tasks"])
    assert "lane_databases" not in result


def test_the_drop_runs_once_per_lane_with_the_lane_variable_in_its_environment(
    repo, cart, tmp_path, monkeypatch  # noqa: F811
) -> None:
    seen = _fake_drop(monkeypatch)
    result, _ = drive(repo, _with(cart, drop="dropdb {name}"), tmp_path, work=_work())
    names = {t["test_database"] for t in result["tasks"]}
    assert sorted(cmd for cmd, _ in seen) == sorted(f"dropdb {name}" for name in names)
    assert all(url and url.endswith(SUFFIX) for _, url in seen)
    assert {r["name"] for r in result["lane_databases"]} == names
    assert {r["status"] for r in result["lane_databases"]} == {"dropped"}


def test_without_a_drop_command_each_name_is_listed_as_pending(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, _with(cart), tmp_path, work=_work())
    names = {t["test_database"] for t in result["tasks"]}
    assert {r["name"] for r in result["lane_databases"]} == names
    assert {r["status"] for r in result["lane_databases"]} == {"pending"}


def test_a_kept_worktree_keeps_its_database(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, _with(cart), tmp_path, work=_work(), keep_worktrees=True)
    assert result["lane_databases"] == []


def test_the_drop_edge_is_not_called_without_a_setting(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    seen = _fake_drop(monkeypatch)
    drive(repo, cart, tmp_path, work=_work())
    assert seen == []
