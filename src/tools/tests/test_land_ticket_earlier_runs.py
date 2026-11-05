"""A ticket land marks every approved ticket task done with the PR, including tasks approved in an earlier run or built elsewhere."""
import json

# The store-mode land harness: fakes for the lease and state edge, a temporary git repo.
from test_land_from_store import _own_tempdir, repo, store  # noqa: F401

from agent_tools import cli, route_edit, run_store, store_cli

URL = "https://github.com/o/r/pull/9"
TASKS = {"early": "p1-a", "remote": "p1-a", "late": "p2-b"}


def _record(task, run):
    return {"run": run, "task": task, "phase": TASKS[task], "initiative": "x", "status": "approved",
            "review": {"verdict": "approve"}, "build": {"patch": "x"}}


def _build(tmp_path, monkeypatch, store, merged_pr):  # noqa: F811
    """Ticket `x`, land run x-2 on its last phase. `early` has a record only under x-1, `remote` none anywhere, `late` under x-2.
    Returns `done`, task to pr for each task the store moved to done, and the mark_done steps the walk ran."""
    runs, done, marks = tmp_path / "runs", {}, []
    for task, phase in TASKS.items():
        (tmp_path / "work/x" / phase).mkdir(parents=True, exist_ok=True)
        (tmp_path / f"work/x/{phase}/{task}.md").write_text(f"---\nid: {task}\nstate: approved\n---\nBody.\n", encoding="utf-8")
    for task, run in (("early", "x-1"), ("late", "x-2")):
        (runs / run / "tasks" / TASKS[task]).mkdir(parents=True)
        (runs / run / "tasks" / TASKS[task] / f"{task}.json").write_text(json.dumps(_record(task, run)), encoding="utf-8")
    for phase in ("p1-a", "p2-b"):
        (runs / f"x-2:{phase}.json").write_text(json.dumps({"initiative": "x", "phase_verdict": {"reasoning": "solid"}}), encoding="utf-8")
    store.item = tmp_path / "work/x/p2-b/late.md"
    provider = tmp_path / "provider.yaml"
    provider.write_text("work_state: store\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"provider_profile: {provider}\n", encoding="utf-8")

    def fake_set_state(runs_dir, initiative, task, state, by, expected=None):
        done.setdefault(task, None)
        return store_cli.StateSet({"state": state})

    def fake_mark_done_with_pr(runs_dir, row, pr, now):
        done[row["task_id"]] = pr

    def step(_repo, step, _forge=None):
        if step["kind"] == "mark_done":
            marks.append(step)
            return real(_repo, step, _forge)
        return True, URL if step["kind"] == "pr_create" else "ok"

    real = cli._execute_land_step
    monkeypatch.setattr(cli, "_execute_land_step", step)
    monkeypatch.setattr(store_cli, "set_state", fake_set_state)
    monkeypatch.setattr(cli, "_mirror_landed", lambda step: done.__setitem__(step["task"], step["pr"]))
    monkeypatch.setattr(route_edit, "mark_done_with_pr", fake_mark_done_with_pr)
    monkeypatch.setattr(run_store, "read_queue", lambda *a, **k: [{"task_id": t, "initiative": "x", "state": "approved"} for t in TASKS])
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
    return done, marks, profile


def _land_ticket(repo, tmp_path, profile):  # noqa: F811
    return cli.main(["runs", "land", "x-2", "--phase", "p2-b", "--repo", str(repo), "--apply", "--gate", "full",
                     "--profile", str(profile), "--runs-dir", str(tmp_path / "runs")])


def test_a_merged_ticket_pr_marks_every_task_done_with_the_pr_url(repo, tmp_path, monkeypatch, store):  # noqa: F811
    done, marks, profile = _build(tmp_path, monkeypatch, store, None)
    assert _land_ticket(repo, tmp_path, profile) == 0
    assert done == {"early": URL, "remote": URL, "late": URL}
    assert [m["task"] for m in marks if m.get("recorded") is False] == ["remote"]


def test_an_already_merged_ticket_pr_marks_every_task_done_with_the_pr(repo, tmp_path, monkeypatch, store):  # noqa: F811
    done, marks, profile = _build(tmp_path, monkeypatch, store, {"number": 535, "state": "merged"})
    assert _land_ticket(repo, tmp_path, profile) == 0
    assert done == {"early": "#535", "remote": "#535", "late": "#535"}
    assert [m["task"] for m in marks if m.get("recorded") is False] == ["remote"]


def test_the_pr_ref_is_the_url_else_the_number_else_nothing():
    assert cli._merged_pr_ref({"number": 535, "url": URL}) == URL
    assert cli._merged_pr_ref({"number": 535, "state": "merged"}) == "#535"
    assert cli._merged_pr_ref(None) == ""
