from devtools import release_rehearsal_gate as gate

SHAS = {"coxswain": "a" * 40}
TITLE = "rehearsal sha=" + "a" * 40 + " dash=pinned previous=v0.47.0 migrations=none"
GREEN = [{"name": "fresh", "conclusion": "success"}, {"name": "upgrade", "conclusion": "success"},
         {"name": "migrate", "conclusion": "skipped"}]
NOTES_LINE = "Cut with `--allow-no-rehearsal`: the runner image was unavailable."


def _run(id_, conclusion="success", title=TITLE, created="2026-10-01T10:00:00Z"):
    return {"databaseId": id_, "displayTitle": title, "conclusion": conclusion, "createdAt": created}


def _decide(runs, jobs=GREEN, declared=(), reason=None, notes="", previous="v0.47.0", shas=SHAS):
    return gate.decide(runs, lambda run: jobs, shas, previous, declared, reason, notes)


def test_run_title_names_the_commit_dash_the_previous_tag_and_the_migrations():
    assert gate.run_title("a" * 40, "pinned", "v0.47.0", []) == TITLE
    assert gate.run_title("a" * 40, "", "v0.47.0", []) == TITLE


def test_run_title_comma_joins_the_migrations():
    assert gate.run_title("a" * 40, "pinned", "v0.47.0", ["split-lake-store", "rename-slot-keys"]).endswith(
        "previous=v0.47.0 migrations=split-lake-store,rename-slot-keys")


def test_a_release_that_tags_dash_looks_for_dashs_commit_in_the_title():
    title = TITLE.replace("dash=pinned", "dash=" + "e" * 40)
    assert _decide([_run(4, title=title)], shas={**SHAS, "dash": "e" * 40})[0]["kind"] == "note"
    assert _decide([_run(4)], shas={**SHAS, "dash": "e" * 40})[0]["kind"] == "refuse"


def test_a_matching_passing_run_permits_and_the_note_names_its_address():
    steps = _decide([_run(42)])
    assert [s["kind"] for s in steps] == ["note"]
    assert "gh run view 42" in steps[0]["detail"]


def test_no_run_refuses_naming_the_title_looked_for():
    steps = _decide([])
    assert [s["kind"] for s in steps] == ["refuse"]
    assert TITLE in steps[0]["detail"] and "window may be full" not in steps[0]["detail"]


def test_exactly_200_runs_with_no_match_says_the_window_may_be_full():
    other = [_run(i, title="rehearsal other") for i in range(200)]
    assert "window may be full" in _decide(other)[0]["detail"]


def test_a_run_for_other_shas_never_matches():
    other = TITLE.replace("a" * 40, "f" * 40, 1)
    assert _decide([_run(1, title=other)])[0]["kind"] == "refuse"


def test_a_title_that_only_starts_with_the_string_never_matches():
    assert _decide([_run(1, title=TITLE + " extra")])[0]["kind"] == "refuse"


def test_a_failed_run_refuses_with_the_failing_condition():
    steps = _decide([_run(3, conclusion="failure")])
    assert steps[0]["kind"] == "refuse" and "concluded failure, not success" in steps[0]["detail"]


def test_a_missing_scenario_job_refuses():
    steps = _decide([_run(3)], jobs=GREEN[:2])
    assert steps[0]["kind"] == "refuse" and "no migrate job" in steps[0]["detail"]


def test_a_skipped_upgrade_refuses_even_when_the_run_succeeded():
    jobs = [GREEN[0], {"name": "upgrade", "conclusion": "skipped"}, GREEN[2]]
    assert "upgrade job concluded skipped" in _decide([_run(3)], jobs=jobs)[0]["detail"]


def test_a_skipped_migrate_with_declared_migrations_refuses():
    steps = _decide([_run(3, title=TITLE.replace("none", "split-lake-store"))], declared=["split-lake-store"])
    assert steps[0]["kind"] == "refuse" and "split-lake-store" in steps[0]["detail"]


def test_a_skipped_migrate_with_no_declared_migrations_permits():
    assert _decide([_run(3)])[0]["kind"] == "note"


def test_a_successful_migrate_with_declared_migrations_permits():
    jobs = [*GREEN[:2], {"name": "migrate", "conclusion": "success"}]
    assert _decide([_run(3, title=TITLE.replace("none", "a"))], jobs=jobs, declared=["a"])[0]["kind"] == "note"


def test_the_newer_of_two_matching_runs_decides_when_it_failed():
    older, newer = _run(1, created="2026-10-01T10:00:00Z"), _run(2, "failure", created="2026-10-02T10:00:00Z")
    assert _decide([older, newer])[0]["kind"] == "refuse"
    assert _decide([newer, older])[0]["kind"] == "refuse"


def test_the_newer_of_two_matching_runs_decides_when_it_passed():
    older, newer = _run(1, "failure", created="2026-10-01T10:00:00Z"), _run(2, created="2026-10-02T10:00:00Z")
    steps = _decide([newer, older])
    assert steps[0]["kind"] == "note" and "gh run view 2" in steps[0]["detail"]


def test_an_override_with_the_notes_line_permits_without_a_lookup():
    steps = _decide([], reason="the runner image was unavailable", notes=f"## Known gaps\n\n{NOTES_LINE}\n")
    assert [s["kind"] for s in steps] == ["note"] and "runner image" in steps[0]["detail"]


def test_an_override_compares_the_reasons_after_trimming():
    assert _decide([], reason="  the runner image was unavailable ", notes=NOTES_LINE.replace(": ", ":  "))[0]["kind"] == "note"


def test_an_override_with_an_empty_or_space_only_reason_refuses():
    assert _decide([_run(1)], reason="", notes=NOTES_LINE)[0]["kind"] == "refuse"
    assert _decide([_run(1)], reason="   ", notes=NOTES_LINE)[0]["kind"] == "refuse"


def test_an_override_with_a_different_reason_than_the_notes_line_refuses():
    assert _decide([_run(1)], reason="the runner was slow", notes=NOTES_LINE)[0]["kind"] == "refuse"


def test_an_override_with_no_notes_line_refuses():
    assert _decide([_run(1)], reason="the runner image was unavailable", notes="## Known gaps\n")[0]["kind"] == "refuse"


def test_a_repository_with_no_resolved_commit_refuses():
    assert "dash" in _decide([_run(1)], shas={**SHAS, "dash": ""})[0]["detail"]
    assert "coxswain" in _decide([_run(1)], shas={"coxswain": ""})[0]["detail"]
    assert "coxswain" in _decide([_run(1)], shas={})[0]["detail"]
