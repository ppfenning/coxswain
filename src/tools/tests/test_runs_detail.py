from datetime import UTC, datetime
from types import SimpleNamespace

from agent_tools.events import Event, from_trace_names
from agent_tools.progress_read import NodeProgress, Progress
from agent_tools.runs_detail import Detail, NodeCall, detail, render


def test_timeline_orders_by_canonical_node_order_and_picks_up_verdicts():
    # from_trace_names gives every node of one attempt the same seq and the
    # edge lists trace files alphabetically -- neither carries start order.
    events = from_trace_names("r1", ["build-1.jsonl", "plan-1.jsonl", "review_adversary-1.jsonl"])
    assert {e.seq for e in events} == {1000}
    events = events + [Event("r1", "verdict", 1, {"node": "plan", "verdict": "approve"})]
    calls = [
        {"node": "build", "attempt": 1, "turns": 4, "cost_usd": 0.5},
        {"node": "plan", "attempt": 1, "turns": 2, "cost_usd": 0.1},
        {"node": "review_adversary", "attempt": 1, "turns": 1, "cost_usd": 0.2},
    ]
    d = detail("r1", True, events, calls, {}, [])
    assert [nc.node for nc in d.timeline] == ["plan", "build", "review_adversary"]
    assert d.timeline[0].verdict == "approve"
    assert d.timeline[1].verdict == ""


def test_the_failed_checks_excerpt_is_in_the_detail_and_rendered():
    # The shape the epic driver writes: per-check results under change_facts.
    record = {"change_facts": {"files_touched": ["a.py"], "checks": [
        {"name": "ok", "passed": True, "excerpt": ""},
        {"name": "t", "passed": False, "log": "checks/p1/t1/2.log",
         "excerpt": "FAILED tests/t.py::test_a - boom\n1 failed"},
    ]}}
    d = detail("r1", False, [], [], record, [])
    assert d.check_excerpt == "FAILED tests/t.py::test_a - boom\n1 failed"
    assert d.check_log == "checks/p1/t1/2.log"
    lines = render(d, 80)
    assert "  FAILED tests/t.py::test_a - boom" in lines
    assert "  full output: runs/r1/checks/p1/t1/2.log" in lines


def test_no_check_excerpt_without_a_record():
    assert detail("r1", False, [], [], {}, []).check_excerpt == ""
    assert detail("r1", False, [], [], {"change_facts": {}}, []).check_excerpt == ""


def test_a_top_level_checks_key_is_not_where_the_excerpt_lives():
    record = {"checks": [{"name": "t", "passed": False, "excerpt": "FAILED t.py::a"}]}
    assert detail("r1", False, [], [], record, []).check_excerpt == ""


def test_objection_prefers_the_arbitration_over_the_adversary():
    events = [Event("r1", "verdict", 1000, {"node": "arbitrate", "verdict": "revise"})]
    record = {
        "arbitration": SimpleNamespace(verdict="revise", reasoning="Arbitration says X. More detail."),
        "adversary": [{"why_wrong": "Adversary says Y."}],
    }
    d = detail("r1", True, events, [], record, [])
    assert d.objection == "Arbitration says X"


def test_objection_falls_back_to_the_adversarys_first_why_wrong():
    events = [Event("r1", "verdict", 1000, {"node": "arbitrate", "verdict": "revise"})]
    record = {"adversary": [{"why_wrong": "Adversary says Y. More."}]}
    d = detail("r1", True, events, [], record, [])
    assert d.objection == "Adversary says Y"


def test_no_objection_when_the_last_verdict_is_not_revise():
    events = [Event("r1", "verdict", 1000, {"node": "arbitrate", "verdict": "approve"})]
    record = {"arbitration": SimpleNamespace(verdict="approve", reasoning="Fine. Good.")}
    d = detail("r1", True, events, [], record, [])
    assert d.objection == ""


def test_a_quarantined_run_carries_its_reason():
    events = [Event("r1", "task_quarantined", 5, {"task": "t1", "reason": "boom"})]
    d = detail("r1", False, events, [], {}, [])
    assert d.status == "quarantined"
    assert d.quarantine_reason == "boom"


def test_an_empty_record_yields_empty_strings_not_a_raise():
    d = detail("r1", True, [], [], {}, [])
    assert d.objection == ""
    assert d.quarantine_reason == ""
    assert d.files_touched == ()
    assert d.changed_lines == 0
    assert d.last_calls == ()


