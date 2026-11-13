"""`cox runs land-batch`: the shaping functions, the subcommand with fake effects, and the two new step-runner paths."""

import argparse
from pathlib import Path

import pytest

from agent_tools import cli, land_batch_group, land_batch_squash, land_limit, land_repo_lease, store_cli
from agent_tools import land_batch_effects as fx
from agent_tools.land_batch_run import Effects, Outcome

ITEMS = [{"id": "t1", "status": "approved", "file": "w/t1.md"}, {"id": "t2", "status": "approved", "file": "w/t2.md"},
         {"id": "t3", "status": "done", "file": "w/t3.md"}]
RECORDS = [{"task": "t1", "build": {"files_touched": ["a.py", "b.py"]}}, {"task": "t2", "build": {"files_touched": ["b.py", "c.py"]}},
           {"task": "t3", "build": {"files_touched": ["z.py"]}}]


def test_files_are_the_approved_tasks_files_once_in_order():
    assert fx.source_files(ITEMS, {r["task"]: r for r in RECORDS}) == ["a.py", "b.py", "c.py"]


def test_a_phase_whose_approved_tasks_name_no_files_has_an_empty_list():
    assert fx.source_files(ITEMS, {"t1": {"build": {}}, "t3": {"build": {"files_touched": ["z.py"]}}}) == []


def test_tasks_are_the_approved_ids():
    assert fx.source_tasks(ITEMS) == ["t1", "t2"]


def test_a_phase_is_ready_when_every_task_is_done_or_approved_and_one_is_approved():
    assert fx.phase_ready(ITEMS)
    assert not fx.phase_ready([*ITEMS, {"id": "t4", "status": "ready"}])
    assert not fx.phase_ready([{"id": "t3", "status": "done"}])


def test_build_source_feeds_the_disjoint_grouping():
    a = fx.build_source("i1", "p", "r1", "/repo", ITEMS, RECORDS, {"t1": "/x/t1.json", "t2": "/x/t2.json"})
    b = fx.build_source("i2", "p", "r2", "/repo", [{"id": "u1", "status": "approved", "file": "w/u1.md"}],
                        [{"task": "u1", "build": {"files_touched": ["d.py"]}}], {"u1": "/x/u1.json"})
    c = fx.build_source("i3", "p", "r3", "/repo", [{"id": "v1", "status": "approved"}], [{"task": "v1"}], {})
    assert a["files"] == ["a.py", "b.py", "c.py"] and a["tasks"] == ["t1", "t2"]
    assert a["task_paths"] == {"t1": "/x/t1.json", "t2": "/x/t2.json"} and a["task_items"] == {"t1": "w/t1.md", "t2": "w/t2.md"}
    assert c["files"] == []
    assert land_batch_group.disjoint_batches([a, b, c], 3) == [[a, b], [c]]


def test_source_ids_parse_or_name_the_bad_one():
    assert fx.parse_source_ids(["i/p/r"]) == ([("i", "p", "r")], None)
    assert fx.parse_source_ids([]) == (None, None)
    assert fx.parse_source_ids(["i/p"])[1] == "--source 'i/p' is not initiative/phase/run"


def test_outcome_carries_the_pr_and_a_stop_is_an_error():
    assert fx.outcome_of(0, ["merge"], "#7") == Outcome(None, "#7")
    assert fx.outcome_of(1, ["push"], "").error == "land stopped after push (exit 1)"
    assert fx.plan_error([{"kind": "refuse", "reason": "why"}]) == "why"
    assert fx.plan_error([{"kind": "push"}]) is None


def _src(initiative, files):
    return {"initiative": initiative, "phase": "p", "run": f"{initiative}-1", "repo": "/repo", "files": files, "tasks": [],
            "task_paths": {}, "task_items": {}}


def _args(tmp_path, **over):
    base = {"repo": str(tmp_path), "source": None, "batch_size": 2, "wait_s": 0.0, "force": False, "no_claim": False,
            "base": None, "worktree_root": "~/worktrees", "apply": True, "runs_dir": str(tmp_path),
            "profile": str(tmp_path / "none.yaml"), "gate": None, "label": "t"}
    return argparse.Namespace(**{**base, **over})


