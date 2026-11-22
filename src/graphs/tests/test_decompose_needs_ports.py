"""A decompose started without the tools' ports refuses at the door: exit 2, before a run, a lease or a model call.

`shell.py` always passes them; a caller that does not would otherwise leave task files with no store rows.
"""

from __future__ import annotations

import pytest

from harness import cli


def test_main_without_ports_refuses_a_decompose_before_anything_opens(tmp_path, capsys) -> None:
    runs = tmp_path / "runs"
    with pytest.raises(SystemExit) as refused:
        cli.main(["decompose", "--team", "t", "--unverified-skills", "--runs-dir", str(runs), "--scripted", "{}"])
    assert refused.value.code == 2
    assert "decompose needs the tools' store and route calls" in capsys.readouterr().err
    assert not runs.exists()
