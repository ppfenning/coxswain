"""`route requeue` core: queue the intake, drop the empty initiative, refuse when a task is still live."""

import copy

from agent_tools import chair_read_intake, intake_state, queue_rows, route, route_drift, route_requeue, run_store

INTAKE = {
    "kind": "intake",
    "initiative": "intake",
    "task_id": "idea",
    "state": "decomposed",
    "extra": {"initiative": "demo"},
}
INITIATIVE = {
    "kind": "initiative",
    "initiative": "demo",
    "task_id": "demo",
    "state": "todo",
    "extra": {"intake": "intake/idea.md"},
}
READY = {"kind": "task", "initiative": "demo", "task_id": "a", "state": "ready"}
DROPPED = {"kind": "task", "initiative": "demo", "task_id": "b", "state": "dropped"}


def _store(monkeypatch, rows):
    writes = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: rows)
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: writes.append(row) or "")
    return writes


def _requeue(tmp_path, id, dry_run=False):
    return route_requeue.requeue(tmp_path, tmp_path / "runs", id, dry_run=dry_run)


def test_an_empty_initiative_is_requeued_and_dropped(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [INTAKE, INITIATIVE])
    code, _ = _requeue(tmp_path, "demo")
    assert code == 0
    assert writes == [
        {"initiative": "intake", "task_id": "idea", "state": "queued", "extra": {}},
        {"initiative": "demo", "task_id": "demo", "state": "dropped"},
    ]


def test_an_initiative_with_a_ready_task_is_refused(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [INTAKE, INITIATIVE, READY])
    code, message = _requeue(tmp_path, "demo")
    assert (code, writes) == (2, [])
    assert "a (ready)" in message


def test_an_unknown_id_is_refused(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [INTAKE, INITIATIVE])
    code, message = _requeue(tmp_path, "nope")
    assert (code, writes) == (2, [])
    assert "nope" in message


def test_a_second_requeue_writes_nothing(tmp_path, monkeypatch):
    queued = {**INTAKE, "state": "queued", "extra": {}}
    writes = _store(monkeypatch, [queued, {**INITIATIVE, "state": "dropped"}])
    code, message = _requeue(tmp_path, "idea")
    assert (code, writes) == (0, [])
    assert "already requeued" in message


LINKED = {**INTAKE, "extra": {"initiative": "demo", "id": "idea", "priority": 2}}


def _applied(rows, writes):
    keys = {(w["initiative"], w["task_id"]): w for w in writes}
    return [{**r, **keys.get((r["initiative"], r["task_id"]), {})} for r in rows]


def _after_requeue():
    rows = [LINKED, INITIATIVE]
    plan = route_requeue.plan_requeue(rows, "demo")
    return _applied(rows, plan.writes)


def test_the_planned_intake_write_is_a_full_row_without_the_initiative_link():
    rows = copy.deepcopy([LINKED, INITIATIVE])

    plan = route_requeue.plan_requeue(rows, "demo")

    assert plan.writes[0] == {
        "initiative": "intake",
        "task_id": "idea",
        "state": "queued",
        "extra": {"id": "idea", "priority": 2},
    }
    assert plan.writes[1] == {"initiative": "demo", "task_id": "demo", "state": "dropped"}
    assert rows == [LINKED, INITIATIVE]


def test_a_queued_intake_with_a_stale_link_is_written_to_clear_it():
    rows = [{**LINKED, "state": "queued"}, {**INITIATIVE, "state": "dropped"}]

    plan = route_requeue.plan_requeue(rows, "demo")

    assert plan.writes == (
        {"initiative": "intake", "task_id": "idea", "state": "queued", "extra": {"id": "idea", "priority": 2}},
    )


def test_the_requeued_intake_is_counted_as_queued():
    assert [r["task_id"] for r in chair_read_intake.intake_from_rows(_after_requeue())] == ["idea"]
    assert chair_read_intake.intake_from_rows([LINKED, INITIATIVE]) == []


def test_the_requeued_intake_exports_without_an_initiative_field():
    content = {"title": "Idea", "needs": [], "surfaces": [], "body": "\nBody.\n"}
    (intake,) = [r for r in _after_requeue() if r["kind"] == "intake"]

    assert "initiative:" in queue_rows.render_item({**LINKED, **content})
    assert "initiative:" not in queue_rows.render_item({**intake, **content})


def test_a_second_requeue_plans_nothing_by_initiative_or_by_intake_id():
    rows = _after_requeue()

    for id in ("demo", "idea"):
        plan = route_requeue.plan_requeue(rows, id)
        assert (plan.writes, plan.note.startswith("already requeued")) == ((), True)


def test_a_dry_run_writes_nothing(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [INTAKE, INITIATIVE])
    code, message = _requeue(tmp_path, "demo", dry_run=True)
    assert (code, writes) == (0, [])
    assert message.startswith("would requeue")


