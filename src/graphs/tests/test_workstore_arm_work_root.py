"""The workstore arm writes under the runner's work root, whatever directory the process was started in.

A decompose launched over ssh on a lane host runs with the ssh login directory (the home directory) as its
process cwd and the work root as `--workdir`. The decompose's task payloads carry store-relative paths
(`work/<initiative>/<phase>/<task>.md`). The built-in `workstore` arm resolved them against the process cwd, so
every task file landed under `~/work/...`, and the settle step, reading the work root, found none of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from graphs.delivery.initiative_decompose import _task_apply
from harness import cli
from harness.gate import auto_apply
from harness.runners import _DelegatingFastPath

TASK = {
    "id": "demo-t1",
    "phase": "p1-first",
    "title": "A task",
    "needs": [],
    "surfaces": ["src/a.py"],
    "body": "Write the thing.",
}


class NoModelRunner:
    """The runner the harness built, with `cwd` set from `--workdir`. The workstore arm must not call it."""

    capabilities: ClassVar[dict[str, bool]] = {"tool_use": True}

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def run(self, **kwargs):
        pytest.fail(f"the model arm must not run: {kwargs.get('role')}")


def cartridge() -> dict:
    """The lane host's cartridge: every work kind applied by the built-in workstore arm."""
    arm = {"risk": "low", "ramp": "gated", "apply_arm": "workstore"}
    return {"context": [], "write_kinds": {"item_create": arm, "item_update": arm, "state_move": arm}}


def proposal(kind: str, apply: dict) -> dict:
    return {
        "kind": kind,
        "risk": "low",
        "target": "demo-t1",
        "evidence": [{"check": "c", "output": "o"}],
        "rationale": "Write the thing.",
        "suggested_action": "create it",
        "apply": apply,
    }


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """`(login, root)`: the process stands in the login directory, the work root is elsewhere."""
    login, root = tmp_path / "login", tmp_path / "workspace"
    login.mkdir()
    root.mkdir()
    monkeypatch.chdir(login)
    return login, root


def test_a_decompose_task_lands_under_the_work_root_not_the_process_cwd(lane) -> None:
    login, root = lane
    apply = _task_apply(TASK, TASK["body"], landing="work", initiative_id="demo")
    assert apply["path"] == "work/demo/p1-first/demo-t1.md"

    ok, detail = auto_apply(proposal("item_create", apply), cartridge=cartridge(), runner=NoModelRunner(root))

    assert ok, detail
    assert (root / apply["path"]).is_file()
    assert not (login / "work").exists()
    assert str(root / apply["path"]) in detail


def test_the_settle_step_finds_what_the_arm_wrote(lane) -> None:
    """The run's own sequence: arm, then settle against `runner.cwd`. Before the fix this was 'not written'."""
    _, root = lane
    runner = NoModelRunner(root)
    apply = _task_apply(TASK, TASK["body"], landing="work", initiative_id="demo")
    auto_apply(proposal("item_create", apply), cartridge=cartridge(), runner=runner)

    rows: list[dict] = []
    in_store, on_disk = cli._settle_decompose_tasks(
        {"demo-t1": apply["path"]},
        root=Path(runner.cwd),
        runs_dir=root / "runs",
        initiative="demo",
        write=lambda _runs, _path, parts, _text: rows.append({"task_id": Path(parts[2]).stem}) or 0,
        read=lambda _runs, _initiative: rows,
    )
    assert (in_store, on_disk) == ({"demo-t1"}, {"demo-t1"})


def test_the_fast_path_wrapper_hands_the_arm_the_inner_runners_root(lane) -> None:
    """With system_one on, the harness holds `_DelegatingFastPath`; `cwd` is read through it from the inner runner."""
    login, root = lane
    wrapped = object.__new__(_DelegatingFastPath)
    object.__setattr__(wrapped, "_inner", NoModelRunner(root))
    apply = _task_apply(TASK, TASK["body"], landing="work", initiative_id="demo")

    ok, detail = auto_apply(proposal("item_create", apply), cartridge=cartridge(), runner=wrapped)

    assert ok, detail
    assert (root / apply["path"]).is_file()
    assert not (login / "work").exists()


