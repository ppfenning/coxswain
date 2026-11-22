import pytest

from agent_tools.chair_widen import _paths, classify_handoff

SURFACES = ["src/ui/regatta.rs", "src/input.rs (new)"]


def test_accessor_only_handoff_gives_one_pair() -> None:
    reason = "src/app.rs is out of bounds, I need an accessor for the selected frame"
    assert classify_handoff(reason, SURFACES) == [("src/app.rs", "accessor")]


def test_pub_crate_ask_gives_pub_crate() -> None:
    reason = "src/app.rs needs pub(crate) on selected_frame."
    assert classify_handoff(reason, SURFACES) == [("src/app.rs", "pub(crate)")]


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("src/app.rs needs an accessor, should we add one?", id="design_question"),
        pytest.param("I need an accessor in src/a.rs. A getter in src/b.rs. A flag in src/c.rs.", id="three_outside_files"),
        pytest.param("src/input.rs needs a new constant and src/ui/regatta.rs a flag.", id="only_surface_files"),
        pytest.param("I need a new file src/frame.rs with a constant.", id="new_file_ask"),
        pytest.param("src/app.rs needs an accessor. src/feed.rs needs its parser rewritten.", id="one_unsmall_ask"),
    ],
)
def test_a_handoff_that_is_not_a_small_widening_gives_none(reason: str) -> None:
    assert classify_handoff(reason, SURFACES) is None


def test_two_outside_files_give_two_pairs() -> None:
    reason = "src/app.rs needs an accessor for the frame. src/feed.rs needs a new field."
    assert classify_handoff(reason, SURFACES) == [("src/app.rs", "accessor"), ("src/feed.rs", "field")]


def test_paths_strip_sentence_period_and_ignore_abbreviations() -> None:
    assert _paths("src/app.rs. Then x/y.py, e.g. later") == ["src/app.rs", "x/y.py"]
