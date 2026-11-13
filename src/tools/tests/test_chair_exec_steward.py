import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_tools import courier
from agent_tools.chair_exec import Deps, _land_event, perform

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


def test_a_tagged_relaunch_after_a_deferred_merge_is_skipped(tmp_path):
    calls: list = []
    sink = SimpleNamespace(pending=lambda repo: True, full=lambda repo: False, collect=lambda: [])
    [result, relaunch] = perform([_level(), TAGGED], replace(_deps(tmp_path, calls), lands=sink), lambda: 1, False)
    assert (result["status"], relaunch["status"]) == ("deferred", "recorded")
    assert relaunch["reason"] == f"not relaunched, merge_level for {EPIC} deferred"
    assert calls == []
    assert _bus(tmp_path) == []


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


# A level or task merge_level is the chair's own land: the port records every argv and answers `cox runs land` by run.
REPO = "/work/repo"
LANDED = (0, "merge: ok\nmark_done: ok\n")
NO_PHASE_RECORD = (1, "land: no phase record at /runs/p1.json\n")
DIRTY = (1, "land: refusing, the working tree is dirty\n")


def _level(epic: str = EPIC, land_at: str = "level", **over) -> dict:
    stamped = {
        "kind": "merge_level", "epoch": 1, "epic": epic, "topic": epic, "initiative": epic, "level": 2,
        "fields": {"head": "abc", "level": "2"}, "land_at": land_at, "repo": REPO, "phase": "p1", "run": "run-b",
        "task_runs": {"t1": "run-a", "t2": "run-b"}, "fetch_runs": ["run-a", "run-b"],
    }
    return {**stamped, **over}


def _landing(tmp_path: Path, calls: list, answers: dict | None = None) -> Deps:
    """No steward_exec: reaching it would refuse. `answers` maps a `cox runs land` line (see _cox) to its (code, output)."""

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["cox", "runs", "land"]:
            return (answers or {}).get(" ".join(argv[2:6]), LANDED)
        return 0, ""

    return replace(_no_executor(tmp_path), run=run, rev_parse=lambda repo, ref: None, label="chair-1")


def _cox(calls: list) -> list[str]:
    return [" ".join(argv[2:6]) for argv in calls if argv[:2] == ["cox", "runs"]]


def _no_commit(monkeypatch) -> None:
    monkeypatch.setattr("agent_tools.chair_exec.land_commit", lambda repo: "abc123")


def test_a_level_merge_fetches_each_run_then_lands_one_phase(tmp_path, monkeypatch):
    _no_commit(monkeypatch)
    calls: list = []
    [result] = perform([_level()], _landing(tmp_path, calls), lambda: 1, False)
    [entry] = _bus(tmp_path)
    assert _cox(calls) == ["fetch run-a", "fetch run-b", "land run-b --phase p1"]
    assert result["status"] == "done"
    assert (entry["event"], entry["topic"], entry["fields"]) == ("level_merged", EPIC, {"head": "abc", "level": "2"})


def test_a_split_level_lands_per_task_from_each_tasks_newest_run(tmp_path, monkeypatch):
    _no_commit(monkeypatch)
    calls: list = []
    answers = {"land run-b --phase p1": NO_PHASE_RECORD}
    [result] = perform([_level()], _landing(tmp_path, calls, answers), lambda: 1, False)
    assert _cox(calls) == [
        "fetch run-a", "fetch run-b", "land run-b --phase p1", "land run-a --task t1", "land run-b --task t2",
    ]
    assert result["status"] == "done"
    assert [e["event"] for e in _bus(tmp_path)] == ["level_merged"]


def test_a_task_merge_lands_each_task_from_its_run(tmp_path, monkeypatch):
    _no_commit(monkeypatch)
    calls: list = []
    [result] = perform([_level(land_at="task")], _landing(tmp_path, calls), lambda: 1, False)
    assert _cox(calls) == ["fetch run-a", "fetch run-b", "land run-a --task t1", "land run-b --task t2"]
    assert result["status"] == "done"
    assert [e["event"] for e in _bus(tmp_path)] == ["level_merged"]


