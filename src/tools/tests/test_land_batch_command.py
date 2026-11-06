"""The land-batch command: source shaping with literals, the subcommand with fake effects, and the two step kinds."""

import argparse
import json
from pathlib import Path

from agent_tools import cli
from agent_tools.land_batch_effects import build_effects, parse_source, ready_source
from agent_tools.land_batch_group import disjoint_batches
from agent_tools.land_batch_run import Effects, Outcome
from agent_tools.land_batch_settings import MergeQueue

ITEMS = [{"id": "t1", "status": "approved"}, {"id": "t2", "status": "done"}, {"id": "t3", "status": "approved"}]
RECORDS = {"t1": {"build": {"files_touched": ["b.py", "a.py"]}}, "t3": {"build": {"files_touched": ["a.py", "c.py"]}}}


def test_ready_source_names_approved_tasks_and_sorted_files():
    got = ready_source("i", "p", "r-1", "/repo", ITEMS, RECORDS)
    assert got == {"initiative": "i", "phase": "p", "run": "r-1", "repo": "/repo", "files": ["a.py", "b.py", "c.py"], "tasks": ["t1", "t3"]}


def test_ready_source_without_files_is_unknown_and_unfinished_phase_is_not_ready():
    assert ready_source("i", "p", "r", "/repo", ITEMS, {})["files"] == []
    assert ready_source("i", "p", "r", "/repo", [*ITEMS, {"id": "t4", "status": "building"}], RECORDS) is None
    assert ready_source("i", "p", "r", "/repo", [{"id": "t2", "status": "done"}], RECORDS) is None


def test_parse_source():
    assert parse_source("i/p/r-1") == ("i", "p", "r-1")
    assert parse_source("i/p") is None and parse_source("i//r") is None


def test_disjoint_input_groups_by_files():
    a = ready_source("i1", "p", "r", "/repo", ITEMS, RECORDS)
    b = ready_source("i2", "p", "r", "/repo", [{"id": "u", "status": "approved"}], {"u": {"build": {"files_touched": ["z.py"]}}})
    c = ready_source("i3", "p", "r", "/repo", [{"id": "v", "status": "approved"}], {"v": {"build": {"files_touched": ["a.py"]}}})
    assert [[s["initiative"] for s in g] for g in disjoint_batches([a, b, c], 3)] == [["i1", "i2"], ["i3"]]


def test_build_effects_plans_with_record_paths_and_returns_pr():
    seen = []

    def run_steps(steps, tickets):
        seen.append((steps, dict(tickets)))
        return None, "https://x/pull/9"

    items = [{"id": "t1", "status": "approved", "file": "/w/t1.md"}]
    fx = build_effects([("i", "p", "r")], "/repo", lambda *_: (items, RECORDS, {"t1": "/runs/r/tasks/p/t1.json"}), run_steps,
                       {"checks": []}, "main", lambda s: None, lambda: 0.0)
    batch = fx.read_sources()
    assert fx.land(batch) == Outcome(None, "https://x/pull/9")
    steps, tickets = seen[0]
    assert tickets == {"t1": "/w/t1.md"}
    assert steps[0]["kind"] == "batch_squash" and {"kind": "mark_done", "task": "t1", "path": "/runs/r/tasks/p/t1.json"} in steps


def _src(name):
    return {"initiative": name, "phase": "p", "run": "r", "repo": "/repo", "files": [name], "tasks": []}


def _run(monkeypatch, capsys, outcomes, refuse=False):
    sources = [_src("a"), _src("b")]
    fx = Effects(lambda: sources, lambda batch: Outcome(*outcomes[tuple(x["initiative"] for x in batch)]), lambda s: None, lambda: 0.0)
    job = cli._BatchJob(Path("/repo"), Path("/runs"), MergeQueue(2, 0.0), lambda guard, lock: fx)
    monkeypatch.setattr(cli, "_land_batch_prepare", lambda a: job)
    monkeypatch.setattr(cli.land_limit, "repo_land_limit", lambda repo: 1)
    refused = object.__new__(cli.store_cli.LeaseRefused)
    object.__setattr__(refused, "holder", "other")
    monkeypatch.setattr(cli.land_repo_lease, "acquire_in_slots", lambda *args: (0, refused if refuse else object()))
    rc = cli._runs_land_batch(argparse.Namespace(label="chair"))
    return rc, capsys.readouterr().out


