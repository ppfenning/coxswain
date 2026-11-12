"""The lander's real ports over a temporary workspace: work files, a sqlite store, a bare origin. Never a live one."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_tools import lander_ports
from harness import store_work_state

ID = ("-c", "user.email=t@invalid", "-c", "user.name=t")
NOW = "2026-11-12T10:00:00+00:00"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *ID, *args], capture_output=True, text=True, check=True).stdout.strip()


def write(path: Path, front: dict, body: str = "body\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}: [{', '.join(v)}]" if isinstance(v, list) else f"{k}: {v}" for k, v in front.items()]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n" + body, encoding="utf-8")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A workspace with one initiative of two phases, its repo, and a store that knows one task's newer state."""
    root = tmp_path / "ws"
    (root / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-qm", "base")
    write(root / "work" / "demo" / "initiative.md", {"id": "demo", "title": "Demo title", "repo": str(repo), "issue": 41})
    write(root / "work" / "demo" / "p1" / "a.md", {"id": "a", "title": "Task A", "state": "approved", "needs": []})
    write(root / "work" / "demo" / "p2" / "b.md", {"id": "b", "title": "Task B", "state": "ready", "needs": ["a"]})
    return root


@pytest.fixture
def ports(ws: Path):
    made = lander_ports.build(ws, {"workspace_dir": str(ws)}, worker="lander-test", forge_module=object())
    try:
        yield made
    finally:
        made.conn.close()


def test_task_rows_take_state_from_the_store_and_the_title_from_the_file() -> None:
    stored = [{"task_id": "a", "phase": "p1", "state": "approved", "needs": [], "merged_commit": "1" * 40}]
    files = {"a": {"title": "Task A", "state": "ready", "phase": "p1"}, "b": {"title": "Task B", "state": "ready", "phase": "p2", "needs": ["a"]}}
    assert lander_ports.task_rows(stored, files) == [
        {"id": "a", "phase": "p1", "state": "approved", "needs": [], "merged_commit": "1" * 40, "title": "Task A"},
        {"id": "b", "phase": "p2", "state": "ready", "needs": ["a"], "merged_commit": None, "title": "Task B"},
    ]


def test_a_branch_pr_counts_as_merged_only_when_the_forge_lists_a_merged_one() -> None:
    assert lander_ports.pr_merged_verdict([{"number": 7, "state": "closed"}, {"number": 9, "state": "merged"}]) == (True, "#9")
    assert lander_ports.pr_merged_verdict([{"number": 7, "state": "open"}]) == (False, "")
    assert lander_ports.pr_merged_verdict("could not list pull requests for init/x: offline") == (
        False, "could not list pull requests for init/x: offline",
    )


def test_the_ports_read_the_workspace(ws: Path, ports) -> None:
    store_work_state.set_state(ports.conn, "demo", "b", "approved", "tester", "p2", NOW)
    rows = {t["id"]: t for t in ports.tasks("demo")}
    assert (rows["a"]["state"], rows["a"]["phase"], rows["a"]["title"]) == ("approved", "p1", "Task A")
    assert (rows["b"]["state"], rows["b"]["needs"]) == ("approved", ["a"]), "the store's state wins over the file's"
    meta = ports.meta("demo")
    assert (meta["initiative"]["id"], meta["initiative"]["title"], meta["initiative"]["issue"]) == ("demo", "Demo title", "41")
    assert meta["gate"] == "ticket", "no gate file means a person merges"
    assert meta["cartridge"] == {} and meta["epic_issue"] is None
    assert ports.repo({"initiative": "demo", "repo": "/somewhere/else"}) == Path(json.loads(json.dumps(str(ws.parent / "repo"))))
    assert ports.repo({"initiative": "unknown", "repo": "/recorded/path"}) == Path("/recorded/path")


def test_the_landing_tables_exist_after_connecting(ports) -> None:
    from harness import landing_store

    assert landing_store.list_records(ports.conn) == [] and landing_store.list_items(ports.conn) == []


def test_notify_appends_one_courier_entry_for_the_chair(ws: Path, ports) -> None:
    ports.notify("blocked", {"initiative": "demo", "phase": "p2", "reason": "on_blocked is hold", "choices": ["split", "drop"]})
    (line,) = (ws / "courier.jsonl").read_text().splitlines()
    entry = json.loads(line)
    assert (entry["ref"], entry["to"], entry["from"], entry["event"]) == ("coxswain://initiative/demo", "chair", "lander-test", "needs_attention")
    assert entry["note"] == "lander: blocked: demo phase p2: on_blocked is hold"
    assert entry["fields"]["choices"] == '["split", "drop"]'


def test_mark_done_closes_an_approved_work_file(ws: Path, ports, capsys) -> None:
    assert ports.mark_done("demo", "a", "https://forge.example/pull/1") is None
    assert "state: done" in (ws / "work" / "demo" / "p1" / "a.md").read_text()


def test_fetch_makes_a_branch_local_and_refuses_one_that_diverged(tmp_path: Path) -> None:
    origin, a, b = tmp_path / "origin.git", tmp_path / "a", tmp_path / "b"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(a))
    (a / "f.txt").write_text("one\n")
    git(a, "add", "--all")
    git(a, "commit", "-qm", "base")
    git(a, "push", "-q", "origin", "HEAD:main")
    git(a, "branch", "init/demo")
    git(a, "push", "-q", "origin", "init/demo")
    git(tmp_path, "clone", "-q", str(origin), str(b))
    assert lander_ports.fetch_branch(b, "init/demo") is None
    assert git(b, "rev-parse", "init/demo") == git(a, "rev-parse", "init/demo")
    for clone, text in ((a, "from a\n"), (b, "from b\n")):
        git(clone, "checkout", "-q", "init/demo")
        (clone / "f.txt").write_text(text)
        git(clone, "commit", "-qam", f"change in {clone.name}")
    git(a, "push", "-q", "origin", "init/demo")
    kept = git(b, "rev-parse", "init/demo")
    refusal = lander_ports.fetch_branch(b, "init/demo")
    assert refusal is not None and refusal.startswith("init/demo has diverged from origin and is not moved here")
    assert git(b, "rev-parse", "init/demo") == kept
    assert lander_ports.fetch_branch(b, "no/such-branch") is None, "a branch that is nowhere is the caller's to judge"


