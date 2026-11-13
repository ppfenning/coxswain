"""`route hold` core: held and released texts, the plan's refusals, the write order, and the queue export round trip."""

from datetime import UTC, datetime
from pathlib import Path

from agent_tools import queue_rows, route_hold

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FREE = "---\ntitle: Idea\npriority: 2\n---\nBody\n"
HELD = '---\ntitle: Idea\npriority: 2\nstage: draft\nhold_reason: "held by pat on 2026-11-03: waiting on design"\n---\nBody\n'
READY = "---\ntitle: Idea\npriority: 2\nstage: ready\n---\nBody\n"
LEGACY = '---\ntitle: Idea\nheld: true\npriority: 2\nhold_reason: "old"\n---\nBody\n'
ROWS = [
    {"kind": "intake", "state": "queued", "task_id": "idea", "initiative": "intake", "extra": {"title": "Idea"}},
    {"kind": "intake", "state": "done", "task_id": "old", "initiative": "intake"},
    {"kind": "task", "initiative": "demo", "task_id": "a", "state": "todo"},
    {"kind": "initiative", "initiative": "demo", "task_id": "demo", "state": "ready", "extra": {"id": "demo", "repo": "r"}},
    {"kind": "task", "initiative": "ghost", "task_id": "a", "state": "todo"},
]
INIT_FREE = "---\nid: demo\ntitle: Demo\n---\nBody\n"
INIT_HELD = '---\nid: demo\ntitle: Demo\nheld: true\nhold_reason: "held by pat on 2026-11-03: waiting on design"\n---\nBody\n'


def test_held_text_sets_both_fields_and_leaves_the_body():
    assert route_hold.held_text(FREE, "waiting on design", "pat", "2026-11-03") == HELD


def test_holding_again_changes_only_the_reason():
    again = route_hold.held_text(HELD, "  needs\nlegal ", "sam", "2026-11-04")
    assert again == HELD.replace("pat on 2026-11-03: waiting on design", "sam on 2026-11-04: needs legal")


def test_held_text_adds_front_matter_to_a_bare_file():
    assert route_hold.held_text("Body\n", "x", "pat", "2026-11-03").endswith('---\nBody\n')


def test_release_sets_ready_and_removes_the_reason_and_leaves_the_rest():
    assert route_hold.released_text(HELD) == READY


def test_releasing_a_ready_intake_returns_the_text_unchanged():
    assert route_hold.released_text(READY) == READY


def test_held_and_released_text_drop_a_legacy_held_line():
    assert route_hold.held_text(LEGACY, "x", "pat", "2026-11-03").count("held:") == 0
    assert route_hold.held_text(LEGACY, "x", "pat", "2026-11-03") == (
        '---\ntitle: Idea\npriority: 2\nhold_reason: "held by pat on 2026-11-03: x"\nstage: draft\n---\nBody\n'
    )
    assert route_hold.released_text(LEGACY) == READY


def test_holding_a_draft_with_the_same_reason_returns_the_text_unchanged():
    assert route_hold.held_text(HELD, "waiting on design", "pat", "2026-11-03") == HELD


def test_staged_text_sets_only_the_stage():
    assert route_hold.staged_text(FREE, "draft") == "---\ntitle: Idea\npriority: 2\nstage: draft\n---\nBody\n"
    assert route_hold.staged_text(HELD, "ready") == HELD.replace("stage: draft", "stage: ready")
    assert route_hold.staged_text(LEGACY, "draft") == LEGACY.replace("hold_reason: \"old\"\n", "hold_reason: \"old\"\nstage: draft\n")


def test_staged_text_returns_the_text_unchanged_for_a_repeated_move():
    assert route_hold.staged_text(HELD, "draft") == HELD
    assert route_hold.staged_text(READY, "ready") == READY


def test_staged_text_keeps_every_other_line_and_the_body_byte_for_byte():
    text = "---\ntitle:   Odd  \n# note\ntags:\n  - a\n  - b\nstage: ready\nz: 1\n---\n\nBody  \n\n"
    assert route_hold.staged_text(text, "draft") == text.replace("stage: ready", "stage: draft")


def test_staged_text_adds_front_matter_to_a_bare_file():
    assert route_hold.staged_text("Body\n", "ready") == "---\nstage: ready\n---\nBody\n"


