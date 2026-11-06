import copy

from graphs.delivery.decompose_order import order_shared_surfaces


def task(id, phase="p1", surfaces=(), needs=()):
    return {"id": id, "phase": phase, "surfaces": list(surfaces), "needs": list(needs)}


def test_two_unordered_tasks_sharing_a_file_get_one_edge_and_one_note():
    tasks = [task("a", surfaces=["src/x.py"]), task("b", surfaces=["src/x.py"])]
    out, notes = order_shared_surfaces(tasks)
    assert [t["needs"] for t in out] == [[], ["a"]]
    assert notes == ["ordered: b now needs a, both touch src/x.py"]


def test_a_pair_sharing_several_files_yields_one_note_naming_all_in_surface_order():
    tasks = [task("a", surfaces=["y.py", "x.py"]), task("b", surfaces=["x.py", "y.py"])]
    out, notes = order_shared_surfaces(tasks)
    assert out[1]["needs"] == ["a"]
    assert notes == ["ordered: b now needs a, both touch y.py, x.py"]


def test_a_direct_edge_in_either_direction_leaves_the_pair_unchanged():
    for tasks in (
        [task("a", surfaces=["x.py"]), task("b", surfaces=["x.py"], needs=["a"])],
        [task("a", surfaces=["x.py"], needs=["b"]), task("b", surfaces=["x.py"])],
    ):
        assert order_shared_surfaces(tasks) == (tasks, [])


def test_a_transitive_path_through_a_third_task_leaves_the_pair_unchanged():
    tasks = [
        task("a", surfaces=["x.py"]),
        task("m", phase="p2", surfaces=["m.py"], needs=["a"]),
        task("b", surfaces=["x.py"], needs=["m"]),
    ]
    assert order_shared_surfaces(tasks) == (tasks, [])


def test_unknown_needs_ids_do_not_count_as_a_path():
    tasks = [task("a", surfaces=["x.py"]), task("b", surfaces=["x.py"], needs=["ghost"])]
    out, notes = order_shared_surfaces(tasks)
    assert out[1]["needs"] == ["ghost", "a"]
    assert len(notes) == 1


def test_no_shared_file_leaves_the_tasks_unchanged():
    tasks = [task("a", surfaces=["x.py"]), task("b", surfaces=["y.py"])]
    assert order_shared_surfaces(tasks) == (tasks, [])


def test_a_directory_surface_does_not_cover_files_under_it():
    tasks = [task("a", surfaces=["src/"]), task("b", surfaces=["src/x.py"])]
    assert order_shared_surfaces(tasks) == (tasks, [])


def test_the_same_file_in_different_phases_is_unchanged():
    tasks = [task("a", phase="p1", surfaces=["x.py"]), task("b", phase="p2", surfaces=["x.py"])]
    assert order_shared_surfaces(tasks) == (tasks, [])


def test_a_new_surface_matches_its_bare_form():
    tasks = [task("a", surfaces=["x.py (new)"]), task("b", surfaces=["x.py"])]
    out, notes = order_shared_surfaces(tasks)
    assert out[1]["needs"] == ["a"]
    assert notes == ["ordered: b now needs a, both touch x.py"]


def test_three_tasks_sharing_one_file_form_a_chain_without_a_cycle():
    tasks = [task(i, surfaces=["x.py"]) for i in "abc"]
    out, notes = order_shared_surfaces(tasks)
    needs = {t["id"]: t["needs"] for t in out}
    assert needs == {"a": [], "b": ["a"], "c": ["a", "b"]}
    assert len(notes) == 3
    # Every need points at an earlier task, so no cycle exists.
    assert all(n < i for i, ns in needs.items() for n in ns)


def test_notes_come_out_in_phase_order_then_list_order():
    tasks = [
        task("a", phase="p1", surfaces=["x.py"]),
        task("c", phase="p2", surfaces=["y.py"]),
        task("b", phase="p1", surfaces=["x.py"]),
        task("d", phase="p2", surfaces=["y.py"]),
    ]
    _, notes = order_shared_surfaces(tasks)
    assert notes == [
        "ordered: b now needs a, both touch x.py",
        "ordered: d now needs c, both touch y.py",
    ]


def test_the_input_is_not_mutated():
    tasks = [task("a", surfaces=["x.py"]), task("b", surfaces=["x.py"])]
    before = copy.deepcopy(tasks)
    out, _ = order_shared_surfaces(tasks)
    assert tasks == before
    assert out[1]["needs"] is not tasks[1]["needs"]


def test_tasks_missing_needs_and_surfaces_do_not_raise():
    tasks = [{"id": "a", "phase": "p1"}, {"id": "b", "phase": "p1"}]
    assert order_shared_surfaces(tasks) == (tasks, [])
