from dataclasses import replace

from agent_tools.route_sync import (
    IssueError,
    Item,
    _fields,
    marked_body,
    marked_issues,
    parse_issue,
    plan,
    render,
    run_id_of,
    unmarked,
)


def _item(**over):
    base = {"id": "item-1", "title": "Fix the thing", "body": "Some body.", "repo": "acme/widgets",
                "state": "ready", "phase": "core", "run": "run-7", "cost_usd": 1.5, "gate": "tests", "issue": None}
    base.update(over)
    return Item(**base)


def test_a_new_item_plans_create_add_five_sets_and_a_writeback_in_order():
    item = _item()
    steps = plan([item], issues={}, project_items={}, tracker="github-projects")
    kinds = [s["kind"] for s in steps]
    assert kinds == ["issue_create", "project_add", "project_set", "project_set",
                      "project_set", "project_set", "project_set", "writeback"]
    assert steps[0] == {"kind": "issue_create", "repo": "acme/widgets", "title": "Fix the thing",
                          "body": "Some body.\n\n<!-- coxswain:item item-1 -->", "label": "coxswain",
                          "item_id": "item-1", "marker": ""}
    assert steps[1] == {"kind": "project_add", "issue": None}
    assert steps[-1] == {"kind": "writeback", "item_id": "item-1", "issue": None, "marker": ""}


def test_two_fresh_items_keep_their_steps_grouped_per_item():
    first = _item(id="item-1", repo="acme/widgets")
    second = _item(id="item-2", repo="acme/gadgets", title="Fix another thing")
    steps = plan([first, second], issues={}, project_items={}, tracker="github-projects")
    block = ["issue_create", "project_add", "project_set", "project_set",
             "project_set", "project_set", "project_set", "writeback"]
    assert [s["kind"] for s in steps] == block + block
    first_block, second_block = steps[:8], steps[8:]
    assert first_block[0]["item_id"] == "item-1" and first_block[-1]["item_id"] == "item-1"
    assert second_block[0]["item_id"] == "item-2" and second_block[-1]["item_id"] == "item-2"


def test_an_item_whose_issue_and_fields_already_match_plans_nothing():
    item = _item(issue="acme/widgets#12")
    issues = {"acme/widgets#12": {"title": "Fix the thing", "body": "Some body.", "labels": ["coxswain"]}}
    project_items = {"acme/widgets#12": {"State": "Ready", "Phase": "core", "Run": "run-7",
                                          "Cost": "$1.50", "Gate": "tests"}}
    assert plan([item], issues, project_items, "github-projects") == []


def test_a_changed_state_plans_exactly_one_project_set():
    item = _item(issue="acme/widgets#12", state="done")
    issues = {"acme/widgets#12": {"title": "Fix the thing", "body": "Some body.", "labels": ["coxswain"]}}
    project_items = {"acme/widgets#12": {"State": "Ready", "Phase": "core", "Run": "run-7",
                                          "Cost": "$1.50", "Gate": "tests"}}
    steps = plan([item], issues, project_items, "github-projects")
    assert steps == [{"kind": "project_set", "issue": "acme/widgets#12", "field": "State", "value": "Landed",
                      "was": "Ready"}]


def test_a_changed_body_plans_one_issue_edit():
    item = _item(issue="acme/widgets#12", body="A new body.")
    issues = {"acme/widgets#12": {"title": "Fix the thing", "body": "Some body.", "labels": ["coxswain"]}}
    project_items = {"acme/widgets#12": {"State": "Ready", "Phase": "core", "Run": "run-7",
                                          "Cost": "$1.50", "Gate": "tests"}}
    steps = plan([item], issues, project_items, "github-projects")
    assert steps == [{"kind": "issue_edit", "issue": "acme/widgets#12", "title": "Fix the thing",
                        "body": "A new body.\n\n<!-- coxswain:item item-1 -->"}]


def test_an_issue_body_differing_only_by_its_item_marker_plans_no_edit():
    item = _item(issue="acme/widgets#12")
    marked = {"title": "Fix the thing", "body": marked_body("item-1", "Some body.")}
    project_items = {"acme/widgets#12": {"State": "Ready", "Phase": "core", "Run": "run-7",
                                          "Cost": "$1.50", "Gate": "tests"}}
    assert plan([item], {"acme/widgets#12": marked}, project_items, "github-projects") == []


def test_marked_body_ends_with_one_marker_and_unmarked_gives_the_body_back_exactly():
    assert marked_body("i-1", "") == "<!-- coxswain:item i-1 -->"
    assert marked_body("i-1", "Body.") == "Body.\n\n<!-- coxswain:item i-1 -->"
    for body in ("", "Body.", "Body.\n", "a\n\n\nb\n\n", "<!-- coxswain:item other -->", "x\n<!-- coxswain:item o -->"):
        assert unmarked(marked_body("i-1", body)) == body
        assert unmarked(marked_body("i-1", body) + "\n") == body  # a trailing newline the forge adds is no drift


QUOTING = "Quote it like this:\n<!-- coxswain:item task:other/t1 -->\nor at the end:\n<!-- coxswain:item task:other/t1 -->"


