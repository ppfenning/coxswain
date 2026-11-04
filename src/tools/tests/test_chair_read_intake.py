import os

import pytest

from agent_tools import chair_read_intake as cri
from agent_tools import route


def _groups(entries, initiatives=()):
    return route.intake_groups(entries, list(initiatives))


def _entry(path, initiative=None, done=False):
    return {"id": path, "title": path, "initiative": initiative, "done": done, "path": path}


def test_intake_groups_parses_each_initiative_once_and_matches_the_per_entry_scan(monkeypatch):
    from agent_tools import intake_state

    initiatives = [
        {"id": "x", "done": False, "text": "---\nid: x\nintake: intake/named.md\n---\n"},
        {"id": "y", "done": True, "text": "---\nid: y\nintake: intake/landed.md\n---\n"},
        {"id": "z", "done": False, "text": "see intake/cited.md"},
    ]
    entries = [
        _entry("intake/fresh.md"),
        _entry("intake/named.md"),
        _entry("intake/landed.md"),
        _entry("intake/with-id.md", initiative="x"),
        _entry("intake/with-done-id.md", initiative="y"),
        _entry("intake/cited.md", done=True),
        _entry("intake/uncited.md", done=True),
    ]
    by_id = {i["id"]: i for i in initiatives}
    expected = {
        name: [e for e in entries if intake_state.intake_group(e, by_id, initiatives) == name]
        for name in ("queued", "decomposed", "landed")
    }
    parsed: list[str] = []
    real = intake_state.intake_field
    monkeypatch.setattr(intake_state, "intake_field", lambda text: parsed.append(text) or real(text))
    assert route.intake_groups(entries, initiatives) == expected
    assert (len(parsed), sorted(len(v) for v in expected.values())) == (3, [1, 3, 3])


def _intake_row(task_id, initiative=None, state="queued", **extra):
    return {
        "kind": "intake",
        "initiative": "intake",
        "task_id": task_id,
        "phase": "",
        "state": state,
        "needs": [],
        "title": task_id,
        "surfaces": [],
        "body": "",
        "extra": {**({"initiative": initiative} if initiative is not None else {}), **extra},
    }


def _task_row(task_id, state, initiative):
    return {
        "kind": "task",
        "initiative": initiative,
        "task_id": task_id,
        "phase": "p1",
        "state": state,
        "needs": [],
        "title": task_id,
        "surfaces": [],
        "body": "",
        "extra": {},
    }


def test_queued_files_sort_oldest_first():
    groups = _groups([_entry("intake/new.md"), _entry("intake/old.md")])
    mtimes = {"intake/new.md": 200.0, "intake/old.md": 100.0}
    assert cri.queued_oldest_first(groups, mtimes) == ["intake/old.md", "intake/new.md"]


def test_equal_mtimes_fall_back_to_path():
    groups = _groups([_entry("intake/b.md"), _entry("intake/a.md")])
    assert cri.queued_oldest_first(groups, {"intake/a.md": 5.0, "intake/b.md": 5.0}) == ["intake/a.md", "intake/b.md"]


def test_a_root_file_whose_initiative_is_done_is_not_returned():
    groups = _groups([_entry("intake/a.md", initiative="x")], [{"id": "x", "done": True, "text": ""}])
    assert cri.queued_oldest_first(groups, {}) == []


def test_a_root_file_with_no_initiative_is_returned():
    groups = _groups([_entry("intake/a.md")])
    assert cri.queued_oldest_first(groups, {"intake/a.md": 1.0}) == ["intake/a.md"]


def test_a_file_under_done_is_not_returned():
    groups = _groups([_entry("intake/a.md", done=True)])
    assert cri.queued_oldest_first(groups, {}) == []


def test_a_profile_with_one_source_is_configured():
    assert cri.has_sources(["github"]) is True


def test_a_profile_with_no_sources_is_not_configured():
    assert cri.has_sources([]) is False


