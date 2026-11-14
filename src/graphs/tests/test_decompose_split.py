import copy

from graphs.delivery.decompose_split import Split, classify, split_foreign

OWN = {"src/a.py", "src/b.py", "docs/x.md"}
TREES = {"/r/tools": {"t/x.py", "t/y.py", "both.py"}, "/r/dash": {"d/z.rs", "both.py"}}


def task(id, surfaces=(), needs=(), phase="p1", body=None):
    return {
        "id": id,
        "title": f"Title {id}",
        "body": body if body is not None else f"Body {id}",
        "phase": phase,
        "needs": list(needs),
        "surfaces": list(surfaces),
    }


def run(tasks, own=OWN, trees=TREES):
    return split_foreign(tasks, own, trees, title="Init")


def test_empty_own_paths_keeps_every_task_and_empties_the_rest():
    tasks = [task("a", ["t/x.py"]), task("b", ["nowhere.py"])]
    assert run(tasks, own=set()) == Split(tasks, [], [], [])


def test_classify_plain_surfaces():
    assert classify("src/a.py", OWN, TREES) == ("own", None)
    assert classify("t/x.py", OWN, TREES) == ("foreign", "/r/tools")
    assert classify("both.py", OWN, TREES) == ("neither", None)
    assert classify("nowhere.py", OWN, TREES) == ("neither", None)


def test_classify_new_surface_uses_its_parent_directory():
    assert classify("src/c.py (new)", OWN, TREES) == ("own", None)
    assert classify("d/new.rs (new)", OWN, TREES) == ("foreign", "/r/dash")
    assert classify("ghost/new.py (new)", OWN, TREES) == ("neither", None)


def test_classify_new_surface_in_a_directory_of_two_trees_is_neither():
    trees = {"/r/tools": {"shared/a.py"}, "/r/dash": {"shared/b.py"}}
    assert classify("shared/c.py (new)", OWN, trees) == ("neither", None)


def test_task_with_only_own_surfaces_or_none_is_kept_untouched():
    tasks = [task("a", ["src/a.py", "src/c.py (new)"]), task("b")]
    out = run(tasks)
    assert out == Split(tasks, [], [], [])
    assert out.kept[0] is tasks[0]


def test_task_with_only_foreign_surfaces_joins_the_follow_up():
    out = run([task("a", ["src/a.py"]), task("b", ["t/x.py", "t/y.py"], needs=["a"])])
    assert [t["id"] for t in out.kept] == ["a"]
    assert out.follow_ups == [
        {
            "repo": "/r/tools",
            "title": "Init: follow-up for tools",
            "body": "b: Title b\nSurfaces: t/x.py, t/y.py\nNeeds: none\n\nBody b",
            "tasks": ["b"],
            "lint": "Split out of the initiative Init: b touch files in /r/tools, not in this repository.",
        }
    ]
    assert out.notes == ["Task b has no surface here; moved t/x.py, t/y.py as b to the follow-up for tools."]


def test_task_with_own_and_foreign_surfaces_stays_with_own_and_copies_the_rest():
    out = run([task("a", ["src/a.py", "t/x.py"], needs=["z"])])
    assert out.kept == [task("a", ["src/a.py"], needs=["z"])]
    assert out.follow_ups[0]["tasks"] == ["a--tools"]
    assert out.follow_ups[0]["body"] == "a--tools: Title a\nSurfaces: t/x.py\nNeeds: none\n\nBody a"
    assert out.notes == ["Task a keeps its own surfaces; moved t/x.py to a--tools in the follow-up for tools."]


def test_task_with_only_neither_surfaces_is_dropped_with_a_reason():
    t = task("a", ["nowhere.py", "both.py"])
    out = run([t])
    assert out.kept == [] and out.follow_ups == []
    assert out.dropped == [
        {
            "task": t,
            "reason": "None of its surfaces is in this repository or a registered one: nowhere.py, both.py.",
        }
    ]
    assert out.notes == [
        "Dropped task a. None of its surfaces is in this repository or a registered one: nowhere.py, both.py."
    ]


def test_neither_surface_is_removed_from_a_task_that_keeps_an_own_surface():
    out = run([task("a", ["src/a.py", "nowhere.py"])])
    assert out.kept == [task("a", ["src/a.py"])]
    assert out.dropped == [] and out.follow_ups == []
    assert out.notes == ["Removed surface nowhere.py from task a: it is in no known repository."]


def test_kept_needs_lose_moved_and_dropped_ids_with_a_note_each():
    tasks = [
        task("a", ["t/x.py"]),
        task("b", ["nowhere.py"]),
        task("c", ["src/a.py"]),
        task("d", ["src/b.py"], needs=["a", "c", "b"]),
    ]
    out = run(tasks)
    assert out.kept[1]["needs"] == ["c"]
    assert out.notes[-2:] == [
        "Removed edge: task d no longer needs a, which was moved to the follow-up for tools.",
        "Removed edge: task d no longer needs b, which was dropped.",
    ]


