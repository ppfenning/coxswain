"""A decompose that ends on a ticket-lint refusal stores the first problem line as the run's outcome."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools.queue_rows import parse_item
from graphs._contract import ContractViolation
from graphs.delivery import initiative_decompose
from graphs.delivery.initiative_decompose import GRAPH_NAME, LintRefusal, first_problem_line
from harness import cli
from harness.store_dialect import json_load
from harness.store_queue import read as read_queue
from harness.store_queue import upsert as upsert_queue
from harness.store_write import Store
from runner import ScriptedRunner

RUN = {"run_id": "r1", "ts": "2026-10-06T00:00:00Z", "principal": "initiative-decompose"}
REACH = {
    "phases": [{"id": "p1", "goal": "g"}],
    "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["~/scratch/notes.md"]}],
    "rationale": "r",
}
OK = {
    "phases": [{"id": "p1", "goal": "g"}],
    "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["graphs/notes.py"]}],
    "rationale": "r",
}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["decompose"] = "acme-skills:decompose"
    cartridge["work_routing"] = {"states": {"active": "work", "planned": "work", "future": "backlog"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "deferred"}
    cartridge["write_kinds"]["state_move"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


@pytest.fixture
def store(store_conn):
    s = Store(store_conn)
    s.record_run(RUN)
    return s


def stored(store: Store) -> dict:
    mark = store.conn.dialect.placeholder
    return json_load(store.conn.query_one(f"SELECT record_json FROM runs WHERE run_id = {mark}", ("r1",))[0])


def exit_code(store: Store | None, run, monkeypatch) -> int:
    """Drive the real `_run_graph` arm that every non-epic, non-cos graph shares, with `run` as the graph."""
    monkeypatch.setattr(cli, "_materialise", lambda spec, args, parser: {})
    spec = SimpleNamespace(graph_name=GRAPH_NAME, run=lambda graph_args, runner: run())
    args = SimpleNamespace(graph=GRAPH_NAME, date="2026-10-06")
    return cli._run_graph(
        specs={GRAPH_NAME: spec}, parser=None, args=args, cartridge={}, runner=None, run_id="r1", store=store
    )


def refuse(text: str):
    def run():
        raise LintRefusal(text)

    return run


def test_first_problem_line_is_the_first_line_with_text_stripped() -> None:
    assert first_problem_line(["  t1: reach — a (fix a)  ", "t2: coupling — b (fix b)"]) == "t1: reach — a (fix a)"
    assert first_problem_line(["", "  ", " t2: x "]) == "t2: x"
    assert first_problem_line([]) is None
    assert first_problem_line(["", "   "]) is None


def test_a_refusal_with_several_problem_lines_stores_only_the_first(store, monkeypatch) -> None:
    text = "\n  t1: reach — one (fix one)  \nt2: coupling — two (fix two)\nt3: reach — three (fix three)"
    assert exit_code(store, refuse(text), monkeypatch) == 1
    assert stored(store) == {**RUN, "outcome": {"refused": "t1: reach — one (fix one)"}}


def test_the_exit_code_is_unchanged_when_the_store_write_raises(store, monkeypatch, capsys) -> None:
    def boom(self, run_id, outcome):
        raise RuntimeError("store down")

    monkeypatch.setattr(Store, "record_outcome", boom)
    assert exit_code(store, refuse("t1: reach — one (fix one)"), monkeypatch) == 1
    err = capsys.readouterr().err
    assert "store down" in err and f"{GRAPH_NAME} failed: t1: reach" in err


def test_a_run_with_no_store_still_exits_non_zero(monkeypatch) -> None:
    assert exit_code(None, refuse("t1: reach — one (fix one)"), monkeypatch) == 1


def test_a_refusal_that_is_not_lint_stores_no_outcome(store, monkeypatch) -> None:
    def run():
        raise ContractViolation("decompose returned no tasks")

    assert exit_code(store, run, monkeypatch) == 1
    assert "outcome" not in stored(store)


def test_a_successful_decompose_stores_no_refused(cart, store) -> None:
    result = initiative_decompose.run(
        {"run_id": "r1", "date": "d", "cartridge": cart, "idea": "x"}, ScriptedRunner({"decompose": OK})
    )
    assert result["tasks"][0]["id"] == "t1"
    assert "outcome" not in stored(store)


def test_the_real_lint_step_refuses_a_reach_problem_and_the_run_gains_refused(cart, store, monkeypatch) -> None:
    """No stub of `lint_tickets`: the decompose graph's own lint step raises, and the cli handler stores its first line."""
    runner = ScriptedRunner({"decompose": REACH})
    args = {"run_id": "r1", "date": "d", "cartridge": cart, "idea": "x"}
    with pytest.raises(LintRefusal) as caught:
        initiative_decompose.run(args, runner)
    first = str(caught.value).splitlines()[0].strip()
    assert first.startswith("t1: reach")

    assert (
        exit_code(store, lambda: initiative_decompose.run(args, ScriptedRunner({"decompose": REACH})), monkeypatch) == 1
    )
    assert stored(store) == {**RUN, "outcome": {"refused": first}}