def test_the_edge_lists_intake_by_mtime_and_skips_done(tmp_path):
    (tmp_path / "intake" / "done").mkdir(parents=True)
    for rel, mtime in (("new.md", 200), ("old.md", 100), ("done/gone.md", 50)):
        p = tmp_path / "intake" / rel
        p.write_text("body\n", encoding="utf-8")
        os.utime(p, (mtime, mtime))
    assert cri.read_intake(tmp_path) == ["intake/old.md", "intake/new.md"]
    assert cri.read_intake(tmp_path / "missing") == []


def test_the_edge_drops_a_root_file_whose_initiative_is_done(tmp_path):
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "a.md").write_text("---\ninitiative: x\n---\nbody\n", encoding="utf-8")
    (tmp_path / "work" / "x" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "x" / "initiative.md").write_text("# x\n", encoding="utf-8")
    (tmp_path / "work" / "x" / "p1" / "t.md").write_text("---\nstate: done\n---\n", encoding="utf-8")
    assert cri.read_intake(tmp_path) == []
    (tmp_path / "intake" / "b.md").write_text("body\n", encoding="utf-8")
    assert cri.read_intake(tmp_path) == ["intake/b.md"]


def test_a_queued_row_with_no_initiative_is_returned():
    rows = [_intake_row("a")]
    assert cri.intake_from_rows(rows) == rows


def test_a_queued_row_whose_initiative_has_an_undone_task_row_is_withheld():
    rows = [_intake_row("a", initiative="x"), _task_row("t1", "todo", "x")]
    assert cri.intake_from_rows(rows) == []


def test_a_queued_row_whose_initiative_items_are_all_done_is_withheld():
    rows = [_intake_row("a", initiative="x"), _task_row("t1", "done", "x")]
    assert cri.intake_from_rows(rows) == []


def test_a_non_queued_intake_row_is_never_returned():
    rows = [_intake_row("a", state="done")]
    assert cri.intake_from_rows(rows) == []


def _ids(rows):
    return [r["task_id"] for r in rows]


def test_rows_come_back_by_priority_descending_then_row_order():
    rows = [
        _intake_row("a"),
        _intake_row("b", priority=5),
        _intake_row("c", priority=5),
        _intake_row("d", priority=2),
    ]
    assert _ids(cri.intake_from_rows(rows)) == ["b", "c", "d", "a"]


def test_a_held_row_is_skipped_even_when_its_after_target_has_landed():
    rows = [
        _intake_row("a", held=True),
        _intake_row("b", held=True, after=["x"]),
        _intake_row("c"),
        _task_row("t", "done", "x"),
    ]
    assert _ids(cri.intake_from_rows(rows)) == ["c"]


def test_a_waiting_row_is_absent_while_its_target_is_open_and_present_once_it_lands():
    def rows(t1_state):
        return [
            _intake_row("w", after=["x"]),
            _task_row("t1", t1_state, "x"),
            _task_row("t2", "done", "x"),
        ]

    assert cri.intake_from_rows(rows("todo")) == []
    assert _ids(cri.intake_from_rows(rows("done"))) == ["w"]


def test_after_naming_nothing_waits_and_after_naming_a_landed_intake_does_not():
    rows = [
        _intake_row("ghost", after=["nope"]),
        _intake_row("src", initiative="x"),
        _intake_row("next", after=["src"]),
        _task_row("t", "done", "x"),
    ]
    assert _ids(cri.intake_from_rows(rows)) == ["next"]


def test_the_file_path_orders_by_priority_then_mtime_then_path():
    groups = _groups(
        [
            {**_entry("intake/a.md"), "priority": 0},
            {**_entry("intake/b.md"), "priority": 3},
            {**_entry("intake/c.md"), "priority": 3},
            {**_entry("intake/d.md"), "priority": 3},
        ]
    )
    mtimes = {"intake/a.md": 1.0, "intake/b.md": 50.0, "intake/c.md": 10.0, "intake/d.md": 10.0}
    assert cri.queued_oldest_first(groups, mtimes) == ["intake/c.md", "intake/d.md", "intake/b.md", "intake/a.md"]


