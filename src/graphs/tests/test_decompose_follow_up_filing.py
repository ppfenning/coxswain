"""The harness passes the profile's repo map to decompose and files each follow-up intake after the initiative."""

from __future__ import annotations

import importlib
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools import route, run_store
from agent_tools.queue_rows import parse_item
from graphs.delivery import initiative_decompose
from harness import cli
from harness.store_write import Store
from runner import ScriptedRunner

INITIATIVE = "demo"


def _checkout(path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    return path


def _task(tid: str, surface: str, body: str) -> dict:
    return {"id": tid, "phase": "p1", "title": tid, "body": body, "needs": [], "surfaces": [surface]}


def _plan(*tasks: dict) -> dict:
    return {"phases": [{"id": "p1", "goal": "g"}], "tasks": list(tasks), "rationale": "r"}


class _Parser:
    def __init__(self, args) -> None:
        self.args = args

    def error(self, message: str) -> None:
        raise AssertionError(message)


@pytest.fixture
def world(tmp_path, monkeypatch, store_conn):
    """Workspace, repositories A and B beside it, a profile naming B, and in-memory task rows."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    repo_a = _checkout(tmp_path / "a", {"src/a.py": "a = 1\n"})
    repo_b = _checkout(tmp_path / "b", {"lib/b.py": "b = 1\n"})
    profile = tmp_path / "profile.yaml"
    profile.write_text(f'repo_map: {{"b": "{repo_b}"}}\n', encoding="utf-8")
    rows: list[dict] = []

    def write(runs_dir, path, parts, text) -> int:
        rows.append(parse_item("task", parts, text))
        return 0

    def apply(item, *, cartridge, runner) -> tuple[bool, str]:
        """Stand in for the model arm: write the file the proposal's action names, under the runner's root."""
        action = str(item["suggested_action"])
        path = Path(runner.cwd) / re.match(r"create (\S+) with", action).group(1)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            action.split("body =\n", 1)[1]
            if item["target"] == "initiative"
            else f"---\nid: {item['target']}\nstate: ready\n---\n{item['rationale']}\n"
        )
        path.write_text(text, encoding="utf-8")
        return True, str(path)

    monkeypatch.setattr(importlib.import_module("harness.gate"), "auto_apply", apply)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(cli, "_write_filed_task", write)
    monkeypatch.setattr(
        cli, "_read_filed_rows", lambda runs_dir, initiative: [r for r in rows if r["initiative"] == initiative]
    )
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: True)
    store = Store(store_conn)
    store.record_run({"run_id": "r1", "ts": "2026-11-06T00:00:00Z", "principal": "initiative-decompose"})
    return SimpleNamespace(
        tmp=tmp_path, workspace=workspace, a=repo_a, b=repo_b, profile=profile, store=store, rows=rows
    )


def _decompose(world, monkeypatch, plan: dict) -> int:
    cartridge = {
        "team": "acme",
        "cartridge_sha": "sha",
        "cartridge_dir": "/fake",
        "context": [],
        "skills": {"decompose": "acme-skills:decompose"},
        "write_kinds": {"item_create": {"risk": "low", "ramp": "gated", "apply_arm": "workstore"}},
        "work_routing": {"states": {"active": "work", "planned": "work", "future": "backlog"}},
        "landing_areas": {"worktree_root": "~/worktrees"},
    }
    material = {"idea": "# Split\n\ngo", "initiative_id": INITIATIVE, "tree": []}
    monkeypatch.setattr(cli, "_materialise", lambda spec, args, parser: dict(material))
    spec = SimpleNamespace(
        name="decompose", graph_name=initiative_decompose.GRAPH_NAME, needs=(), run=initiative_decompose.run
    )
    args = SimpleNamespace(
        graph="decompose",
        date="2026-11-06",
        runs_dir=world.tmp / "runs",
        ledger=world.tmp / "ledger.jsonl",
        provider_profile=str(world.profile),
        repo=str(world.a),
        assume="a",
        result_out=None,
    )
    runner = ScriptedRunner({"decompose": plan})
    runner.cwd = world.workspace
    return cli._run_graph(
        specs={"decompose": spec},
        parser=_Parser(args),
        args=args,
        cartridge=cartridge,
        runner=runner,
        run_id="r1",
        store=world.store,
    )


def _files(root: Path, pattern: str) -> list[Path]:
    return sorted(root.glob(pattern))


def test_a_task_for_repository_b_becomes_an_intake_that_waits_on_the_initiative(world, monkeypatch, capsys) -> None:
    plan = _plan(_task("t1", "src/a.py", "edit a"), _task("t2", "lib/b.py", "edit b in the other repo"))
    assert _decompose(world, monkeypatch, plan) == 0
    out = capsys.readouterr().out

    tasks = _files(world.workspace / "work", "**/demo-t*.md")
    assert [p.name for p in tasks] == ["demo-t1.md"]
    intake = [p for p in world.workspace.rglob("*.md") if "intake" in p.parts]
    assert len(intake) == 1
    text = intake[0].read_text(encoding="utf-8")
    assert "edit b in the other repo" in text
    assert f"after: [{INITIATIVE}]" in text
    assert str(world.b) in text
    assert "lint:" in text
    assert f"decompose: filed follow-up for {world.b}:" in out
    initiative = (world.workspace / "work" / INITIATIVE / "initiative.md").read_text(encoding="utf-8")
    assert "Task demo-t2 has no surface here" in initiative


def test_a_task_touching_no_known_repository_is_dropped_and_nothing_is_filed(world, monkeypatch, capsys) -> None:
    plan = _plan(_task("t1", "src/a.py", "edit a"), _task("t2", "elsewhere/c.py", "edit c nowhere"))
    assert _decompose(world, monkeypatch, plan) == 0

    assert [p.name for p in _files(world.workspace / "work", "**/demo-t*.md")] == ["demo-t1.md"]
    assert [p for p in world.workspace.rglob("*.md") if "intake" in p.parts] == []
    assert "filed follow-up" not in capsys.readouterr().out


def test_a_follow_up_whose_write_fails_prints_a_line_and_exits_non_zero(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr(route, "write_filed_item", lambda *a, **k: 1)
    plan = _plan(_task("t1", "src/a.py", "edit a"), _task("t2", "lib/b.py", "edit b"))
    assert _decompose(world, monkeypatch, plan) == 1
    assert f"decompose: follow-up for {world.b} was not filed" in capsys.readouterr().err


def test_repo_trees_skips_the_target_a_non_checkout_and_a_non_string() -> None:
    listing = {"/r/b": [{"repo": "/r/b", "path": "x.py"}]}
    trees = cli._repo_trees(
        {"a": "/r/a", "b": "/r/b", "c": "/r/c", "d": 4},
        Path("/r/ws"),
        "/r/a",
        home="/h",
        is_checkout=lambda p: str(p) in ("/r/a", "/r/b"),
        listing=lambda repo: listing.get(repo, []),
    )
    assert trees == {"/r/b": ["x.py"]}


def test_a_profile_without_a_repo_map_passes_nothing(tmp_path) -> None:
    assert cli._decompose_repo_trees({}, tmp_path, None) == {}
    assert cli._decompose_repo_trees({"repo_map": "oops"}, tmp_path, None) == {}


def test_a_layout_follow_up_is_filed_as_an_intake_waiting_on_the_initiative(tmp_path) -> None:
    from agent_tools.follow_up_intake import follow_up_intake_file
    from graphs.delivery.decompose_split import split_foreign

    own = {"src/tools/agent_tools/a.py"}
    tasks = [
        {**_task("t1", "src/tools/agent_tools/a.py", "edit a"), "title": "t1"},
        {**_task("t2", "src/ui/regatta.rs", "edit the dashboard"), "title": "t2"},
    ]
    split = split_foreign(tasks, own, {}, title="Split", own_key="coxswain")
    ((_, text),) = follow_up_intake_file(
        split.follow_ups[0], after=INITIATIVE, date="2026-11-10", home=str(tmp_path)
    ).items()
    assert f"after: [{INITIATIVE}]" in text
    assert "~/repos/coxswain-dash" in text
    assert "edit the dashboard" in text
