import json
from dataclasses import replace
from pathlib import Path

from agent_tools import courier
from agent_tools.chair_exec import Deps, perform

EPIC = "epic-x"
SCOPED = {"epoch": 1, "epic": EPIC, "topic": EPIC}
MERGE = {"kind": "merge_level", **SCOPED, "level": 2, "fields": {"head": "abc", "level": "2"}}
GATE = {"kind": "run_gate", **SCOPED, "fields": {"head": "abc"}}
QUARANTINE = {"kind": "steward_event", **SCOPED, "event": "needs_attention", "fields": {"task": "t1"}}
RELAUNCH = {"kind": "relaunch", **SCOPED, "initiative": EPIC, "fields": {"signature": "s1"}}


def _deps(tmp_path: Path, calls: list, event: str = "level_merged", reason: str = "", code: int = 0) -> Deps:
    def steward_exec(action, checks):
        calls.append(("steward_exec", action["kind"], tuple(checks)))
        return {"event": event, "fields": action["fields"], "reason": reason}

    return Deps(
        run=lambda argv: calls.append(("run", argv)) or (code, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "",
        repo_for=lambda action: "r",
        work_dir=tmp_path,
        label="chair-1",
        steward_exec=steward_exec,
        steward_checks=lambda action: ("ruff", "pytest"),
    )


def _bus(tmp_path: Path) -> list[dict]:
    path = tmp_path / "courier.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_merge_level_publishes_level_merged_with_the_loops_checks(tmp_path):
    calls: list = []
    [result] = perform([MERGE], _deps(tmp_path, calls), lambda: 1, False)
    [entry] = _bus(tmp_path)
    assert result["status"] == "done"
    assert calls == [("steward_exec", "merge_level", ("ruff", "pytest"))]
    assert (entry["event"], entry["topic"], entry["to"]) == ("level_merged", EPIC, "chair")
    assert entry["fields"] == {"head": "abc", "level": "2"}
    assert not courier.needs_model(entry)


def test_failed_merge_level_publishes_merge_failed(tmp_path):
    [result] = perform([MERGE], _deps(tmp_path, [], "merge_failed", "conflict"), lambda: 1, False)
    [entry] = _bus(tmp_path)
    assert (result["status"], result["reason"]) == ("failed", "conflict")
    assert entry["event"] == "merge_failed"
    assert courier.needs_model(entry)


def test_run_gate_publishes_gate_passed(tmp_path):
    [result] = perform([GATE], _deps(tmp_path, [], "gate_passed"), lambda: 1, False)
    assert result["status"] == "done"
    assert [e["event"] for e in _bus(tmp_path)] == ["gate_passed"]


def test_run_gate_publishes_gate_failed(tmp_path):
    [result] = perform([GATE], _deps(tmp_path, [], "gate_failed"), lambda: 1, False)
    [entry] = _bus(tmp_path)
    assert result["status"] == "failed"
    assert (entry["event"], entry["topic"]) == ("gate_failed", EPIC)
    assert courier.needs_model(entry)


def _no_executor(tmp_path: Path, calls: list | None = None) -> Deps:
    return Deps(
        run=lambda argv: (calls.append(("run", argv)) if calls is not None else None) or (0, ""),
        delete_branches=lambda r, p: ([], ""), acquire_lease=lambda h, ho: "",
        record=lambda a: None, run_id=lambda a: "", repo_for=lambda a: "r", work_dir=tmp_path,
    )


def test_an_event_that_does_not_fit_the_kind_is_refused_and_reported(tmp_path):
    [result] = perform([GATE], _deps(tmp_path, [], "level_merged"), lambda: 1, False)
    [entry] = _bus(tmp_path)
    assert result["status"] == "refused"
    assert (entry["event"], entry["topic"]) == ("needs_attention", EPIC)
    assert entry["fields"] == {"action": "run_gate", "reason": result["reason"]}
    assert courier.needs_model(entry)


def test_merge_level_without_a_steward_executor_is_refused(tmp_path):
    [result] = perform([MERGE], _no_executor(tmp_path), lambda: 1, False)
    [entry] = _bus(tmp_path)
    assert result["status"] == "refused"
    assert (entry["event"], entry["topic"]) == ("needs_attention", EPIC)
    assert entry["fields"] == {"action": "merge_level", "reason": "merge_level has no steward executor"}


def test_an_executor_that_raises_is_a_refusal_not_an_exception(tmp_path):
    deps = _deps(tmp_path, [])

    def boom(action, checks):
        raise RuntimeError("disk gone")

    [result] = perform([MERGE], replace(deps, steward_exec=boom), lambda: 1, False)
    [entry] = _bus(tmp_path)
    assert result["status"] == "refused"
    assert result["reason"] == "merge_level raised RuntimeError: disk gone"
    assert (entry["event"], entry["fields"]["reason"]) == ("needs_attention", result["reason"])


TAGGED = {"kind": "relaunch", "epoch": 1, "initiative": EPIC, "after_steward": True}


def test_a_tagged_relaunch_after_a_refused_merge_runs_once(tmp_path):
    calls: list = []
    [merge, relaunch] = perform([MERGE, TAGGED], _no_executor(tmp_path, calls), lambda: 1, False)
    assert (merge["status"], relaunch["status"]) == ("refused", "done")
    assert [c[0] for c in calls] == ["run"]
    assert [e["event"] for e in _bus(tmp_path)] == ["needs_attention"]


def test_a_tagged_relaunch_after_a_done_merge_runs_once(tmp_path):
    calls: list = []
    [merge, relaunch] = perform([MERGE, TAGGED], _deps(tmp_path, calls), lambda: 1, False)
    assert (merge["status"], relaunch["status"]) == ("done", "done")
    assert [c[0] for c in calls] == ["steward_exec", "run"]
    assert [e["event"] for e in _bus(tmp_path)] == ["level_merged"]


def test_a_tagged_relaunch_after_a_failed_merge_is_skipped(tmp_path):
    calls: list = []
    [merge, relaunch] = perform([MERGE, TAGGED], _deps(tmp_path, calls, "merge_failed", "conflict"), lambda: 1, False)
    assert merge["status"] == "failed"
    assert relaunch["status"] == "recorded"
    assert "merge_level" in relaunch["reason"] and EPIC in relaunch["reason"]
    assert [c[0] for c in calls] == ["steward_exec"]
    assert [e["event"] for e in _bus(tmp_path)] == ["merge_failed"]


def test_a_tagged_relaunch_after_a_failed_gate_is_skipped(tmp_path):
    calls: list = []
    [gate, relaunch] = perform([GATE, TAGGED], _deps(tmp_path, calls, "gate_failed"), lambda: 1, False)
    assert (gate["status"], relaunch["status"]) == ("failed", "recorded")
    assert "run_gate" in relaunch["reason"]
    assert [c[0] for c in calls] == ["steward_exec"]


def test_quarantine_steward_event_publishes_needs_attention_once(tmp_path):
    [result] = perform([QUARANTINE], _deps(tmp_path, []), lambda: 1, False)
    [entry] = _bus(tmp_path)
    assert result["status"] == "done"
    assert (entry["event"], entry["topic"], entry["fields"]) == ("needs_attention", EPIC, {"task": "t1"})
    assert courier.needs_model(entry)


def test_fenced_steward_event_publishes_nothing(tmp_path):
    [result] = perform([QUARANTINE], _deps(tmp_path, []), lambda: 2, False)
    assert result["status"] == "fenced"
    assert _bus(tmp_path) == []


def test_dry_run_merges_runs_and_publishes_nothing(tmp_path):
    calls: list = []
    results = perform([MERGE, GATE, QUARANTINE, RELAUNCH], _deps(tmp_path, calls), lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run"] * 4
    assert results[0]["reason"] == f"would merge level 2 of {EPIC} and publish its result event on {EPIC}"
    assert results[2]["reason"] == f"would publish needs_attention on {EPIC}"
    assert calls == []
    assert _bus(tmp_path) == []


def test_steward_relaunch_uses_the_launch_path_and_publishes_relaunched(tmp_path):
    calls: list = []
    planner = {"kind": "relaunch", "epoch": 1, "initiative": EPIC}
    [planned] = perform([planner], _deps(tmp_path, calls), lambda: 1, False)
    [stewarded] = perform([RELAUNCH], _deps(tmp_path, calls), lambda: 1, False)
    assert calls[0] == calls[1] and calls[0][0] == "run"
    assert (planned["status"], stewarded["status"]) == ("done", "done")
    assert [e["event"] for e in _bus(tmp_path)] == ["relaunched"]


def test_failed_steward_relaunch_publishes_nothing(tmp_path):
    [result] = perform([RELAUNCH], _deps(tmp_path, [], code=1), lambda: 1, False)
    assert result["status"] == "failed"
    assert _bus(tmp_path) == []


def test_a_non_steward_kind_performs_as_before(tmp_path):
    action = {"kind": "needs_chair", "epoch": 1, "reason": "look"}
    [result] = perform([action], _deps(tmp_path, []), lambda: 1, False)
    [dry] = perform([action], _deps(tmp_path, []), lambda: 1, True)
    assert (result["status"], result["reason"]) == ("recorded", "look")
    assert (dry["status"], dry["reason"]) == ("dry_run", "")
    assert _bus(tmp_path) == []
