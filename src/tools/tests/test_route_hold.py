"""`route hold` core: held and released texts, the plan's refusals, the write order, and the queue export round trip."""

from datetime import UTC, datetime
from pathlib import Path

from agent_tools import queue_rows, route_hold

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FREE = "---\ntitle: Idea\npriority: 2\n---\nBody\n"
HELD = '---\ntitle: Idea\npriority: 2\nheld: true\nhold_reason: "held by pat on 2026-11-03: waiting on design"\n---\nBody\n'
ROWS = [
    {"kind": "intake", "state": "queued", "task_id": "idea", "initiative": "intake", "extra": {"title": "Idea"}},
    {"kind": "intake", "state": "done", "task_id": "old", "initiative": "intake"},
    {"kind": "task", "initiative": "demo", "task_id": "a", "state": "todo"},
]


def test_held_text_sets_both_fields_and_leaves_the_body():
    assert route_hold.held_text(FREE, "waiting on design", "pat", "2026-11-03") == HELD


def test_holding_again_changes_only_the_reason():
    again = route_hold.held_text(HELD, "  needs\nlegal ", "sam", "2026-11-04")
    assert again == HELD.replace("pat on 2026-11-03: waiting on design", "sam on 2026-11-04: needs legal")


def test_held_text_adds_front_matter_to_a_bare_file():
    assert route_hold.held_text("Body\n", "x", "pat", "2026-11-03").endswith('---\nBody\n')


def test_release_removes_both_fields_and_leaves_the_rest():
    assert route_hold.released_text(HELD) == FREE


def test_releasing_a_free_intake_returns_the_text_unchanged():
    assert route_hold.released_text(FREE) == FREE


def test_crlf_files_keep_their_line_ending():
    text = FREE.replace("\n", "\r\n")
    assert route_hold.held_text(text, "x", "pat", "2026-11-03").count("\r\n") == text.count("\r\n") + 2
    assert route_hold.released_text(route_hold.held_text(text, "x", "pat", "2026-11-03")) == text


def test_a_queued_intake_is_planned_by_its_task_id():
    assert route_hold.plan_hold(ROWS, "idea", "why").task_id == "idea"
    assert route_hold.plan_release(ROWS, "idea").task_id == "idea"


def test_an_initiative_id_is_refused_naming_the_id():
    for plan in (route_hold.plan_hold(ROWS, "demo", "why"), route_hold.plan_release(ROWS, "demo")):
        assert plan == route_hold.Refused("demo is an initiative, not a queued intake")


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
    assert calls[0][2]["extra"] == {"title": "Idea", "held": "true", "hold_reason": "held by pat on 2026-11-03: waiting on design"}
    assert calls[1][2] == [ws / "intake" / "idea.md"]


def test_release_clears_the_row_and_the_file(tmp_path, monkeypatch):
    ws = make_ws(tmp_path, HELD)
    calls = spy(monkeypatch, ws)
    held_rows = [{**ROWS[0], "extra": {"title": "Idea", "held": "true", "hold_reason": "r"}}]
    assert route_hold.release(ws, ws / "runs", "idea", held_rows) == 0
    assert calls[0][2]["extra"] == {"title": "Idea"}
    assert (ws / "intake" / "idea.md").read_text() == FREE


def test_releasing_a_free_intake_writes_nothing(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    calls = spy(monkeypatch, ws)
    assert route_hold.release(ws, ws / "runs", "idea", ROWS) == 0
    assert calls == []


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    calls = spy(monkeypatch, ws)
    assert route_hold.hold(ws, ws / "runs", "idea", "why", "pat", NOW, ROWS, dry_run=True) == 0
    assert capsys.readouterr().out == "would hold intake idea: held by pat on 2026-11-03: why\n"
    assert calls == [] and (ws / "intake" / "idea.md").read_text() == FREE


def test_a_refusal_writes_nothing_and_names_the_id(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    calls = spy(monkeypatch, ws)
    assert route_hold.hold(ws, ws / "runs", "demo", "why", "pat", NOW, ROWS) == 2
    assert route_hold.release(ws, ws / "runs", "old", ROWS) == 2
    assert capsys.readouterr().out == "hold: refused: demo is an initiative, not a queued intake\nrelease: refused: no queued intake named old\n"
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
    assert "held: 'true'\n" in kept  # the loader reads every scalar as a string, so the renderer quotes it; a reader still sees true
    assert "hold_reason: 'held by pat on 2026-11-03: waiting on design'" in kept
    assert queue_rows.parse_item("intake", ("idea.md",), kept) == row
    assert route_hold.released_text(kept).count("held") == 0
