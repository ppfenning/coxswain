from devtools.release_check_notes import bullets_from_notes, resolve_citations, sections_from_notes
from devtools.release_notes_blanks import GAPS_BLANK, OPENER_BLANK, find_blanks
from devtools.release_notes_draft import _past_tense, render_notes

COMPONENTS = [("cartridges", "0.37.0"), ("graphs", "0.37.0"), ("dash", "0.36.0")]
FACTS = {"schema_version": 12, "has_migrations": False}


def pr(component: str, number: int, title: str) -> dict:
    return {"component": component, "number": number, "title": title}


def render(prs: dict, facts: dict = FACTS) -> str:
    return render_notes("0.37.0", prs, COMPONENTS, facts)


def test_bullets_are_in_number_order_with_the_citation():
    text = render({"graphs": [pr("graphs", 9, "Fix b"), pr("graphs", 3, "Add a")]})
    assert "- Added a (graphs #3)\n- Fixed b (graphs #9)\n" in text


def test_known_verbs_become_past_tense_and_unknown_ones_pass_through():
    assert _past_tense("Add a flag") == "Added a flag"
    assert _past_tense("fix the loop") == "Fixed the loop"
    assert _past_tense("Bump the pin") == "Bump the pin"
    assert _past_tense("Added a flag") == "Added a flag"


def test_a_trailing_full_stop_is_not_doubled_or_left_after_the_citation():
    text = render({"dash": [pr("dash", 1, "Fix the pane.")]})
    assert "- Fixed the pane (dash #1)\n" in text
    assert ".." not in text.split("## Known gaps")[0]


def test_a_component_without_prs_has_no_section_but_is_still_listed():
    text = render({"graphs": [pr("graphs", 1, "Add a")]})
    assert "## coxswain-graphs" in text
    assert "## coxswain-cartridges" not in text
    assert "coxswain-cartridges 0.37.0" in text.split("## Components")[1]


def test_sections_follow_manifest_order_not_dict_order():
    text = render({"dash": [pr("dash", 1, "Add a")], "cartridges": [pr("cartridges", 2, "Add b")]})
    assert text.index("## coxswain-cartridges") < text.index("## coxswain-dash")


def test_both_markers_appear_exactly_once():
    text = render({"graphs": [pr("graphs", 1, "Add a")]})
    assert text.count(OPENER_BLANK) == 1 and text.count(GAPS_BLANK) == 1
    assert find_blanks(text) == ("opener", "known-gaps")
    assert text.startswith(f"# 0.37.0\n\n{OPENER_BLANK}\n")


def test_the_upgrading_sentence_changes_with_has_migrations():
    without = render({}, {"schema_version": 12, "has_migrations": False}).split("## Upgrading")[1]
    with_ = render({}, {"schema_version": 13, "has_migrations": True}).split("## Upgrading")[1]
    assert "schema version is 12" in without and "has no migrations" in without
    assert "schema version is 13" in with_ and "includes migrations" in with_


def test_the_draft_meets_the_format_release_check_reads():
    prs = {
        "cartridges": [pr("cartridges", 4, "Add x")],
        "graphs": [pr("graphs", 7, "Fix y")],
        "coxswain": [pr("coxswain", 2, "Update z")],
    }
    text = render(prs)
    names = {"cartridges", "graphs", "dash", "coxswain"}
    sections = sections_from_notes(text, names)
    bullets = bullets_from_notes(text)
    assert [sections[n] for n, _ in bullets] == ["cartridges", "graphs", None]
    pairs = [resolve_citations(sections[n], line, names) for n, line in bullets]
    assert pairs == [[("cartridges", "4")], [("graphs", "7")], [("coxswain", "2")]]