def test_an_update_and_a_state_move_resolve_against_the_work_root(lane) -> None:
    login, root = lane
    runner = NoModelRunner(root)
    rel = "work/demo/p1-first/demo-t1.md"
    create = _task_apply(TASK, TASK["body"], landing="work", initiative_id="demo")
    assert auto_apply(proposal("item_create", create), cartridge=cartridge(), runner=runner)[0]

    item = {**create["item"], "title": "A renamed task"}
    ok, detail = auto_apply(proposal("item_update", {"path": rel, "item": item}), cartridge=cartridge(), runner=runner)
    assert ok, detail
    assert "A renamed task" in (root / rel).read_text(encoding="utf-8")

    ok, detail = auto_apply(proposal("state_move", {"path": rel, "state": "done"}), cartridge=cartridge(), runner=runner)
    assert ok, detail
    assert "state: done" in (root / rel).read_text(encoding="utf-8")
    assert not (login / "work").exists()


def test_an_intake_retire_moves_within_the_work_root(lane) -> None:
    login, root = lane
    intake = root / "intake" / "idea.md"
    intake.parent.mkdir(parents=True)
    intake.write_text("---\ntitle: An idea\n---\n\nThe idea.\n", encoding="utf-8")
    apply = {"path": "intake/idea.md", "retire_to": "intake/done/idea.md", "fields": {"initiative": "demo"}}

    ok, detail = auto_apply(proposal("state_move", apply), cartridge=cartridge(), runner=NoModelRunner(root))

    assert ok, detail
    assert not intake.exists()
    assert "initiative: demo" in (root / "intake" / "done" / "idea.md").read_text(encoding="utf-8")
    assert not (login / "intake").exists()


def test_an_absolute_path_is_written_where_it_names(lane, tmp_path: Path) -> None:
    _, root = lane
    target = tmp_path / "elsewhere" / "demo-t1.md"
    apply = {"path": str(target), "item": _task_apply(TASK, TASK["body"], landing="work", initiative_id="demo")["item"]}

    ok, detail = auto_apply(proposal("item_create", apply), cartridge=cartridge(), runner=NoModelRunner(root))

    assert (ok, detail) == (True, f"{target} created")
    assert target.is_file()


def test_a_runner_without_a_cwd_resolves_against_the_process_cwd(lane) -> None:
    """A caller with no work root keeps the old meaning: the process cwd."""
    login, _ = lane

    class Bare:
        def run(self, **kwargs):
            pytest.fail("the model arm must not run")

    apply = _task_apply(TASK, TASK["body"], landing="work", initiative_id="demo")
    ok, detail = auto_apply(proposal("item_create", apply), cartridge=cartridge(), runner=Bare())

    assert ok, detail
    assert (login / apply["path"]).is_file()


def test_settle_names_a_task_file_found_outside_the_work_root_and_leaves_it(lane, capsys) -> None:
    """A file an older arm left under the process cwd is reported with both paths, and never moved."""
    login, root = lane
    rel = "work/demo/p1-first/demo-t1.md"
    stray = login / rel
    stray.parent.mkdir(parents=True)
    stray.write_text("---\nid: demo-t1\n---\n", encoding="utf-8")

    in_store, on_disk = cli._settle_decompose_tasks(
        {"demo-t1": rel},
        root=root,
        runs_dir=root / "runs",
        initiative="demo",
        write=lambda *_: pytest.fail("nothing is filed from outside the work root"),
        read=lambda *_: [],
    )

    assert (in_store, on_disk) == (set(), set())
    err = capsys.readouterr().err
    assert f"found at {stray}, outside the work root {root}" in err
    assert stray.is_file()
    assert not (root / rel).exists()
