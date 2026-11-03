from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import harness.cli as cli


class _Parser:
    def error(self, message: str) -> None:
        raise SystemExit(message)


class _Launched(Exception):
    pass


def _run_phase_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, flag: int | None, profile: str | None, ready: int = 3
) -> int:
    """Drive the phase arm with `ready` tasks; return the max_parallel handed to run_phase."""
    initiative = {
        "id": "demo",
        "phases": ["p1"],
        "items": [{"id": f"t{n}", "phase": "p1", "state": "ready"} for n in range(ready)],
    }
    monkeypatch.setattr(cli.workstore, "read_initiative", lambda name: initiative)
    seen: dict[str, Any] = {}

    def _launch(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise _Launched

    monkeypatch.setattr(cli, "run_phase", _launch)
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(profile or "tiers: {}\n", encoding="utf-8")
    args = SimpleNamespace(
        graph="phase",
        initiative="demo",
        phase_name="p1",
        max_parallel=flag,
        date="2026-11-03",
        provider_profile=str(profile_path),
    )
    with contextlib.suppress(_Launched):
        cli._run_graph(
            specs={"lifecycle": SimpleNamespace(run=None)},
            parser=_Parser(),
            args=args,
            cartridge={},
            runner=None,
            run_id="r1",
        )
    return seen["max_parallel"]


def test_the_option_defaults_to_none() -> None:
    parser = cli._build_parser({})
    assert parser.parse_args(["--team", "t", "phase"]).max_parallel is None
    assert parser.parse_args(["--team", "t", "--max-parallel", "2", "phase"]).max_parallel == 2


def test_the_command_line_beats_the_profile(monkeypatch, tmp_path, capsys) -> None:
    assert _run_phase_arm(monkeypatch, tmp_path, flag=2, profile="max_parallel: 5\n") == 2
    assert "running up to 2 at once" in capsys.readouterr().out


def test_the_profile_applies_when_no_flag_is_given(monkeypatch, tmp_path, capsys) -> None:
    assert _run_phase_arm(monkeypatch, tmp_path, flag=None, profile="max_parallel: 5\n") == 5
    assert "running up to 5 at once" in capsys.readouterr().out


def test_no_cap_prints_the_free_lane_count(monkeypatch, tmp_path, capsys) -> None:
    assert _run_phase_arm(monkeypatch, tmp_path, flag=None, profile=None, ready=3) == 3
    assert "phase p1: 3 task(s) ready, running up to 3 at once" in capsys.readouterr().out


@pytest.mark.parametrize(("flag", "expected"), [(None, 4), (2, 2)])
def test_cos_keeps_its_default_of_four(monkeypatch, flag, expected) -> None:
    import harness.cos as cos

    seen: dict[str, Any] = {}

    def _run_cos(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise _Launched

    monkeypatch.setattr(cli, "_cos_docket_args", lambda **kw: {"intake_root": "intake"})
    monkeypatch.setattr(cos, "assemble_docket", lambda **kw: {})
    monkeypatch.setattr(cos, "run_cos", _run_cos)
    args = SimpleNamespace(graph="cos", max_parallel=flag, date="2026-11-03", ledger="ledger")
    with contextlib.suppress(_Launched):
        cli._run_graph(specs={}, parser=_Parser(), args=args, cartridge={}, runner=None, run_id="r1")
    assert seen["max_parallel"] == expected


def test_a_bad_profile_value_is_a_usage_error(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="max_parallel must be a positive integer, got 0"):
        _run_phase_arm(monkeypatch, tmp_path, flag=None, profile="max_parallel: 0\n")
