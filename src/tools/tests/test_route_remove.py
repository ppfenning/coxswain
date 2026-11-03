"""`route remove` core: an intake moves to done, an initiative is dropped through decline's path, a live run refuses."""

from datetime import UTC, datetime
from pathlib import Path

from agent_tools import draft_apply, route_remove
from agent_tools.store_cli import StateSet

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
INTAKE_ROWS = [{"kind": "intake", "state": "queued", "task_id": "idea", "initiative": None}]
INITIATIVE_ROWS = [
    {"kind": "task", "initiative": "demo", "task_id": "a", "state": "todo"},
    {"kind": "task", "initiative": "demo", "task_id": "b", "state": "ready"},
]


def files_store() -> draft_apply.Store:
    return draft_apply.Store("files", lambda initiative: [], lambda *args: None)


def make_ws(tmp_path: Path) -> Path:
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "idea.md").write_text("---\ntitle: Idea\n---\nBody\n")
    root = tmp_path / "work" / "demo"
    (root / "1-build").mkdir(parents=True)
    (root / "initiative.md").write_text("---\ntitle: T\ndraft: false\n---\nProse\n")
    (root / "1-build" / "a.md").write_text("---\nstate: todo\n---\nA\n")
    (root / "1-build" / "b.md").write_text("---\nstate: ready\n---\nB\n")
    return tmp_path


def snapshot(ws: Path) -> dict[str, str]:
    return {str(p): p.read_text() for p in sorted(ws.rglob("*.md"))}


def run(ws: Path, id: str, rows: list[dict], live: tuple[str, ...] = (), dry_run: bool = False) -> int:
    return route_remove.remove(ws, id, "stale", "operator", NOW, rows, live, files_store(), dry_run=dry_run)


def test_the_removal_line_and_the_done_text():
    assert route_remove.removed_line("operator", "2026-10-05", "stale") == "removed by operator on 2026-10-05: stale"
    assert route_remove.intake_done_text("Body", "operator", "2026-10-05", "stale") == "Body\nremoved by operator on 2026-10-05: stale\n"


def test_remove_an_intake_moves_it_to_done_with_the_line(tmp_path):
    ws = make_ws(tmp_path)
    assert run(ws, "idea", INTAKE_ROWS) == 0
    assert not (ws / "intake" / "idea.md").exists()
    assert (ws / "intake" / "done" / "idea.md").read_text() == "---\ntitle: Idea\n---\nBody\nremoved by operator on 2026-10-05: stale\n"


def test_remove_an_initiative_drops_todo_and_ready_tickets(tmp_path):
    ws = make_ws(tmp_path)
    assert run(ws, "demo", INITIATIVE_ROWS) == 0
    demo = ws / "work" / "demo"
    assert [(demo / "1-build" / f"{t}.md").read_text() for t in "ab"] == ["---\nstate: dropped\n---\nA\n", "---\nstate: dropped\n---\nB\n"]
    text = (demo / "initiative.md").read_text()
    assert "declined_by: operator\n" in text
    assert 'declined_reason: "stale"\n' in text


def test_remove_on_a_live_run_is_refused_and_names_the_stop(tmp_path, capsys):
    ws = make_ws(tmp_path)
    before = snapshot(ws)
    assert run(ws, "demo", INITIATIVE_ROWS, live=("r1",)) == 2
    assert snapshot(ws) == before
    assert "cox runs stop r1" in capsys.readouterr().out


def test_dry_run_writes_nothing(tmp_path, capsys):
    ws = make_ws(tmp_path)
    before = snapshot(ws)
    assert run(ws, "demo", INITIATIVE_ROWS, dry_run=True) == 0
    assert run(ws, "idea", INTAKE_ROWS, dry_run=True) == 0
    assert snapshot(ws) == before
    assert "would remove" in capsys.readouterr().out


def test_store_mode_expects_the_real_old_state(tmp_path):
    ws, calls = make_ws(tmp_path), []
    rows = lambda initiative: [{"initiative": initiative, "task_id": t, "state": "todo"} for t in "ab"]  # noqa: E731
    store = draft_apply.Store("store", rows, lambda i, t, to, by, expected: calls.append((t, to, expected)) or StateSet({}))
    assert route_remove.remove(ws, "demo", "stale", "operator", NOW, INITIATIVE_ROWS, (), store) == 0
    assert calls == [("a", "dropped", "todo"), ("b", "dropped", "ready")]


def queued(*names: str) -> list[dict]:
    return [{"kind": "intake", "state": "queued", "task_id": name, "initiative": None} for name in names]


def add_intakes(ws: Path, *names: str) -> None:
    for name in names:
        (ws / "intake" / f"{name}.md").write_text(f"---\ntitle: {name}\n---\nBody\n")


def add_initiative(ws: Path, name: str) -> list[dict]:
    root = ws / "work" / name
    (root / "1-build").mkdir(parents=True)
    (root / "initiative.md").write_text("---\ntitle: T\ndraft: false\n---\nProse\n")
    (root / "1-build" / "a.md").write_text("---\nstate: todo\n---\nA\n")
    return [{"kind": "task", "initiative": name, "task_id": "a", "state": "todo"}]


