"""The chair asks for triage of a quarantine that needs a tweak; the epic triages the task before any rebuild.

Under work_state: store a task whose row carries the chair's flag is triaged whatever its attempt count, the flag is
removed once triage answered, and each class's effect is applied to the board: an append-only amendment is written to
the row and built on, a drop drops the task, a combine folds it into a sibling, and a hold leaves it blocked with the
mark a person reads.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core import workstore
from core.workstore import HOLD_MARK, HOLD_REASON, TRIAGE_FLAG, TRIAGE_RECORDS
from graphs.ops import triage_quarantine
from harness import epic as epic_module
from harness import store_queue
from harness.store_migrate import open_store
from harness.store_write import Store
from runner.claude_code_runner import files_touched_from_patch
from runner.protocol import RunnerError
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    SPECS,
    Runner,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)

TRIAGE_SPECS = {**SPECS, "triage-quarantine": triage_quarantine.SPEC}
OBJECTION = "the probe reads nothing"
DIAGNOSIS = f"the reviewer said {OBJECTION}"
ACTION = "read one row from the vendor schema and print its columns"
INITIATIVE = "demo-initiative"


class TriageClassRunner(Runner):
    """Scripts `triage` with one answer, and picks a build's patch by the `task` it is for, not by the prompt's text."""

    def __init__(self, patches, answer) -> None:
        super().__init__(patches)
        self.answer = answer

    def run(self, *, role, tier=None, hints=None, schema, prompt, context=(), thread=None, budget_usd=None, task=None,
            wait_if_paused=None):
        if role == "triage":
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt, "budget_usd": budget_usd})
            if isinstance(self.answer, BaseException):
                raise self.answer
            return dict(self.answer)
        if role == "build" and task in self.patches:
            with self.lock:
                self.calls.append({"role": role, "tier": tier, "prompt": prompt, "budget_usd": budget_usd, "task": task})
            patch = self.patches[task]
            return {"patch": patch, "summary": f"built {task}", "files_touched": files_touched_from_patch(patch), "commands_run": []}
        return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context, thread=thread,
                           budget_usd=budget_usd, task=task)


def _answer(class_: str, **extra) -> dict:
    return {"class": class_, "diagnosis": DIAGNOSIS, "cites": ["attempt-1|reason"], "action": ACTION, **extra}


def _work() -> dict:
    """p1: t1-probe (one review attempt on its body, below the cap) and t2-bench; p2: t3-cutover, which needs t1-probe."""
    work = initiative()
    t1 = next(item for item in work["items"] if item["id"] == "t1-probe")
    t1["attempts"] = [
        {"run": "epic-prior-1", "phase": "p1-foundations", "reason": OBJECTION, "kind": "refused", "cause": "review",
         "body_sha": workstore.body_sha(t1["body"]), "ts": "2026-09-01T00:00:00+00:00"},
    ]
    return work


def _store(work: dict, *, flagged: tuple[str, ...] = ("t1-probe",), records: list | None = None) -> Store:
    store = Store(open_store("sqlite:///:memory:", datetime.now(UTC).isoformat()))
    for item in work["items"]:
        extra = {TRIAGE_FLAG: f"triage:{item['id']}:epic-prior-1"} if item["id"] in flagged else {}
        if records is not None and item["id"] == "t1-probe":
            extra[TRIAGE_RECORDS] = records
        store_queue.upsert(store.conn, {
            "initiative": INITIATIVE, "task_id": item["id"], "kind": "task", "phase": item["phase"], "state": item["state"],
            "needs": list(item["needs"]), "title": item["title"], "surfaces": [], "body": item["body"], "extra": extra,
        })
    return store


def _drive(repo, cart, tmp_path, answer, *, flagged=("t1-probe",), records=None, work=None):  # noqa: F811
    # As the base cartridge declares them: no apply arm names a model for an amendment.
    kinds = {"ticket_amend": {"risk": "low", "ramp": "eligible"}, "item_create": {"risk": "medium", "ramp": "gated"}}
    cart = {**cart, "write_kinds": {**cart["write_kinds"], **kinds}}
    work = work or _work()
    store = _store(work, flagged=flagged, records=records)
    patches = {t: new_file_patch(f"{t}.txt") for t in ("t1-probe", "t2-bench", "t3-cutover")}
    runner = TriageClassRunner(patches, answer)
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, store=store, work_state="store", specs=TRIAGE_SPECS)
    return result, runner, store


def _row(store: Store, task: str) -> dict:
    return next(r for r in store_queue.read(store.conn, INITIATIVE) if r["task_id"] == task)


def _built(runner, task: str) -> list[dict]:
    return [c for c in runner.calls if c["role"] == "build" and c.get("task") == task]


def _triage_calls(runner) -> list[dict]:
    return [c for c in runner.calls if c["role"] == "triage"]


def test_without_the_flag_a_review_quarantine_below_the_cap_rebuilds_with_no_triage(repo, cart, tmp_path) -> None:  # noqa: F811
    _, runner, _ = _drive(repo, cart, tmp_path, _answer("hold"), flagged=())
    assert not _triage_calls(runner)
    assert len(_built(runner, "t1-probe")) == 1


def test_a_flagged_task_is_triaged_from_the_whole_initiative_and_the_flag_becomes_a_record(repo, cart, tmp_path) -> None:  # noqa: F811
    _, runner, store = _drive(repo, cart, tmp_path, _answer("hold"))
    (call,) = _triage_calls(runner)
    assert "make the vendor join measurable end to end" in call["prompt"]
    assert "t2-bench" in call["prompt"] and "bench harness" in call["prompt"] and "t3-cutover" in call["prompt"]
    extra = _row(store, "t1-probe")["extra"]
    assert TRIAGE_FLAG not in extra
    assert extra[TRIAGE_RECORDS] == [{"class": "hold", "diagnosis": DIAGNOSIS, "run": "epic-1:p1-foundations:t1-probe"}]


def test_ticket_defect_with_no_arm_writes_the_amendment_to_the_row_and_builds_on_it(repo, cart, tmp_path) -> None:  # noqa: F811
    result, runner, store = _drive(repo, cart, tmp_path, _answer("ticket_defect"))
    row = _row(store, "t1-probe")
    assert row["body"].startswith("read the vendor schema") and row["body"].rstrip().endswith(ACTION)
    assert OBJECTION in row["body"].removeprefix("read the vendor schema")  # the appended text quotes the objection
    (build,) = _built(runner, "t1-probe")
    assert ACTION in build["prompt"]
    assert row["state"] == "approved"
    assert not any(p["kind"] == "ticket_amend" for p in result["proposals"])
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])


def test_a_flagged_amendment_the_harness_cannot_write_holds_the_task_unbuilt(monkeypatch, repo, cart, tmp_path) -> None:  # noqa: F811
    monkeypatch.setattr(epic_module, "_amended_body", lambda old, action: f"{action}\n{old}")  # not append-only
    result, runner, store = _drive(repo, cart, tmp_path, _answer("ticket_defect"))
    row = _row(store, "t1-probe")
    assert not _built(runner, "t1-probe")
    assert row["state"] == "blocked" and row["extra"][HOLD_REASON].startswith(HOLD_MARK)
    assert [p["target"] for p in result["proposals"] if p["kind"] == "ticket_amend"] == ["t1-probe"]


@pytest.mark.parametrize("class_", ["hold", "genuine_reject", "shape"])
def test_a_hold_leaves_the_task_blocked_with_the_mark_and_unbuilt(repo, cart, tmp_path, class_) -> None:  # noqa: F811
    result, runner, store = _drive(repo, cart, tmp_path, _answer(class_))
    row = _row(store, "t1-probe")
    assert row["state"] == "blocked"
    reason = row["extra"][HOLD_REASON]
    assert reason.startswith(HOLD_MARK) and OBJECTION in reason and ACTION in reason
    assert not _built(runner, "t1-probe")
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])


def test_a_platform_defect_files_the_intake_and_holds_the_task(repo, cart, tmp_path) -> None:  # noqa: F811
    result, runner, store = _drive(repo, cart, tmp_path, _answer("platform_defect"))
    assert [p["target"] for p in result["proposals"] if p["kind"] == "item_create"] == ["t1-probe"]
    row = _row(store, "t1-probe")
    assert row["state"] == "blocked" and row["extra"][HOLD_REASON].startswith(HOLD_MARK)
    assert not _built(runner, "t1-probe")


def test_a_repeat_of_a_stored_triage_record_holds_the_task(repo, cart, tmp_path) -> None:  # noqa: F811
    prior = [{"class": "drop", "diagnosis": DIAGNOSIS, "run": "epic-0:p1-foundations"}]
    result, runner, store = _drive(repo, cart, tmp_path, _answer("drop"), records=prior)
    row = _row(store, "t1-probe")
    assert row["state"] == "blocked" and "repeats" in row["extra"][HOLD_REASON]
    assert not _built(runner, "t1-probe")
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])
    assert len(row["extra"][TRIAGE_RECORDS]) == 2


def test_a_drop_drops_the_task_and_its_dependant_builds(repo, cart, tmp_path) -> None:  # noqa: F811
    _, runner, store = _drive(repo, cart, tmp_path, _answer("drop"))
    assert _row(store, "t1-probe")["state"] == "dropped"
    assert not _built(runner, "t1-probe")
    assert len(_built(runner, "t3-cutover")) == 1


def test_a_combine_folds_the_task_into_its_sibling_and_moves_the_need(repo, cart, tmp_path) -> None:  # noqa: F811
    _, runner, store = _drive(repo, cart, tmp_path, _answer("combine", into="t2-bench"))
    assert _row(store, "t1-probe")["state"] == "dropped"
    sibling = _row(store, "t2-bench")
    assert sibling["body"].startswith("time the join")
    assert "t1-probe" in sibling["body"] and "read the vendor schema" in sibling["body"]
    assert _row(store, "t3-cutover")["needs"] == ["t2-bench"]
    assert not _built(runner, "t1-probe")
    (build,) = _built(runner, "t2-bench")
    assert "read the vendor schema" in build["prompt"]


def test_a_flagged_task_whose_triage_failed_is_quarantined_and_not_built(repo, cart, tmp_path) -> None:  # noqa: F811
    result, runner, store = _drive(repo, cart, tmp_path, RunnerError("triage is down"))
    assert any(q["id"] == "t1-probe" for q in result["quarantined"])
    assert not _built(runner, "t1-probe")
    assert TRIAGE_FLAG in _row(store, "t1-probe")["extra"]


# ── the pure parts ──────────────────────────────────────────────────────────


def test_combined_needs_move_a_need_to_the_sibling_without_a_self_need() -> None:
    items = [
        {"id": "a", "needs": []},
        {"id": "b", "needs": ["a"]},
        {"id": "c", "needs": ["a", "b"]},
    ]
    assert epic_module.combined_needs(items, "a", "b") == {"b": [], "c": ["b"]}


def test_combined_needs_refuse_a_cycle() -> None:
    items = [
        {"id": "a", "needs": []},
        {"id": "b", "needs": ["c"]},
        {"id": "c", "needs": ["a"]},
    ]
    assert epic_module.combined_needs(items, "a", "b") is None


def test_a_file_body_rewrite_keeps_the_frontmatter_bytes(tmp_path) -> None:
    path = tmp_path / "t.md"
    path.write_text("---\nid: t\nstate: ready\nneeds: [x]\n---\n\nold body\n", encoding="utf-8")
    epic_module._rewrite_file_body(path, "old body\n\nnew text")
    assert path.read_text(encoding="utf-8") == "---\nid: t\nstate: ready\nneeds: [x]\n---\n\nold body\n\nnew text\n"


def test_a_file_needs_rewrite_changes_only_that_key(tmp_path) -> None:
    path = tmp_path / "t.md"
    path.write_text("---\nid: t\nneeds:\n- a\n- b\ntitle: x\n---\n\nbody\n", encoding="utf-8")
    epic_module._rewrite_file_needs(path, ["c"])
    text = path.read_text(encoding="utf-8")
    assert workstore.read_item(path)["needs"] == ["c"] and "title: x" in text and text.endswith("---\n\nbody\n")
