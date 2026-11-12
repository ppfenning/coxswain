import json
import subprocess

from agent_tools import cli, land

BRANCH = "card/x/wire"


def _phase(**extra):
    return {"run": "epic-x-5", "phase": "seams", "initiative": "x", "card_branch": BRANCH, "last_phase": True, **extra}


def _task(task, status="approved", repo="wire"):
    return {"task": task, "title": task, "repo": repo, "status": status, "review": {"verdict": "approve"}}


def _plan(record, ids, records=None, ticket_tasks=("a", "b", "z"), present=("z",), phases=()):
    items = [{"id": t, "status": "approved"} for t in ids]
    return land.land_plan(record, {}, "main", items=items, task_records=records or [_task(t) for t in ids],
                          repo="wire", ticket_tasks=ticket_tasks, ticket_present=present, phases=phases)


def _kinds(steps):
    return [s["kind"] for s in steps]


def _marked(steps):
    return [s["task"] for s in steps if s["kind"] == "mark_done"]


def test_granularity_defaults_to_initiative():
    values = ("initiative", "ticket", "level", "task", "bogus")
    assert [land.granularity({"land_at": v}) for v in values] == ["initiative", "initiative", "initiative", "task", "initiative"]
    assert land.granularity({}) == "initiative"


def test_ticket_plans_one_pr_and_marks_every_ticket_task_after_the_merge():
    steps = _plan(_phase(), "ab")
    assert _kinds(steps).count("pr_create") == 1
    assert _marked(steps) == ["a", "b", "z"]
    assert _kinds(steps).index("merge") < _kinds(steps).index("mark_done")
    assert "pr_create" not in _kinds(_plan(_phase(last_phase=False), "ab"))
    assert _marked(_plan(_phase(last_phase=False), "ab")) == []


def test_ticket_with_phases_merges_into_one_branch_and_opens_no_pr_after_the_first():
    items = [{"id": "a", "status": "approved"}]
    kw = {"items": items, "task_records": [_task("a")], "repo": "wire", "ticket_tasks": ("a", "z"),
          "ticket_present": ("z",), "phases": ["p1", "p2"]}
    first = land.land_plan(_phase(card_branch="", phase="p1", last_phase=False), {}, "main", **kw)
    last = land.land_plan(_phase(card_branch="", phase="p2"), {}, "main", **kw)
    assert _kinds(first).count("pr_create") == 1 and _kinds(first)[0] == "pr_branch_cut" and _marked(first) == []
    assert "pr_create" not in _kinds(last) and _kinds(last).index("merge") < _kinds(last).index("mark_done")
    assert _marked(last) == ["a", "z"]


def test_ticket_without_a_card_branch_plans_one_pr_for_the_ticket_not_the_phase():
    rec = {**_task("a"), "arbitration": {"verdict": "approve"}}
    phases = ("p1", "p2")
    last = _plan(_phase(card_branch="", phase="p2"), "a", [rec], ("a", "z"), phases=phases)
    early = _plan(_phase(card_branch="", phase="p1", last_phase=False), "a", [rec], ("a", "z"), phases=phases)
    assert "pr_create" not in _kinds(last) and _kinds(last)[-5:-2] == ["wait_checks", "merge", "clean_phase"]
    assert _marked(last) == ["a", "z"]
    assert _kinds(early).count("pr_create") == 1 and "merge" not in _kinds(early) and _marked(early) == []


def test_task_keeps_the_per_task_path():
    steps = _plan(_phase(land_at="task", last_phase=False), "ab")
    assert "pr_create" not in _kinds(steps)
    assert _marked(steps) == ["a", "b"]


def test_task_with_a_card_branch_on_the_last_phase_keeps_the_legacy_plan():
    items = [{"id": t, "status": "approved"} for t in "ab"]
    legacy = land.land_plan(_phase(land_at="task"), {}, "main", items=items, task_records=[_task("a"), _task("b")], repo="wire")
    steps = _plan(_phase(land_at="task"), "ab")
    assert steps == legacy and _kinds(steps).count("pr_create") == 1 and "merge" not in _kinds(steps)


