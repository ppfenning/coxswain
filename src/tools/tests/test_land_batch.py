from agent_tools.land import pr_footer
from agent_tools.land_batch import batch_plan

SOURCES = [
    {"initiative": "a", "phase": "p1", "run": "r1", "repo": "x", "files": ["f1.py"], "tasks": ["t1", "t2"]},
    {"initiative": "b", "phase": "p2", "run": "r2", "repo": "x", "files": ["f2.py", "g2.py"], "tasks": ["t3"]},
    {"initiative": "c", "phase": "p3", "run": "r3", "repo": "x", "files": ["f3.py"], "tasks": ["t4"]},
]
PATHS = {"t1": "/w/t1.md", "t2": "/w/t2.md", "t3": "/w/t3.md", "t4": "/w/t4.md"}


def plan(sources=SOURCES, paths=PATHS):
    return batch_plan(sources, {}, "main", paths, "abcdef0123")


def test_step_kinds_for_three_sources():
    assert [s["kind"] for s in plan()] == [
        "batch_squash", "batch_squash", "batch_squash", "checks", "push", "pr_create", "wait_checks", "merge",
        "clean_phase", "clean_phase", "clean_phase", "mark_done", "mark_done", "mark_done", "mark_done"]


def test_squash_shape_and_first_flag():
    squashes = plan()[:3]
    assert [s["first"] for s in squashes] == [True, False, False]
    assert squashes[1] == {"kind": "batch_squash", "branch": "epic/b/p2", "onto": "queue/abcdef0123", "from": "main",
                           "subject": "epic b: p2", "source": "b/p2", "first": False}


def test_batch_branch_steps():
    steps = plan()
    assert steps[3]["worktree_of"] == "queue/abcdef0123"
    assert steps[4] == {"kind": "push", "branch": "queue/abcdef0123"}
    assert steps[6] == {"kind": "wait_checks", "branch": "queue/abcdef0123"}


def test_merge_rebases_and_keeps_commits():
    assert plan()[7] == {"kind": "merge", "squash": False, "method": "rebase", "delete_branch": True,
                         "branch": "queue/abcdef0123", "default_branch": "main"}


def test_one_mark_done_per_task_with_path():
    done = [s for s in plan() if s["kind"] == "mark_done"]
    assert [(s["task"], s["path"]) for s in done] == [(t, PATHS[t]) for t in ("t1", "t2", "t3", "t4")]


def test_clean_phase_per_source():
    clean = [s for s in plan() if s["kind"] == "clean_phase"]
    assert clean[0] == {"kind": "clean_phase", "run": "r1", "phase_branch": "epic/a/p1",
                        "pr_branch": "queue/abcdef0123", "tasks": ["t1", "t2"]}
    assert [c["phase_branch"] for c in clean] == ["epic/a/p1", "epic/b/p2", "epic/c/p3"]


def test_pr_title_and_body_name_every_source():
    pr = plan()[5]
    assert pr["title"] == "queue: 3 phases - a/p1, b/p2, c/p3"
    assert pr["head"] == "queue/abcdef0123" and pr["base"] == "main"
    for text in ("a/p1", "b/p2", "c/p3", "t1", "t2", "t3", "t4", "f1.py", "g2.py", "f3.py"):
        assert text in pr["body"]
    assert pr["body"].endswith(pr_footer("r1"))


def test_empty_batch_is_a_note():
    assert [s["kind"] for s in plan([])] == ["note"]


def test_repeated_source_is_refused():
    steps = plan([SOURCES[0], SOURCES[1], SOURCES[0]])
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "a/p1" in steps[0]["reason"]


def test_task_missing_from_paths_is_refused():
    steps = plan(paths={"t1": "/w/t1.md", "t2": "/w/t2.md", "t3": "/w/t3.md"})
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "t4" in steps[0]["reason"]
