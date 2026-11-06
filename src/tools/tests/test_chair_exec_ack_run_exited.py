"""A fetch_exit that lands a run acks the bus entries announcing that run, and nothing else."""

from pathlib import Path

import pytest

from agent_tools import chair_exec, courier
from agent_tools.chair_exec import Deps, perform, run_exited_ids

REF = courier.Reference("task", "t1")


def _entry(message_id: str, run: str, **over: object) -> dict:
    entry = courier.send(
        REF, "lane", "chair", "run exited", message_id,
        event="run_exited", fields=courier.run_exited_fields(run, "alpha", "done"),
    )
    return {**entry, **over}


def _bus(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "courier.jsonl"
    path.write_text("".join(courier.append_line("", e) for e in entries), encoding="utf-8")
    return path


def _acked(path: Path) -> dict[str, bool]:
    return {e["id"]: bool(e["ack"]) for e in courier.entries(path.read_text(encoding="utf-8"))}


def _fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int = 0) -> dict:
    monkeypatch.setattr(chair_exec.chair_apply_fetch, "apply_fetched_approvals", lambda *a: (["t1"], []))
    deps = Deps(
        run=lambda argv: (code, "boom" if code else ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "",
        repo_for=lambda action: "",
        runs_dir=tmp_path,
        work_dir=tmp_path,
    )
    return perform([{"kind": "fetch_exit", "run": "r-1", "initiative": "alpha", "epoch": 1}], deps, lambda: 1, False)[0]


def test_a_successful_fetch_acks_only_that_runs_entries(tmp_path, monkeypatch) -> None:
    path = _bus(tmp_path, _entry("a", "r-1"), _entry("b", "r-2"), _entry("c", "r-1"))
    result = _fetch(tmp_path, monkeypatch)
    assert (result["status"], result["reason"]) == ("done", "fetched r-1; approved: t1")
    assert _acked(path) == {"a": True, "b": False, "c": True}


def test_a_failed_fetch_acks_nothing(tmp_path, monkeypatch) -> None:
    path = _bus(tmp_path, _entry("a", "r-1"))
    assert _fetch(tmp_path, monkeypatch, code=1)["status"] == "failed"
    assert _acked(path) == {"a": False}


def test_a_missing_bus_file_is_harmless(tmp_path, monkeypatch) -> None:
    result = _fetch(tmp_path, monkeypatch)
    assert (result["status"], result["reason"]) == ("done", "fetched r-1; approved: t1")
    assert not (tmp_path / "courier.jsonl").exists()


def test_an_ack_failure_leaves_the_result_done(tmp_path, monkeypatch) -> None:
    path = _bus(tmp_path, _entry("a", "r-1"))

    def refuse(*args: object) -> bool:
        raise OSError("read-only")

    monkeypatch.setattr(chair_exec.courier, "ack_file", refuse)
    result = _fetch(tmp_path, monkeypatch)
    assert (result["status"], result["reason"]) == ("done", "fetched r-1; approved: t1")
    assert _acked(path) == {"a": False}


def test_run_exited_ids_skips_acked_and_other_events() -> None:
    entries = [
        _entry("a", "r-1"),
        _entry("b", "r-1", ack=True),
        courier.send(REF, "lane", "chair", "note", "c"),
        _entry("d", "r-2"),
    ]
    assert run_exited_ids(entries, "r-1") == ["a"]