def test_crlf_files_keep_their_line_ending():
    text = FREE.replace("\n", "\r\n")
    assert route_hold.held_text(text, "x", "pat", "2026-11-03").count("\r\n") == text.count("\r\n") + 2
    assert route_hold.released_text(route_hold.held_text(text, "x", "pat", "2026-11-03")) == READY.replace("\n", "\r\n")
    assert route_hold.staged_text(text, "draft").count("\r\n") == text.count("\r\n") + 1


def test_the_row_helpers_put_stage_in_extra_and_drop_held():
    row = {"kind": "intake", "extra": {"title": "Idea", "held": "true", "hold_reason": "r"}}
    assert route_hold.HELD_KEYS == ("stage", "held", "hold_reason")
    assert route_hold._row_held(row, "why")["extra"] == {"title": "Idea", "stage": "draft", "hold_reason": "why"}
    assert route_hold._row_without_held(row)["extra"] == {"title": "Idea", "stage": "ready"}
    assert route_hold._row_staged(row, "draft")["extra"] == {"title": "Idea", "held": "true", "hold_reason": "r", "stage": "draft"}
    assert route_hold._row_held({"kind": "intake"}, "why")["extra"] == {"stage": "draft", "hold_reason": "why"}


def test_the_initiative_row_helpers_keep_held_true():
    row = {"kind": "initiative", "extra": {"id": "demo"}}
    held = route_hold._initiative_row_held(row, "why")
    assert held["extra"] == {"id": "demo", "held": "true", "hold_reason": "why"}
    assert route_hold._initiative_row_without_held(held)["extra"] == {"id": "demo"}


def test_a_queued_intake_is_planned_by_its_task_id():
    assert route_hold.plan_hold(ROWS, "idea", "why").task_id == "idea"
    assert route_hold.plan_release(ROWS, "idea").task_id == "idea"


def test_an_initiative_id_is_planned():
    for plan in (route_hold.plan_hold(ROWS, "demo", "why"), route_hold.plan_release(ROWS, "demo")):
        assert plan == route_hold.route_guard.InitiativeFound("demo")


def test_an_id_that_names_an_intake_and_an_initiative_resolves_to_the_intake():
    rows = [*ROWS, {"kind": "task", "initiative": "idea", "task_id": "a", "state": "todo"}]
    assert route_hold.plan_hold(rows, "idea", "why") == route_hold.route_guard.QueuedIntake("idea")


def test_a_done_intake_and_an_unknown_id_are_refused_naming_the_id():
    for id in ("old", "nope"):
        assert route_hold.plan_hold(ROWS, id, "why") == route_hold.Refused(f"no queued intake named {id}")
        assert route_hold.plan_release(ROWS, id) == route_hold.Refused(f"no queued intake named {id}")


def test_a_hold_without_a_reason_is_refused():
    assert route_hold.plan_hold(ROWS, "idea", " ") == route_hold.Refused("a hold on idea needs a reason")


def make_ws(tmp_path: Path, text: str = FREE) -> Path:
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "idea.md").write_text(text)
    return tmp_path


def spy(monkeypatch, ws: Path, took: bool = True) -> list:
    calls = []
    path = ws / "intake" / "idea.md"
    monkeypatch.setattr(route_hold.run_store, "upsert_row", lambda runs_dir, row: calls.append(("store", path.read_text(), row)) or took)
    monkeypatch.setattr(route_hold.queue_export, "record_written_paths", lambda w, paths: calls.append(("file", path.read_text(), list(paths))))
    return calls