def test_a_split_task_that_stays_keeps_its_id_in_needs():
    out = run([task("a", ["src/a.py", "t/x.py"]), task("b", ["src/b.py"], needs=["a"])])
    assert out.kept[1]["needs"] == ["a"]


def test_follow_ups_are_one_per_repo_in_first_appearance_order_with_inner_needs():
    tasks = [
        task("a", ["d/z.rs"]),
        task("b", ["t/x.py"]),
        task("c", ["d/z.rs"], needs=["a", "b", "q"]),
    ]
    out = run(tasks)
    assert [f["repo"] for f in out.follow_ups] == ["/r/dash", "/r/tools"]
    assert out.follow_ups[0]["tasks"] == ["a", "c"]
    assert "Needs: a\n" in out.follow_ups[0]["body"]
    assert out.follow_ups[0]["body"].split("\n\n")[0].startswith("a: Title a")


def test_foreign_only_task_spanning_two_repos_becomes_one_copy_per_repo():
    out = run([task("a", ["t/x.py", "d/z.rs"])])
    assert out.kept == []
    assert [(f["repo"], f["tasks"]) for f in out.follow_ups] == [
        ("/r/tools", ["a--tools"]),
        ("/r/dash", ["a--dash"]),
    ]


def test_labels_widen_when_two_receiving_repos_share_a_last_segment():
    trees = {"/a/tools": {"p/x.py"}, "/b/tools": {"q/y.py"}}
    out = run([task("m", ["p/x.py"]), task("n", ["q/y.py", "src/a.py"])], trees=trees)
    assert [f["title"] for f in out.follow_ups] == [
        "Init: follow-up for a-tools",
        "Init: follow-up for b-tools",
    ]
    assert [f["tasks"] for f in out.follow_ups] == [["m"], ["n--b-tools"]]


def test_labels_stay_plain_when_the_sharing_repo_receives_nothing():
    trees = {"/a/tools": {"p/x.py"}, "/b/tools": {"q/y.py"}}
    out = run([task("m", ["p/x.py", "src/a.py"])], trees=trees)
    assert out.follow_ups[0]["title"] == "Init: follow-up for tools"
    assert out.follow_ups[0]["tasks"] == ["m--tools"]


def test_inputs_are_not_mutated():
    tasks = [task("a", ["src/a.py", "t/x.py", "nowhere.py"], needs=["b"]), task("b", ["t/y.py"])]
    before = copy.deepcopy((tasks, OWN, TREES))
    run(tasks)
    assert (tasks, OWN, TREES) == before


def test_classify_falls_back_to_the_path_layout_when_no_listing_places_the_surface():
    assert classify("src/ui/regatta.rs", OWN, {}, "coxswain") == ("foreign", "~/repos/coxswain-dash")
    assert classify("src/tools/agent_tools/dash_feed.py", OWN, {}, "coxswain-dash") == ("foreign", "~/repos/coxswain")
    assert classify("src/ui/new.rs (new)", OWN, {}, "coxswain") == ("foreign", "~/repos/coxswain-dash")


def test_classify_layout_fallback_never_beats_a_listing_or_a_layout_miss():
    assert classify("src/ui/x.rs", {"src/ui/x.rs"}, {}, "coxswain") == ("own", None)
    assert classify("t/x.py", OWN, TREES, "coxswain") == ("foreign", "/r/tools")
    assert classify("src/ui/regatta.rs", OWN, {}, "coxswain-dash") == ("neither", None)
    assert classify("nowhere.py", OWN, {}, "coxswain") == ("neither", None)
    assert classify("src/ui/regatta.rs", OWN, {}) == ("neither", None)


def test_a_layout_placed_task_becomes_a_follow_up_for_the_layout_repository():
    tasks = [task("a", ["src/a.py"]), task("b", ["src/ui/regatta.rs", "Cargo.toml"])]
    out = split_foreign(tasks, OWN, {}, title="Init", own_key="coxswain")
    assert [t["id"] for t in out.kept] == ["a"]
    assert [(f["repo"], f["tasks"]) for f in out.follow_ups] == [("~/repos/coxswain-dash", ["b"])]
    assert out.dropped == []


def test_without_an_own_key_the_layout_places_nothing():
    tasks = [task("a", ["src/a.py"]), task("b", ["src/ui/regatta.rs"])]
    out = split_foreign(tasks, OWN, {}, title="Init")
    assert out.follow_ups == []
    assert [d["task"]["id"] for d in out.dropped] == ["b"]
