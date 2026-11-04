import argparse
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
    walk_help,
)

_HELP = {
    "cox": (
        "usage: cox [-h] {dev,runs} ...\n\n"
        "positional arguments:\n"
        "  {dev,runs}\n"
        "    dev                 maintainer commands\n"
        "    runs                inspect runs\n"
    ),
    "cox dev": (
        "usage: cox dev [-h] {release-check} ...\n\n"
        "positional arguments:\n"
        "  {release-check}\n"
        "    release-check       gather facts and print drifts\n"
    ),
    "cox dev release-check": "usage: cox dev release-check [-h]\n",
    "cox runs": (
        "usage: cox runs [-h] {top} ...\n\n"
        "positional arguments:\n"
        "  {top}\n"
        "    top                 live table of runs in flight\n"
    ),
    "cox runs top": "usage: cox runs top [-h]\n",
}

_HELP_WITH_A_PLAIN_CHOICE = {
}


def test_walk_help_returns_only_leaf_commands_from_a_subparsers_choices_line():
    assert walk_help(_HELP) == {"cox dev release-check", "cox runs top"}


def test_walk_help_does_not_read_a_flags_or_a_plain_positionals_choice_list_as_a_subcommand():
    assert walk_help(_HELP_WITH_A_PLAIN_CHOICE) == set()


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


def _run_from(help_texts):
    def run(cmd, root):
        key = " ".join(cmd[:-1])
        return (0, help_texts[key]) if key in help_texts else (1, "")

    return run


def test_gather_cli_facts_reports_one_generator_error_when_cox_is_not_on_path(tmp_path):
    facts = gather_cli_facts(str(tmp_path), lambda cmd, root: (127, ""))
    assert facts.get("generator_error")
    assert "cli_commands" not in facts and "doc_commands" not in facts


def test_gather_cli_facts_reports_one_generator_error_when_the_generator_module_is_missing(tmp_path):
    facts = gather_cli_facts(str(tmp_path), _run_from(_HELP))
    assert facts.get("generator_error")
    assert "cli_commands" not in facts and "doc_commands" not in facts


_FAKE_PARSE_SUBCOMMANDS = (
    "import re\n\n"
    "_POS = re.compile(r'positional arguments:\\n(.*?)(?:\\n\\n|\\Z)', re.DOTALL)\n"
    "_SUB = re.compile(r'^( *)\\{([^}]+)\\}\\n(?:\\1 +\\S.*\\n?)+', re.MULTILINE)\n\n"
    "def parse_subcommands(help_text):\n"
    "    section = _POS.search(help_text)\n"
    "    if section is None:\n"
    "        return set()\n"
    "    block = _SUB.search(section.group(1))\n"
    "    return set(block.group(2).split(',')) if block else set()\n"
)


def _write_generator(root, source):
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "_cli.py").write_text(source)


def test_gather_cli_facts_reads_doc_commands_from_parse_subcommands(tmp_path):
    _write_generator(tmp_path, _FAKE_PARSE_SUBCOMMANDS)
    facts = gather_cli_facts(str(tmp_path), _run_from(_HELP))
    assert facts["cli_commands"] == {"cox dev release-check", "cox runs top"}
    assert facts["doc_commands"] == {
        "docs/reference/cli/dev.md": {"cox dev release-check"},
        "docs/reference/cli/runs.md": {"cox runs top"},
    }


def test_gather_cli_facts_reports_one_generator_error_when_parse_subcommands_raises(tmp_path):
    _write_generator(tmp_path, "def parse_subcommands(help_text):\n    raise ValueError('boom')\n")
    facts = gather_cli_facts(str(tmp_path), _run_from(_HELP))
    assert facts.get("generator_error")
    assert "cli_commands" not in facts and "doc_commands" not in facts


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


def test_plain_env_forces_no_color_and_strips_force_color():
    from devtools.cli import _plain_env

    assert _plain_env({"FORCE_COLOR": "1", "PATH": "/x"}) == {
        "PATH": "/x", "NO_COLOR": "1", "PYTHON_COLORS": "0",
    }


def test_release_check_gathers_cli_facts_through_help_run_not_real_run(tmp_path, monkeypatch):
    from devtools import cli

    recorded = {}

    def fake_gather_cli_facts(root, run):
        recorded["run"] = run
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
    assert recorded["run"] is cli._help_run
