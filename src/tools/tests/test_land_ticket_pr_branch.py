"""A non-card ticket phase plans a merge into the one `pr/<initiative>` branch.

Payload keys the executor reads:

- `pr_branch_cut`: `branch` (pr/<initiative>), `from` (default branch). Create the branch from `from` when absent, else reuse it.
- `merge_phase`: `branch` (epic/<initiative>/<phase>), `into` (the pr branch), `from`, `subject`. Local merge commit naming the phase.
- `pr_create`: `title`, `body`, `head`, `base`, `draft` (True). Only the first phase, and a one-phase ticket.
- `pr_update`: `head` (finds the open PR), `title`, `body`, and `pr` (number) when a resume found one.
- `pr_ready`: `head`, and `pr` when known.
- `mark_merged`: `task`. State stays approved; the merge flag is set.
- `phase_done`: `phase`, `initiative`.
- `wait_checks` and `merge` end the last phase only; `mark_done` follows per ticket task.
"""

from agent_tools import land

PHASES = ["one", "two"]


def _phase(phase, last=False):
    return {"run": "epic-x-5", "phase": phase, "initiative": "x", "last_phase": last}


def _task(task, status="approved"):
    return {"task": task, "title": task, "repo": "wire", "status": status, "review": {"verdict": "approve"}}


def _plan(phase, ids, records=None, phases=PHASES, **kw):
    items = [{"id": t, "status": "approved"} for t in ids]
    return land.land_plan(_phase(phase, phase == phases[-1]), kw.pop("branches", {}), "main", items=items,
                          task_records=records or [_task(t) for t in ids], repo="wire", phases=phases,
                          ticket_tasks=kw.pop("ticket_tasks", ids), **kw)


def _kinds(steps):
    return [s["kind"] for s in steps]


def test_phase_one_of_two_cuts_merges_pushes_and_opens_a_draft_pr():
    steps = _plan("one", ["a", "b"])
    assert _kinds(steps) == ["pr_branch_cut", "merge_phase", "checks", "push", "pr_create",
                             "mark_merged", "mark_merged", "phase_done"]
    assert steps[0] == {"kind": "pr_branch_cut", "branch": "pr/x", "from": "main"}
    assert steps[1] == {"kind": "merge_phase", "branch": "epic/x/one", "into": "pr/x", "from": "main",
                        "subject": "epic x: merge phase one"}
    assert steps[3] == {"kind": "push", "branch": "pr/x"}
    assert (steps[4]["draft"], steps[4]["head"], steps[4]["base"]) == (True, "pr/x", "main")
    assert steps[-1] == {"kind": "phase_done", "phase": "one", "initiative": "x"}


def test_phase_two_of_three_updates_the_pr_and_never_creates_one():
    steps = _plan("two", ["c"], phases=["one", "two", "three"])
    assert _kinds(steps) == ["pr_branch_cut", "merge_phase", "checks", "push", "pr_update", "mark_merged", "phase_done"]
    assert steps[4]["head"] == "pr/x" and "pr" not in steps[4]


def test_last_phase_updates_readies_merges_and_marks_every_ticket_task_done():
    steps = _plan("two", ["c"], ticket_tasks=["a", "b", "c"], ticket_present=["a", "b"])
    assert _kinds(steps)[3:] == ["push", "pr_update", "pr_ready", "mark_merged", "phase_done", "wait_checks", "merge",
                                 "mark_done", "mark_done", "mark_done"]
    assert [s["task"] for s in steps if s["kind"] == "mark_done"] == ["a", "b", "c"]
    assert "pr_create" not in _kinds(steps)


def test_an_open_pr_found_by_resume_gives_update_its_number():
    facts = {"pr": 7, "pr_head": "abc", "remote_tip": "abc", "local_tip": "abc"}
    steps = _plan("one", ["a"], phase_pr=facts)
    assert _kinds(steps)[4] == "pr_update" and steps[4]["pr"] == 7


def test_a_phase_with_a_quarantined_task_plans_only_a_refuse_naming_it():
    steps = _plan("one", ["a", "b"], [_task("a"), _task("b", "quarantined")])
    assert _kinds(steps) == ["refuse"] and "b is quarantined" in steps[0]["reason"]


def test_an_empty_phase_branch_gathers_instead_of_a_bare_merge_phase():
    rec = {**_task("a"), "run": "x-1", "build": {"patch": "diff"}}
    steps = _plan("one", ["a"], [rec], branches={"epic/x/one": []})
    assert _kinds(steps)[:3] == ["pr_branch_cut", "patch_apply", "checks"] and "merge_phase" not in _kinds(steps)


def test_a_one_phase_ticket_opens_a_draft_then_readies_it():
    steps = _plan("one", ["a"], phases=["one"])
    assert _kinds(steps)[4:6] == ["pr_create", "pr_ready"] and steps[4]["draft"] is True


def test_gate_ticket_stops_before_the_final_merge_and_full_keeps_it():
    steps = _plan("two", ["c"], ticket_tasks=["c"])
    gated = land.gate_steps(steps, "ticket", "ticket", ["c"])
    assert _kinds(gated)[-2:] == ["wait_checks", "note"] and "merge" not in _kinds(gated)
    assert _kinds(land.gate_steps(steps, "full", "ticket")) == _kinds(steps)
    first = _plan("one", ["a"])
    assert land.gate_steps(first, "ticket", "ticket", ["a", "c"]) == first