def test_all_landed_exits_0(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, {("a", "b"): (None, "https://x/pull/9")})
    assert rc == 0
    assert "batch landed a/p pr=https://x/pull/9" in out and "batch summary: ci_runs=1 landed=2 returned=0 deferred=0" in out


def test_partly_returned_exits_0(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, {("a", "b"): ("boom", None), ("a",): ("conflict in a", None), ("b",): (None, "https://x/pull/10")})
    assert rc == 0
    assert "batch returned a/p: conflict in a" in out and "batch landed b/p pr=https://x/pull/10" in out


def test_every_source_returned_exits_1(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, {("a", "b"): ("boom", None), ("a",): ("x", None), ("b",): ("y", None)})
    assert rc == 1 and "landed=0 returned=2" in out


def test_busy_lease_prints_busy_text_and_exits_2(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, {("a", "b"): (None, "pr")}, refuse=True)
    assert rc == 2
    assert cli.land_repo_lease.refusal_message("/repo", "other") in out
    assert "batch landed" not in out


def _granted(epoch=3):
    got = object.__new__(cli.store_cli.LeaseGranted)
    object.__setattr__(got, "epoch", epoch)
    return got


def test_lease_is_taken_after_collecting_and_released_per_batch(monkeypatch, capsys):
    events = []
    sources = [_src("a"), _src("b")]

    def read():
        events.append("read")
        return sources

    fx = Effects(read, lambda batch: events.append("land") or Outcome(None, "pr"), lambda s: None, lambda: 0.0)
    job = cli._BatchJob(Path("/repo"), Path("/runs"), MergeQueue(2, 0.0), lambda guard, lock: fx)
    monkeypatch.setattr(cli, "_land_batch_prepare", lambda a: job)
    monkeypatch.setattr(cli.land_limit, "repo_land_limit", lambda repo: 1)
    monkeypatch.setattr(cli.land_repo_lease, "acquire_in_slots", lambda *args: events.append(("acquire", args[1:3])) or (2, _granted()))
    monkeypatch.setattr(cli.land_repo_lease, "renew_slot", lambda *args: _granted())
    monkeypatch.setattr(cli.land_repo_lease, "release_slot", lambda *args: events.append(("release", args[1:3], args[4])))
    assert cli._runs_land_batch(argparse.Namespace(label="chair")) == 0
    assert [e if isinstance(e, str) else e[0] for e in events] == ["read", "acquire", "land", "release"]
    assert events[1][1][0] == "/repo" and events[3] == ("release", ("/repo", 2), 3)


def test_each_batch_syncs_the_default_branch_before_its_steps(monkeypatch):
    events, walked = [], []
    monkeypatch.setattr(cli, "_sync_default_branch", lambda repo, branch: events.append("sync") or ("fast_forward", "moved"))
    monkeypatch.setattr(cli, "_land_execute", lambda *a, **k: walked.append(a[1]) or events.append("walk") or (0, ["merge"], "https://x/pull/1"))
    monkeypatch.setattr(cli, "_append_land_log", lambda runs_dir, row: events.append(("log", row["run"], row["pr"])))
    monkeypatch.setattr(cli, "_lake_after_land", lambda *a: None)
    monkeypatch.setattr(cli, "_holder_label", lambda a: "chair")
    steps = [{"kind": "push", "branch": "queue/k"}, {"kind": "mark_done", "task": "t1", "path": "/r/r/tasks/p/t1.json"}]
    for _ in range(2):
        got = cli._land_batch_steps(argparse.Namespace(worktree_root="/wt"), Path("/repo"), Path("/runs"), "main", object(), "files", None,
                                    steps, {"t1": "/w/t1.md"}, None, None)
        assert got == (None, "https://x/pull/1")
    assert events == ["sync", "walk", ("log", "queue/k", "https://x/pull/1")] * 2
    assert walked[1][1]["item"] == "/w/t1.md" and walked[1][1]["path"] == "/r/r/tasks/p/t1.json"