def test_a_failed_land_publishes_merge_failed_and_blocks_nothing(tmp_path, monkeypatch):
    _no_commit(monkeypatch)
    calls: list = []
    alpha, beta = _level("alpha", run="run-a"), _level("beta", run="run-b")
    behind = {"kind": "relaunch", "epoch": 1, "initiative": "alpha", "after_steward": True}
    plain = {"kind": "land", "epoch": 1, "task_id": "t9", "repo": REPO, "run": "run-z"}
    answers = {"land run-a --phase p1": DIRTY}
    results = perform([alpha, behind, beta, plain], _landing(tmp_path, calls, answers), lambda: 1, False)
    assert [(r["action"]["kind"], r["status"]) for r in results] == [
        ("merge_level", "failed"), ("needs_chair", "recorded"), ("relaunch", "recorded"),
        ("merge_level", "done"), ("land", "landed"),
    ]
    assert results[0]["reason"] == DIRTY[1]
    assert "reason" not in _bus(tmp_path)[0]["fields"] and "working tree is dirty" in _bus(tmp_path)[0]["note"]
    # The failed steward is the existing after_steward case: the relaunch behind it is recorded, not launched.
    assert "not relaunched, merge_level for alpha failed" in results[2]["reason"]
    assert not any(argv[:3] == ["cox", "route", "launch"] and "work/alpha" in argv for argv in calls)
    assert [(e["topic"], e["event"]) for e in _bus(tmp_path)] == [("alpha", "merge_failed"), ("beta", "level_merged")]
    assert _cox(calls)[-2:] == ["land run-b --phase p1", "land run-z --task t9"]


def test_a_live_land_freeze_ends_a_steward_land_before_any_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_tools.chair_exec._frozen_reason", lambda deps: "land freeze: main is red")
    calls: list = []
    [result] = perform([_level()], _landing(tmp_path, calls), lambda: 1, False)
    assert (result["status"], result["reason"]) == ("failed", "land freeze: main is red")
    assert calls == []
    assert [e["event"] for e in _bus(tmp_path)] == ["merge_failed"]


def test_a_repo_blocked_by_an_earlier_land_holds_back_a_steward_land(tmp_path, monkeypatch):
    _no_commit(monkeypatch)
    calls: list = []
    plain = {"kind": "land", "epoch": 1, "task_id": "t9", "repo": REPO, "run": "run-z"}
    answers = {"land run-z --task t9": DIRTY}
    results = perform([plain, _level()], _landing(tmp_path, calls, answers), lambda: 1, False)
    [merge] = [r for r in results if r["action"]["kind"] == "merge_level"]
    assert merge["status"] == "failed" and merge["reason"].startswith(f"an earlier land in {REPO}")
    assert _cox(calls) == ["land run-z --task t9"]


def test_a_refused_steward_land_records_its_needs_chair_row(tmp_path, monkeypatch):
    _no_commit(monkeypatch)
    answers = {"land run-b --phase p1": DIRTY}
    results = perform([_level()], _landing(tmp_path, [], answers), lambda: 1, False)
    assert [r["action"]["kind"] for r in results] == ["merge_level", "needs_chair"]
    assert results[1]["status"] == "recorded"


def test_an_initiatives_own_relaunch_runs_behind_a_landed_merge(tmp_path, monkeypatch):
    _no_commit(monkeypatch)
    calls: list = []
    behind = {"kind": "relaunch", "epoch": 1, "initiative": EPIC, "after_steward": True}
    [merge, relaunch] = perform([_level(), behind], _landing(tmp_path, calls), lambda: 1, False)
    assert (merge["status"], relaunch["status"]) == ("done", "done")
    assert calls[-1][:3] == ["cox", "route", "launch"]


def test_a_missing_repo_phase_or_run_is_merge_failed_naming_it(tmp_path):
    for name in ("repo", "phase", "run"):
        calls: list = []
        [result] = perform([_level(**{name: ""})], _landing(tmp_path, calls), lambda: 1, False)
        assert result["status"] == "failed"
        assert result["reason"] == f"merge_level at land_at level lacks {name}"
        assert calls == []
    assert [e["event"] for e in _bus(tmp_path)] == ["merge_failed"] * 3


def test_a_level_merge_with_a_task_that_has_no_run_is_merge_failed_before_any_call(tmp_path):
    calls: list = []
    [result] = perform([_level(task_runs={"t1": "run-a", "t2": ""})], _landing(tmp_path, calls), lambda: 1, False)
    assert (result["status"], result["reason"]) == ("failed", "merge_level at land_at level lacks run of task t2")
    assert calls == []


def test_only_a_land_that_merged_something_is_level_merged():
    action = {"kind": "land_phase"}
    no_run = {"action": action, "status": "recorded", "reason": "no approved task of the phase has a known run yet"}
    assert _land_event(no_run) == ("merge_failed", no_run["reason"])
    assert _land_event({**no_run, "already_landed": True})[0] == "level_merged"
    assert _land_event({**no_run, "reason": "already done on another machine: t1"})[0] == "level_merged"
    assert _land_event({**no_run, "status": "landed"})[0] == "level_merged"
    assert [_land_event({**no_run, "status": s})[0] for s in ("refused", "failed", "not_landed", "busy", "skipped")] == [
        "merge_failed"
    ] * 5


