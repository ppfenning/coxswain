"""Intake filing, entries, status lines and priority carry `after`, `held` and `priority`."""

import pytest

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
            "stage": "draft",
            "priority": 4,
            "epic": "",
            "target_release": "",
            "repo": "",
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


def _filed(**fields):
    return route.intake_file("Ship it", "Body.", "repo", "2026-11-03", **fields)["intake/2026-11-03-ship-it.md"]


@pytest.mark.parametrize(
    ("fields", "line"),
    [
        ({"epic": "E1"}, "epic: E1"),
        ({"priority": 3}, "priority: 3"),
        ({"priority": -2}, "priority: -2"),
        ({"target_release": "2026.12.0"}, "target_release: 2026.12.0"),
        ({"epic": "a: b"}, 'epic: "a: b"'),
    ],
)
def test_intake_file_writes_each_field_as_its_own_line_after_repo(fields, line):
    assert _filed(**fields) == f"---\nid: ship-it\ntitle: Ship it\nrepo: repo\n{line}\n---\n\nBody.\n"


def test_intake_file_writes_the_fields_after_stage_and_before_after():
    text = _filed(stage="draft", epic="E1", priority=3, target_release="2026.12.0", after=["a"])

    assert text == (
        "---\nid: ship-it\ntitle: Ship it\nrepo: repo\nstage: draft\nepic: E1\npriority: 3\n"
        "target_release: 2026.12.0\nafter: [a]\n---\n\nBody.\n"
    )


def test_intake_file_empty_fields_write_no_line():
    assert _filed(epic="", priority="", target_release="") == _filed()


def test_intake_file_fields_round_trip_through_the_entry():
    entry = route.intake_entries({"x.md": _filed(epic="E1", priority=3, target_release="2026.12.0")})[0]

    assert (entry["epic"], entry["priority"], entry["target_release"], entry["repo"]) == ("E1", 3, "2026.12.0", "repo")


def test_entry_fields_default_to_empty_strings():
    entry = route.intake_entries({"idea.md": "---\nid: idea\n---\n\nBody.\n"})[0]

    assert (entry["epic"], entry["target_release"], entry["repo"]) == ("", "", "")


READY = {"id": "go", "stage": "ready", "repo": "widgets", "epic": "E1", "priority": 3, "target_release": "2026.12.0"}
DRAFT = {"id": "maybe", "stage": "draft", "repo": "widgets", "epic": "", "priority": 0, "target_release": ""}


def test_status_shows_a_ready_intake_with_its_fields_and_omits_empty_ones():
    groups = {"queued": [READY, {**READY, "id": "bare", "epic": "", "priority": 0, "target_release": ""}],
              "decomposed": [], "landed": []}

    assert route.render_status([], groups).splitlines()[0] == (
        "intake queued: 2 — go [stage ready, repo widgets, epic E1, priority 3, target 2026.12.0],"
        " bare [stage ready, repo widgets]"
    )


def test_status_lists_a_draft_under_its_own_line_apart_from_the_ready_queue():
    groups = {"queued": [DRAFT, READY], "decomposed": [], "landed": []}

    assert route.render_status([], groups).splitlines() == [
        "intake queued: 1 — go [stage ready, repo widgets, epic E1, priority 3, target 2026.12.0]",
        "intake drafts: 1 — maybe [stage draft, repo widgets]",
        "intake decomposed: 0",
        "intake landed: 0",
    ]


def test_status_has_no_drafts_line_without_a_draft():
    assert "drafts" not in route.render_status([], {"queued": [READY], "decomposed": [], "landed": []})


def test_status_shows_a_deferred_intake_target_in_its_group_and_on_its_queue_entry():
    groups = {"queued": [READY], "decomposed": [], "landed": []}

    lines = route.render_status([], groups, deferred={"2026.12.0": ["go"]}).splitlines()

    assert lines[0].endswith("target 2026.12.0]")
    assert lines[3:] == ["deferred to 2026.12.0:", "  go"]


def test_held_row_shows_its_tags_when_it_carries_them():
    held = [{"id": "maybe", "reason": "draft", "stage": "draft", "epic": "E1"}]

    assert route.render_status([], None, held=held).splitlines() == ["held:", "  maybe [stage draft, epic E1]: draft"]


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


def test_a_quoted_held_value_is_held_in_an_entry():
    text = "---\nid: idea\nheld: 'true'\n---\n\nBody.\n"

    (entry,) = route.intake_entries({"idea.md": text})

    assert (entry["held"], entry["stage"]) == (True, "draft")


def test_an_entry_with_no_stage_or_hold_is_ready():
    (entry,) = route.intake_entries({"idea.md": "---\nid: idea\n---\n\nBody.\n"})

    assert (entry["held"], entry["stage"]) == (False, "ready")


def test_a_draft_entry_is_held_and_not_runnable():
    (entry,) = route.intake_entries({"idea.md": "---\nid: idea\nstage: draft\n---\n\nBody.\n"})
    row = {"kind": "intake", "state": "queued", "task_id": "idea", "initiative": None, "extra": {"stage": "draft"}}

    assert (entry["held"], entry["stage"]) == (True, "draft")
    assert intake_queue.runnable_intake([row]) == []


def test_intake_file_writes_the_stage_after_repo():
    files = route.intake_file("Ship it", "Body.", "repo", "2026-11-03", stage="draft")

    assert files == {
        "intake/2026-11-03-ship-it.md": "---\nid: ship-it\ntitle: Ship it\nrepo: repo\nstage: draft\n---\n\nBody.\n"
    }


def test_intake_file_writes_a_ready_stage_in_sequence_mode():
    files = route.intake_file("Ship it", "", "repo", "2026-11-03", ids="sequence", allocate=lambda: "i7", stage="ready")

    assert files == {"intake/i7.md": "---\nid: i7\ntitle: Ship it\nstage: ready\n---\n\nShip it\n"}


def test_intake_file_omits_the_stage_line_when_none_is_passed():
    (text,) = route.intake_file("Ship it", "Body.", "repo", "2026-11-03").values()

    assert "stage" not in text


def _intake_row(extra):
    return {**_row(0), "extra": extra, "body": "Body."}


def test_decompose_into_rows_refuses_a_draft_and_writes_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, *a, **k: [_intake_row({"stage": "draft"})])
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: calls.append(row) or "")

    code, lines = route.decompose_into_rows(tmp_path, "idea", calls.append)

    assert code == 2
    assert lines == ["routing: intake idea is a draft; ready it with `cox route ready idea`"]
    assert calls == []


def test_decompose_into_rows_refuses_a_quoted_held_intake(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, *a, **k: [_intake_row({"held": "'true'"})])

    code, _ = route.decompose_into_rows(tmp_path, "idea", calls.append)

    assert (code, calls) == (2, [])


def test_decompose_into_rows_runs_a_ready_intake(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, *a, **k: [_intake_row({})])

    code, lines = route.decompose_into_rows(
        tmp_path, "idea", lambda intake: seen.append(intake) or {"id": "init", "tasks": [], "lint": []}
    )

    assert len(seen) == 1
    assert "draft" not in " ".join(lines)


def test_the_profile_intake_stage_key_parses_and_is_known():
    for written, kept in (("draft", "draft"), ("ready", "ready"), ("'Draft'", "draft")):
        profile, unknown = route._parse_profile(f"intake_stage: {written}\n")

        assert profile["intake_stage"] == kept
        assert unknown == []


def test_the_profile_refuses_a_bad_intake_stage():
    with pytest.raises(route.ProfileError, match="intake_stage"):
        route._parse_profile("intake_stage: later\n")