def test_a_body_that_quotes_a_marker_keeps_it_claims_only_its_own_item_and_is_not_re_edited():
    item = _item(id="t2", marker_key="task:mine/t2", body=QUOTING)
    (create,) = [s for s in plan([item], {}, {}, "github-projects") if s["kind"] == "issue_create"]
    assert unmarked(create["body"]) == QUOTING
    assert marked_issues([{"number": 7, "body": create["body"]}]) == {"task:mine/t2": "7"}
    on_file = {"7": {"title": item.title, "body": create["body"]}}
    project_items = {"7": {"State": "Ready", "Phase": "core", "Run": "run-7", "Cost": "$1.50", "Gate": "tests"}}
    assert plan([replace(item, issue="7")], on_file, project_items, "github-projects") == []


def test_a_marker_quoted_anywhere_but_the_last_line_marks_nothing():
    assert marked_issues([{"number": 3, "body": "<!-- coxswain:item task:a/t1 -->\nthen more text"}]) == {}
    assert unmarked("<!-- coxswain:item task:a/t1 -->\nthen more text") == "<!-- coxswain:item task:a/t1 -->\nthen more text"


def test_two_tasks_of_one_id_in_two_initiatives_carry_two_markers():
    a = _item(id="t1", marker_key="task:alpha/t1")
    b = _item(id="t1", marker_key="task:beta/t1")
    creates = [s for s in plan([a, b], {}, {}, "github-projects") if s["kind"] == "issue_create"]
    assert [c["marker"] for c in creates] == ["task:alpha/t1", "task:beta/t1"]
    assert marked_issues([{"number": n, "body": c["body"]} for n, c in zip((5, 6), creates, strict=True)]) == {
        "task:alpha/t1": "5", "task:beta/t1": "6"}


def test_marked_issues_maps_each_item_to_its_lowest_marked_issue():
    rows = [{"number": 14, "body": marked_body("i-1", "B")}, {"number": 9, "body": marked_body("i-1", "B")},
            {"number": 3, "body": "no marker"}, {"number": 5, "body": marked_body("i-2", "")}]
    assert marked_issues(rows) == {"i-1": "9", "i-2": "5"}


def test_an_issue_missing_from_the_labelled_map_plans_no_issue_edit():
    item = _item(issue="acme/widgets#99")
    issues = {}  # #99 lost the coxswain label, or is gone; nothing on file to edit against.
    project_items = {"acme/widgets#99": {"State": "Ready", "Phase": "core", "Run": "run-7",
                                          "Cost": "$1.50", "Gate": "tests"}}
    assert plan([item], issues, project_items, "github-projects") == []


def test_an_unknown_state_plans_a_single_refuse_step():
    item = _item(state="blocked")
    steps = plan([item], issues={}, project_items={}, tracker="github-projects")
    assert steps == [{"kind": "refuse", "item_id": "item-1", "detail": "unknown state: 'blocked'"}]


def _closes(item, state):
    steps = plan([item], issues={"5": {"title": "Fix the thing", "body": "Some body.", "state": state}},
                 project_items={}, tracker="github-projects")
    return [s for s in steps if s["kind"] == "issue_close"], steps


def test_a_done_or_dropped_item_with_an_open_issue_plans_a_close_after_its_other_steps():
    for state in ("done", "dropped"):
        closes, steps = _closes(_item(state=state, issue="5"), "OPEN")
        assert closes == [{"kind": "issue_close", "issue": "5"}] and steps[-1] == closes[0]
    assert render(closes) == ["issue_close 5"]


def test_no_close_for_a_closed_issue_a_live_item_or_an_item_with_no_issue():
    assert _closes(_item(state="done", issue="5"), "CLOSED")[0] == []
    assert _closes(_item(state="ready", issue="5"), "OPEN")[0] == []
    assert plan([_item(state="done")], issues={}, project_items={}, tracker="github-projects")[-1]["kind"] == "writeback"
    unlabelled = plan([_item(state="done", issue="5")], issues={}, project_items={}, tracker="github-projects")
    assert "issue_close" not in [s["kind"] for s in unlabelled]


def test_a_dropped_item_plans_only_its_issue_steps_and_an_approved_item_creates_its_issue():
    dropped, steps = _closes(_item(state="dropped", issue="5"), "OPEN")
    assert [s["kind"] for s in steps] == ["issue_close"]
    approved = plan([_item(state="approved")], issues={}, project_items={}, tracker="github-projects")
    assert [s["kind"] for s in approved] == ["issue_create", "writeback"]


def test_tracker_none_plans_nothing():
    item = _item()
    assert plan([item], issues={}, project_items={}, tracker="none") == []


def test_render_has_one_line_per_step():
    item = _item()
    steps = plan([item], issues={}, project_items={}, tracker="github-projects")
    lines = render(steps)
    assert len(lines) == len(steps)
    assert lines[0] == "issue_create acme/widgets: 'Fix the thing'"
    assert lines[1] == "project_add (new issue)"
    assert lines[-1] == "writeback item-1 -> (new issue)"


