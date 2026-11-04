import copy

from agent_tools import route

INTAKE = {
    "kind": "intake", "initiative": "", "task_id": "I1", "phase": "", "state": "queued",
    "needs": [], "title": "Idea", "surfaces": [], "body": "the idea", "extra": {"id": "I1"},
}
RESULT = {
    "run_id": "r1", "idea": "the idea", "shape": "initiative",
    "phases": [{"id": "p1", "goal": "g"}, {"id": "p2", "goal": "h"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "title": "T1", "body": "x", "needs": [], "surfaces": ["a.py"]},
        {"id": "t2", "phase": "p2", "title": "T2", "body": "y", "needs": ["t1"], "surfaces": ["b.py", "c.py"]},
    ],
    "moves": [], "challenge": {}, "proposals": [], "totals": {},
}
PROFILE = {
    "harness_dir": "/h", "workspace_dir": "/w", "team": "t", "cartridges_dir": "/c",
    "provider_profile": "pp", "assume": "a",
}
NEEDS = {"idea": "i.md", "initiative_id": "init-a"}


def test_keeps_every_task_field_phases_and_order():
    plan = route.plan_from_result(RESULT, "init-a", "A", "about A")
    assert plan["tasks"] == RESULT["tasks"]
    assert plan["phases"] == RESULT["phases"]


def test_id_title_body_and_lint():
    plan = route.plan_from_result(RESULT, "init-a", "A", "about A")
    assert (plan["id"], plan["title"], plan["body"], plan["lint"]) == ("init-a", "A", "about A", [])
    assert route.plan_from_result({**RESULT, "lint": ["bad"]}, "i", "A", "b")["lint"] == ["bad"]


def test_taskless_result_gives_no_rows_and_the_empty_reason():
    plan = route.plan_from_result({"phases": [], "tasks": []}, "init-a", "A", "b")
    rows, failure = route.plan_writes(plan, INTAKE, [])
    assert plan["tasks"] == []
    assert rows == []
    assert failure.startswith("routing:")


def test_result_without_tasks_key_is_empty_plan():
    assert route.plan_from_result({}, "i", "A", "b")["tasks"] == []


def test_input_is_unchanged_and_not_shared():
    before = copy.deepcopy(RESULT)
    plan = route.plan_from_result(RESULT, "init-a", "A", "about A")
    plan["tasks"][0]["needs"].append("zz")
    plan["tasks"][1]["surfaces"].append("zz")
    plan["phases"][0]["goal"] = "zz"
    assert before == RESULT


def test_plan_goes_through_plan_writes_to_rows():
    plan = route.plan_from_result(RESULT, "init-a", "A", "about A")
    rows, failure = route.plan_writes(plan, INTAKE, [])
    assert failure is None
    assert rows[0]["kind"] == "initiative"
    assert {r["task_id"] for r in rows if r["kind"] == "task"} == {"t1", "t2"}


def test_task_with_needs_is_written_ready_and_initiative_stays_todo():
    plan = route.plan_from_result(RESULT, "init-a", "A", "about A")
    rows, failure = route.plan_writes(plan, INTAKE, [])
    assert failure is None
    tasks = {r["task_id"]: r for r in rows if r["kind"] == "task"}
    assert set(tasks) == {"t1", "t2"}
    assert tasks["t2"]["needs"] == ["t1"]
    assert {t["state"] for t in tasks.values()} == {"ready"}
    assert next(r for r in rows if r["kind"] == "initiative")["state"] == "todo"


INITIATIVE = "---\nid: init-a\ntitle: A\nrepo: /old\nintake: intake/n.md\n---\n\nbody\n"


def test_plan_carries_repo_and_defaults_to_empty():
    assert route.plan_from_result(RESULT, "init-a", "A", "b", repo="/r/x")["repo"] == "/r/x"
    assert route.plan_from_result(RESULT, "init-a", "A", "b")["repo"] == ""


def test_initiative_with_repo_replaces_an_existing_line():
    expected = "---\nid: init-a\ntitle: A\nrepo: /new\nintake: intake/n.md\n---\n\nbody\n"
    assert route.initiative_with_repo(INITIATIVE, "/new") == expected
    empty = INITIATIVE.replace("repo: /old", 'repo: ""')
    assert route.initiative_with_repo(empty, "/new") == expected


def test_initiative_with_repo_adds_a_missing_line():
    bare = "---\nid: init-a\ntitle: A\n---\n\nbody\n"
    out = route.initiative_with_repo(bare, "/new")
    assert out == "---\nid: init-a\ntitle: A\nrepo: /new\n---\n\nbody\n"
    assert route.parse_frontmatter(out) == ({"id": "init-a", "title": "A", "repo": "/new"}, "body")


def test_initiative_with_repo_leaves_text_alone_for_an_empty_repo():
    assert route.initiative_with_repo(INITIATIVE, "") == INITIATIVE


def test_missing_repo_refusal_is_none_for_a_present_repo():
    assert route.missing_repo_refusal("n", {"repo": "/r"}) is None


def test_missing_repo_refusal_names_intake_and_field_for_empty_or_absent_repo():
    line = "routing: intake n has no repo: field"
    assert route.missing_repo_refusal("n", {"repo": ""}) == line
    assert route.missing_repo_refusal("n", {}) == line


def test_harness_argv_carries_result_out_for_decompose():
    argv = route.harness_argv(PROFILE, "decompose", "r1", result_out="/tmp/x.json", **NEEDS)
    assert argv[argv.index("--result-out"):argv.index("--result-out") + 2] == ["--result-out", "/tmp/x.json"]


def test_harness_argv_without_result_out_is_unchanged():
    plain = route.harness_argv(PROFILE, "decompose", "r1", **NEEDS)
    with_out = route.harness_argv(PROFILE, "decompose", "r1", result_out="/tmp/x.json", **NEEDS)
    assert "--result-out" not in plain
    assert [a for a in with_out if a not in ("--result-out", "/tmp/x.json")] == plain
    assert plain == [
        "/h/.venv/bin/python", "/h/shell.py", "decompose", "--team", "t", "--cartridges-dir", "/c",
        "--provider-profile", "pp", "--runs-dir", "/w/runs", "--assume", "a", "--run-id", "r1",
        "--idea", "i.md", "--initiative-id", "init-a", "--workdir", "/w",
    ]
