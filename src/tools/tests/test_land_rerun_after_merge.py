"""A land re-run after the ticket PR merged finishes the bookkeeping instead of refusing on a record count."""
import json

# The store-mode land harness: fakes for the lease and state edge, a temporary git repo.
from test_land_from_store import _own_tempdir, repo, store  # noqa: F401

from agent_tools import cli, route_edit, run_store, store_cli

URL = "https://github.com/o/r/pull/9"
PHASES = {"a1": "p1-a", "a2": "p1-a", "b1": "p2-b", "b2": "p2-b", "c1": "p3-c", "c2": "p3-c"}
# Run x-3 holds all six; x-1 and x-2 hold the earlier copies of the first four.
RUNS = {"x-1": ("a1", "a2"), "x-2": ("b1", "b2"), "x-3": tuple(PHASES)}


def _record(task, run):
    return {"run": run, "task": task, "phase": PHASES[task], "initiative": "x", "status": "approved",
            "review": {"verdict": "approve"}, "build": {"patch": "x"}}


def _build(tmp_path, monkeypatch, store, merged_pr, done_by_hand=("a1", "b1"), unmarkable=()):  # noqa: F811
    """Ticket `x` over three phases and three runs, the land re-run on x-3. `done_by_hand` are state done already.
    Returns `done`, task to pr for each task the land marked, `ran`, the step kinds executed, and the profile."""
    runs, done, ran = tmp_path / "runs", {}, []
    for task, phase in PHASES.items():
        state = "done" if task in done_by_hand else "approved"
        (tmp_path / "work/x" / phase).mkdir(parents=True, exist_ok=True)
        (tmp_path / f"work/x/{phase}/{task}.md").write_text(f"---\nid: {task}\nstate: {state}\n---\nBody.\n", encoding="utf-8")
    for run, tasks in RUNS.items():
        for task in tasks:
            (runs / run / "tasks" / PHASES[task]).mkdir(parents=True, exist_ok=True)
            (runs / run / "tasks" / PHASES[task] / f"{task}.json").write_text(json.dumps(_record(task, run)), encoding="utf-8")
    for phase in ("p1-a", "p2-b", "p3-c"):
        (runs / f"x-3:{phase}.json").write_text(json.dumps({"initiative": "x", "phase_verdict": {"reasoning": "solid"}}), encoding="utf-8")
    store.item = tmp_path / "work/x/p3-c/c1.md"
    provider = tmp_path / "provider.yaml"
    provider.write_text("work_state: store\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"provider_profile: {provider}\n", encoding="utf-8")
    rows = [{"task_id": t, "initiative": "x", "state": "done" if t in done_by_hand else "approved"} for t in PHASES if t not in unmarkable]

    def fake_set_state(runs_dir, initiative, task, state, by, expected=None):
        done.setdefault(task, None)
        return store_cli.StateSet({"state": state})

    def fake_mark_done_with_pr(runs_dir, row, pr, now):
        done[row["task_id"]] = pr

    def step(_repo, step, _forge=None):
        ran.append(step["kind"])
        if step["kind"] == "mark_done":
            return (False, "boom") if step["task"] in unmarkable else real(_repo, step, _forge)
        return True, URL if step["kind"] == "pr_create" else "ok"

    real = cli._execute_land_step
    monkeypatch.setattr(cli, "_execute_land_step", step)
    monkeypatch.setattr(store_cli, "set_state", fake_set_state)
    monkeypatch.setattr(cli, "_mirror_landed", lambda step: done.__setitem__(step["task"], step["pr"]))
    monkeypatch.setattr(route_edit, "mark_done_with_pr", fake_mark_done_with_pr)
    monkeypatch.setattr(run_store, "task_state_of", lambda _runs, _initiative, task: "done" if task in done_by_hand else "approved")
    monkeypatch.setattr(run_store, "read_queue", lambda *a, **k: rows)
    monkeypatch.setattr(run_store, "run_task_ids", lambda *a: [])
    monkeypatch.setattr(run_store, "task_record", lambda *a: None)
    monkeypatch.setattr(cli, "_phase_merged_pr", lambda *a: merged_pr)
    monkeypatch.setattr(cli, "_phase_pr_facts", lambda *a: None)
    monkeypatch.setattr(cli, "_phase_task_facts", lambda *a: [])
    monkeypatch.setattr(cli, "_ticket_present", lambda _repo, _head, records: {r["task"] for r in records})
    monkeypatch.setattr(cli, "_merge_updating", lambda *a, **k: (True, "merged", None))
    monkeypatch.setattr(cli, "_close_review_prs", lambda *a: [])
    monkeypatch.setattr(cli.base_branch, "base_ref", lambda *a, **k: "main")
    monkeypatch.setattr(cli, "_land_repo_facts", lambda *a, **k: {"cartridge_error": None, "checks": [], "cartridge_checks": []})
    monkeypatch.setattr(cli, "_land_refetch_phase_branch", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_repo_is_detached", lambda *a, **k: False)
    return done, ran, profile


def _rerun(repo, tmp_path, profile, *extra):  # noqa: F811
    return cli.main(["runs", "land", "x-3", *extra, "--repo", str(repo), "--apply", "--gate", "full",
                     "--profile", str(profile), "--runs-dir", str(tmp_path / "runs")])


def test_a_rerun_after_merge_marks_every_task_not_yet_done_with_the_merged_pr(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    done, ran, profile = _build(tmp_path, monkeypatch, store, {"number": 535, "state": "merged"})
    assert _rerun(repo, tmp_path, profile) == 0
    assert done == {"a2": "#535", "b2": "#535", "c1": "#535", "c2": "#535"}
    assert not {"cherry_pick", "squash_phase", "checks", "push", "pr_create", "merge"} & set(ran)
    assert "expected 1" not in capsys.readouterr().out


def test_a_second_rerun_changes_nothing(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    done, ran, profile = _build(tmp_path, monkeypatch, store, {"number": 535, "state": "merged"}, done_by_hand=tuple(PHASES))
    assert _rerun(repo, tmp_path, profile) == 0
    assert done == {}
    assert "mark_done" not in ran
    assert "expected 1" not in capsys.readouterr().out


def test_the_merged_pr_url_is_the_ref_when_the_forge_gave_one(repo, tmp_path, monkeypatch, store):  # noqa: F811
    done, _, profile = _build(tmp_path, monkeypatch, store, {"number": 535, "state": "merged", "url": URL})
    assert _rerun(repo, tmp_path, profile) == 0
    assert set(done.values()) == {URL}


def test_a_task_that_cannot_be_marked_is_reported_and_the_exit_is_not_zero(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    done, _, profile = _build(tmp_path, monkeypatch, store, {"number": 535, "state": "merged"}, unmarkable=("a2",))
    assert _rerun(repo, tmp_path, profile) != 0
    assert "a2 not marked done" in capsys.readouterr().out
    assert done == {"b2": "#535", "c1": "#535", "c2": "#535"}


def test_several_files_for_one_task_prefer_the_own_run_then_the_newest_earlier_run(tmp_path):
    runs = tmp_path / "runs"
    for run in ("x-1", "x-2", "x-3"):
        (runs / run / "tasks/p1-a").mkdir(parents=True)
        (runs / run / "tasks/p1-a/a1.json").write_text(json.dumps({"run": run, "task": "a1", "phase": "p1-a"}), encoding="utf-8")
    record, where, count = cli._land_record(runs, "x-3", "a1")
    assert (record["run"], count) == ("x-3", 1)
    assert where == str(runs / "x-3/tasks/p1-a/a1.json")
    (runs / "x-3/tasks/p1-a/a1.json").unlink()
    assert cli._land_record(runs, "x-3", "a1")[0]["run"] == "x-2"
    assert cli._land_record(runs, "x-3", "nope")[2] == 0