def test_hold_writes_the_store_first_and_the_file_second(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    calls = spy(monkeypatch, ws)
    assert route_hold.hold(ws, ws / "runs", "idea", "waiting on design", "pat", NOW, ROWS) == 0
    assert [c[0] for c in calls] == ["store", "file"]
    assert calls[0][1] == FREE and calls[1][1] == HELD
    assert calls[0][2]["extra"] == {"title": "Idea", "stage": "draft", "hold_reason": "held by pat on 2026-11-03: waiting on design"}
    assert calls[1][2] == [ws / "intake" / "idea.md"]


def test_release_readies_the_row_and_the_file(tmp_path, monkeypatch):
    ws = make_ws(tmp_path, HELD)
    calls = spy(monkeypatch, ws)
    held_rows = [{**ROWS[0], "extra": {"title": "Idea", "stage": "draft", "hold_reason": "r"}}]
    assert route_hold.release(ws, ws / "runs", "idea", held_rows) == 0
    assert calls[0][2]["extra"] == {"title": "Idea", "stage": "ready"}
    assert (ws / "intake" / "idea.md").read_text() == READY


def test_releasing_a_ready_intake_writes_nothing(tmp_path, monkeypatch):
    ws = make_ws(tmp_path, READY)
    calls = spy(monkeypatch, ws)
    ready_rows = [{**ROWS[0], "extra": {"title": "Idea", "stage": "ready"}}]
    assert route_hold.release(ws, ws / "runs", "idea", ready_rows) == 0
    assert calls == []


def test_set_stage_writes_the_stage_to_the_row_and_the_file_and_keeps_the_reason(tmp_path, monkeypatch):
    ws = make_ws(tmp_path, HELD)
    calls = spy(monkeypatch, ws)
    held_rows = [{**ROWS[0], "extra": {"title": "Idea", "stage": "draft", "hold_reason": "r"}}]
    assert route_hold.set_stage(ws, ws / "runs", "idea", "ready", held_rows, False) == 0
    assert calls[0][2]["extra"] == {"title": "Idea", "stage": "ready", "hold_reason": "r"}
    assert (ws / "intake" / "idea.md").read_text() == HELD.replace("stage: draft", "stage: ready")


def test_set_stage_dry_run_and_repeat_write_nothing(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, READY)
    calls = spy(monkeypatch, ws)
    ready_rows = [{**ROWS[0], "extra": {"title": "Idea", "stage": "ready"}}]
    assert route_hold.set_stage(ws, ws / "runs", "idea", "draft", ready_rows, True) == 0
    assert route_hold.set_stage(ws, ws / "runs", "idea", "ready", ready_rows, False) == 0
    assert capsys.readouterr().out == "would stage intake idea: stage draft\nstage: intake idea: nothing to change\n"
    assert calls == [] and (ws / "intake" / "idea.md").read_text() == READY


def test_set_stage_refuses_an_id_that_is_not_a_queued_intake_naming_the_id(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    calls = spy(monkeypatch, ws)
    for id in ("old", "nope", "demo"):
        assert route_hold.plan_stage(ROWS, id) == route_hold.Refused(f"no queued intake named {id}")
        assert route_hold.set_stage(ws, ws / "runs", id, "draft", ROWS, False) == 2
    assert route_hold.set_stage(ws, ws / "runs", "idea", "later", ROWS, False) == 2
    assert capsys.readouterr().out == (
        "stage: refused: no queued intake named old\nstage: refused: no queued intake named nope\n"
        "stage: refused: no queued intake named demo\nstage: refused: later is not a stage for idea; use draft or ready\n"
    )
    assert calls == [] and (ws / "intake" / "idea.md").read_text() == FREE


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    calls = spy(monkeypatch, ws)
    assert route_hold.hold(ws, ws / "runs", "idea", "why", "pat", NOW, ROWS, dry_run=True) == 0
    assert capsys.readouterr().out == "would hold intake idea: held by pat on 2026-11-03: why\n"
    assert calls == [] and (ws / "intake" / "idea.md").read_text() == FREE


def test_a_refusal_writes_nothing_and_names_the_id(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    calls = spy(monkeypatch, ws)
    assert route_hold.hold(ws, ws / "runs", "nope", "why", "pat", NOW, ROWS) == 2
    assert route_hold.release(ws, ws / "runs", "old", ROWS) == 2
    assert capsys.readouterr().out == "hold: refused: no queued intake named nope\nrelease: refused: no queued intake named old\n"
    assert calls == [] and (ws / "intake" / "idea.md").read_text() == FREE


def test_a_store_that_refuses_leaves_the_file_alone(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    calls = spy(monkeypatch, ws, took=False)
    assert route_hold.hold(ws, ws / "runs", "idea", "why", "pat", NOW, ROWS) == 1
    assert [c[0] for c in calls] == ["store"] and (ws / "intake" / "idea.md").read_text() == FREE


def test_a_missing_intake_file_is_refused(tmp_path, monkeypatch):
    (tmp_path / "intake").mkdir()
    calls = spy(monkeypatch, tmp_path)
    assert route_hold.hold(tmp_path, tmp_path / "runs", "idea", "why", "pat", NOW, ROWS) == 2
    assert calls == []


def test_a_queue_export_round_trip_keeps_the_hold():
    row = queue_rows.parse_item("intake", ("idea.md",), HELD)
    kept = queue_rows.render_item(row)
    assert "stage: draft\n" in kept
    assert "hold_reason: 'held by pat on 2026-11-03: waiting on design'" in kept
    assert queue_rows.parse_item("intake", ("idea.md",), kept) == row
    assert "hold_reason" not in route_hold.released_text(kept) and "stage: ready\n" in route_hold.released_text(kept)


def make_initiative(ws: Path, name: str = "demo", text: str = INIT_FREE) -> Path:
    (ws / "work" / name).mkdir(parents=True)
    path = ws / "work" / name / "initiative.md"
    path.write_text(text)
    return path


def spy_initiative(monkeypatch, path: Path, took: bool = True) -> list:
    calls = []
    monkeypatch.setattr(route_hold.run_store, "upsert_row", lambda runs_dir, row: calls.append(("store", path.read_text(), row)) or took)
    monkeypatch.setattr(route_hold.queue_export, "record_written_paths", lambda w, paths: calls.append(("file", path.read_text(), list(paths))))
    return calls


def test_holding_then_releasing_an_initiative_round_trips_the_file_and_the_row(tmp_path, monkeypatch):
    path = make_initiative(tmp_path)
    calls = spy_initiative(monkeypatch, path)
    assert route_hold.hold(tmp_path, tmp_path / "runs", "demo", "waiting on design", "pat", NOW, ROWS) == 0
    assert [c[0] for c in calls] == ["store", "file"]
    assert calls[0][1] == INIT_FREE and calls[1][1] == INIT_HELD
    held = {"id": "demo", "repo": "r", "held": "true", "hold_reason": "held by pat on 2026-11-03: waiting on design"}
    assert calls[0][2]["extra"] == held and calls[1][2] == [path]
    assert route_hold.release(tmp_path, tmp_path / "runs", "demo", [ROWS[2], {**ROWS[3], "extra": held}]) == 0
    assert calls[2][2]["extra"] == {"id": "demo", "repo": "r"}
    assert path.read_text() == INIT_FREE


def test_releasing_an_initiative_that_is_not_held_writes_nothing(tmp_path, monkeypatch):
    path = make_initiative(tmp_path)
    calls = spy_initiative(monkeypatch, path)
    assert route_hold.release(tmp_path, tmp_path / "runs", "demo", ROWS) == 0
    assert calls == [] and path.read_text() == INIT_FREE


def test_a_missing_initiative_file_is_refused(tmp_path, monkeypatch, capsys):
    calls = spy_initiative(monkeypatch, tmp_path / "none.md")
    assert route_hold.hold(tmp_path, tmp_path / "runs", "demo", "why", "pat", NOW, ROWS) == 2
    assert capsys.readouterr().out == f"hold: refused: no initiative file at {tmp_path / 'work' / 'demo' / 'initiative.md'}\n"
    assert calls == []


def test_an_initiative_with_no_store_row_is_refused(tmp_path, monkeypatch, capsys):
    path = make_initiative(tmp_path, "ghost")
    calls = spy_initiative(monkeypatch, path)
    assert route_hold.release(tmp_path, tmp_path / "runs", "ghost", ROWS) == 2
    assert capsys.readouterr().out == "release: refused: no initiative row for ghost in the store\n"
    assert calls == []


def test_dry_run_on_an_initiative_prints_a_would_line_and_writes_nothing(tmp_path, monkeypatch, capsys):
    path = make_initiative(tmp_path)
    calls = spy_initiative(monkeypatch, path)
    assert route_hold.hold(tmp_path, tmp_path / "runs", "demo", "why", "pat", NOW, ROWS, dry_run=True) == 0
    assert capsys.readouterr().out == "would hold initiative demo: held by pat on 2026-11-03: why\n"
    assert calls == [] and path.read_text() == INIT_FREE
