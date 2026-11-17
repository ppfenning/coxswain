from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from harness import epic


def _hung_runner(seen: list[float]):
    def run(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    return run


def test_hung_create_is_reported_not_raised(monkeypatch, tmp_path: Path) -> None:
    seen: list[float] = []
    monkeypatch.setattr(epic.subprocess, "run", _hung_runner(seen))
    ctx = SimpleNamespace(repo=tmp_path)
    result = epic._open_review_pr(ctx, title="t", body="b", head="h", base="main")
    assert result == (False, "gh pr create timed out after 120s")
    assert seen == [epic._GH_TIMEOUT_SECONDS]


def test_hung_view_is_reported_not_raised(monkeypatch, tmp_path: Path) -> None:
    seen: list[float] = []
    monkeypatch.setattr(epic.subprocess, "run", _hung_runner(seen))
    ctx = SimpleNamespace(repo=tmp_path)
    assert epic._existing_pr(ctx, "review/x/y") == (False, "gh pr view timed out after 120s")
    assert seen == [epic._GH_TIMEOUT_SECONDS]


def test_hung_view_surfaces_through_grain_pr(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(epic.subprocess, "run", _hung_runner([]))
    monkeypatch.setattr(epic, "_push_review_ref", lambda ctx, local, name: (True, ""))
    ctx = SimpleNamespace(repo=tmp_path, initiative_id="init")
    ok, detail = epic._open_grain_pr(ctx, grain="task", name="t1", local="scratch", title="t", base="main")
    assert ok is False
    assert "gh pr view timed out after 120s" in detail