NOW = "2026-10-30T00:00:00Z"


def filing(store):
    """Stands in for `route file`'s writer on the test's own connection: same codec, `parse_item`, same row."""

    def write(runs_dir, path, parts, text) -> int:
        upsert_queue(store.conn, parse_item("task", parts, text))
        return 0

    return write


def settle(expected, root, store, write=None):
    return cli._settle_decompose_tasks(
        expected,
        root=root,
        runs_dir=root / "runs",
        initiative="demo",
        write=write or filing(store),
        read=lambda runs_dir, initiative: read_queue(store.conn, initiative),
    )


def task_file(root, rel: str, task: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {task}\nphase: p1\nstate: ready\nneeds: []\nsurfaces: [a.py]\ntitle: Task {task}\n---\n\nbody {task}\n",
        encoding="utf-8",
    )


def approved(*tasks: str) -> tuple[list[dict], list[dict]]:
    proposals = [{"kind": "item_create", "target": t, "evidence": [{"check": "phase", "output": "p1"}]} for t in tasks]
    proposals.append({"kind": "item_create", "target": "initiative", "evidence": []})
    diffs = [{"kind": "item_create", "target": t, "decision": "approved"} for t in (*tasks, "initiative")]
    return proposals, diffs


def expected_for(*tasks: str) -> dict[str, str]:
    proposals, diffs = approved(*tasks)
    ids = initiative_decompose.approved_task_ids(proposals, diffs, [])
    return initiative_decompose.expected_task_files(proposals, ids, landing="work", initiative_id="demo")


def test_expected_task_files_names_each_approved_task_path_and_skips_the_initiative() -> None:
    assert expected_for("t1", "t2") == {"t1": "work/demo/p1/t1.md", "t2": "work/demo/p1/t2.md"}
    proposals, _ = approved("t1")
    assert initiative_decompose.expected_task_files(proposals, [], landing="work", initiative_id=None) == {}


def test_a_task_is_cleared_when_the_gate_approved_it_or_the_policy_applied_it() -> None:
    proposals, _ = approved("t1", "t2", "t3")
    gated = [{"kind": "item_create", "target": "t1", "decision": "approved"}]
    rejected = [{"kind": "item_create", "target": "t3", "decision": "rejected"}]
    auto = [proposals[1], proposals[3]]  # t2, and the initiative's own create
    ids = initiative_decompose.approved_task_ids
    assert ids(proposals, gated + rejected, auto) == {"t1", "t2"}
    assert ids(proposals, [], []) == set()


def test_the_closing_line_counts_a_task_written_only_when_it_is_in_both_places() -> None:
    close = initiative_decompose.decompose_closing
    assert close(["a", "b"], {"a", "b"}, {"a", "b"}) == ("decompose: 2 task(s) written, 0 missing:", 0)
    assert close(["a", "b", "c"], {"a", "b"}, {"a", "c"}) == ("decompose: 1 task(s) written, 2 missing: b, c", 2)
    assert close([], set(), set()) == ("decompose: 0 task(s) written, 0 missing:", 0)


