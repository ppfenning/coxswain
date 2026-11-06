"""Intake filing, entries, status lines and priority carry `after`, `held` and `priority`."""

from agent_tools import intake_queue, route, run_store


def test_intake_file_default_has_no_after_line():
    files = route.intake_file("Ship it", "Body.", "repo", "2026-11-03")

    assert files == {
        "intake/2026-11-03-ship-it.md": "---\nid: ship-it\ntitle: Ship it\nrepo: repo\n---\n\nBody.\n"
    }


def test_intake_file_writes_after_as_a_list_in_slug_mode():
    files = route.intake_file("Ship it", "Body.", "repo", "2026-11-03", after=["a", "b"])

    assert files == {
        "intake/2026-11-03-ship-it.md": "---\nid: ship-it\ntitle: Ship it\nrepo: repo\nafter: [a, b]\n---\n\nBody.\n"
    }


def test_intake_file_writes_after_in_sequence_mode():
    files = route.intake_file("Ship it", "", "repo", "2026-11-03", ids="sequence", allocate=lambda: "i7", after=["i3"])

    assert files == {"intake/i7.md": "---\nid: i7\ntitle: Ship it\nafter: [i3]\n---\n\nShip it\n"}


def test_intake_file_after_round_trips_through_the_entry():
    (text,) = route.intake_file("Ship it", "Body.", "repo", "2026-11-03", after=["a", "b"]).values()

    assert route.intake_entries({"x.md": text})[0]["after"] == ["a", "b"]


def test_entry_carries_after_held_and_priority():
    text = "---\nid: idea\ntitle: Idea\nafter: [a, b]\nheld: true\npriority: 4\n---\n\nBody.\n"

    assert route.intake_entries({"idea.md": text}) == [
        {
            "id": "idea",
            "title": "Idea",
            "initiative": None,
            "done": False,
            "path": "intake/idea.md",
            "after": ["a", "b"],
            "held": True,
            "priority": 4,
        }
    ]


def test_entry_defaults_when_the_front_matter_has_none_of_the_three():
    text = "---\nid: idea\ntitle: Idea\n---\n\nBody.\n"

    entry = route.intake_entries({"idea.md": text})[0]

    assert (entry["after"], entry["held"], entry["priority"]) == ([], False, 0)


def test_entry_reads_a_block_list_after_and_a_negative_priority():
    text = "---\nid: idea\nafter:\n  - a\n  - b\nheld: false\npriority: -2\n---\n\nBody.\n"

    entry = route.intake_entries({"idea.md": text})[0]

    assert (entry["after"], entry["held"], entry["priority"]) == (["a", "b"], False, -2)


GROUPS = {
    "queued": [{"id": "one"}, {"id": "two"}, {"id": "three"}],
    "decomposed": [],
    "landed": [],
}


def test_status_default_is_unchanged():
    assert route.render_status([], GROUPS) == "\n".join(
        ["intake queued: 3 — one, two, three", "intake decomposed: 0", "intake landed: 0"]
    )


def test_a_waiting_queued_entry_names_what_it_waits_on():
    waiting = {"two": ["one"], "three": ["one", "two"]}

    assert route.render_status([], GROUPS, waiting=waiting).splitlines()[0] == (
        "intake queued: 3 — one, two (waiting on one), three (waiting on one, two)"
    )


def test_group_line_ignores_waiting_outside_the_queued_group():
    assert route._intake_group_line("landed", [{"id": "one"}], {"one": ["x"]}) == "intake landed: 1 — one"


def test_held_rows_render_under_a_held_heading_with_the_reason():
    held = [{"id": "one", "reason": "needs a decision"}, {"id": "two", "reason": ""}]

    assert route.render_status([], GROUPS, held=held).splitlines()[3:] == [
        "held:",
        "  one: needs a decision",
        "  two",
    ]


def test_held_comes_before_problems_and_gate():
    out = route.render_status([], None, ["bad"], "open", held=[{"id": "one", "reason": "r"}])

    assert out.splitlines() == ["held:", "  one: r", "problem: bad", "gate: open"]


def test_empty_held_adds_nothing():
    assert route.render_status([], GROUPS, held=[]) == route.render_status([], GROUPS)


def _row(priority):
    return {
        "kind": "intake",
        "initiative": "intake",
        "task_id": "idea",
        "phase": "",
        "state": "queued",
        "needs": [],
        "title": "idea",
        "surfaces": [],
        "body": "",
        "extra": {},
        "priority": priority,
    }


def test_set_intake_priority_adds_the_key_to_a_file_without_one(tmp_path, monkeypatch):
    path = tmp_path / "intake" / "idea.md"
    path.parent.mkdir()
    path.write_text("---\nid: idea\ntitle: idea\n---\n\nBody.\n")
    taken = []
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: taken.append(row) or True)

    assert route.set_intake_priority(tmp_path / "runs", tmp_path, _row(0), 5) == 0

    assert [r["priority"] for r in taken] == [5]
    assert path.read_text() == "---\nid: idea\ntitle: idea\npriority: 5\n---\n\nBody.\n"


def test_set_intake_priority_replaces_an_existing_key_in_place(tmp_path, monkeypatch):
    path = tmp_path / "intake" / "idea.md"
    path.parent.mkdir()
    path.write_text("---\nid: idea\npriority: 1\ntitle: idea\n---\n\nBody.\n")
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: True)

    assert route.set_intake_priority(tmp_path / "runs", tmp_path, _row(1), 9) == 0

    assert path.read_text() == "---\nid: idea\npriority: 9\ntitle: idea\n---\n\nBody.\n"


def test_set_intake_priority_leaves_the_file_when_the_store_refuses(tmp_path, monkeypatch, capsys):
    path = tmp_path / "intake" / "idea.md"
    path.parent.mkdir()
    path.write_text("---\nid: idea\n---\n\nBody.\n")
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: False)

    assert route.set_intake_priority(tmp_path / "runs", tmp_path, _row(0), 5) == 1

    assert path.read_text() == "---\nid: idea\n---\n\nBody.\n"
    assert "the store did not take the priority for intake idea" in capsys.readouterr().out


def test_set_intake_priority_without_a_file_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: True)

    assert route.set_intake_priority(tmp_path / "runs", tmp_path, _row(0), 5) == 0
    assert not (tmp_path / "intake").exists()


def test_a_single_quoted_true_hold_is_held_and_not_runnable():
    row = {"kind": "intake", "state": "queued", "task_id": "idea", "initiative": None, "extra": {"held": "'true'"}}

    assert intake_queue.runnable_intake([row]) == []
    assert intake_queue.held_rows([row]) == [row]
