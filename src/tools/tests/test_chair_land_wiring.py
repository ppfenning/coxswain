"""The chair's live run deps land through ProcessLands over `<runs>/chair-lands`; a dry run keeps no sink."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_tools import cli, commands  # noqa: F401  (cli imports commands first; keep the import order the suite uses)
from agent_tools.chair_land_proc import LandSpec, encode_spec, land_key, paths_for
from agent_tools.chair_land_sink import ProcessLands


def _deps(tmp_path: Path, dry_run: bool):
    return cli._chair_run_deps(
        tmp_path / "runs", {}, "chair", 1, "h", dry_run, print, tmp_path / "profile.yaml", "files"
    )


def _no_spawn(monkeypatch) -> None:
    real = subprocess.Popen

    def refuse(argv, *args, **kwargs):
        # building the deps runs git reads for the checkout state; only a land child is forbidden
        assert "agent_tools.chair_land_proc" not in argv, "building the run deps started a land process"
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_live_deps_land_through_process_lands_over_the_runs_dir(tmp_path, monkeypatch) -> None:
    _no_spawn(monkeypatch)
    deps = _deps(tmp_path, False)
    try:
        lands = deps.exec_deps.lands
        assert isinstance(lands, ProcessLands)
        assert lands._dir == tmp_path / "runs" / "chair-lands"
        assert not lands._dir.exists()
        assert deps.facts_deps.landing == lands.landing
    finally:
        deps.stop_lands()


def test_a_process_land_does_not_hold_the_restart(tmp_path, monkeypatch) -> None:
    _no_spawn(monkeypatch)
    deps = _deps(tmp_path, False)
    try:
        lands = deps.exec_deps.lands
        action = {"kind": "land_phase", "initiative": "i", "phase": "p", "repo": "r"}
        spec_path, _ = paths_for(lands._dir, land_key(action))
        lands._dir.mkdir(parents=True)
        spec_path.write_text(encode_spec(LandSpec(land_key(action), action, ["true"], "r", 60.0, 0.0, "h", 1)))
        assert lands.landing() == [{"initiative": "i", "phase": "p", "repo": "r"}]
        assert deps.lands_in_flight() is False
    finally:
        deps.stop_lands()


def test_a_dry_run_has_no_land_sink(tmp_path) -> None:
    deps = _deps(tmp_path, True)
    assert deps.exec_deps.lands is None
    assert deps.lands_in_flight() is False
