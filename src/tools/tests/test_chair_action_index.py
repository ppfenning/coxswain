"""The tick's action index: each reader of the chair_actions rows, handed only the kinds and causes `gather_facts`
selects for it, answers exactly as it does handed every row.

The rows mix every kind and cause the readers act on with the needs_chair noise that makes up most of a real window, in
an order that is not time order, plus a row whose kind is not text."""

from datetime import UTC, datetime

from agent_tools import chair_facts_derive as derive
from agent_tools.chair_action_rows import freeze
from agent_tools.chair_facts_read import LAUNCH_KINDS, duration_records
from agent_tools.chair_types import HELD_FILE_CAUSE, STALE_CHECKOUT_CAUSE

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def act(ts: str, kind: str, status: str = "done", **doc: object) -> dict[str, object]:
    return {"ts": ts, "holder": "chair", "kind": kind, "target": str(doc.get("task_id") or doc.get("initiative") or ""),
            "status": status, "reason": str(doc.pop("reason", "")), "action_json": {"kind": kind, "status": status, **doc}}


ROWS = [
    act("2026-11-18T09:50:00Z", "launch_epic", initiative="a"),
    act("2026-11-18T11:10:00Z", "steer_clear", initiative="a", other="b"),
    act("2026-11-18T11:20:00Z", "steer_clear", initiative="a", other="b"),
    act("2026-11-18T11:31:00Z", "relaunch", "lease_held", initiative="b", reason="run: b-3 holds runs:b; refusing to start b-4"),
    act("2026-11-18T11:30:00Z", "relaunch", "done", initiative="b"),
    act("2026-11-18T11:00:00Z", "land_phase", "refused", initiative="a", phase="p1", run="a-1", head="h1"),
    act("2026-11-18T11:05:00Z", "land_phase", "landed", initiative="a", phase="p0", run="a-1"),
    act("2026-11-18T11:06:00Z", "partial_land", "failed", initiative="a", phase="p2", run="a-1"),
    act("2026-11-18T11:07:00Z", "merge_level", "merge_failed", initiative="a", phase="p1", reason="conflict"),
    act("2026-11-18T11:08:00Z", "land", "refused", initiative="a", task_id="t1"),
    act("2026-11-18T10:00:00Z", "requeue_task", task_id="t2", initiative="a"),
    act("2026-11-18T11:40:00Z", "needs_chair", "recorded", cause="no_run", initiative="a"),
    act("2026-11-18T10:31:00Z", "needs_chair", "recorded", cause="decompose_stalled", initiative="a", run="a-2"),
    act("2026-11-18T10:32:00Z", "needs_chair", "recorded", cause=derive.MISSING_IDEA_CAUSE, intake="intake/x.md", fingerprint="f1"),
    act("2026-11-18T10:33:00Z", "needs_chair", "recorded", cause=HELD_FILE_CAUSE, path="work/a/p1/t1.md", blob="b1"),
    act("2026-11-18T10:34:00Z", "needs_chair", "recorded", cause=STALE_CHECKOUT_CAUSE, host="h", repo="r"),
    act("2026-11-18T10:35:00Z", "needs_chair", "recorded", cause="relaunch_loop", reason="runs a-1 and a-2 were both quarantined"),
    act("2026-11-18T10:36:00Z", "stale_to_draft", "done", initiative="a", stale_tasks=["t2", "t1"]),
    act("2026-11-18T10:37:00Z", "launch_decompose", "refused", intake_ids=["intake/x.md"], idea_missing="no idea", fingerprint="f1"),
    act("2026-11-18T10:38:00Z", "waiting", initiative="a", phase="p1", quarantined=1, awaiting_land=0),
    act("2026-11-18T09:00:00Z", "pull", "done"),
    act("2026-11-18T09:30:00Z", "pull", "done"),
    *(act(f"2026-11-1{n % 8}T0{n % 10}:00:00Z", "needs_chair", "recorded", cause=f"held t{n}", initiative="c") for n in range(40)),
    act("2026-11-18T08:00:00Z", "status", "done"),
    {"ts": "2026-11-18T07:00:00Z", "holder": "chair", "kind": "odd", "target": "", "status": "done", "action_json": {"kind": 7}},
]
APPROVED = [
    {"initiative": "a", "id": "t1", "repo": "r", "run": "a-1", "merged": False},
    {"initiative": "a", "id": "t2", "repo": "r", "run": "a-1", "merged": False},
]