@pytest.fixture
def queue(monkeypatch, tmp_path):
    """Fake effects over two disjoint sources; `bad` names the initiatives whose batch fails. Returns the lease calls."""
    state = {"bad": set(), "lease": store_cli.LeaseGranted(1, "me"), "calls": []}
    sources = [_src("i1", ["a.py"]), _src("i2", ["b.py"])]

    def land(group):
        state["calls"].append([s["initiative"] for s in group])
        return Outcome("boom", None) if state["bad"] & {s["initiative"] for s in group} else Outcome(None, "#9")

    monkeypatch.setattr(cli, "_runs_dir_for_land", lambda a: (tmp_path, None))
    monkeypatch.setattr(cli, "_leader_guard_or_refuse", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_holder_label", lambda a: "t")
    monkeypatch.setattr(cli, "_batch_effects", lambda *a: Effects(lambda: sources, land, lambda s: None, lambda: 0.0))
    monkeypatch.setattr(land_limit, "repo_land_limit", lambda repo: 1)
    monkeypatch.setattr(land_repo_lease, "acquire_in_slots", lambda *a: (0, state["lease"]))
    monkeypatch.setattr(land_repo_lease, "release_slot", lambda *a: store_cli.LeaseGranted(1, "me"))
    return state


def test_an_all_landed_batch_prints_each_source_and_exits_0(queue, tmp_path, capsys):
    assert cli._runs_land_batch(_args(tmp_path)) == 0
    assert capsys.readouterr().out == (
        "batch landed i1/p pr=#9\nbatch landed i2/p pr=#9\nbatch summary: ci_runs=1 landed=2 returned=0 deferred=0\n")


def test_a_partly_returned_batch_exits_0(queue, tmp_path, capsys):
    queue["bad"] = {"i2"}
    assert cli._runs_land_batch(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "batch landed i1/p pr=#9" in out and "batch returned i2/p: boom" in out


def test_every_source_returned_exits_1(queue, tmp_path, capsys):
    queue["bad"] = {"i1", "i2"}
    assert cli._runs_land_batch(_args(tmp_path)) == 1
    assert "batch landed" not in capsys.readouterr().out


def test_a_busy_lease_prints_the_land_text_and_exits_2(queue, tmp_path, capsys):
    queue["lease"] = store_cli.LeaseRefused(4, "chair-x@host:1")
    assert cli._runs_land_batch(_args(tmp_path)) == 2
    out = capsys.readouterr().out
    assert out == land_repo_lease.refusal_message(str(tmp_path), "chair-x@host:1") + "\n"


def test_a_malformed_source_exits_2(queue, tmp_path, capsys):
    assert cli._runs_land_batch(_args(tmp_path, source=["i/p"])) == 2
    assert "not initiative/phase/run" in capsys.readouterr().out


def test_a_batch_squash_step_reaches_squash_onto_batch(monkeypatch):
    seen = []
    monkeypatch.setattr(land_batch_squash, "squash_onto_batch", lambda repo, step: (seen.append((repo, step)), (True, "ok"))[1])
    step = {"kind": "batch_squash", "branch": "epic/i/p", "onto": "queue/k", "from": "main", "subject": "s", "source": "i/p", "first": True}
    assert cli._execute_land_step(Path("/repo"), step, object()) == (True, "ok")
    assert seen == [(Path("/repo"), step)]


def test_a_merge_step_passes_its_rebase_method_to_the_forge():
    seen = []

    class Forge:
        @staticmethod
        def merge(repo, step):
            seen.append(step["method"])
            return True, "merged"

    step = {"kind": "merge", "squash": False, "method": "rebase", "delete_branch": True, "branch": "queue/k", "default_branch": "main"}
    assert cli._execute_land_step(Path("/repo"), step, Forge) == (True, "merged")
    assert seen == ["rebase"]