def test_the_file_path_skips_held_and_any_non_empty_after():
    groups = _groups(
        [
            {**_entry("intake/held.md"), "held": True},
            {**_entry("intake/waits.md"), "after": ["x"]},
            {**_entry("intake/empty.md"), "after": []},
        ]
    )
    mtimes = {"intake/held.md": 1.0, "intake/waits.md": 2.0, "intake/empty.md": 3.0}
    assert cri.queued_oldest_first(groups, mtimes) == ["intake/empty.md"]


def test_the_edge_reads_priority_held_and_after_from_frontmatter(tmp_path):
    (tmp_path / "intake").mkdir()
    for name, text, mtime in (
        ("plain.md", "body\n", 100),
        ("urgent.md", "---\npriority: 3\n---\nbody\n", 200),
        ("held.md", "---\nheld: true\n---\nbody\n", 50),
        ("waits.md", "---\nafter: [x]\n---\nbody\n", 60),
    ):
        p = tmp_path / "intake" / name
        p.write_text(text, encoding="utf-8")
        os.utime(p, (mtime, mtime))
    (tmp_path / "work" / "x" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "x" / "initiative.md").write_text("# x\n", encoding="utf-8")
    (tmp_path / "work" / "x" / "p1" / "t.md").write_text("---\nstate: done\n---\n", encoding="utf-8")
    assert cri.read_intake(tmp_path) == ["intake/urgent.md", "intake/plain.md"]


def _stems(paths):
    return {p.rsplit("/", 1)[-1].removesuffix(".md") for p in paths}


def test_parity_the_same_board_as_rows_and_as_files_agrees(tmp_path):
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "a.md").write_text("---\ninitiative: x\n---\nbody\n", encoding="utf-8")
    (tmp_path / "intake" / "b.md").write_text("body\n", encoding="utf-8")
    (tmp_path / "work" / "x" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "x" / "initiative.md").write_text("# x\n", encoding="utf-8")
    task = tmp_path / "work" / "x" / "p1" / "t.md"
    for state in ("todo", "done"):
        task.write_text(f"---\nstate: {state}\n---\n", encoding="utf-8")
        rows = [_intake_row("a", initiative="x"), _intake_row("b"), _task_row("t", state, "x")]
        assert {r["task_id"] for r in cri.intake_from_rows(rows)} == _stems(cri.read_intake(tmp_path)) == {"b"}


def test_the_edge_reads_sources_from_the_profile(tmp_path):
    profile = tmp_path / "profile.yaml"
    assert cri.read_sources_configured(profile) is False
    profile.write_text('sources: {"github": {"repos": ["a/b"]}}\n', encoding="utf-8")
    assert cri.read_sources_configured(profile) is True
    profile.write_text("sources: {}\n", encoding="utf-8")
    assert cri.read_sources_configured(profile) is False


_BAD = route.ProfileError("line 3: sources: {")


def test_a_failed_reread_keeps_the_previous_value_and_marks_it_stale():
    assert cri.keep_last_good(True, _BAD) == (True, True)
    assert cri.keep_last_good(False, _BAD) == (False, True)


def test_a_good_reread_replaces_the_previous_value_and_clears_the_mark():
    assert cri.keep_last_good(True, False) == (False, False)


def test_a_failed_first_load_stays_the_error():
    assert cri.keep_last_good(None, _BAD) == (_BAD, False)


def test_the_edge_keeps_the_last_good_profile_until_a_read_succeeds_again(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text('sources: {"github": {"repos": ["a/b"]}}\n', encoding="utf-8")
    sources, stale = cri.sources_configured_keeping_last_good(profile)
    assert (sources(), stale()) == (True, False)
    profile.write_text("sources: {\n", encoding="utf-8")  # malformed: an unknown key only warns since parse-profile-drops-unknown-keys
    assert isinstance(cri.read_sources_configured(profile), route.ProfileError)
    assert (sources(), stale()) == (True, True)
    profile.write_text("sources: {}\n", encoding="utf-8")
    assert (sources(), stale()) == (False, False)


def test_the_edge_raises_when_the_very_first_load_fails(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("sources: {\n", encoding="utf-8")  # malformed: an unknown key only warns since parse-profile-drops-unknown-keys
    sources, _stale = cri.sources_configured_keeping_last_good(profile)
    with pytest.raises(route.ProfileError):
        sources()
