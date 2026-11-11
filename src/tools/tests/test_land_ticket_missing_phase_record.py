"""A ticket land names a record file that exists for every mark_done, or marks from the store, or refuses in the dry run."""
import json
from pathlib import Path

# The store-mode land harness: fakes for the lease and state edge, a temporary git repo.
from test_land_from_store import _own_tempdir, repo, store  # noqa: F401

from agent_tools import cli, land, route_edit, run_store, store_cli

URL = "https://github.com/o/r/pull/9"
PHASES = ["p1-a", "p2-b", "p3-c", "p4-d"]
# task: (phase, run holding its record). Runs 1, 2 and 3, four phases, six tasks.
TASKS = {"a": ("p1-a", "x-1"), "b": ("p2-b", "x-1"), "c": ("p2-b", "x-2"),
         "d": ("p3-c", "x-2"), "e": ("p3-c", "x-3"), "f": ("p4-d", "x-3")}


def _build(tmp_path, monkeypatch, store):  # noqa: F811
    """Ticket `x`, land run x-3 on its last phase `p4-d`, whose phase record lives only in the store: no `x-3:p4-d.json`.
    Returns `done`, task to pr for each task the store moved to done, and the profile path."""
    runs, done = tmp_path / "runs", {}
    for task, (phase, run) in TASKS.items():
        (tmp_path / "work/x" / phase).mkdir(parents=True, exist_ok=True)
        (tmp_path / f"work/x/{phase}/{task}.md").write_text(f"---\nid: {task}\nstate: approved\n---\nBody.\n", encoding="utf-8")
        (runs / run / "tasks" / phase).mkdir(parents=True, exist_ok=True)
        record = {"run": run, "task": task, "phase": phase, "initiative": "x", "status": "approved",
                  "review": {"verdict": "approve"}, "build": {"patch": "x"}}
        (runs / run / "tasks" / phase / f"{task}.json").write_text(json.dumps(record), encoding="utf-8")
    body = {"initiative": "x", "phase_verdict": {"reasoning": "solid"}}
    for phase in PHASES[:-1]:
        (runs / f"x-3:{phase}.json").write_text(json.dumps(body), encoding="utf-8")
    real_manifests = run_store.phase_manifests
    store.item = tmp_path / "work/x/p4-d/f.md"
    provider = tmp_path / "provider.yaml"
    provider.write_text("work_state: store\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"provider_profile: {provider}\n", encoding="utf-8")

    def fake_set_state(runs_dir, initiative, task, state, by, expected=None):
        done.setdefault(task, None)
        return store_cli.StateSet({"state": state})

    def fake_mark_done_with_pr(runs_dir, row, pr, now):
        done[row["task_id"]] = pr

    real = cli._execute_land_step
    monkeypatch.setattr(cli, "_execute_land_step",
                        lambda _repo, step, _forge=None: real(_repo, step, _forge) if step["kind"] == "mark_done"
                        else (True, URL if step["kind"] == "pr_create" else "ok"))
    monkeypatch.setattr(store_cli, "set_state", fake_set_state)
    monkeypatch.setattr(cli, "_mirror_landed", lambda step: done.__setitem__(step["task"], step["pr"]))
    monkeypatch.setattr(route_edit, "mark_done_with_pr", fake_mark_done_with_pr)
    monkeypatch.setattr(run_store, "phase_manifests",
                        lambda d, r: [*real_manifests(d, r), {**body, "run_id": f"{r}:p4-d"}] if r == "x-3" else real_manifests(d, r))
    monkeypatch.setattr(run_store, "read_queue", lambda *a, **k: [{"task_id": t, "initiative": "x", "state": "approved"} for t in TASKS])
    monkeypatch.setattr(run_store, "run_task_ids", lambda *a: [])
    monkeypatch.setattr(run_store, "task_record", lambda *a: None)
    monkeypatch.setattr(cli, "_phase_merged_pr", lambda *a: None)
    monkeypatch.setattr(cli, "_phase_pr_facts", lambda *a: None)
    monkeypatch.setattr(cli, "_phase_task_facts", lambda *a: [])
    monkeypatch.setattr(cli, "_ticket_present", lambda _repo, _head, records: {r["task"] for r in records})
    monkeypatch.setattr(cli, "_merge_updating", lambda *a, **k: (True, "merged", None))
    monkeypatch.setattr(cli, "_close_review_prs", lambda *a: [])
    monkeypatch.setattr(cli.base_branch, "base_ref", lambda *a, **k: "main")
    monkeypatch.setattr(cli, "_land_repo_facts", lambda *a, **k: {"cartridge_error": None, "checks": [], "cartridge_checks": []})
    monkeypatch.setattr(cli, "_land_refetch_phase_branch", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_repo_is_detached", lambda *a, **k: False)
    return done, profile


def _land(repo, tmp_path, profile, *flags):  # noqa: F811
    return cli.main(["runs", "land", "x-3", "--phase", "p4-d", "--repo", str(repo), "--gate", "full",
                     "--profile", str(profile), "--runs-dir", str(tmp_path / "runs"), *flags])


def _plan(out: str) -> list[dict]:
    return json.JSONDecoder().raw_decode(out[out.index("[\n"):])[0]


def test_the_dry_run_names_only_record_files_that_exist(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    _, profile = _build(tmp_path, monkeypatch, store)
    assert _land(repo, tmp_path, profile) == 0
    captured = capsys.readouterr()
    marks = [s for s in _plan(captured.out) if s["kind"] == "mark_done"]
    assert sorted(m["task"] for m in marks) == sorted(TASKS)
    assert all(Path(m["path"]).is_file() for m in marks if "path" in m)
    assert {m["task"]: Path(m["path"]).parents[2].name for m in marks} == {t: run for t, (_, run) in TASKS.items()}
    assert "Traceback" not in captured.out + captured.err
    assert not (tmp_path / "runs/x-3:p4-d.json").exists()


def test_the_applied_land_marks_all_six_tasks_done_with_the_pr(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    done, profile = _build(tmp_path, monkeypatch, store)
    assert _land(repo, tmp_path, profile, "--apply") == 0
    captured = capsys.readouterr()
    assert done == dict.fromkeys(TASKS, URL)
    assert "Traceback" not in captured.out + captured.err


def test_a_task_with_no_record_file_and_no_approved_item_refuses_the_dry_run(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    _, profile = _build(tmp_path, monkeypatch, store)
    plan_of = land.land_plan
    # A mark_done for a task the ticket has no item for: the plan cannot produce one from approved items, so add it.
    monkeypatch.setattr(land, "land_plan", lambda *a, **k: [*plan_of(*a, **k), {"kind": "mark_done", "task": "ghost"}])
    assert _land(repo, tmp_path, profile) == 2
    refusals = [s for s in _plan(capsys.readouterr().out) if s["kind"] == "refuse"]
    assert len(refusals) == 1
    assert "ghost" in refusals[0]["reason"] and "x-3:p4-d.json" in refusals[0]["reason"]
    assert not (tmp_path / "runs/x-3:p4-d.json").exists()


def test_a_missing_record_with_an_approved_item_marks_from_the_store():
    steps = cli._land_enrich([{"kind": "mark_done", "task": "t"}], path="/nowhere/x-3:p.json", worktree_root="w",
                             task_paths={}, task_items={"t": "work/x/p/t.md"}, store_runs_dir="runs", check_paths=True)
    assert steps == [{"kind": "mark_done", "task": "t", "recorded": False, "item": "work/x/p/t.md",
                      "from": "approved", "to": "done", "runs_dir": "runs"}]
