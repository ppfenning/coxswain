"""A ticket phase land merges into the one `pr/<initiative>` branch.

Payload keys the executor reads:

- `pr_branch_cut`: `branch` (pr/<initiative>), `from` (default branch), `phase`, `if_missing` True. Create `branch`
  from `from` only when it does not exist; never reset a branch that exists. Planned on the first phase when the
  branch has no tip. A later phase reuses the branch and plans no cut, or refuses when the branch is gone.
- `merge_phase`: `branch` (epic/<initiative>/<phase>), `onto` (the pr branch), `phase`, `message` (the merge commit
  message, naming the phase). A local merge commit, never a squash.
- gather steps (`cherry_pick`, `patch_apply`) in place of `merge_phase`: `onto` is the pr branch and `keep_branch` is
  True, so the executor must not recreate the branch from `from`.
- `pr_create`: `title`, `body`, `head` (pr branch), `base` (default branch), `draft` True. First phase only.
- `pr_update`: `head`, `base`, `title`, `body`, and `pr` (the number) when an open PR was found by its head branch.
- `pr_ready`: `head`, and `pr` when known. Last phase only, after `pr_update`.
- `mark_merged`: `task`, `merged` True. The task state stays approved. The flag means the work is on the pr branch,
  not on the default branch; only the last phase's `mark_done` means the PR merged.
- `phase_done`: `initiative`, `phase`, `pr_branch`. Every plan of this shape carries it, and `gate_steps` keys on it.

The last phase then plans `wait_checks` (`branch` is the PR head, `pr` when known), `merge` (squash of the pr branch),
`clean_phase` and `mark_done` per ticket task. Those `mark_done` steps are the ones the legacy ticket plan emits,
`recorded: False` included. The PR url that the ticket merge records on each task is resolved by the executor from
the PR head (`head` or `branch` is the pr branch) or from `pr`, since only the first phase's `pr_create` returns a url.
"""
from agent_tools import land

PHASES = ["seams", "wiring"]
TASKS = ("a", "b", "z")


def _phase(phase, **extra):
    return {"run": "epic-x-5", "phase": phase, "initiative": "x", **extra}


def _task(task, status="approved"):
    return {"task": task, "title": task, "repo": "wire", "status": status, "review": {"verdict": "approve"}}


def _plan(phase, ids="ab", records=None, branches=None, **kw):
    items = [{"id": t, "status": "approved"} for t in ids]
    return land.land_plan(_phase(phase), branches or {}, "main", items=items, task_records=records or [_task(t) for t in ids],
                          repo="wire", ticket_tasks=TASKS, ticket_present=("z",), phases=PHASES, **kw)


def _kinds(steps):
    return [s["kind"] for s in steps]


def test_phase_one_of_two_cuts_merges_pushes_and_opens_a_draft_pr():
    steps = _plan("seams")
    assert _kinds(steps) == ["pr_branch_cut", "merge_phase", "checks", "push", "pr_create", "mark_merged", "mark_merged", "phase_done"]
    assert steps[0] == {"kind": "pr_branch_cut", "branch": "pr/x", "from": "main", "phase": "seams", "if_missing": True}
    assert steps[1] == {"kind": "merge_phase", "branch": "epic/x/seams", "onto": "pr/x", "phase": "seams",
                        "message": "epic x: merge phase seams"}
    create = steps[4]
    assert (create["draft"], create["head"], create["base"]) == (True, "pr/x", "main")
    assert steps[5] == {"kind": "mark_merged", "task": "a", "merged": True}
    assert steps[-1] == {"kind": "phase_done", "initiative": "x", "phase": "seams", "pr_branch": "pr/x"}


def test_phase_two_reuses_the_branch_and_opens_no_pr():
    open_pr = {"local_tip": "t", "remote_tip": "t", "pr": 9, "pr_head": "t"}
    for facts in (None, open_pr):
        steps = _plan("wiring", phase_pr=facts)
        assert "pr_create" not in _kinds(steps) and "pr_branch_cut" not in _kinds(steps)
    kinds = _kinds(steps)
    assert kinds[kinds.index("pr_update"):kinds.index("pr_update") + 2] == ["pr_update", "pr_ready"]
    assert steps[kinds.index("pr_update")]["pr"] == 9


def test_the_last_phase_ends_in_merge_and_marks_every_ticket_task_done():
    steps = _plan("wiring")
    kinds = _kinds(steps)
    assert kinds[kinds.index("pr_update"):kinds.index("pr_update") + 2] == ["pr_update", "pr_ready"]
    assert kinds[-6:] == ["wait_checks", "merge", "clean_phase", "mark_done", "mark_done", "mark_done"]
    assert kinds.index("merge") < kinds.index("mark_done")
    assert [s["task"] for s in steps if s["kind"] == "mark_done"] == ["a", "b", "z"]
    assert steps[kinds.index("merge")]["branch"] == "pr/x"