def test_a_gate_runs_its_checks_in_the_tree_and_an_unresolved_cartridge_fails_it(tmp_path: Path) -> None:
    landing = {"level_checks": [{"name": "ok", "cmd": "true"}, {"name": "red", "cmd": "echo broken here; exit 3"}],
               "ticket_checks": [{"name": "ok", "cmd": "test -d ."}]}
    assert lander_ports.run_gate(tmp_path, tmp_path, "ticket", landing, None) == (True, "")
    passed, output = lander_ports.run_gate(tmp_path, tmp_path, "level", landing, None)
    assert passed is False and "red: echo broken here; exit 3" in output and "broken here" in output
    assert lander_ports.run_gate(tmp_path, tmp_path, "level", {}, None) == (True, "")
    assert lander_ports.run_gate(tmp_path, tmp_path, "level", landing, "ValueError: no such team") == (
        False, "cartridge checks unavailable (ValueError: no such team)",
    )


def test_a_phase_has_one_open_rectify_task_and_a_new_one_takes_the_first_free_id() -> None:
    def row(ident: str, state: str, phase: str = "p1") -> dict:
        return {"id": ident, "phase": phase, "state": state}

    assert lander_ports.rectify_task("p1", [row("a", "approved")]) == ("p1-rectify", False)
    assert lander_ports.rectify_task("p1", [row("a", "approved"), row("p1-rectify", "ready")]) == ("p1-rectify", True)
    assert lander_ports.rectify_task("p1", [row("p1-rectify", "approved")]) == ("p1-rectify-2", False)
    assert lander_ports.rectify_task("p1", [row("p1-rectify", "approved"), row("p1-rectify-2", "quarantined")]) == ("p1-rectify-2", True)
    assert lander_ports.rectify_task("p2", [row("p1-rectify", "ready")]) == ("p2-rectify", False)


def test_a_rectify_task_is_filed_once_as_a_ready_work_file_and_a_store_row(ws: Path, ports) -> None:
    from core import workstore

    ident = ports.file_rectify("demo", "p1", "Rectify phase p1: resolve its merge with init/demo", "Start from this tree.")
    assert ident == "p1-rectify"
    item = workstore.read_item(ws / "work" / "demo" / "p1" / "p1-rectify.md")
    assert (item["state"], item["title"], item["body"].strip(), item["needs"]) == (
        "ready", "Rectify phase p1: resolve its merge with init/demo", "Start from this tree.", [],
    )
    rows = {t["id"]: t for t in ports.tasks("demo")}
    assert (rows["p1-rectify"]["state"], rows["p1-rectify"]["phase"]) == ("ready", "p1")
    assert ports.file_rectify("demo", "p1", "again", "again") == "p1-rectify", "the open one is named, not doubled"
    assert sorted(p.name for p in (ws / "work" / "demo" / "p1").glob("*.md")) == ["a.md", "p1-rectify.md"]
    assert ports.file_rectify("demo", "integrate", "Integrate with main", "body") == "integrate-rectify"
    assert (ws / "work" / "demo" / "integrate" / "integrate-rectify.md").is_file()


def test_reading_the_records_for_the_chair_leaves_no_trace_and_never_raises(tmp_path: Path, ws: Path, ports) -> None:
    from harness import landing_store

    empty = tmp_path / "elsewhere" / "runs"
    empty.mkdir(parents=True)
    assert lander_ports.read_records(empty) == []
    assert list(empty.iterdir()) == [], "no store file is made by looking"
    landing_store.put_record(ports.conn, {"initiative": "demo", "repo": "r", "branch": "init/demo", "parent": "main",
                                          "state": "building", "phases": {"p1": {"state": "merged"}}, "updated_at": NOW})
    (read,) = lander_ports.read_records(ws / "runs")
    assert (read["initiative"], read["phases"]["p1"]["state"]) == ("demo", "merged")
    notes: list[str] = []
    (empty / "cox.db").write_bytes(b"not a database")
    assert lander_ports.read_records(empty, notes.append) == []
    assert len(notes) == 1 and notes[0].startswith("note: landing records not read (")


def test_send_back_moves_an_approved_work_file_to_ready_and_lists_the_workspaces_initiatives(ws: Path, ports) -> None:
    move = lander_ports.send_back(ws, ports.conn, "lander-test", "files")
    assert move("demo", "a", "commit 000000000000 is not in this clone") is None
    assert "state: ready" in (ws / "work" / "demo" / "p1" / "a.md").read_text()
    assert move("demo", "b", "already ready") is None, "a task that is not approved is left as it is"
    assert "state: ready" in (ws / "work" / "demo" / "p2" / "b.md").read_text()
    assert lander_ports.initiatives_in(ws) == ["demo"]
