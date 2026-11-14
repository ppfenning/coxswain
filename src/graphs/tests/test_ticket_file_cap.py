from graphs.delivery.ticket_lint import lint_tickets


def _paths(n: int, suffix: str = "") -> list[str]:
    return [f"graphs/delivery/mod_{i}.py{suffix}" for i in range(n)]


def _tree(n: int) -> list[dict]:
    return [{"path": p, "repo": "graphs"} for p in _paths(n)]


def _task(surfaces: list[str], **over):
    return {"id": "t1", "phase": "p1", "title": "x", "body": "", "needs": [], "surfaces": surfaces, **over}


def _size(tasks, tree, **kwargs):
    return [p for p in lint_tickets(tasks, tree, [], "graphs", **kwargs) if p.rule == "size"]


def test_a_task_at_the_cap_passes():
    assert _size([_task(_paths(12))], _tree(12)) == []


def test_a_task_over_the_cap_names_count_and_cap():
    (problem,) = _size([_task(_paths(13))], _tree(13))
    assert "13" in problem.detail
    assert "12" in problem.detail
    assert problem.severity == "advisory"


def test_a_larger_cap_parameter_lets_it_pass():
    assert _size([_task(_paths(13))], _tree(13), max_files_per_task=20) == []


def test_new_surfaces_count_like_any_other():
    surfaces = [*_paths(10), *_paths(3, " (new)")]
    (problem,) = _size([_task(surfaces)], _tree(10))
    assert "13" in problem.detail


def test_a_body_sentence_is_not_a_way_around_the_flag():
    body = "This task cannot be split because the files share one rename."
    (problem,) = _size([_task(_paths(13), body=body)], _tree(13))
    assert "split" in problem.fix
    assert "body" in problem.fix