def test_three_approved_creates_leave_three_store_rows_and_three_files(store, tmp_path, capsys) -> None:
    expected = expected_for("t1", "t2", "t3")
    for task, rel in expected.items():
        task_file(tmp_path, rel, task)
    in_store, on_disk = settle(expected, tmp_path, store)
    line = initiative_decompose.decompose_closing(expected, in_store, on_disk)
    assert line == ("decompose: 3 task(s) written, 0 missing:", 0)
    assert [r["task_id"] for r in read_queue(store.conn, "demo")] == ["t1", "t2", "t3"]
    assert len(list(tmp_path.glob("work/demo/p1/*.md"))) == 3
    assert capsys.readouterr().err == ""


def test_a_create_whose_file_was_never_written_is_named_with_its_reason(store, tmp_path, capsys) -> None:
    expected = expected_for("t1", "t2", "t3")
    for task in ("t1", "t3"):
        task_file(tmp_path, expected[task], task)
    in_store, on_disk = settle(expected, tmp_path, store)
    line = initiative_decompose.decompose_closing(expected, in_store, on_disk)
    assert line == ("decompose: 2 task(s) written, 1 missing: t2", 1)
    assert "decompose: t2 not written:" in capsys.readouterr().err


def test_a_create_whose_store_write_fails_is_named_and_its_file_stays(store, tmp_path, monkeypatch, capsys) -> None:
    expected = expected_for("t1", "t2")
    for task, rel in expected.items():
        task_file(tmp_path, rel, task)
    good = filing(store)

    def flaky(runs_dir, path, parts, text) -> int:
        if path.stem == "t2":
            raise RuntimeError("store down")
        return good(runs_dir, path, parts, text)

    in_store, on_disk = settle(expected, tmp_path, store, flaky)
    line = initiative_decompose.decompose_closing(expected, in_store, on_disk)
    assert line == ("decompose: 1 task(s) written, 1 missing: t2", 1)
    assert on_disk == {"t1", "t2"}
    assert "decompose: t2 is on disk but the write failed: RuntimeError: store down" in capsys.readouterr().err


def test_a_task_file_with_extra_frontmatter_reaches_the_store_with_it(store, tmp_path) -> None:
    expected = expected_for("t1")
    task_file(tmp_path, expected["t1"], "t1")
    path = tmp_path / expected["t1"]
    path.write_text(path.read_text(encoding="utf-8").replace("title:", "budget_usd: 1.5\ntitle:"), encoding="utf-8")
    settle(expected, tmp_path, store)
    assert read_queue(store.conn, "demo")[0]["extra"]["budget_usd"] in (1.5, "1.5")


def test_the_real_route_file_writer_and_reader_agree_on_a_file_backed_store(tmp_path, monkeypatch, capsys) -> None:
    """No stand-ins: `route.write_filed_item` and `run_store.read_queue` run their harness subprocess against one sqlite file."""
    from agent_tools import run_store
    from harness.store_dialect import default_url
    from harness.store_migrate import open_store

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    url = default_url(runs_dir)
    open_store(url, NOW).close()
    graphs_root = Path(cli.__file__).resolve().parent.parent
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(graphs_root), str(graphs_root.parent / "cartridges")]))
    monkeypatch.setattr(run_store, "_harness_python", lambda: Path(sys.executable))
    monkeypatch.setattr(run_store, "_store_url", lambda _runs_dir: url)
    expected = expected_for("t1", "t2", "t3")
    for task, rel in expected.items():
        task_file(tmp_path, rel, task)
    in_store, on_disk = cli._settle_decompose_tasks(expected, root=tmp_path, runs_dir=runs_dir, initiative="demo")
    assert (in_store, on_disk) == ({"t1", "t2", "t3"}, {"t1", "t2", "t3"})
    assert sorted(r["task_id"] for r in run_store.read_queue(runs_dir, "demo")) == ["t1", "t2", "t3"]
    assert capsys.readouterr().err == ""


def test_a_task_already_in_the_store_keeps_its_row_on_a_second_decompose(store, tmp_path, capsys) -> None:
    expected = expected_for("t1")
    task_file(tmp_path, expected["t1"], "t1")
    upsert_queue(store.conn, {**parse_item("task", ("demo", "p1", "t1.md"), (tmp_path / expected["t1"]).read_text()), "state": "running"})
    in_store, on_disk = settle(expected, tmp_path, store)
    assert (in_store, on_disk) == ({"t1"}, {"t1"})
    assert read_queue(store.conn, "demo")[0]["state"] == "running"
    assert "already in the store as 'running'" in capsys.readouterr().err
