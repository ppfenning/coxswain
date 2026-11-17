"""A task a person readied by hand, or whose initiative a person released, is built fresh by the next run.

The person fixed what failed it, so the attempts and the chair's triage request from the runs before that point are
history, not a reason. `route edit --state ready` marks the task row and `route release` the initiative row with the
newest run of the initiative then (`FRESH_AFTER_RUN`); a run after it counts no attempt and honours no triage flag
from a run at or before it.
"""

from __future__ import annotations

from core import workstore
from core.workstore import FRESH_AFTER_RUN, TRIAGE_FLAG
from harness import store_queue
from tests.test_epic_attempt_cap import TRIAGE_SPECS, _amend_cart, _store_with_two_recorded_attempts
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    TriageAttemptRunner,
    builds_per_task,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)
from tests.test_epic_triage_classes import INITIATIVE, TriageClassRunner, _answer, _built, _row, _store, _triage_calls
from tests.test_epic_triage_records import RECORD, _write

PHASE = "p1-foundations"


def _mark(store, task: str, **extra: object) -> None:
    row = _row(store, task)
    store_queue.upsert(store.conn, {**row, "extra": {**row["extra"], **extra}})


def _initiative_row(store, **extra: object) -> None:
    store_queue.upsert(store.conn, {
        "initiative": INITIATIVE, "task_id": INITIATIVE, "kind": "initiative", "phase": "", "state": "todo", "needs": [],
        "title": "demo", "surfaces": [], "body": "make the join measurable", "extra": {"id": INITIATIVE, **extra},
    })


def _drive_flagged(repo, cart, tmp_path, flag_run: str, *, task_mark=None, initiative_mark=None):  # noqa: F811
    kinds = {"ticket_amend": {"risk": "low", "ramp": "eligible"}, "item_create": {"risk": "medium", "ramp": "gated"}}
    cart = {**cart, "write_kinds": {**cart["write_kinds"], **kinds}}
    work = initiative()
    store = _store(work, flagged=())
    _mark(store, "t1-probe", **{TRIAGE_FLAG: f"triage:t1-probe:{flag_run}"})
    if task_mark is not None:
        _mark(store, "t1-probe", **{FRESH_AFTER_RUN: task_mark})
    if initiative_mark is not None:
        _initiative_row(store, **{FRESH_AFTER_RUN: initiative_mark})
    _write(tmp_path / "runs", flag_run, RECORD)
    runner = TriageClassRunner({t: new_file_patch(f"{t}.txt") for t in ("t1-probe", "t2-bench", "t3-cutover")}, _answer("hold"))
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, store=store, work_state="store", specs=TRIAGE_SPECS)
    return result, runner, store


def test_a_triage_flag_from_before_a_hand_ready_is_dropped_and_the_task_builds(repo, cart, tmp_path, capsys) -> None:  # noqa: F811
    _, runner, store = _drive_flagged(repo, cart, tmp_path, "demo-initiative-9", task_mark="demo-initiative-9")
    assert not _triage_calls(runner)
    assert len(_built(runner, "t1-probe")) == 1
    assert TRIAGE_FLAG not in _row(store, "t1-probe")["extra"]
    assert "  triage: t1-probe not triaged; its request came from run demo-initiative-9" in capsys.readouterr().out


def test_a_triage_flag_from_before_the_initiative_release_is_dropped(repo, cart, tmp_path) -> None:  # noqa: F811
    _, runner, _ = _drive_flagged(repo, cart, tmp_path, "demo-initiative-9", initiative_mark="demo-initiative-9")
    assert not _triage_calls(runner)
    assert len(_built(runner, "t1-probe")) == 1


def test_a_triage_flag_from_a_run_after_the_mark_is_still_triaged(repo, cart, tmp_path) -> None:  # noqa: F811
    _, runner, store = _drive_flagged(
        repo, cart, tmp_path, "demo-initiative-10", task_mark="demo-initiative-9", initiative_mark="demo-initiative-8",
    )
    assert len(_triage_calls(runner)) == 1
    assert not _built(runner, "t1-probe")
    assert _row(store, "t1-probe")["state"] == "blocked"


def test_attempts_before_the_mark_do_not_cap_the_task_and_it_builds(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, _ = _store_with_two_recorded_attempts(tmp_path)  # epic-prior-1 and epic-prior-2, on the current body
    work = workstore.read_initiative(wi)
    store = _store(work, flagged=())
    _mark(store, "t1-probe", **{FRESH_AFTER_RUN: "epic-prior-2"})
    runner = TriageAttemptRunner({"t1-probe": new_file_patch("t1-probe.txt")})
    result, _ = drive(
        repo, _amend_cart(cart), tmp_path, runner=runner, work=work, store=store, work_state="store", specs=TRIAGE_SPECS,
    )
    assert not any(call["role"] == "triage" for call in runner.calls)
    assert builds_per_task(runner)["t1-probe"] == 1
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])