def test_gate_stops_at_the_one_pull_request_for_ticket():
    steps = _plan(_phase(), "ab")
    gated = land.gate_steps(steps, "ticket", "ticket", ["a", "b", "z"])
    assert _kinds(gated)[-2:] == ["pr_create", "note"] and _marked(gated) == []
    epic = land.gate_steps(steps, "epic", "ticket", ["a", "b", "z"])
    assert _kinds(epic)[-2:] == ["wait_checks", "note"] and "merge" not in _kinds(epic) and _marked(epic) == []
    assert _kinds(land.gate_steps(steps, "full", "ticket")) == _kinds(steps)


def test_gate_stop_for_task_keeps_the_configured_level():
    rec = {**_task("a"), "arbitration": {"verdict": "approve"}}
    task = _plan(_phase(card_branch="", land_at="task"), "a", [rec])
    assert _kinds(task).count("pr_create") == 1
    assert land.gate_steps(task, "phase", "task") == land.gate_steps(task, "phase")
    assert land.gate_steps(task, "epic", "task") == land.gate_steps(task, "epic")


def test_a_quarantined_task_in_the_ticket_set_is_not_marked_done():
    steps = _plan(_phase(), "ab", [_task("a"), _task("b", "quarantined")], ("a", "b", "z"))
    assert _marked(steps) == ["a", "z"]
    assert [s["unmerged"] for s in steps if s["kind"] == "note"] == [["b"]]
    assert "- b" not in steps[_kinds(steps).index("pr_create")]["body"]


def test_a_skipped_task_in_the_ticket_set_is_not_marked_done():
    steps = _plan(_phase(), "ab", [_task("a"), _task("b", repo="elsewhere")], ("a", "b", "z"))
    assert _marked(steps) == ["a", "z"]


def test_an_earlier_phase_task_whose_work_is_not_in_the_pr_is_not_marked_done():
    for record, phases in ((_phase(), ()), (_phase(card_branch="", phase="p2"), ("p1", "p2"))):
        steps = _plan(record, "a", [{**_task("a"), "arbitration": {"verdict": "approve"}}], ("a", "y", "z"), present=("z",),
                      phases=phases)
        assert _marked(steps) == ["a", "z"]
        assert [s["unmerged"] for s in steps if s["kind"] == "note"] == [["y"]]
        assert _kinds(steps).index("merge") < _kinds(steps).index("mark_done")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


