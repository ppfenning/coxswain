import json
import subprocess

import pytest

from devtools import cli, release_check, release_check_notes
from devtools.release_notes_blanks import GAPS_BLANK, OPENER_BLANK, replace_blank

V0, V1 = "2026.10.0", "2026.11.0"
_MANIFEST = f"""
[coxswain]
version = "{V0}"

[components]
dash = {{ repo = "o/dash", tag = "v0.42.0", flag = "dash" }}
"""

_INDEX = f"""# Releases

## `{V0}`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| dash | `o/dash` | `v0.42.0` | flag: `dash` (pinned) |

See the [{V0} release notes]({V0}.md) for what landed in each component.
"""

_NAV = f"nav:\n  - Releases:\n      - Releases: releases/index.md\n      - {V0}: releases/{V0}.md\n"

_PRS = {
    "ppfenning/coxswain": [(11, "Add the gate", "2026-02-01T00:00:00Z"), (12, "Fix an old bug", "2025-12-01T00:00:00Z")],
    "o/dash": [(21, "Fix the retry loop", "2026-03-01T00:00:00Z")],
}

_OPENER = "The gate landed. The retry loop was fixed. Nothing else moved."


def _fake_run_with(dash_commits):
    """Canned git and gh output for the coxswain checkout and dash, which has `dash_commits` past its pin."""
    def run(argv):
        text = " ".join(argv)
        repo = "o/dash" if "/dash" in text else "ppfenning/coxswain"
        if argv[:3] == ["git", "ls-remote", "--tags"]:
            return ""
        if "symbolic-ref" in argv:
            return "origin/main\n"
        if "rev-list" in argv:
            return f"{dash_commits}\n"
        if "tag" in argv:
            return "v0.42.0\n" if repo == "o/dash" else "v0.47.0\n"
        if "log" in argv:
            return "2026-01-01T00:00:00+00:00\n"
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps([{"number": n, "title": t, "mergedAt": m, "baseRefName": "main"} for n, t, m in _PRS[repo]])
        raise AssertionError(argv)
    return run


_fake_run = _fake_run_with(3)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_repository_url", lambda: None)
    umbrella = tmp_path / "coxswain"
    (umbrella / "docs" / "releases").mkdir(parents=True)
    (umbrella / "manifest.toml").write_text(_MANIFEST)
    (umbrella / "docs" / "releases" / "index.md").write_text(_INDEX)
    (umbrella / "mkdocs.yml").write_text(_NAV)
    return tmp_path


def _args(tree, *extra):
    return cli.build_parser().parse_args(["notes-draft", V1, "--manifest", str(tree / "coxswain" / "manifest.toml"), *extra])


def _paths(tree):
    umbrella = tree / "coxswain"
    return umbrella / "docs" / "releases" / f"{V1}.md", umbrella / "docs" / "releases" / "index.md", umbrella / "mkdocs.yml"


def _draft(tree, *extra, complete=None):
    return cli._release_notes_draft(_args(tree, *extra), run=_fake_run, complete=complete)


def test_draft_writes_the_repositorys_prs_and_a_changed_dashs_and_registers_the_page(tree, capsys):
    assert _draft(tree) == 0
    notes, index, nav = _paths(tree)
    text = notes.read_text()
    assert "## coxswain (umbrella)\n\n- Added the gate (coxswain #11)" in text
    assert "## coxswain-dash\n\n- Fixed the retry loop (dash #21)" in text
    assert "#12" not in text
    assert OPENER_BLANK in text and GAPS_BLANK in text
    assert f"| dash |  | `v{V1}` |  |" in index.read_text()
    assert f"      - {V1}: releases/{V1}.md\n      - {V0}: releases/{V0}.md" in nav.read_text()
    out = capsys.readouterr().out
    assert f"wrote {notes}" in out and "blanks still to fill: opener, known-gaps" in out


def test_draft_leaves_an_unchanged_dash_out_and_lists_it_at_its_pin(tree):
    assert cli._release_notes_draft(_args(tree), run=_fake_run_with(0)) == 0
    notes, index, _ = _paths(tree)
    assert "## coxswain-dash" not in notes.read_text() and "(coxswain #11)" in notes.read_text()
    assert "| dash |  | `v0.42.0` |  (pinned) |" in index.read_text()


def test_rerun_over_a_draft_with_blanks_changes_nothing(tree):
    _draft(tree)
    before = [p.read_bytes() for p in _paths(tree)]
    assert _draft(tree) == 0
    assert [p.read_bytes() for p in _paths(tree)] == before


def test_a_finished_file_is_refused_and_left_alone(tree, capsys):
    notes, index, nav = _paths(tree)
    notes.write_text(f"# {V1}\n\nHand finished.\n")
    assert _draft(tree) == 2
    assert notes.read_text() == f"# {V1}\n\nHand finished.\n"
    assert index.read_text() == _INDEX and nav.read_text() == _NAV
    assert "no blanks left" in capsys.readouterr().out


def test_a_file_with_one_blank_left_is_rewritten(tree):
    notes, _, _ = _paths(tree)
    notes.write_text(f"# {V1}\n\n{OPENER_BLANK}\n")
    assert _draft(tree) == 0
    assert "(coxswain #11)" in notes.read_text()


def test_cheap_opener_fills_the_opener_and_leaves_known_gaps(tree, capsys):
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return _OPENER

    assert _draft(tree, "--cheap-opener", complete=complete) == 0
    text = _paths(tree)[0].read_text()
    assert _OPENER in text and OPENER_BLANK not in text and GAPS_BLANK in text
    assert "Added the gate (coxswain #11)" in prompts[0]
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
    return {
        "release_notes": f"docs/releases/{V1}.md",
        "notes_text": text,
        "component_dirs": {"dash": "dash"},
        "landed": {"dash": {"21"}, "coxswain": {"11"}},
        "notes_bullets": release_check_notes.bullets_from_notes(text),
        "notes_sections": release_check_notes.sections_from_notes(text, {"dash", "coxswain"}),
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
