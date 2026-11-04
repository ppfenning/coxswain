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


def _run_epic_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, profile: str | None, runner: Any = None
) -> dict[str, Any]:
    """Drive the epic arm up to run_epic; return the keyword arguments it was called with."""
    initiative = {"id": "demo", "phases": ["p1"], "items": [{"id": "t0", "phase": "p1", "state": "ready"}]}
    monkeypatch.setattr(cli.workstore, "read_initiative", lambda name: initiative)
    seen: dict[str, Any] = {}

    def _launch(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise _Launched

    monkeypatch.setattr(epic, "run_epic", _launch)
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(profile or "tiers: {}\n", encoding="utf-8")
    args = SimpleNamespace(
        graph="epic",
        initiative="demo",
        repo=str(tmp_path),
        provider_profile=str(profile_path),
        max_parallel=None,
        date="2026-11-04",
        ledger="ledger",
        runs_dir=str(tmp_path / "runs"),
        worktree_root=str(tmp_path / "worktrees"),
        assume=None,
        fix_attempts=1,
        resume_from=None,
        base=None,
    )
    with contextlib.suppress(_Launched):
        cli._run_graph(
            specs={},
            parser=_Parser(),
            args=args,
            cartridge={},
            runner=runner,
            run_id="r1",
        )
    return seen


def test_a_profile_with_both_keys_reaches_run_epic(monkeypatch, tmp_path) -> None:
    runner = SimpleNamespace(calls=[{"cost_usd": 1.5}, {"cost_usd": 2.0}])
    seen = _run_epic_arm(monkeypatch, tmp_path, profile="run_ceiling_usd: 20\ntask_estimate_usd: 4\n", runner=runner)
    assert callable(seen["headroom_usd"])
    assert seen["est_task_usd"] == 4.0
    assert isinstance(seen["est_task_usd"], float)
    assert seen["headroom_usd"]() == 16.5


def test_headroom_follows_the_runner_as_it_spends(monkeypatch, tmp_path) -> None:
    runner = SimpleNamespace(calls=[])
    seen = _run_epic_arm(monkeypatch, tmp_path, profile="run_ceiling_usd: 10\n", runner=runner)
    assert seen["headroom_usd"]() == 10.0
    runner.calls.append({"cost_usd": 3.25})
    assert seen["headroom_usd"]() == 6.75


def test_a_runner_without_calls_has_spent_nothing(monkeypatch, tmp_path) -> None:
    seen = _run_epic_arm(monkeypatch, tmp_path, profile="run_ceiling_usd: 10\n", runner=None)
    assert seen["headroom_usd"]() == 10.0


def test_a_profile_with_neither_key_passes_a_callable_returning_none(monkeypatch, tmp_path) -> None:
    seen = _run_epic_arm(monkeypatch, tmp_path, profile=None, runner=SimpleNamespace(calls=[{"cost_usd": 9.0}]))
    assert callable(seen["headroom_usd"])
    assert seen["headroom_usd"]() is None
    assert seen["est_task_usd"] == 6.0


@pytest.mark.parametrize(
    ("profile", "key"),
    [
        ("run_ceiling_usd: 0\n", "run_ceiling_usd"),
        ("run_ceiling_usd: -3\n", "run_ceiling_usd"),
        ("run_ceiling_usd: true\n", "run_ceiling_usd"),
        ("task_estimate_usd: abc\n", "task_estimate_usd"),
        ("task_estimate_usd: 0\n", "task_estimate_usd"),
    ],
)
def test_a_bad_profile_value_is_a_usage_error_naming_the_key(monkeypatch, tmp_path, profile, key) -> None:
    with pytest.raises(SystemExit, match=f"{key} must be a positive number"):
        _run_epic_arm(monkeypatch, tmp_path, profile=profile)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(6, 6.0), (2.5, 2.5), (None, 1.0)],
)
def test_positive_number_accepts_positive_values_and_defaults(value, expected) -> None:
    assert cli._positive_number("k", value, 1.0) == expected


@pytest.mark.parametrize("value", [0, -1, "x", True])
def test_positive_number_rejects_the_rest_naming_the_key(value) -> None:
    assert cli._positive_number("k", value, 1.0) == f"k must be a positive number, got {value}"