def test_every_reader_answers_the_same_from_its_selection_as_from_every_row():
    for rows in (ROWS, [freeze(r) for r in ROWS]):
        index = derive.action_index(rows)

        def pick(*selectors, index=index):
            return derive.selected(index, *selectors)

        pairs = [
            (lambda r: derive.land_refusals_from_actions(r, NOW), pick("land_phase", "partial_land")),
            (derive.decompose_stalled_keys, pick(("needs_chair", "decompose_stalled"))),
            (derive.stale_checkout_keys, pick(("needs_chair", STALE_CHECKOUT_CAUSE))),
            (derive.held_file_keys, pick(("needs_chair", HELD_FILE_CAUSE))),
            (derive.stale_draft_keys, pick("stale_to_draft")),
            (derive.relaunch_loop_keys, pick(("needs_chair", "relaunch_loop"))),
            (lambda r: derive.launch_history_from_actions(r, [], NOW), pick("launch_epic", "relaunch")),
            (lambda r: derive.lease_refusals_from_actions(r, NOW), pick("launch_epic", "relaunch")),
            (derive.steer_streaks_from_actions, pick("steer_clear", *sorted(LAUNCH_KINDS))),
            (derive.land_fail_streaks_from_actions, pick(*derive.LAND_FAIL_KINDS)),
            (lambda r: derive.no_run_streaks_from_actions(APPROVED, r),
             pick("requeue_task", *sorted(derive.HOLD_LAND_KINDS), ("needs_chair", "no_run"))),
            (lambda r: derive.approval_holds_from_actions(APPROVED, r), pick(*sorted(derive.HOLD_KINDS))),
            (derive.waiting_reported_from_actions, pick("waiting", *sorted(LAUNCH_KINDS))),
            (lambda r: derive.pull_age_s(r, NOW), pick("pull")),
            (lambda r: derive.decompose_miss_fact_keys(r, lambda path: "f2", frozenset({"intake/x.md"})),
             pick("launch_decompose", ("needs_chair", derive.MISSING_IDEA_CAUSE))),
            (lambda r: duration_records(r, {("a", "p0"): ("s", "m")}, {}), pick("launch_epic", "relaunch", "land_phase")),
        ]
        for reader, chosen in pairs:
            assert len(chosen) < len(rows)
            assert reader(chosen) == reader(rows)


def test_the_readers_answer_something_from_these_rows():
    """So the equality above is not empty against empty."""
    assert derive.land_refusals_from_actions(ROWS, NOW) and derive.decompose_stalled_keys(ROWS)
    assert derive.held_file_keys(ROWS) and derive.stale_checkout_keys(ROWS) and derive.relaunch_loop_keys(ROWS)
    assert derive.steer_streaks_from_actions(ROWS) and derive.land_fail_streaks_from_actions(ROWS)
    assert derive.no_run_streaks_from_actions(APPROVED, ROWS) and derive.approval_holds_from_actions(APPROVED, ROWS)
    assert derive.waiting_reported_from_actions(ROWS) and derive.pull_age_s(ROWS, NOW) is not None
    assert derive.lease_refusals_from_actions(ROWS, NOW) and derive.launch_history_from_actions(ROWS, [], NOW)
    assert derive.decompose_miss_fact_keys(ROWS, lambda path: "f2", frozenset({"intake/x.md"}))["decompose_misses"]


def test_a_selection_keeps_the_read_order_and_always_holds_a_row_whose_kind_is_not_text():
    index = derive.action_index(ROWS)
    chosen = derive.selected(index, "pull", "launch_epic", "pull")
    assert chosen == [ROWS[0], ROWS[20], ROWS[21], ROWS[-1]]
    assert derive.selected(index, ("needs_chair", "no_such_cause")) == [ROWS[-1]]