def test_a_task_whose_work_is_not_in_the_pr_gets_the_note_not_a_mark():
    steps = land.land_plan(_phase("wiring"), {}, "main", items=[{"id": "a", "status": "approved"}], task_records=[_task("a")],
                           repo="wire", ticket_tasks=("a", "y", "z"), ticket_present=("z",), phases=PHASES)
    assert [s["unmerged"] for s in steps if s["kind"] == "note"] == [["y"]]
    assert [s["task"] for s in steps if s["kind"] == "mark_done"] == ["a", "z"]


def test_a_quarantined_task_plans_only_a_refuse_naming_it():
    steps = _plan("seams", records=[_task("a"), _task("b", "quarantined")])
    assert _kinds(steps) == ["refuse"] and "b is quarantined" in steps[0]["reason"]
    items = [{"id": "a", "status": "approved"}, {"id": "b", "status": "quarantined"}]
    steps = land.land_plan(_phase("seams"), {}, "main", items=items, task_records=[_task("a")], repo="wire",
                           ticket_tasks=TASKS, phases=PHASES)
    assert _kinds(steps) == ["refuse"] and "b is quarantined" in steps[0]["reason"]


def test_an_empty_phase_branch_gathers_onto_the_kept_pr_branch():
    record = {**_task("a"), "run": "r1", "build": {"patch": "diff"}}
    steps = _plan("wiring", ids="a", records=[record], branches={"epic/x/wiring": []})
    assert "merge_phase" not in _kinds(steps)
    gather = steps[_kinds(steps).index("patch_apply")]
    assert (gather["onto"], gather["keep_branch"]) == ("pr/x", True)


def test_gate_ticket_stops_before_the_final_merge_and_full_keeps_it():
    last = _plan("wiring")
    gated = land.gate_steps(last, "ticket", "ticket", TASKS)
    assert _kinds(gated)[-2:] == ["phase_done", "note"] and "merge" not in _kinds(gated)
    assert _kinds(land.gate_steps(last, "full", "ticket")) == _kinds(last)
    first = _plan("seams")
    assert land.gate_steps(first, "ticket", "ticket", TASKS) == first


def test_gate_ticket_stops_before_the_merge_of_a_last_phase_that_gathers():
    record = {**_task("a"), "run": "r1", "build": {"patch": "diff"}}
    for facts in (None, {"local_tip": "t", "remote_tip": "t", "pr": 9, "pr_head": "t"}):
        last = _plan("wiring", ids="a", records=[record], branches={"epic/x/wiring": []}, phase_pr=facts)
        assert not {"pr_branch_cut", "merge_phase"} & set(_kinds(last)) and "merge" in _kinds(last)
        gated = land.gate_steps(last, "ticket", "ticket", TASKS)
        assert _kinds(gated)[-2:] == ["phase_done", "note"]
        assert not {"wait_checks", "merge", "mark_done"} & set(_kinds(gated))
        assert _kinds(land.gate_steps(last, "full", "ticket")) == _kinds(last)


def test_a_first_phase_rerun_reuses_a_branch_that_has_a_tip_and_resumes_its_open_pr():
    tips = {"local_tip": "t", "remote_tip": "t"}
    kinds = _kinds(_plan("seams", phase_pr=tips))
    assert "pr_branch_cut" not in kinds and "pr_create" in kinds
    steps = _plan("seams", phase_pr={**tips, "pr": 9, "pr_head": "t"})
    assert "pr_branch_cut" not in _kinds(steps) and "pr_create" not in _kinds(steps)
    assert steps[_kinds(steps).index("pr_update")]["pr"] == 9


def test_a_later_phase_finding_no_pr_branch_refuses_and_a_diverged_open_pr_refuses():
    gone = _plan("wiring", phase_pr={"local_tip": None, "remote_tip": None})
    assert _kinds(gone) == ["refuse"] and "pr/x does not exist" in gone[0]["reason"]
    diverged = _plan("wiring", phase_pr={"local_tip": "t", "remote_tip": "t", "pr": 9, "pr_head": "other"})
    assert _kinds(diverged) == ["refuse"] and "#9" in diverged[0]["reason"]


def test_the_last_phase_marks_the_same_tasks_done_as_the_legacy_ticket_plan():
    kw = {"items": [{"id": "a", "status": "approved"}], "task_records": [_task("a")], "repo": "wire",
          "ticket_tasks": ("a", "u", "y", "z"), "ticket_present": ("z",), "ticket_unrecorded": ("u",)}
    legacy = land.land_plan(_phase("wiring", last_phase=True), {}, "main", **kw)
    new = land.land_plan(_phase("wiring"), {}, "main", phases=PHASES, **kw)
    closing = [s for s in new if s["kind"] in ("mark_done", "note")]
    assert closing == [s for s in legacy if s["kind"] in ("mark_done", "note")]
    assert {"kind": "mark_done", "task": "u", "recorded": False} in closing
    assert _kinds(new).index("merge") < _kinds(new).index("mark_done")