def _cli_steps(tmp_path, monkeypatch, capsys, stacked, *gate, land_phase="p2-b"):
    repo, work, runs = tmp_path / "wire", tmp_path / "work" / "x", tmp_path / "runs"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "agents/x-1/early")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "early work")
    _git(repo, "checkout", "-q", "-b", "epic/x/p2-b", "agents/x-1/early" if stacked else "main")
    for phase, task in (("p1-a", "early"), ("p2-b", "late")):
        (work / phase).mkdir(parents=True)
        (work / phase / f"{task}.md").write_text(f"---\nid: {task}\nstate: approved\n---\nBody.\n")
        (runs / "x-1" / "tasks" / phase).mkdir(parents=True)
        record = {**_task(task), "run": "x-1", "build": {"files_touched": ["f"]}}
        (runs / "x-1" / "tasks" / phase / f"{task}.json").write_text(json.dumps(record))
    for phase in ("p1-a", "p2-b"):
        (runs / f"x-1:{phase}.json").write_text(json.dumps({"initiative": "x"}))
    monkeypatch.setattr(cli.base_branch, "base_ref", lambda *a, **k: "main")
    monkeypatch.setattr(cli, "_land_repo_facts", lambda *a, **k: {"cartridge_error": None, "checks": [], "cartridge_checks": []})
    monkeypatch.setattr(cli, "_land_refetch_phase_branch", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_repo_is_detached", lambda *a, **k: False)
    cli.main(["runs", "land", "x-1", "--phase", land_phase, "--repo", str(repo), "--runs-dir", str(runs), *gate])
    out = capsys.readouterr().out
    return json.loads(out[out.index("["):])


def test_cli_land_marks_earlier_phase_tasks_done_when_their_work_is_in_the_stacked_branch(tmp_path, monkeypatch, capsys):
    steps = _cli_steps(tmp_path, monkeypatch, capsys, True, "--gate", "full")
    assert _kinds(steps).count("pr_create") == 1
    assert _marked(steps) == ["early", "late"]


def test_cli_land_leaves_an_earlier_phase_task_whose_work_is_not_in_the_branch(tmp_path, monkeypatch, capsys):
    steps = _cli_steps(tmp_path, monkeypatch, capsys, False, "--gate", "full")
    assert _marked(steps) == ["late"]
    assert [s["unmerged"] for s in steps if s["kind"] == "note" and "unmerged" in s] == [["early"]]


def test_cli_land_with_the_default_gate_stops_at_the_one_pull_request(tmp_path, monkeypatch, capsys):
    steps = _cli_steps(tmp_path, monkeypatch, capsys, True)
    assert _kinds(steps)[-2:] == ["pr_create", "note"] and _marked(steps) == []


def test_cli_dry_run_for_a_deferred_phase_plans_no_pr_and_prints_the_note(tmp_path, monkeypatch, capsys):
    steps = _cli_steps(tmp_path, monkeypatch, capsys, True, "--gate", "full", land_phase="p1-a")
    assert not {"pr_create", "merge", "mark_done"} & set(_kinds(steps))
    assert [s["reason"] for s in steps if s["kind"] == "note"] == [
        "phase p1-a: ticket granularity, the one pull request opens at the last phase"]


def test_cli_land_of_the_last_phase_opens_one_pr_covering_the_earlier_phases(tmp_path, monkeypatch, capsys):
    steps = _cli_steps(tmp_path, monkeypatch, capsys, True, "--gate", "full")
    assert _kinds(steps).count("pr_create") == 1
    body = next(s["body"] for s in steps if s["kind"] == "pr_create")
    assert "- early: early" in body and "- late: late" in body
    assert sorted(_marked(steps)) == ["early", "late"]
    assert _kinds(steps).index("merge") < _kinds(steps).index("mark_done")


def _initiative(tmp_path, land_at=None):
    folder = tmp_path / "work" / "x"
    folder.mkdir(parents=True)
    extra = f"land_at: {land_at}\n" if land_at else ""
    (folder / "initiative.md").write_text(f"---\nid: x\n{extra}---\n", encoding="utf-8")
    return tmp_path / "work"


def test_phase_land_at_keeps_the_value_its_run_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({"landing_areas": {"land_at": "initiative"}}, None))
    assert cli._phase_land_at({"land_at": "task"}, {}, _initiative(tmp_path), "x") == "task"


def test_phase_land_at_without_a_recorded_value_reads_the_cartridge_as_the_chair_does(tmp_path, monkeypatch):
    """A run-store phase manifest carries no land_at: the land must not fall back to initiative when the chair plans at task."""
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({"landing_areas": {"land_at": "task"}}, None))
    assert cli._phase_land_at({}, {}, _initiative(tmp_path), "x") == "task"


def test_phase_land_at_prefers_the_initiative_value_over_the_cartridge(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({"landing_areas": {"land_at": "task"}}, None))
    assert cli._phase_land_at({}, {}, _initiative(tmp_path, "ticket"), "x") == "initiative"


def test_phase_land_at_with_no_cartridge_and_no_initiative_value_is_initiative(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({}, None))
    assert cli._phase_land_at({}, {}, tmp_path / "work", "x") == "initiative"