def test_a_land_the_sink_still_holds_for_the_repo_defers_the_steward_land(tmp_path):
    calls: list = []
    sink = SimpleNamespace(pending=lambda repo: True, full=lambda repo: False, collect=lambda: [])
    [result] = perform([_level()], replace(_landing(tmp_path, calls), lands=sink), lambda: 1, False)
    assert (result["status"], result["reason"]) == ("deferred", f"a land is still running in {REPO}")
    assert calls == []
    assert _bus(tmp_path) == []


class _BlockingSink:
    """A land sink whose repo is held from the moment a land is handed to it, like a land waiting on checks."""

    def __init__(self) -> None:
        self.held = False
        self.started: list[dict] = []

    def pending(self, repo: str) -> bool:
        return self.held

    def full(self, repo: str) -> bool:
        return False

    def landing(self) -> list[dict[str, str]]:
        return []

    def collect(self) -> list:
        return []

    def submit(self, action, work):
        self.held = True
        self.started.append(action)
        return {"action": action, "status": "in_progress", "reason": ""}


def _land_phase_action() -> dict:
    return {"kind": "land_phase", "epoch": 1, "initiative": EPIC, "repo": REPO, "phase": "p1", "run": "run-b"}


def test_a_merge_planned_after_a_blocking_land_phase_defers_and_before_it_lands(tmp_path, monkeypatch):
    _no_commit(monkeypatch)
    after = _BlockingSink()
    calls: list = []
    [land, merge] = perform([_land_phase_action(), _level()], replace(_landing(tmp_path, calls), lands=after), lambda: 1, False)
    assert (land["status"], merge["status"]) == ("in_progress", "deferred")
    assert merge["reason"] == f"a land is still running in {REPO}"
    assert _cox(calls) == []
    assert _bus(tmp_path) == []

    before = _BlockingSink()
    calls = []
    [merge, land] = perform([_level(), _land_phase_action()], replace(_landing(tmp_path, calls), lands=before), lambda: 1, False)
    assert (merge["status"], land["status"]) == ("done", "in_progress")
    assert _cox(calls) == ["fetch run-a", "fetch run-b", "land run-b --phase p1"]
    assert [e["event"] for e in _bus(tmp_path)] == ["level_merged"]
    assert len(before.started) == 1


def test_a_task_merge_without_task_runs_is_merge_failed(tmp_path):
    [result] = perform([_level(land_at="task", task_runs={})], _landing(tmp_path, []), lambda: 1, False)
    assert (result["status"], result["reason"]) == ("failed", "merge_level at land_at task lacks task_runs")


def test_a_fetch_that_does_not_exit_zero_stops_the_merge_before_the_land(tmp_path):
    calls: list = []
    refusing = replace(
        _landing(tmp_path, calls), run=lambda argv: calls.append(argv) or (1, "fetch: refuse: run-a is still live on h")
    )
    [result] = perform([_level()], refusing, lambda: 1, False)
    assert result["status"] == "failed"
    assert result["reason"].startswith("fetch of run-a skipped: fetch: refuse")
    assert _cox(calls) == ["fetch run-a"]
    assert [e["event"] for e in _bus(tmp_path)] == ["merge_failed"]


def test_a_door_that_raises_is_merge_failed_not_an_exception(tmp_path):
    def boom(argv):
        raise RuntimeError("ssh gone")

    [result] = perform([_level()], replace(_landing(tmp_path, []), run=boom), lambda: 1, False)
    assert (result["status"], result["reason"]) == ("failed", "land raised RuntimeError: ssh gone")
    assert [e["event"] for e in _bus(tmp_path)] == ["merge_failed"]


def test_a_ticket_merge_still_goes_through_the_steward_port(tmp_path):
    calls: list = []
    [result] = perform([_level(land_at="ticket")], _deps(tmp_path, calls), lambda: 1, False)
    assert result["status"] == "done"
    assert calls == [("steward_exec", "merge_level", ("ruff", "pytest"))]


def test_dry_run_says_what_each_land_at_would_land(tmp_path):
    calls: list = []
    actions = [_level(), _level(land_at="task"), _level(land_at="ticket")]
    results = perform(actions, _landing(tmp_path, calls), lambda: 1, True)
    assert [r["reason"] for r in results] == [
        f"would land phase p1 of {EPIC} as one pull request in {REPO} and publish its result event on {EPIC}",
        f"would land each approved task of p1 of {EPIC} in {REPO} and publish its result event on {EPIC}",
        f"would merge level 2 of {EPIC} and publish its result event on {EPIC}",
    ]
    assert calls == [] and _bus(tmp_path) == []