def test_an_initiative_whose_only_tasks_are_dropped_counts_as_empty(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [INTAKE, INITIATIVE, DROPPED])
    code, _ = _requeue(tmp_path, "demo")
    assert code == 0
    assert [row["state"] for row in writes] == ["queued", "dropped"]


UNLINKED_INTAKE = {"kind": "intake", "initiative": "intake", "task_id": "idea", "state": "queued", "extra": {}}
UNLINKED_INITIATIVE = {"kind": "initiative", "initiative": "demo", "task_id": "demo", "state": "todo"}


def _initiative_md(tmp_path, intake):
    path = tmp_path / "work" / "demo" / "initiative.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nintake: {intake}\n---\n\nbody\n")


def test_initiative_md_names_the_intake_when_the_row_has_no_initiative(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [UNLINKED_INTAKE, UNLINKED_INITIATIVE])
    _initiative_md(tmp_path, "intake/idea.md")
    code, message = _requeue(tmp_path, "demo")
    assert code == 0
    assert writes == [{"initiative": "demo", "task_id": "demo", "state": "dropped"}]
    assert "intake idea queued" in message


def test_a_field_naming_an_unknown_intake_is_refused(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [UNLINKED_INTAKE, UNLINKED_INITIATIVE])
    _initiative_md(tmp_path, "intake/ghost.md")
    code, message = _requeue(tmp_path, "demo")
    assert (code, writes) == (2, [])
    assert "ghost" in message


def test_a_field_naming_a_non_queued_intake_is_refused(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [{**UNLINKED_INTAKE, "state": "decomposed"}, UNLINKED_INITIATIVE])
    _initiative_md(tmp_path, "intake/idea.md")
    code, message = _requeue(tmp_path, "demo")
    assert (code, writes) == (2, [])
    assert "decomposed, not queued" in message


def test_no_row_and_no_field_keeps_the_original_refusal(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [UNLINKED_INTAKE, UNLINKED_INITIATIVE])
    code, message = _requeue(tmp_path, "demo")
    assert (code, writes) == (2, [])
    assert message == "requeue: refused: no intake or initiative named demo, or no intake linked to the initiative"


def test_the_row_link_wins_over_the_field(tmp_path, monkeypatch):
    other = {**UNLINKED_INTAKE, "task_id": "other"}
    writes = _store(monkeypatch, [INTAKE, other, INITIATIVE])
    _initiative_md(tmp_path, "intake/other.md")
    code, _ = _requeue(tmp_path, "demo")
    assert code == 0
    assert writes[0] == {"initiative": "intake", "task_id": "idea", "state": "queued", "extra": {}}


SAME_INTAKE = {
    "kind": "intake",
    "initiative": "intake",
    "task_id": "demo",
    "state": "decomposed",
    "extra": {"initiative": "demo"},
}
SAME_INITIATIVE = {**INITIATIVE, "extra": {"intake": "intake/demo.md"}}


def test_requeueing_an_intake_whose_id_is_its_initiative_id_names_the_fresh_id(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [SAME_INTAKE, SAME_INITIATIVE])
    code, message = _requeue(tmp_path, "demo")
    assert code == 0
    assert "the next decompose uses demo-2" in message
    assert writes == [
        {"initiative": "intake", "task_id": "demo", "state": "queued", "extra": {}},
        {"initiative": "demo", "task_id": "demo", "state": "dropped"},
    ]


def test_the_dry_run_message_names_the_fresh_id(tmp_path, monkeypatch):
    writes = _store(monkeypatch, [SAME_INTAKE, SAME_INITIATIVE])
    code, message = _requeue(tmp_path, "demo", dry_run=True)
    assert (code, writes) == (0, [])
    assert message.startswith("would requeue:")
    assert "demo-2" in message


def test_an_already_requeued_pair_names_the_fresh_id(tmp_path, monkeypatch):
    queued = {**SAME_INTAKE, "state": "queued", "extra": {}}
    writes = _store(monkeypatch, [queued, {**SAME_INITIATIVE, "state": "dropped"}])
    code, message = _requeue(tmp_path, "demo")
    assert (code, writes) == (0, [])
    assert "already requeued" in message
    assert "demo-2" in message


def test_an_initiative_id_that_differs_from_the_intake_id_gets_no_fresh_id_sentence():
    plan = route_requeue.plan_requeue([INTAKE, INITIATIVE], "demo")
    assert plan.note == "intake idea queued, initiative demo dropped"


def test_the_fresh_id_skips_an_initiative_id_already_taken():
    taken = {**SAME_INITIATIVE, "task_id": "demo-2", "state": "dropped"}
    plan = route_requeue.plan_requeue([SAME_INTAKE, SAME_INITIATIVE, taken], "demo")
    assert "the next decompose uses demo-3" in plan.note


