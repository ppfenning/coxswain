import json
from pathlib import Path

import pytest

from agent_tools import dash_feed, dash_feed_tasks

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_task_checks_v1.json").read_text(encoding="utf-8"))
ROW = {"kind": "task", "initiative": "dash-feed", "task_id": "a", "phase": "p2-feed", "state": "done", "needs": [], "reason": ""}
ATTEMPT = {"run_id": "dash-feed-1", "task_id": "a"}


def _totals(example):
    return {"totals": dict(example["checks"])}


def _write(root, text, run="dash-feed-1", phase="p2-feed", task="a"):
    path = root / run / "tasks" / phase / f"{task}.json"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def _tasks(root):
    entries, _ = dash_feed._run_tasks([ROW], [ATTEMPT], {}, {}, "dash-feed", root)
    return entries[0]


@pytest.mark.parametrize("example", FIXTURE["examples"])
def test_fixture_examples_come_back_exactly(example):
    assert dash_feed_tasks.checks_from_scope(_totals(example)) == example["checks"]


@pytest.mark.parametrize(
    "scope",
    [
        None,
        {},
        {"totals": None},
        {"totals": {}},
        {"totals": {"tests_selected": 1, "tests_total": 2}},
        {"totals": {"tests_selected": "1", "tests_total": 2, "cpu_seconds": 1.0}},
        {"totals": {"tests_selected": True, "tests_total": 2, "cpu_seconds": 1.0}},
        {"totals": {"tests_selected": -1, "tests_total": 2, "cpu_seconds": 1.0}},
        {"totals": {"tests_selected": 1, "tests_total": 2, "cpu_seconds": float("nan")}},
    ],
)
def test_missing_or_malformed_totals_give_none(scope):
    assert dash_feed_tasks.checks_from_scope(scope) is None


def test_an_entry_without_figures_has_no_checks_key():
    entry = dash_feed_tasks._entry(ROW, {"a": "done"}, [ATTEMPT], {"run": "dash-feed-1", "check_scope": {"totals": {}}}, None)
    assert entry == {
        "id": "a", "phase": "p2-feed", "state": "done", "run": "dash-feed-1", "tries": 1, "node": "", "turns": 0,
        "cost": 0.0, "reason": "",
    }


def test_an_entry_with_figures_carries_checks():
    latest = {"run": "dash-feed-1", "check_scope": _totals(FIXTURE["examples"][0])}
    entry = dash_feed_tasks._entry(ROW, {"a": "done"}, [ATTEMPT], latest, None)
    assert entry["checks"] == FIXTURE["checks"]


def test_the_edge_tolerates_a_missing_record(tmp_path):
    assert "checks" not in _tasks(tmp_path)
    assert dash_feed._task_check_scope(tmp_path, "dash-feed-1", "p2-feed", "a") is None


@pytest.mark.parametrize("text", ["{not json", "[]", '{"check_scope": 3}', "{}"])
def test_the_edge_tolerates_an_unreadable_record(tmp_path, text):
    _write(tmp_path, text)
    assert "checks" not in _tasks(tmp_path)


def test_the_edge_tolerates_undecodable_bytes(tmp_path):
    path = tmp_path / "dash-feed-1" / "tasks" / "p2-feed" / "a.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")
    assert dash_feed._task_check_scope(tmp_path, "dash-feed-1", "p2-feed", "a") is None


def test_the_edge_passes_the_latest_runs_totals_to_the_entry(tmp_path):
    _write(tmp_path, json.dumps({"check_scope": _totals(FIXTURE["examples"][1])}))
    assert _tasks(tmp_path)["checks"] == FIXTURE["examples"][1]["checks"]


def test_latest_by_task_carries_check_scope_only_when_the_reader_finds_one():
    by_task = {"a": [ATTEMPT], "b": [{"run_id": "dash-feed-1", "task_id": "b"}]}
    scope = {"totals": {}}
    got = dash_feed._latest_by_task(by_task, {}, lambda task, run: scope if task == "a" else None)
    assert got["a"]["check_scope"] == scope
    assert "check_scope" not in got["b"]
    assert "check_scope" not in dash_feed._latest_by_task(by_task, {})["a"]
