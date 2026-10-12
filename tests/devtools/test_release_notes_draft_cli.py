import json
import subprocess

import pytest

from devtools import cli, release_check, release_check_notes
from devtools.release_notes_blanks import GAPS_BLANK, OPENER_BLANK, replace_blank

_MANIFEST = """
[coxswain]
version = "0.1.0"

[components]
alpha = { repo = "o/alpha", tag = "v0.1.0" }
beta  = { repo = "o/beta",  tag = "v0.1.0" }
gamma = { repo = "o/gamma", tag = "v0.0.5" }
"""

_INDEX = """# Releases

## `0.1.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| alpha | `o/alpha` | `v0.1.0` |  |

See the [0.1.0 release notes](0.1.0.md) for what landed in each component.
"""

_NAV = "nav:\n  - Releases:\n      - Releases: releases/index.md\n      - 0.1.0: releases/0.1.0.md\n"

_PRS = {
    "o/alpha": [(11, "Add the gate", "2026-02-01T00:00:00Z"), (12, "Fix an old bug", "2025-12-01T00:00:00Z")],
    "o/beta": [(21, "Fix the retry loop", "2026-03-01T00:00:00Z")],
    "o/gamma": [(31, "Add a thing", "2026-03-01T00:00:00Z")],
}
_COMMITS = {"alpha": "3", "beta": "2", "gamma": "0"}

_OPENER = "The gate landed. The retry loop was fixed. Nothing else moved."


def _fake_run(argv):
    """Canned git and gh output keyed on the component named in argv; gamma has no commits past its pin."""
    text = " ".join(argv)
    name = next(n for n in _COMMITS if f"/{n}" in text)
    if argv[:3] == ["git", "ls-remote", "--tags"]:
        return ""
    if "symbolic-ref" in argv:
        return "origin/main\n"
    if "rev-list" in argv:
        return _COMMITS[name] + "\n"
    if "tag" in argv:
        return "v0.1.0\n"
    if "log" in argv:
        return "2026-01-01T00:00:00+00:00\n"
    if argv[:3] == ["gh", "pr", "list"]:
        return json.dumps([{"number": n, "title": t, "mergedAt": m, "baseRefName": "main"}
                           for n, t, m in _PRS[f"o/{name}"]])
    raise AssertionError(argv)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_tools_repository_url", lambda: None)
    umbrella = tmp_path / "coxswain"
    (umbrella / "docs" / "releases").mkdir(parents=True)
    (umbrella / "manifest.toml").write_text(_MANIFEST)
    (umbrella / "docs" / "releases" / "index.md").write_text(_INDEX)
    (umbrella / "mkdocs.yml").write_text(_NAV)
    return tmp_path


def _args(tree, *extra):
    return cli.build_parser().parse_args(
        ["notes-draft", "0.2.0", "--root", str(tree), "--manifest", str(tree / "coxswain" / "manifest.toml"), *extra])


def _paths(tree):
    umbrella = tree / "coxswain"
    return umbrella / "docs" / "releases" / "0.2.0.md", umbrella / "docs" / "releases" / "index.md", umbrella / "mkdocs.yml"


def _draft(tree, *extra, complete=None):
    return cli._release_notes_draft(_args(tree, *extra), run=_fake_run, complete=complete)


def test_draft_writes_bumped_components_only_and_registers_the_page(tree, capsys):
    assert _draft(tree) == 0
    notes, index, nav = _paths(tree)
    text = notes.read_text()
    assert "- Added the gate (alpha #11)" in text and "- Fixed the retry loop (beta #21)" in text
    assert "#12" not in text and "#31" not in text and "## coxswain-gamma" not in text
    assert OPENER_BLANK in text and GAPS_BLANK in text
    assert "| alpha |  | `v0.2.0` |  |" in index.read_text() and "| gamma |  | `v0.0.5` |  (pinned) |" in index.read_text()
    assert "      - 0.2.0: releases/0.2.0.md\n      - 0.1.0: releases/0.1.0.md" in nav.read_text()
    out = capsys.readouterr().out
    assert f"wrote {notes}" in out and "blanks still to fill: opener, known-gaps" in out


def test_rerun_over_a_draft_with_blanks_changes_nothing(tree):
    _draft(tree)
    before = [p.read_bytes() for p in _paths(tree)]
    assert _draft(tree) == 0
    assert [p.read_bytes() for p in _paths(tree)] == before


def test_a_finished_file_is_refused_and_left_alone(tree, capsys):
    notes, index, nav = _paths(tree)
    notes.write_text("# 0.2.0\n\nHand finished.\n")
    assert _draft(tree) == 2
    assert notes.read_text() == "# 0.2.0\n\nHand finished.\n"
    assert index.read_text() == _INDEX and nav.read_text() == _NAV
    assert "no blanks left" in capsys.readouterr().out


def test_a_file_with_one_blank_left_is_rewritten(tree):
    notes, _, _ = _paths(tree)
    notes.write_text(f"# 0.2.0\n\n{OPENER_BLANK}\n")
    assert _draft(tree) == 0
    assert "(alpha #11)" in notes.read_text()


def test_cheap_opener_fills_the_opener_and_leaves_known_gaps(tree, capsys):
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return _OPENER

    assert _draft(tree, "--cheap-opener", complete=complete) == 0
    text = _paths(tree)[0].read_text()
    assert _OPENER in text and OPENER_BLANK not in text and GAPS_BLANK in text
    assert "Added the gate (alpha #11)" in prompts[0]
    assert "blanks still to fill: known-gaps" in capsys.readouterr().out


def test_cheap_opener_that_breaks_the_rules_keeps_the_marker_and_says_why(tree, capsys):
    assert _draft(tree, "--cheap-opener", complete=lambda prompt: "One sentence only.") == 0
    assert OPENER_BLANK in _paths(tree)[0].read_text()
    assert "opener left blank" in capsys.readouterr().out


def test_cheap_opener_whose_model_call_fails_keeps_the_marker(tree, capsys):
    def complete(prompt):
        raise subprocess.CalledProcessError(1, "claude")

    assert _draft(tree, "--cheap-opener", complete=complete) == 0
    assert OPENER_BLANK in _paths(tree)[0].read_text()
    assert "opener left blank: the model call failed" in capsys.readouterr().out


def _notes_facts(text):
    components = {"alpha", "beta", "gamma"}
    return {
        "release_notes": "docs/releases/0.2.0.md",
        "notes_text": text,
        "component_dirs": {name: name for name in components},
        "landed": {"alpha": {"11"}, "beta": {"21"}, "gamma": set(), "coxswain": set()},
        "notes_bullets": release_check_notes.bullets_from_notes(text),
        "notes_sections": release_check_notes.sections_from_notes(text, components | {"coxswain"}),
    }


def test_the_written_draft_has_only_the_two_blank_findings(tree):
    _draft(tree)
    drifts = release_check.run_checks(_notes_facts(_paths(tree)[0].read_text()), (release_check.check_notes_page,))
    assert [d.check for d in drifts] == ["notes_blank", "notes_blank"]
    assert "opener" in drifts[0].correction and "Known gaps" in drifts[1].correction


def test_a_draft_with_both_blanks_filled_reports_no_findings(tree):
    _draft(tree)
    text = replace_blank(replace_blank(_paths(tree)[0].read_text(), "opener", _OPENER), "known-gaps", "Nothing yet.")
    assert release_check.run_checks(_notes_facts(text), (release_check.check_notes_page,)) == []