STALE_MD = "---\nid: idea\ninitiative: demo\ntitle: Idea\n---\n\nBody.\ninitiative: in the body\n"
CLEAN_MD = "---\nid: idea\ntitle: Idea\n---\n\nBody.\ninitiative: in the body\n"


def _files(tmp_path, other=False, done=False):
    intake = tmp_path / "intake" / "done" / "idea.md" if done else tmp_path / "intake" / "idea.md"
    intake.parent.mkdir(parents=True)
    intake.write_text(STALE_MD)
    initiative = tmp_path / "work" / "demo" / "initiative.md"
    initiative.parent.mkdir(parents=True)
    initiative.write_text("---\nintake: intake/idea.md\n---\n")
    if other:
        (initiative.parent / "notes.md").write_text("keep\n")
    return intake, initiative


def test_without_initiative_line_edits_the_frontmatter_only():
    assert route_requeue.without_initiative_line(STALE_MD) == CLEAN_MD
    assert route_requeue.without_initiative_line(CLEAN_MD) == CLEAN_MD
    assert route_requeue.without_initiative_line("initiative: demo\n") == "initiative: demo\n"


def test_a_requeue_cleans_the_intake_file_and_removes_the_initiative_folder(tmp_path, monkeypatch):
    _store(monkeypatch, [INTAKE, INITIATIVE])
    intake, initiative = _files(tmp_path)
    code, _ = _requeue(tmp_path, "demo")
    assert code == 0
    assert intake.read_text() == CLEAN_MD
    assert not initiative.parent.exists()


def test_a_requeue_cleans_the_done_copy_of_the_intake(tmp_path, monkeypatch):
    _store(monkeypatch, [INTAKE, INITIATIVE])
    intake, _ = _files(tmp_path, done=True)
    _requeue(tmp_path, "demo")
    assert intake.read_text() == CLEAN_MD


def test_a_work_folder_holding_another_file_is_kept(tmp_path, monkeypatch):
    _store(monkeypatch, [INTAKE, INITIATIVE])
    _, initiative = _files(tmp_path, other=True)
    _requeue(tmp_path, "demo")
    assert not initiative.exists()
    assert (initiative.parent / "notes.md").read_text() == "keep\n"


def test_a_dry_run_leaves_the_files_untouched(tmp_path, monkeypatch):
    _store(monkeypatch, [INTAKE, INITIATIVE])
    intake, initiative = _files(tmp_path)
    _requeue(tmp_path, "demo", dry_run=True)
    assert (intake.read_text(), initiative.is_file()) == (STALE_MD, True)


def test_a_refused_requeue_leaves_the_files_untouched(tmp_path, monkeypatch):
    _store(monkeypatch, [INTAKE, INITIATIVE, READY])
    intake, initiative = _files(tmp_path)
    code, _ = _requeue(tmp_path, "demo")
    assert (code, intake.read_text(), initiative.is_file()) == (2, STALE_MD, True)


def test_a_requeue_with_the_files_absent_returns_zero(tmp_path, monkeypatch):
    _store(monkeypatch, [INTAKE, INITIATIVE])
    code, _ = _requeue(tmp_path, "demo")
    assert code == 0


def test_a_second_requeue_after_a_clean_first_is_a_no_op(tmp_path, monkeypatch):
    queued = {**INTAKE, "state": "queued", "extra": {}}
    writes = _store(monkeypatch, [queued, {**INITIATIVE, "state": "dropped"}])
    intake, _ = _files(tmp_path)
    intake.write_text(CLEAN_MD)
    code, message = _requeue(tmp_path, "idea")
    assert (code, writes, intake.read_text()) == (0, [], CLEAN_MD)
    assert "already requeued" in message


def test_a_requeue_with_no_rows_left_still_cleans_a_stale_file(tmp_path, monkeypatch):
    queued = {**INTAKE, "state": "queued", "extra": {}}
    writes = _store(monkeypatch, [queued, {**INITIATIVE, "state": "dropped"}])
    intake, initiative = _files(tmp_path)
    code, message = _requeue(tmp_path, "idea")
    assert (code, writes, intake.read_text(), initiative.parent.exists()) == (0, [], CLEAN_MD, False)
    assert "already requeued" in message


def test_the_requeued_intake_reads_back_as_queued_and_shows_no_drift(tmp_path, monkeypatch):
    _store(monkeypatch, [INTAKE, INITIATIVE])
    _files(tmp_path)
    _requeue(tmp_path, "demo")
    files = {p.name: p.read_text() for p in (tmp_path / "intake").glob("*.md")}
    (entry,) = route.intake_entries(files)
    assert intake_state.intake_group(entry, {}, []) == "queued"
    groups = route.intake_groups([entry], [])
    assert [e["id"] for e in groups["queued"]] == ["idea"]
    on_file = [(e["path"], name) for name, entries in groups.items() for e in entries]
    stored = [{"task_id": "idea", "state": "queued"}]
    assert route_drift.drift([], [], on_file, stored) == []
