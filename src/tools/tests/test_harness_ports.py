"""`harness_ports.withdraw_tasks`: a file goes only after its store row is dropped, and a whole withdrawal declines
the initiative through `draft_apply`, falling back to row by row when that is refused.

Debt (#1341): these monkeypatch `run_store`, `store_cli` and `draft_apply`, as they did in graphs' `test_cli.py`.
They should run against a real store (the `store_conn` fixture) instead.
"""

from __future__ import annotations

from agent_tools import harness_ports


def test_the_default_withdrawal_keeps_the_file_when_the_store_does_not_drop_the_row(
    tmp_path, monkeypatch, capsys
) -> None:
    from agent_tools import run_store, store_cli

    rel = "work/demo/p1/t1.md"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("x", encoding="utf-8")
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None: [{"task_id": "t1"}])
    monkeypatch.setattr(store_cli, "set_state", lambda *a, **k: store_cli.Failed(1, "down"))

    assert harness_ports.withdraw_tasks(tmp_path / "runs", tmp_path, "demo", [rel], False) == []

    assert (tmp_path / rel).exists()
    assert "decompose: t1 not withdrawn" in capsys.readouterr().err


def test_the_whole_withdrawal_declines_the_initiative_through_draft_apply_and_deletes_the_files(
    tmp_path, monkeypatch
) -> None:
    from agent_tools import draft_apply, run_store

    rels = ["work/demo/p1/t1.md", "work/demo/p1/t2.md"]
    (tmp_path / "work" / "demo" / "p1").mkdir(parents=True)
    for rel in rels:
        (tmp_path / rel).write_text("x", encoding="utf-8")
    seen: list[tuple] = []
    monkeypatch.setattr(
        draft_apply, "remove", lambda work_dir, initiative, *rest: seen.append((work_dir, initiative)) or 0
    )
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None: [])

    assert harness_ports.withdraw_tasks(tmp_path / "runs", tmp_path, "demo", rels, True) == rels

    assert seen == [(tmp_path / "work", "demo")]
    assert not any((tmp_path / rel).exists() for rel in rels)


def test_a_refused_whole_withdrawal_falls_back_to_dropping_each_row(tmp_path, monkeypatch) -> None:
    from agent_tools import draft_apply, run_store, store_cli

    rel = "work/demo/p1/t1.md"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("x", encoding="utf-8")
    dropped: list[str] = []
    monkeypatch.setattr(draft_apply, "remove", lambda *a, **k: 2)
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None: [{"task_id": "t1"}])
    monkeypatch.setattr(
        store_cli, "set_state", lambda runs_dir, init, task, *a: dropped.append(task) or store_cli.StateSet({})
    )

    assert harness_ports.withdraw_tasks(tmp_path / "runs", tmp_path, "demo", [rel], True) == [rel]

    assert dropped == ["t1"]
    assert not (tmp_path / rel).exists()


def test_the_default_withdrawal_drops_the_row_then_deletes_the_file(tmp_path, monkeypatch) -> None:
    from agent_tools import run_store, store_cli

    rel = "work/demo/p1/t1.md"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("x", encoding="utf-8")
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None: [{"task_id": "t1"}])
    monkeypatch.setattr(store_cli, "set_state", lambda *a, **k: store_cli.StateSet({}))

    assert harness_ports.withdraw_tasks(tmp_path / "runs", tmp_path, "demo", [rel], False) == [rel]

    assert not (tmp_path / rel).exists()
