from agent_tools.release_gate import (
    deferred_initiatives,
    deferred_intakes,
    held_initiatives,
    is_deferred,
    release_tuple,
)


def initiative(name: str, **extra: object) -> dict:
    return {"kind": "initiative", "initiative": name, "task_id": name, "state": "todo", "extra": dict(extra)}


def intake(task_id: str, state: str = "queued", **extra: object) -> dict:
    return {"kind": "intake", "initiative": "intake", "task_id": task_id, "state": state, "extra": dict(extra)}


def test_release_tuple_plain_version() -> None:
    assert release_tuple("2026.11.5") == (2026, 11, 5)


def test_release_tuple_git_describe_string() -> None:
    assert release_tuple("2026.11.5-12-gabc1234") == (2026, 11, 5)


def test_release_tuple_drops_a_leading_v() -> None:
    assert release_tuple("v1.2") == (1, 2)


def test_release_tuple_unparseable_is_none() -> None:
    assert [release_tuple(text) for text in ("unknown", "", "beta")] == [None, None, None]


def test_is_deferred_when_current_is_below_target() -> None:
    assert is_deferred("2026.12.0", "2026.11.5") is True


def test_is_deferred_false_when_equal_or_above() -> None:
    assert (is_deferred("2026.11.5", "2026.11.5"), is_deferred("2026.11.5", "2027.1.0")) == (False, False)


def test_work_for_the_next_release_is_open_now() -> None:
    # target_release names the release work ships in: with 2026.11.5 installed, 2026.11.6 is the one in progress.
    assert (is_deferred("2026.11.6", "2026.11.5"), is_deferred("2026.11.6", "2026.11.5-30-gabc1234")) == (False, False)


def test_work_for_the_release_after_next_waits() -> None:
    assert is_deferred("2026.11.7", "2026.11.5") is True


def test_is_deferred_false_for_a_describe_string_at_the_target() -> None:
    assert is_deferred("2026.11.5", "2026.11.5-12-gabc1234") is False


def test_is_deferred_false_for_an_unparseable_current() -> None:
    assert is_deferred("2026.12.0", "unknown") is False


def test_is_deferred_false_for_an_unparseable_target() -> None:
    assert is_deferred("soon", "2026.11.5") is False


def test_held_initiatives_from_the_initiative_row() -> None:
    assert held_initiatives([initiative("a", held=True), initiative("b")]) == frozenset({"a"})


def test_held_initiatives_from_an_intake_held_after_decomposition() -> None:
    assert held_initiatives([initiative("a"), intake("i", held="true", initiative="a")]) == frozenset({"a"})


def test_held_initiatives_ignores_removed_and_unnamed_intakes() -> None:
    rows = [intake("i", state="removed", held=True, initiative="a"), intake("j", held=True)]
    assert held_initiatives(rows) == frozenset()


def test_deferred_initiatives_from_the_initiative_row() -> None:
    assert deferred_initiatives([initiative("a", target_release="2026.12.0")], "2026.11.5") == {"a": "2026.12.0"}


def test_deferred_initiatives_from_an_intake_naming_it() -> None:
    rows = [initiative("a"), intake("i", initiative="a", target_release="2026.12.0")]
    assert deferred_initiatives(rows, "2026.11.5") == {"a": "2026.12.0"}


def test_deferred_initiatives_initiative_row_wins_over_intake() -> None:
    rows = [initiative("a", target_release="2026.11.0"), intake("i", initiative="a", target_release="2026.12.0")]
    assert deferred_initiatives(rows, "2026.11.5") == {}


def test_deferred_initiatives_accepts_a_numeric_target() -> None:
    assert deferred_initiatives([initiative("a", target_release=2027.1)], "2026.11.5") == {"a": "2027.1"}


def test_deferred_initiatives_empty_for_an_unparseable_current() -> None:
    assert deferred_initiatives([initiative("a", target_release="2026.12.0")], "unknown") == {}


def test_deferred_intakes_only_queued_and_below_target() -> None:
    rows = [
        intake("late", target_release="2026.12.0"),
        intake("ok", target_release="2026.11.5"),
        intake("gone", state="removed", target_release="2026.12.0"),
        intake("plain"),
    ]
    assert deferred_intakes(rows, "2026.11.5") == {"late": "2026.12.0"}


def test_deferred_intakes_empty_for_an_unparseable_current() -> None:
    assert deferred_intakes([intake("late", target_release="2026.12.0")], "") == {}