def test_last_calls_is_the_tails_last_three_entries():
    d = detail("r1", True, [], [], {}, ["Read", "Grep", "Edit", "Bash"])
    assert d.last_calls == ("Grep", "Edit", "Bash")


def test_files_touched_and_changed_lines_come_from_change_facts():
    record = {"change_facts": {"files_touched": ["a.py", "b.py"], "changed_lines": 12}}
    d = detail("r1", True, [], [], record, [])
    assert d.files_touched == ("a.py", "b.py")
    assert d.changed_lines == 12


def test_render_cuts_every_line_to_width():
    d = Detail(
        run="r1",
        alive=True,
        status="running",
        timeline=(NodeCall(node="plan", attempt=1, turns=3, cost_usd=1.2345, verdict="approve"),),
        objection="a very long objection sentence that should be cut",
        quarantine_reason="",
        files_touched=("a.py", "b.py"),
        changed_lines=10,
        last_calls=("Read", "Edit"),
    )
    lines = render(d, 10)
    assert lines
    assert all(len(line) <= 10 for line in lines)


def test_a_live_run_with_a_foreign_or_expired_lease_reads_fenced():
    assert [detail("r1", True, [], [], {}, [], standing=s).status for s in ("fenced", "expired")] == ["fenced", "fenced"]


def test_a_held_or_absent_lease_stays_running_and_the_default_is_none():
    assert [detail("r1", True, [], [], {}, [], standing=s).status for s in ("held", "none")] == ["running", "running"]
    assert detail("r1", True, [], [], {}, []).status == "running"


def test_a_dead_process_stays_exited_under_a_fenced_lease():
    assert detail("r1", False, [], [], {}, [], standing="fenced").status == "exited"


def test_quarantined_still_wins_over_fenced():
    events = [Event("r1", "task_quarantined", 0, {"task": "t1", "reason": "x"})]
    assert detail("r1", True, events, [], {}, [], standing="fenced").status == "quarantined"


NOW = datetime(2026, 10, 30, 12, 0, 30, tzinfo=UTC)


def _node(**over):
    base = {
        "node": "build", "attempt": 1, "task": "t1", "tier": "mid", "status": "running", "turns": 3,
        "cost_usd": None, "started_at": "2026-10-30T12:00:00Z", "last_tool": "Edit",
        "last_tool_at": "2026-10-30T12:00:18Z", "worktree": "/w/t1", "branch": "feat/t1",
    }
    return NodeProgress(**{**base, **over})


def _progress(*nodes):
    return Progress(version=1, run="r1", written_at="2026-10-30T12:00:29Z", nodes=tuple(nodes))


def test_a_running_build_shows_its_last_tool_age_and_a_dash_for_null_cost():
    d = detail("r1", True, [], [], {}, [], progress=_progress(_node()), now=NOW)
    assert d.nodes[0].last_tool_age_s == 12.0
    assert d.nodes[0].cost_usd is None
    assert render(d, 100)[-1] == "  build t1 mid running 3 - Edit 12s ago feat/t1"


def test_a_done_plan_takes_turns_and_cost_from_its_finished_call():
    plan = _node(node="plan", status="done", turns=1, cost_usd=0.0, branch=None)
    calls = [{"node": "plan", "attempt": 1, "turns": 5, "cost_usd": 0.25}]
    d = detail("r1", True, [], calls, {}, [], progress=_progress(plan), now=NOW)
    assert (d.nodes[0].turns, d.nodes[0].cost_usd) == (5, 0.25)
    assert render(d, 100)[-1] == "  plan t1 mid done 5 $0.25 Edit 12s ago /w/t1"


def test_a_quarantined_task_overrides_the_records_status():
    events = [Event("r1", "task_quarantined", 5, {"task": "t1", "reason": "boom"})]
    d = detail("r1", True, events, [], {}, [], progress=_progress(_node(), _node(task="t2")), now=NOW)
    assert [n.status for n in d.nodes] == ["quarantined", "running"]


def test_no_progress_leaves_the_nodes_empty_and_the_render_unchanged():
    calls = [{"node": "plan", "attempt": 1, "turns": 2, "cost_usd": 0.1}]
    d = detail("r1", True, from_trace_names("r1", ["plan-1.jsonl"]), calls, {}, [])
    assert d.nodes == ()
    assert render(d, 80) == ["run r1 [running]", "plan#1  2  $0.10  "]
