import argparse
import sys
from types import SimpleNamespace

import pytest

from devtools.release_check import Drift
from devtools.release_check_cli import (
    HAND_BUILT,
    check_cli_surface,
    commands_in_doc,
    gather_cli_facts,
    table_commands,
    table_suppressed,
)


def test_commands_in_doc_matches_a_backticked_cox_mention_and_drops_a_trailing_flag():
    assert commands_in_doc("Run `cox dev release-check --json` first.") == {"cox dev release-check"}


def test_commands_in_doc_does_not_read_the_bare_program_name_as_a_command():
    assert commands_in_doc("Run `cox` to see what's available.") == set()


def test_check_cli_surface_flags_a_command_missing_from_the_umbrella_docs():
    facts = {
        "cli_commands": {"cox dev release-check"},
        "doc_commands": {"docs/reference/cli/runs.md": set()},
        "readme_commands": {"dev": {"cox dev release-check"}},
    }
    assert check_cli_surface(facts) == [
        Drift("cli_surface", "cox --help", None, "docs/reference/cli/dev.md", None,
              "add cox dev release-check to docs/reference/cli/dev.md"),
    ]


def test_check_cli_surface_flags_a_command_missing_from_its_provider_readme():
    facts = {
        "cli_commands": {"cox dev release-check"},
        "doc_commands": {"docs/reference/cli/dev.md": {"cox dev release-check"}},
        "readme_commands": {"dev": set()},
    }
    assert check_cli_surface(facts) == [
        Drift("cli_surface", "cox --help", None, "dev/README.md", None,
              "add cox dev release-check to dev/README.md"),
    ]


def test_check_cli_surface_flags_a_documented_command_the_cli_no_longer_has():
    facts = {
        "cli_commands": set(),
        "doc_commands": {"docs/reference/cli/dev.md": {"cox dev retired"}},
        "readme_commands": {"dev": {"cox dev retired"}},
    }
    assert check_cli_surface(facts) == [
        Drift("cli_surface", "docs/reference/cli/dev.md", None, "cox --help", None,
              "remove cox dev retired from docs/reference/cli/dev.md"),
        Drift("cli_surface", "dev/README.md", None, "cox --help", None,
              "remove cox dev retired from dev/README.md"),
    ]


def test_check_cli_surface_has_no_readme_to_check_when_no_readme_names_the_group():
    facts = {
        "cli_commands": {"cox dev release-check"},
        "doc_commands": {"docs/reference/cli/dev.md": {"cox dev release-check"}},
        "readme_commands": {},
    }
    assert check_cli_surface(facts) == []


def test_check_cli_surface_reports_no_drift_when_the_facts_agree():
    facts = {
        "cli_commands": {"cox dev release-check"},
        "doc_commands": {"docs/reference/cli/dev.md": {"cox dev release-check"}},
        "readme_commands": {"dev": {"cox dev release-check"}},
    }
    assert check_cli_surface(facts) == []


def test_check_cli_surface_reports_no_drift_when_the_generated_pages_match_the_surface():
    generated = {"/tmp/out/dev.md": {"cox dev release-check"}, "/tmp/out/runs.md": {"cox runs top"}}
    facts = {
        "cli_commands": {"cox dev release-check", "cox runs top"},
        "doc_commands": generated,
        "readme_commands": {"dev": {"cox dev release-check"}, "runs": {"cox runs top"}},
    }
    assert check_cli_surface(facts) == []


def test_check_cli_surface_reports_exactly_one_drift_when_the_generator_is_unavailable():
    facts = {
        "generator_error": "docs/_cli.py generator unavailable: no generator at /repo/coxswain/docs/_cli.py",
        "cli_commands": {"cox dev release-check"},
        "doc_commands": {},
        "readme_commands": {},
    }
    drifts = check_cli_surface(facts)
    assert len(drifts) == 1
    assert drifts == [
        Drift("cli_surface", "docs/_cli.py", None, "cox --help", None,
              "docs/_cli.py generator unavailable: no generator at /repo/coxswain/docs/_cli.py"),
    ]


def test_check_cli_surface_treats_a_bare_group_mention_as_a_namespace_not_a_stray_command():
    facts = {
        "cli_commands": {"cox setup profile", "cox dev release-check"},
        "doc_commands": {
            "docs/reference/cli/setup.md": {"cox setup profile"},
            "docs/reference/cli/dev.md": {"cox dev release-check"},
        },
        "readme_commands": {
            "setup": {"cox setup", "cox setup profile"},
            "dev": {"cox dev", "cox dev release-check"},
        },
    }
    assert check_cli_surface(facts) == []


