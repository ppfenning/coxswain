"""Under work_state store the epic's attempt cap and its resume from the last attempt read the runs' task records (H4.3).

Nothing writes an attempt into a work file there, so a task the epic quarantined twice reached the next run with no
attempt: it was built a third time, and a rebuild started from nothing. These runs are real `run_epic` runs on a real
repository and one throwaway sqlite store: the quarantines and their task records are the epic's own writes, and only
the row's move back to `ready` between runs is done here, through the store's own state writer, as the chair moves it.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime

from core.workstore import HOLD_REASON
from harness import epic, epic_execute, rescue_select, resume, store_queue, store_work_state
from harness.resume import result_path
from harness.store_write import Store
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    REVISE,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)
from tests.test_epic_triage_classes import (
    INITIATIVE,
    TRIAGE_SPECS,
    TriageClassRunner,
    _answer,
    _built,
    _row,
    _store,
    _triage_calls,
)

PHASE = "p1-foundations"


def _revised(patch: str) -> TriageClassRunner:
    """Builds t1-probe with `patch`, and its reviewer says revise every time: the task is quarantined, its patch kept."""
    runner = TriageClassRunner({"t1-probe": patch}, _answer("hold"))
    runner.review = {"t1-probe": REVISE}
    return runner


def _work() -> dict:
    one = initiative(two_phases=False)
    return {**one, "items": [i for i in one["items"] if i["id"] == "t1-probe"]}


def _ready_again(store: Store) -> None:
    moved = store_work_state.set_state(
        store.conn, INITIATIVE, "t1-probe", "ready", "chair", PHASE, datetime.now(UTC).isoformat(), expected="quarantined"
    )
    assert not isinstance(moved, store_work_state.Mismatch), moved


def _run(repo, cart, tmp_path, store, n, runner):  # noqa: F811
    result, _ = drive(
        repo, cart, tmp_path, runner=runner, work=_work(), store=store, work_state="store", specs=TRIAGE_SPECS,
        run_id=f"{INITIATIVE}-{n}",
    )
    return result


def test_a_store_mode_task_quarantined_twice_is_triaged_not_built_a_third_time(repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store(_work(), flagged=())
    for n in (1, 2):
        result = _run(repo, cart, tmp_path, store, n, _revised(new_file_patch("t1-probe.txt")))
        assert [q["id"] for q in result["quarantined"]] == ["t1-probe"], result["quarantined"]
        assert _row(store, "t1-probe")["state"] == "quarantined"
        _ready_again(store)

    third = TriageClassRunner({"t1-probe": new_file_patch("t1-probe.txt")}, _answer("hold"))
    _run(repo, cart, tmp_path, store, 3, third)
    assert not _built(third, "t1-probe")
    (call,) = _triage_calls(third)
    assert f"{INITIATIVE}-1" in call["prompt"] and f"{INITIATIVE}-2" in call["prompt"]
    assert _row(store, "t1-probe")["state"] == "blocked"


def test_a_store_mode_rebuild_resumes_from_the_last_attempt_kept_only_in_the_store(repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store(_work(), flagged=())
    first = _revised(new_file_patch("t1-probe.txt"))
    assert [q["id"] for q in _run(repo, cart, tmp_path, store, 1, first)["quarantined"]] == ["t1-probe"]
    _ready_again(store)
    # Another lane ran it: this machine has no file of that run, only the store's copy of its task record.
    shutil.rmtree(tmp_path / "runs" / f"{INITIATIVE}-1")

    second = TriageClassRunner({"t1-probe": new_file_patch("t1-probe.txt")}, _answer("hold"))
    _run(repo, cart, tmp_path, store, 2, second)
    (build,) = _built(second, "t1-probe")
    assert "Your previous attempt is in the tree you are working in" in build["prompt"]
    assert f"{INITIATIVE}-1" in build["prompt"]


def test_with_record_attempts_fills_only_an_item_with_none_of_its_own() -> None:
    recorded = [{"run": "r-1", "reason": "x"}]
    assert epic.with_record_attempts({"id": "t"}, recorded) == {"id": "t", "attempts": recorded}
    own = {"id": "t", "attempts": [{"run": "r-0"}]}
    assert epic.with_record_attempts(own, recorded) is own
    bare = {"id": "t"}
    assert epic.with_record_attempts(bare, []) is bare


def test_a_rebuild_after_a_fresh_start_mark_does_not_resume_or_cite_the_attempt_before_it(repo, cart, tmp_path) -> None:  # noqa: F811
    """Regression (review of #1198): the cap left out records at or before `fresh_after_run`, but the resume did not, so a
    task a person readied by hand was rebuilt from the attempt the person had just set aside."""
    store = _store(_work(), flagged=())
    _run(repo, cart, tmp_path, store, 1, _revised(new_file_patch("t1-probe.txt")))
    _ready_again(store)
    row = _row(store, "t1-probe")
    store_queue.upsert(store.conn, {**row, "extra": {**row["extra"], "fresh_after_run": {INITIATIVE: 1}}})

    second = TriageClassRunner({"t1-probe": new_file_patch("t1-probe.txt")}, _answer("hold"))
    _run(repo, cart, tmp_path, store, 2, second)
    (build,) = _built(second, "t1-probe")
    assert "Your previous attempt is in the tree you are working in" not in build["prompt"]
    assert f"{INITIATIVE}-1" not in build["prompt"]


def test_records_after_keeps_only_the_runs_after_the_mark() -> None:
    found = [({"run": "demo-1"}, "r1"), ({"run": "demo-2"}, "r2"), ({"run": "demo.p1-1"}, "r3")]
    assert epic.records_after(found, {}) == found
    assert epic.records_after(found, {"demo": 1}) == [({"run": "demo-2"}, "r2"), ({"run": "demo.p1-1"}, "r3")]
    assert epic.records_after(found, {"demo": 2, "demo.p1": 1}) == []


def _malform(tmp_path, store: Store, n: int) -> None:
    """Run n's task record for t1-probe, in its file and in the store, gets a verdict that is not an object, as a
    `review: "skipped"` record does: the record `task_record` refuses. Its cause stays."""
    run = f"{INITIATIVE}-{n}"
    path = result_path(tmp_path / "runs", run, PHASE, "t1-probe")
    record = {**json.loads(path.read_text(encoding="utf-8")), "review": "skipped"}
    assert record.get("cause"), record  # the quarantine put its cause on the record
    path.write_text(json.dumps(record), encoding="utf-8")
    store.record_task_record(run, PHASE, "t1-probe", record, datetime.now(UTC).isoformat())


def test_a_malformed_earlier_task_record_still_counts_toward_the_attempt_cap(repo, cart, tmp_path) -> None:  # noqa: F811
    """Review of #1222: a refused record was dropped, so the cap counted 1 of 2 runs and the task built a third time."""
    store = _store(_work(), flagged=())
    for n in (1, 2):
        result = _run(repo, cart, tmp_path, store, n, _revised(new_file_patch("t1-probe.txt")))
        assert [q["id"] for q in result["quarantined"]] == ["t1-probe"], result["quarantined"]
        _ready_again(store)
    _malform(tmp_path, store, 1)

    third = TriageClassRunner({"t1-probe": new_file_patch("t1-probe.txt")}, _answer("hold"))
    _run(repo, cart, tmp_path, store, 3, third)
    assert not _built(third, "t1-probe")
    (call,) = _triage_calls(third)
    assert f"{INITIATIVE}-1" in call["prompt"] and f"{INITIATIVE}-2" in call["prompt"]


def test_a_malformed_newest_task_record_holds_the_task_rather_than_resume_from_an_older_patch(repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store(_work(), flagged=())
    for n in (1, 2):
        _run(repo, cart, tmp_path, store, n, _revised(new_file_patch("t1-probe.txt")))
        _ready_again(store)
    _malform(tmp_path, store, 2)

    third = TriageClassRunner({"t1-probe": new_file_patch("t1-probe.txt")}, _answer("hold"))
    _run(repo, cart, tmp_path, store, 3, third)
    assert not _built(third, "t1-probe") and not _triage_calls(third)
    row = _row(store, "t1-probe")
    assert row["state"] == "blocked"
    assert f"newest task record (run {INITIATIVE}-2) is malformed" in row["extra"][HOLD_REASON]


def test_a_malformed_record_with_no_cause_holds_the_task_and_a_readable_history_does_not() -> None:
    read = ({"run": "demo-2", "cause": "review"}, {"cause": "review"})
    causeless = ({"run": "demo-1", "malformed": "task record: a key is not a string"}, {})
    with_cause = ({"run": "demo-1", "malformed": "x", "cause": "code"}, {})
    assert epic.unreadable_reason([read]) is None and epic.unreadable_reason([with_cause, read]) is None
    assert "run demo-1 is malformed" in str(epic.unreadable_reason([causeless, read]))


def test_a_malformed_rescue_failed_row_is_kept_so_a_rescue_never_repeats(tmp_path) -> None:
    """Review of #1222: a dropped `rescue_failed` row let `rescue_select.eligible` pass again."""
    store = _store(_work(), flagged=())
    store.conn.execute(
        "INSERT INTO attempts (run_id, task_id, seq, phase_id, kind, reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"{INITIATIVE}-1", "t1-probe", 1, PHASE, "rescue_failed", "checks failed", b"\x00"),  # a ts no reader can parse
    )
    ctx = epic.Ctx(
        repo=tmp_path, cartridge={}, runner=None, specs={}, run_id=f"{INITIATIVE}-2", date="2026-11-18", max_parallel=1,
        ledger_path=tmp_path / "ledger", provider_profile="p", runs_dir=tmp_path / "runs", worktree_root=tmp_path / "wt",
        assume=None, fix_attempts=None, initiative_id=INITIATIVE, default_ref="main", store=store,
    )
    (row,) = epic_execute._stored_rescues(ctx, "t1-probe")
    assert (row["run_id"], row["kind"], row["cause"]) == (f"{INITIATIVE}-1", "rescue_failed", None)
    body = _work()["items"][0]["body"]
    harness_attempt = {"run": f"{INITIATIVE}-0", "phase": PHASE, "reason": "lost", "ts": "2026-11-17", "cause": "harness"}
    alone = {"body": body, "attempts": [harness_attempt]}
    assert rescue_select.eligible(epic.with_stored_rescues(alone, []), "diff")[0] is True  # without the row: a rescue
    eligible, why = rescue_select.eligible(epic.with_stored_rescues(alone, [row]), "diff")
    assert not eligible, why


def _records_ctx(tmp_path, store: Store) -> epic.Ctx:
    return epic.Ctx(
        repo=tmp_path, cartridge={}, runner=None, specs={}, run_id=f"{INITIATIVE}-3", date="2026-11-18", max_parallel=1,
        ledger_path=tmp_path / "ledger", provider_profile="p", runs_dir=tmp_path / "runs", worktree_root=tmp_path / "wt",
        assume=None, fix_attempts=None, initiative_id=INITIATIVE, default_ref="main", store=store,
    )


RECORD = {"initiative": INITIATIVE, "cause": "review", "cause_why": "the reviewer said revise", "review": {"verdict": "revise"}}


def test_a_store_record_that_is_not_an_object_is_still_the_newest_attempt_and_holds_the_task(tmp_path) -> None:
    """Review of #1243: a `task_records` row whose `record_json` is not an object was skipped, as a refused one was."""
    store = _store(_work(), flagged=())
    now = datetime.now(UTC).isoformat()
    store.record_task_record(f"{INITIATIVE}-1", PHASE, "t1-probe", RECORD, now)
    store.record_task_record(f"{INITIATIVE}-2", PHASE, "t1-probe", RECORD, now)
    store.conn.execute(
        "UPDATE task_records SET record_json = ? WHERE run_id = ?", ('"skipped"', f"{INITIATIVE}-2")
    )  # another lane's run 2, held only in the store, with a record that is a bare string
    found = epic.caused_records(_records_ctx(tmp_path, store), PHASE, "t1-probe")
    assert [(a["run"], "malformed" in a) for a, _ in found] == [(f"{INITIATIVE}-1", False), (f"{INITIATIVE}-2", True)]
    assert f"newest task record (run {INITIATIVE}-2) is malformed" in str(epic.unreadable_reason(found))


def test_a_record_file_that_cannot_be_read_is_an_attempt_unless_the_store_holds_a_readable_copy(tmp_path) -> None:
    store = _store(_work(), flagged=())
    unreadable = result_path(tmp_path / "runs", f"{INITIATIVE}-1", PHASE, "t1-probe")
    unreadable.mkdir(parents=True)  # a path read_text cannot read, as root or not: an OSError
    ctx = _records_ctx(tmp_path, store)
    ((attempt, record),) = epic.caused_records(ctx, PHASE, "t1-probe")
    assert attempt["run"] == f"{INITIATIVE}-1" and "could not be read" in attempt["malformed"] and record == {}
    assert epic.unreadable_reason([(attempt, record)]) is not None

    store.record_task_record(f"{INITIATIVE}-1", PHASE, "t1-probe", RECORD, datetime.now(UTC).isoformat())
    ((attempt, record),) = epic.caused_records(ctx, PHASE, "t1-probe")
    assert "malformed" not in attempt and attempt["cause"] == "review" and record["review"] == {"verdict": "revise"}


def test_a_truncated_record_file_beside_an_older_copy_with_no_cause_holds_the_task(tmp_path) -> None:
    """Review of #1252: a crash while `record_task_cause` re-saved the file with its cause left it truncated, and the
    store's earlier mirror, with no cause, stood in for it: the attempt vanished and nothing held the task."""
    store = _store(_work(), flagged=())
    run = f"{INITIATIVE}-1"
    earlier = "2026-11-18T09:00:00+00:00"
    store.record_task_record(run, PHASE, "t1-probe", {"initiative": INITIATIVE, "review": {"verdict": "revise"}}, earlier)
    path = result_path(tmp_path / "runs", run, PHASE, "t1-probe")
    path.parent.mkdir(parents=True)
    path.write_text('{"initiative": "demo-initiative", "cause": "rev', encoding="utf-8")  # cut off mid-write
    ctx = _records_ctx(tmp_path, store)
    ((attempt, record),) = epic.caused_records(ctx, PHASE, "t1-probe")
    assert attempt["run"] == run and "not JSON" in attempt["malformed"] and record == {}
    assert f"newest task record (run {run}) is malformed" in str(epic.unreadable_reason([(attempt, record)]))

    # A copy that names a cause but is older than the file does not stand in either: it may predate what the file held.
    store.record_task_record(run, PHASE, "t1-probe", RECORD, earlier)
    ((attempt, _),) = epic.caused_records(ctx, PHASE, "t1-probe")
    assert "malformed" in attempt


def test_a_staged_write_that_is_never_committed_leaves_the_record_file_as_it_was(tmp_path) -> None:
    """An atomic save is a staged file put in place by one `os.replace`; a crash between the two is this test's gap."""
    path = result_path(tmp_path / "runs", f"{INITIATIVE}-1", PHASE, "t1-probe")
    saved = resume.save_result(RECORD, runs_dir=tmp_path / "runs", run_id=f"{INITIATIVE}-1", phase=PHASE, task="t1-probe")
    assert saved == path and json.loads(path.read_text(encoding="utf-8")) == RECORD
    assert sorted(p.name for p in path.parent.iterdir()) == ["t1-probe.json"]  # nothing staged is left behind

    staged = resume.stage_text(path, '{"cause": "co')  # the process dies here, before the replace
    assert json.loads(path.read_text(encoding="utf-8")) == RECORD
    assert sorted(path.parent.glob("t1-probe.json")) == [path]  # the reader's glob never sees the staged file
    resume.commit_staged(staged, path)
    assert path.read_text(encoding="utf-8") == '{"cause": "co'

