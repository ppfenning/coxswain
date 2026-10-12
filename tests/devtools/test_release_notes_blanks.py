from devtools.release_notes_blanks import GAPS_BLANK, OPENER_BLANK, find_blanks, replace_blank


def test_the_markers_are_distinct_single_line_comments():
    assert OPENER_BLANK != GAPS_BLANK
    assert OPENER_BLANK not in GAPS_BLANK and GAPS_BLANK not in OPENER_BLANK
    for marker in (OPENER_BLANK, GAPS_BLANK):
        assert "\n" not in marker
        assert marker.startswith("<!--") and marker.endswith("-->")


def test_find_blanks_reports_both_in_fixed_order():
    text = f"# 1.0\n\n{GAPS_BLANK}\n\n{OPENER_BLANK}\n"
    assert find_blanks(text) == ("opener", "known-gaps")


def test_find_blanks_reports_one():
    assert find_blanks(f"# 1.0\n\n{GAPS_BLANK}\n") == ("known-gaps",)


def test_find_blanks_is_empty_when_none_remain():
    assert find_blanks("# 1.0\n\nAn opener.\n") == ()


def test_replace_blank_fills_the_opener_and_leaves_the_gaps_marker():
    text = f"{OPENER_BLANK}\n\n{GAPS_BLANK}\n"
    result = replace_blank(text, "opener", "Hello.")
    assert result == f"Hello.\n\n{GAPS_BLANK}\n"
    assert find_blanks(result) == ("known-gaps",)


def test_replace_blank_returns_text_unchanged_when_the_marker_is_absent():
    text = f"Hello.\n\n{GAPS_BLANK}\n"
    assert replace_blank(text, "opener", "Other.") == text


def test_replace_blank_returns_text_unchanged_for_an_unknown_name():
    text = f"{OPENER_BLANK}\n"
    assert replace_blank(text, "nope", "x") == text
