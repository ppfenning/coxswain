"""`cox release` is a moved pointer, as `cox dev` is: every `release ...`, `deploy` included, prints it and exits 2.

`cox release deploy` once shipped wired to a module that never landed, then refused with "not available yet" (#1229).
It deployed nothing, so it is gone; the pointer says to run `cox upgrade` on each host instead.
"""

from __future__ import annotations

import pytest

from agent_tools import cli


@pytest.mark.parametrize(
    "argv", [["release", "deploy", "2026.11.6"], ["release", "0.15.0", "--dry-run"], ["release", "check"], ["release"]]
)
def test_every_release_subcommand_prints_the_moved_pointer_and_exits_two(capsys, argv):
    assert cli.main(argv) == 2
    out = capsys.readouterr().out
    assert out == cli._RELEASE_MOVED + "\n"
    assert "run `cox upgrade` on that host" in out


def test_help_no_longer_offers_a_command_that_is_not_available():
    assert "not available" not in cli.build_parser().format_help()