def test_render_of_a_refuse_step():
    steps = [{"kind": "refuse", "item_id": "item-1", "detail": "unknown state: 'blocked'"}]
    assert render(steps) == ["refuse item-1: unknown state: 'blocked'"]


def test_an_item_with_no_repository_is_refused_not_planned():
    steps = plan([_item(repo="")], issues={}, project_items={}, tracker="github-projects")
    assert steps == [{"kind": "refuse", "item_id": "item-1", "detail": "no repository resolves for this item"}]


def test_an_empty_field_with_no_value_on_the_board_plans_no_set():
    steps = plan([_item(phase="", gate="")], issues={}, project_items={}, tracker="github-projects")
    assert [s["field"] for s in steps if s["kind"] == "project_set"] == ["State", "Run", "Cost"]


def _links(steps):
    return [s for s in steps if s["kind"] == "sub_issue_link"]


def test_a_task_under_a_planned_initiative_links_last_and_the_initiative_is_created_first():
    initiative = _item(id="initiative:i1", run="", phase="", gate="", initiative=True)
    task = _item(id="t1", parent="initiative:i1")
    steps = plan([initiative, task], issues={}, project_items={}, tracker="github-projects")
    assert steps[0]["kind"] == "issue_create" and steps[0]["item_id"] == "initiative:i1"
    assert steps[-1] == {"kind": "sub_issue_link", "repo": "acme/widgets", "parent_item": "initiative:i1",
                         "parent_issue": None, "child_item": "t1", "child_issue": None}
    assert len(_links(steps)) == 1
    assert render(_links(steps)) == ["sub_issue_link (new issue) under initiative:i1"]


def test_a_task_links_under_a_parent_issue_on_file_when_the_parent_is_not_in_the_plan():
    task = _item(id="t1", parent="initiative:i1", parent_issue="10", issue="11")
    steps = plan([task], issues={"11": {"title": task.title, "body": task.body}}, project_items={},
                 tracker="github-projects")
    assert [(s["parent_issue"], s["child_issue"]) for s in _links(steps)] == [("10", "11")]


def test_no_link_for_an_item_with_no_parent_or_a_parent_with_no_issue_anywhere():
    assert _links(plan([_item()], issues={}, project_items={}, tracker="github-projects")) == []
    orphan = _item(parent="initiative:i1")
    assert _links(plan([orphan], issues={}, project_items={}, tracker="github-projects")) == []


def test_a_done_initiative_sets_its_board_state_but_its_open_issue_is_not_closed():
    issues = {"5": {"title": "Fix the thing", "body": "Some body.", "state": "OPEN"}}
    initiative = _item(id="initiative:i1", issue="5", state="done", initiative=True)
    steps = plan([initiative], issues=issues, project_items={}, tracker="github-projects")
    assert [s["value"] for s in steps if s["kind"] == "project_set" and s["field"] == "State"] == ["Landed"]
    assert not any(s["kind"] == "issue_close" for s in steps)
    task = _item(id="t1", issue="5", state="done")
    assert any(s["kind"] == "issue_close" for s in plan([task], issues=issues, project_items={}, tracker="github-projects"))


def test_parse_issue_resolves_bare_single_and_double_quoted_numbers_to_the_same_int():
    assert [parse_issue(raw) for raw in ("1688", "'1688'", '"1688"', 1688)] == [1688, 1688, 1688, 1688]


def test_parse_issue_refuses_a_word_trailing_letters_and_unmatched_quotes():
    for raw in ("abc", "1688abc", "'1688\"", "'1688", "''", "", None, "acme/widgets#12"):
        assert isinstance(parse_issue(raw), IssueError)


def test_items_with_a_bare_single_quoted_or_double_quoted_issue_sync_the_same_issue():
    issues = {"1688": {"title": "Fix the thing", "body": "Some body.", "state": "OPEN"}}
    planned = [plan([_item(issue=raw, state="done")], issues, {}, "github-projects")
               for raw in ("1688", "'1688'", '"1688"')]
    assert planned[0] == planned[1] == planned[2]
    assert [s["issue"] for s in planned[1] if s["kind"] == "issue_close"] == ["1688"]


def test_a_non_numeric_issue_is_left_unchanged_for_the_existing_refusal():
    assert _item(issue="1688abc").issue == "1688abc"


_RECORD = {"run": "task-1", "phase": "build", "reason": "the fix loop stopped: x", "ts": "2026-10-29",
           "body_sha": "3cb77492f2d4", "kind": "refused", "cause": "harness", "note": "n"}


def test_the_run_field_is_the_id_for_a_plain_id_and_for_an_attempt_record():
    assert _fields(_item(run="run-7"))["Run"] == "run-7"
    assert _fields(_item(run=_RECORD))["Run"] == "task-1"


def test_run_id_of_reads_the_record_text_the_front_matter_parser_returns():
    text = "{run: task-1, phase: build, reason: 'the fix loop stopped: x', kind: refused}"
    assert run_id_of(text) == "task-1"
    assert run_id_of("") == run_id_of(None) == run_id_of({"kind": "refused"}) == run_id_of("{kind: refused}") == ""
