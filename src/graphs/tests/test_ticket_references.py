from graphs.delivery.ticket_lint import lint_tickets
from graphs.delivery.ticket_references import references_problems

LIB = "pkg/lib.py"
CALLER = "pkg/caller.py"
LIB_TEXT = "def parse(row, strict):\n    return row\n\n\ndef keep(row):\n    return row\n"
CALLER_TEXT = "from pkg.lib import parse\n\n\ndef run(row):\n    return parse(row, True)\n"
TREE = [{"path": LIB}, {"path": CALLER}]


def _task(**over):
    return {"id": "t1", "phase": "p1", "title": "x", "body": "", "needs": [], "surfaces": [LIB], **over}


def test_a_caller_of_a_changed_function_is_reported_with_owner_none():
    tasks = [_task(body="Remove the `strict` parameter. Change the `parse` signature.")]
    problems = references_problems(tasks, TREE, {LIB: LIB_TEXT, CALLER: CALLER_TEXT})
    assert [(p.task, p.rule) for p in problems] == [("t1", "references")]
    assert problems[0].detail.startswith("pkg/caller.py references parse from pkg/lib.py (owner: none)")
    assert "breaks the build" not in problems[0].detail
    assert problems[0].severity == "advisory"


def test_a_caller_owned_by_a_later_task_is_reported_as_breaking():
    tasks = [_task(body="Change the `parse` signature."), _task(id="t2", surfaces=[CALLER])]
    problems = references_problems(tasks, TREE, {LIB: LIB_TEXT, CALLER: CALLER_TEXT})
    assert len(problems) == 1
    assert "(owner: t2)" in problems[0].detail
    assert "breaks the build until t2 runs" in problems[0].detail
    assert "add t2 to this task's needs" in problems[0].fix


def test_the_same_caller_is_not_breaking_when_its_task_is_in_needs():
    tasks = [_task(body="Change the `parse` signature.", needs=["t2"]), _task(id="t2", surfaces=[CALLER])]
    problems = references_problems(tasks, TREE, {LIB: LIB_TEXT, CALLER: CALLER_TEXT})
    assert len(problems) == 1
    assert "(owner: t2)" in problems[0].detail
    assert "breaks the build" not in problems[0].detail


def test_an_additive_change_reports_referrers_without_the_breaking_flag():
    tasks = [_task(body="Add a `fresh` helper beside the others."), _task(id="t2", surfaces=[CALLER])]
    problems = references_problems(tasks, TREE, {LIB: LIB_TEXT, CALLER: CALLER_TEXT})
    assert len(problems) == 1
    assert "(owner: t2)" in problems[0].detail
    assert "breaks the build" not in problems[0].detail


def test_a_name_in_a_python_comment_or_string_is_not_a_reference():
    text = "# parse is called elsewhere\nNOTE = 'parse'\n"
    assert references_problems([_task()], TREE, {LIB: LIB_TEXT, CALLER: text}) == []


def test_a_sql_name_in_a_comment_elsewhere_is_not_reported():
    schema = "create table orders (id int);\n"
    other = "-- from orders we read nothing\n/* join orders */\nselect 1;\n"
    tasks = [_task(surfaces=["db/schema.sql"], body="Drop `orders`.")]
    tree = [{"path": "db/schema.sql"}, {"path": "db/other.sql"}]
    assert references_problems(tasks, tree, {"db/schema.sql": schema, "db/other.sql": other}) == []


def test_a_sql_table_in_a_query_of_another_file_is_reported():
    schema = "create table if not exists orders (id int);\n"
    other = "select id from orders;\n"
    tasks = [_task(surfaces=["db/schema.sql"], body="Drop `orders`.")]
    tree = [{"path": "db/schema.sql"}, {"path": "db/other.sql"}]
    problems = references_problems(tasks, tree, {"db/schema.sql": schema, "db/other.sql": other})
    assert [p.detail for p in problems] == ["db/other.sql references orders from db/schema.sql (owner: none)"]


def test_lint_tickets_carries_the_problem_and_stays_silent_without_texts():
    tasks = [_task(body="Change the `parse` signature.")]
    assert lint_tickets(tasks, TREE, [], "graphs") == []
    problems = lint_tickets(tasks, TREE, [], "graphs", texts={LIB: LIB_TEXT, CALLER: CALLER_TEXT})
    assert [p.rule for p in problems] == ["references"]
