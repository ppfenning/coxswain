from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import harness.cli as cli
import harness.epic as epic


class _Parser:
    def error(self, message: str) -> None:
        raise SystemExit(message)


class _Launched(Exception):
    pass


def _run_epic_arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]) -> dict[str, Any]:
    """Parse `argv` with the real parser, drive the epic arm up to run_epic, and return its keyword arguments."""
    monkeypatch.setattr(cli.workstore, "read_initiative", lambda name: {"id": "demo", "items": []})
    seen: dict[str, Any] = {}

    def _launch(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise _Launched

    monkeypatch.setattr(epic, "run_epic", _launch)
    profile = tmp_path / "profile.yaml"
    profile.write_text("tiers: {}\n", encoding="utf-8")
    parsed = cli._build_parser({}).parse_args(
        ["epic", "--team", "acme", "--initiative", "demo", "--repo", str(tmp_path), *argv]
    )
    # `fix_attempts` is not a flag of the bare parser; the launcher sets it from the graph specs' needs.
    args = SimpleNamespace(
        **{**vars(parsed), "provider_profile": str(profile), "runs_dir": str(tmp_path / "runs"), "fix_attempts": 1}
    )
    with contextlib.suppress(_Launched):
        cli._run_graph(specs={}, parser=_Parser(), args=args, cartridge={}, runner=None, run_id="r1")
    return seen


def test_repeated_phase_flags_reach_run_epic_as_a_frozenset(monkeypatch, tmp_path) -> None:
    seen = _run_epic_arm(monkeypatch, tmp_path, ["--phase", "a", "--phase", "b"])
    assert seen["only_phases"] == frozenset({"a", "b"})
    assert isinstance(seen["only_phases"], frozenset)


def test_no_phase_flag_passes_no_subset(monkeypatch, tmp_path) -> None:
    seen = _run_epic_arm(monkeypatch, tmp_path, [])
    assert "only_phases" not in seen
    assert seen["run_id"] == "r1"


def test_the_parser_collects_phase_in_order_and_defaults_to_none() -> None:
    parser = cli._build_parser({})
    assert parser.parse_args(["epic", "--team", "t", "--phase", "a", "--phase", "b"]).phase == ["a", "b"]
    assert parser.parse_args(["epic", "--team", "t"]).phase is None


@pytest.mark.parametrize("graph", ["phase", "rescue", "sweep"])
def test_phase_is_refused_on_any_graph_but_epic(graph, capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([graph, "--team", "t", "--phase", "a"])
    assert raised.value.code == 2
    assert "--phase applies to the epic graph only" in capsys.readouterr().err