def test_check_cli_surface_neither_requires_nor_flags_a_suppressed_groups_commands():
    facts = {
        "cli_commands": {"cox dev release-check", "cox release publish"},
        "doc_commands": {"docs/reference/cli/dev.md": {"cox dev release-check"}},
        "readme_commands": {"dev": {"cox dev release-check"}, "release": set()},
        "suppressed": {"cox release"},
    }
    assert check_cli_surface(facts) == []  # not required: release/README.md and its doc page stay silent
    facts["readme_commands"]["release"] = {"cox release archive"}
    assert check_cli_surface(facts) == []  # not flagged: release/README.md mentions it anyway


def test_check_cli_surface_names_the_differing_command_on_a_genuine_mismatch():
    facts = {
        "cli_commands": {"cox dev release-check", "cox dev doctor"},
        "doc_commands": {"/tmp/out/dev.md": {"cox dev release-check"}},
        "readme_commands": {"dev": {"cox dev release-check", "cox dev doctor"}},
    }
    assert check_cli_surface(facts) == [
        Drift("cli_surface", "cox --help", None, "/tmp/out/dev.md", None,
              "add cox dev doctor to /tmp/out/dev.md"),
    ]


def test_gather_cli_facts_reads_the_real_command_table_into_group_labelled_pages(tmp_path):
    pytest.importorskip("agent_tools.cli")
    facts = gather_cli_facts(str(tmp_path))
    assert "generator_error" not in facts
    assert "cox setup doctor" in facts["cli_commands"]
    assert set(facts["doc_commands"]) == {f"docs/reference/cli/{cmd.split()[1]}.md" for cmd in facts["cli_commands"]}
    assert "cox setup doctor" in facts["doc_commands"]["docs/reference/cli/setup.md"]


def test_gather_cli_facts_reports_only_a_generator_error_when_the_table_import_fails(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent_tools.cli", None)
    facts = gather_cli_facts(str(tmp_path))
    assert "COMMAND_TABLE" in facts["generator_error"]
    assert set(facts) == {"generator_error", "readme_commands"}


def _group(name, help="a group"):
    return SimpleNamespace(name=name, help=help)


def _row(name, summary="a row", subcommands=()):
    return SimpleNamespace(name=name, summary=summary, subcommands=subcommands)


def test_table_commands_on_the_real_command_table_reaches_setup_and_dev_but_not_release():
    table = pytest.importorskip("agent_tools.cli").COMMAND_TABLE
    commands = table_commands(table) | set(HAND_BUILT)
    assert {"cox setup doctor", "cox dev"} <= commands
    assert not any(cmd.split()[1] == "release" for cmd in commands)


def test_table_commands_names_a_group_with_no_rows_as_one_leaf():
    assert table_commands([(_group("dash"), [])]) == {"cox dash"}


def test_table_commands_yields_one_command_per_row():
    assert table_commands([(_group("setup"), [_row("doctor"), _row("profile")])]) == {
        "cox setup doctor", "cox setup profile",
    }


def test_table_commands_replaces_a_row_with_its_subcommands():
    table = [(_group("route"), [_row("launch", subcommands=(_row("epic"), _row("task")))])]
    assert table_commands(table) == {"cox route launch epic", "cox route launch task"}


def test_table_commands_leaves_out_a_suppressed_group_and_a_suppressed_row():
    table = [
        (_group("release", help=argparse.SUPPRESS), [_row("publish")]),
        (_group("setup"), [_row("doctor"), _row("old", summary=argparse.SUPPRESS)]),
    ]
    assert table_commands(table) == {"cox setup doctor"}


def test_table_suppressed_lists_the_groups_whose_help_is_suppressed():
    table = [(_group("release", help=argparse.SUPPRESS), []), (_group("setup"), [])]
    assert table_suppressed(table) == {"cox release"}


def test_release_check_passes_only_the_root_to_gather_cli_facts(tmp_path, monkeypatch):
    from devtools import cli

    calls = []

    def fake_gather_cli_facts(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(cli.release_check_cli, "gather_cli_facts", fake_gather_cli_facts)
    monkeypatch.setattr(cli.release_check_manifest, "gather_manifest_facts", lambda *a, **k: {})
    monkeypatch.setattr(cli.release_check_notes, "gather_notes_facts", lambda *a, **k: {})
    monkeypatch.setattr(cli.release_check_pages, "gather_page_facts", lambda *a, **k: {})
    monkeypatch.setattr(cli.release_check_readmes, "gather_readmes_facts", lambda *a, **k: {})
    monkeypatch.setattr(cli.release_check, "gather_version_facts", lambda *a, **k: {})
    monkeypatch.setattr(cli.release_check_index, "gather_release_index_facts", lambda *a, **k: {})
    monkeypatch.setattr(cli.release_check, "CHECKS", ())

    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text('[coxswain]\nversion = "0.1.0"\n')

    rc = cli.main(["release-check", "--manifest", str(manifest_path), "--root", str(tmp_path)])

    assert rc == 0
    assert calls == [((str(tmp_path.resolve()),), {})]
