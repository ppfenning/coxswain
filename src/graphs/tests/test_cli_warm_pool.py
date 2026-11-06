from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import harness.cli as cli
import harness.epic as epic


class _Parser:
    def error(self, message: str) -> None:
        raise SystemExit(message)


def _epic_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], landing_areas: dict[str, Any] | None
) -> Any:
    """Run the epic arm with a stub driver; return the `pool` it was handed."""
    seen: dict[str, Any] = {}

    def _driver(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"totals": {}, "exit_code": 0}

    monkeypatch.setattr(epic, "run_epic", _driver)
    monkeypatch.setattr(epic, "work_state_of", lambda profile: None)
    monkeypatch.setattr(cli.workstore, "read_initiative", lambda name: {"id": "demo", "items": []})
    monkeypatch.setattr(cli, "_read_profile", lambda path: {})
    monkeypatch.setattr(cli, "_usage_bound", lambda parser, args, runner: (None, None))
    monkeypatch.setattr(cli, "register_stack_dump", lambda stream: None)
    args = cli._build_parser({}).parse_args(["--team", "t", *argv, "epic"])
    args.fix_attempts = 0
    args.initiative = "demo"
    args.repo = str(tmp_path)
    profile = tmp_path / "profile.yaml"
    profile.write_text("tiers: {}\n", encoding="utf-8")
    args.provider_profile = str(profile)
    cli._run_graph(
        specs={},
        parser=_Parser(),
        args=args,
        cartridge={"landing_areas": landing_areas or {}},
        runner=None,
        run_id="r1",
    )
    return seen["pool"]


_SYNC = ["uv", "sync", "--frozen"]
_DECLARED = {"sync": _SYNC, "lockfiles": ["uv.lock"]}


def _root(tmp_path: Path) -> list[str]:
    return ["--worktree-root", str(tmp_path / "worktrees")]


def test_default_settings_reach_the_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pool = _epic_pool(monkeypatch, tmp_path, _root(tmp_path), _DECLARED)
    assert pool.pool_root == tmp_path / "_pool"
    assert list(pool.sync_argv) == _SYNC
    assert list(pool.lock_names) == ["uv.lock"]
    assert pool.keep == cli.DEFAULT_POOL_KEEP


def test_the_default_worktree_root_puts_the_pool_beside_it_in_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _epic_pool(monkeypatch, tmp_path, [], _DECLARED).pool_root == tmp_path / "_pool"


def test_pool_keep_flag_overrides_the_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _epic_pool(monkeypatch, tmp_path, [*_root(tmp_path), "--pool-keep", "9"], _DECLARED).keep == 9
    assert _epic_pool(monkeypatch, tmp_path, [*_root(tmp_path), "--pool-keep", "0"], _DECLARED).keep == 0


def test_a_negative_pool_keep_is_refused() -> None:
    with pytest.raises(SystemExit):
        cli._build_parser({}).parse_args(["--team", "t", "--pool-keep=-1", "epic"])


def test_no_pool_flag_passes_no_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _epic_pool(monkeypatch, tmp_path, [*_root(tmp_path), "--no-pool"], _DECLARED) is None


@pytest.mark.parametrize(
    "areas",
    [
        {},
        {"lockfiles": ["uv.lock"]},
        {"sync": []},
        {"sync": ""},
        {"sync": 3},
        {"sync": "uv 'sync"},
        {"sync": _SYNC},
        {"sync": _SYNC, "lockfiles": []},
        {"sync": _SYNC, "lockfiles": ""},
        {"sync": _SYNC, "lockfiles": 3},
    ],
)
def test_an_undeclared_sync_command_or_lock_names_pass_no_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, areas: dict[str, Any]
) -> None:
    assert _epic_pool(monkeypatch, tmp_path, _root(tmp_path), areas) is None


def test_strings_split_like_lists() -> None:
    kwargs = {"enabled": True, "keep": 2, "worktree_root": "/w/trees"}
    from_strings = cli._pool_setting(landing_areas={"sync": "uv sync --frozen", "lockfiles": "uv.lock"}, **kwargs)
    from_lists = cli._pool_setting(landing_areas=_DECLARED, **kwargs)
    assert list(from_strings.sync_argv) == list(from_lists.sync_argv) == _SYNC
    assert list(from_strings.lock_names) == list(from_lists.lock_names) == ["uv.lock"]
    assert from_strings.pool_root == Path("/w/_pool")