def run_many(
    ws: Path, ids: list[str], rows: list[dict], live: dict[str, tuple[str, ...]] | None = None, **kwargs
) -> int:
    return route_remove.remove_many(ws, ids, "stale", "operator", NOW, rows, live or {}, files_store(), **kwargs)


def test_expand_targets_keeps_the_given_order_and_drops_repeats():
    assert route_remove.expand_targets(queued("a", "b"), ["b", "a", "b"], None) == (["b", "a"], None)
    assert route_remove.expand_targets(INITIATIVE_ROWS, ["demo"], None) == (["demo"], None)
    assert route_remove.expand_targets([], [], None) == ([], "no targets given")


def test_expand_targets_globs_queued_intake_ids_only():
    rows = queued("idea-1", "idea-2", "other") + [{"kind": "intake", "state": "done", "task_id": "idea-3"}]
    assert route_remove.expand_targets(rows, ["other"], "idea-*") == (["other", "idea-1", "idea-2"], None)
    assert route_remove.expand_targets(rows, [], "IDEA-*") == ([], "no queued intake matches IDEA-*")


def test_remove_many_removes_two_explicit_ids(tmp_path, capsys):
    ws = make_ws(tmp_path)
    add_intakes(ws, "two")
    assert run_many(ws, ["idea", "two"], queued("idea", "two")) == 0
    assert sorted(p.name for p in (ws / "intake" / "done").iterdir()) == ["idea.md", "two.md"]
    assert "removed 2 of 2" in capsys.readouterr().out


def test_remove_many_by_glob_takes_two_of_three(tmp_path, capsys):
    ws = make_ws(tmp_path)
    add_intakes(ws, "idea-2", "other")
    assert run_many(ws, [], queued("idea", "idea-2", "other"), match="idea*") == 0
    assert (ws / "intake" / "other.md").exists()
    assert "removed 2 of 2" in capsys.readouterr().out


def test_remove_many_with_a_glob_matching_none_is_refused(tmp_path, capsys):
    ws = make_ws(tmp_path)
    before = snapshot(ws)
    assert run_many(ws, [], INTAKE_ROWS, match="zzz*") == 2
    assert snapshot(ws) == before
    assert "no queued intake matches zzz*" in capsys.readouterr().out


def test_a_glob_matching_none_empties_the_list_even_beside_explicit_ids(tmp_path, capsys):
    ws = make_ws(tmp_path)
    before = snapshot(ws)
    assert route_remove.expand_targets(INTAKE_ROWS, ["idea"], "zzz*") == ([], "no queued intake matches zzz*")
    assert run_many(ws, ["idea"], INTAKE_ROWS, match="zzz*") == 2
    assert snapshot(ws) == before
    assert "no queued intake matches zzz*" in capsys.readouterr().out


def test_an_intake_named_two_ways_is_listed_once():
    rows = [{"kind": "intake", "state": "queued", "task_id": "idea", "extra": {"id": "IDEA-7"}}]
    assert route_remove.expand_targets(rows, ["IDEA-7"], "idea*") == (["idea"], None)
    assert route_remove.expand_targets(rows, [], "IDEA-*") == (["idea"], None)


def test_remove_many_goes_on_after_a_refused_target(tmp_path, capsys):
    ws = make_ws(tmp_path)
    assert run_many(ws, ["demo", "idea"], INITIATIVE_ROWS + INTAKE_ROWS, live={"demo": ("r1",)}) == 2
    assert (ws / "intake" / "done" / "idea.md").exists()
    assert (ws / "work" / "demo" / "1-build" / "a.md").read_text() == "---\nstate: todo\n---\nA\n"
    out = capsys.readouterr().out
    assert "cox runs stop r1" in out
    assert "removed 1 of 2" in out


def test_a_live_run_on_one_initiative_does_not_block_another(tmp_path, capsys):
    ws = make_ws(tmp_path)
    rows = INITIATIVE_ROWS + add_initiative(ws, "other")
    assert run_many(ws, ["demo", "other"], rows, live={"demo": ("r1",)}) == 2
    assert (ws / "work" / "demo" / "1-build" / "a.md").read_text() == "---\nstate: todo\n---\nA\n"
    assert (ws / "work" / "other" / "1-build" / "a.md").read_text() == "---\nstate: dropped\n---\nA\n"
    out = capsys.readouterr().out
    assert out.count("cox runs stop") == 1
    assert "removed 1 of 2" in out


def test_remove_many_dry_run_leaves_every_file(tmp_path, capsys):
    ws = make_ws(tmp_path)
    add_intakes(ws, "idea-2")
    before = snapshot(ws)
    assert run_many(ws, [], queued("idea", "idea-2"), match="idea*", dry_run=True) == 0
    assert snapshot(ws) == before
    assert "would remove 2 of 2" in capsys.readouterr().out
