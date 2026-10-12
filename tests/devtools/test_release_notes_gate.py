import pytest

from devtools import cli, release, release_check
from devtools.release_notes_blanks import GAPS_BLANK, OPENER_BLANK

_MANIFEST_TOML = """
[coxswain]
version = "0.1.0"

[components.harness]
repo = "org/harness"
tag = "v0.1.0"
"""

_CLEAN = "# coxswain 0.1.0\n\nA finished opener.\n\n- coxswain harness #12 landed the gate.\n\n## Known gaps\n\nNothing yet.\n"
_BOTH = f"# coxswain 0.1.0\n\n{OPENER_BLANK}\n\n- coxswain harness #12 landed the gate.\n\n## Known gaps\n\n{GAPS_BLANK}\n"
_ONE = _CLEAN.replace("A finished opener.", OPENER_BLANK)


def _blank_drifts(text):
    facts = {"release_notes": "docs/releases/0.1.0.md", "notes_text": text}
    return [d for d in release_check.run_checks(facts) if d.check == "notes_blank"]


@pytest.fixture(autouse=True)
def _maintainer_checkout(monkeypatch):
    monkeypatch.setattr(cli, "_maintainer_remote_url", lambda directory: "git@github.com:ppfenning/coxswain.git")


def test_release_check_reports_exactly_one_finding_per_blank_and_no_others():
    facts = {"release_notes": "docs/releases/0.1.0.md", "notes_text": _BOTH}
    drifts = release_check.run_checks(facts)
    assert [d.check for d in drifts] == ["notes_blank", "notes_blank"]
    assert "opener" in drifts[0].correction and "Known gaps" in drifts[1].correction


def test_release_check_reports_one_finding_for_one_blank():
    drifts = _blank_drifts(_ONE)
    assert len(drifts) == 1 and "opener" in drifts[0].correction


def test_release_check_leaves_a_clean_text_unchanged():
    assert release_check.run_checks({"release_notes": "docs/releases/0.1.0.md", "notes_text": _CLEAN}) == []


def test_blank_names_is_a_pure_function_from_text_to_names():
    assert release.blank_names(_BOTH) == ["opener", "known-gaps"]
    assert release.blank_names(_CLEAN) == []


def test_gather_version_facts_reads_the_notes_for_the_manifest_version(tmp_path):
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    (tmp_path / "docs" / "releases" / "0.1.0.md").write_text(_ONE)
    facts = release_check.gather_version_facts({"coxswain": {"version": "0.1.0"}}, "m.toml", {}, str(tmp_path))
    assert facts["notes_text"] == _ONE
    assert release_check.gather_version_facts({"coxswain": {"version": "0.9.0"}}, "m.toml", {}, str(tmp_path))["notes_text"] == ""


def test_gate_refuses_a_blank_even_when_doc_drift_is_allowed():
    drift = _blank_drifts(_ONE)
    steps = release.gate(drift, "docs land next sprint")
    assert [s["kind"] for s in steps] == ["refuse"] and "opener" in steps[0]["detail"]
    assert release.gate([], None) == []


def _run_release(tmp_path, monkeypatch, text, *flags):
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(_MANIFEST_TOML)
    (tmp_path / "coxswain" / "docs" / "releases").mkdir(parents=True)
    (tmp_path / "coxswain" / "docs" / "releases" / "0.1.0.md").write_text(text)
    calls = []

    def fake_run(argv, cwd):
        calls.append(argv)
        return (0, "1\n") if "rev-list" in argv else (0, "")

    monkeypatch.setattr(release_check, "CHECKS", (release_check.check_notes_blanks,))
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [])
    monkeypatch.setattr(cli, "_real_run", fake_run)
    monkeypatch.setattr(cli, "_component_declares_tag_trigger", lambda directory: True)
    rc = cli.main(["release", "0.1.0", "--manifest", str(manifest_path), "--root", str(tmp_path), *flags])
    return rc, calls


def test_release_refuses_a_notes_text_with_a_blank_before_any_step_runs(tmp_path, monkeypatch, capsys):
    rc, calls = _run_release(tmp_path, monkeypatch, _BOTH)
    out = capsys.readouterr().out
    assert rc == 2
    assert "refuse notes_blank" in out and "the opener blank" in out
    assert [c for c in calls if "rev-list" not in c] == []


def test_release_accepts_a_clean_notes_text(tmp_path, monkeypatch, capsys):
    rc, _calls = _run_release(tmp_path, monkeypatch, _CLEAN, "--dry-run")
    out = capsys.readouterr().out
    assert rc == 0 and "refuse" not in out
    assert "tag harness" in out