def test_a_refused_sync_returns_the_batch_without_running_a_step(monkeypatch):
    monkeypatch.setattr(cli, "_sync_default_branch", lambda repo, branch: ("refuse", "main has diverged"))
    monkeypatch.setattr(cli, "_land_execute", lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked")))
    got = cli._land_batch_steps(argparse.Namespace(worktree_root="/wt"), Path("/repo"), Path("/runs"), "main", object(), "files", None, [], {}, None, None)
    assert got == ("cannot sync main: main has diverged", "")


def _prepare_env(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_runs_dir_for_land", lambda a: (tmp_path / "runs", None))
    monkeypatch.setattr(cli, "_read_text_or_none", lambda p: None)
    monkeypatch.setattr(cli, "_profile_path", lambda a: tmp_path / "profile.yaml")
    monkeypatch.setattr(cli.base_branch, "base_ref", lambda repo, flag=None: "origin/main")
    monkeypatch.setattr(cli.forge, "forge_for", lambda name: object())
    monkeypatch.setattr(cli.forge, "forge_name", lambda profile: "local")
    monkeypatch.setattr(cli, "_land_repo_facts", lambda *a: {})
    monkeypatch.setattr(cli, "_land_phase_record", lambda runs_dir, run, phase: (
        {"run": run}, [{"task": "t1", "build": {"files_touched": ["a.py"]}}], {"t1": "/runs/r/tasks/p/t1.json"}, "x"))
    monkeypatch.setattr(cli, "_phase_items", lambda *a, **k: ([{"id": "t1", "status": "approved", "file": "/w/t1.md"}], "/w"))
    monkeypatch.setattr(cli, "_holder_label", lambda a: "chair")
    monkeypatch.setattr(cli, "_leader_guard_or_refuse", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_lake_provider", lambda a: ({}, None))
    monkeypatch.setattr(cli.work_state, "work_state_mode", lambda provider: "files")


def _ns(**over):
    base = {"repo": "/repo", "source": ["i/p/r"], "apply": False, "batch_size": None, "wait_s": None, "force": False, "no_claim": False,
            "worktree_root": "/wt", "label": "chair"}
    return argparse.Namespace(**{**base, **over})


def test_prepare_refuses_a_malformed_or_missing_source(monkeypatch, tmp_path, capsys):
    _prepare_env(monkeypatch, tmp_path)
    assert cli._land_batch_prepare(_ns(source=["i/p"])) == 2
    assert "i/p" in capsys.readouterr().out
    assert cli._land_batch_prepare(_ns(source=[])) == 2
    assert "none given" in capsys.readouterr().out


def test_prepare_dry_run_reads_the_phase_and_prints_the_batches(monkeypatch, tmp_path, capsys):
    _prepare_env(monkeypatch, tmp_path)
    assert cli._land_batch_prepare(_ns(source=["i/p/r"], batch_size=2)) == 0
    assert json.loads(capsys.readouterr().out) == {"batches": [["i/p"]]}


def test_prepare_refuses_a_dirty_repo_when_applying(monkeypatch, tmp_path, capsys):
    _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_repo_is_dirty", lambda repo: True)
    assert cli._land_batch_prepare(_ns(apply=True)) == 2
    assert "dirty" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_repo_is_dirty", lambda repo: False)
    assert isinstance(cli._land_batch_prepare(_ns(apply=True, batch_size=2, wait_s=5.0)), cli._BatchJob)


def test_batch_squash_step_reaches_squash_onto_batch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.land_batch_squash, "squash_onto_batch", lambda repo, step: calls.append((repo, step)) or (True, "ok"))
    step = {"kind": "batch_squash", "branch": "epic/i/p", "onto": "queue/k", "from": "main", "subject": "s", "source": "i/p", "first": True}
    assert cli._execute_land_step(Path("/repo"), step) == (True, "ok")
    assert calls == [(Path("/repo"), step)]


def test_merge_step_passes_rebase_method_to_forge():
    class Forge:
        seen = None

        def merge(self, repo, step):
            Forge.seen = step
            return True, "merged"

    step = {"kind": "merge", "method": "rebase", "squash": False, "branch": "queue/k"}
    assert cli._execute_land_step(Path("/repo"), step, Forge()) == (True, "merged")
    assert Forge.seen["method"] == "rebase"
