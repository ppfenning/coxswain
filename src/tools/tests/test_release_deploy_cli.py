"""`cox release deploy` refuses until its host-by-host edge exists; every other `release ...` prints the moved pointer.

The command shipped wired to `agent_tools.release_deploy_edge`, a module that never landed. These tests used to stub
that module into `sys.modules`, so they passed while the real command raised ImportError. Nothing is stubbed here.
"""

from __future__ import annotations

import pytest

from agent_tools import cli


def test_deploy_refuses_with_exit_two_and_names_what_to_run_instead(capsys):
    assert cli.main(["release", "deploy", "2026.11.6"]) == 2
    err = capsys.readouterr().err
    assert "cox release deploy: not available" in err
    assert "run `cox upgrade` on each host" in err


@pytest.mark.parametrize("argv", [["release", "0.15.0", "--dry-run"], ["release", "check", "--json"], ["release"]])
def test_other_release_subcommands_print_the_moved_pointer_and_exit_two(capsys, argv):
    assert cli.main(argv) == 2
    assert capsys.readouterr().out == cli._DEV_MOVED + "\n"


def test_deploy_without_a_version_is_an_argparse_refusal(capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(["release", "deploy"])
    assert refused.value.code == 2
    assert "VERSION" in capsys.readouterr().err


def test_help_says_deploy_is_not_available_yet():
    text = cli.build_parser().format_help()
    assert cli._RELEASE_HELP.split(" (")[0] in text
    assert "not available yet" in cli._RELEASE_HELP
    assert "cox release deploy 2026.11.6" not in text
