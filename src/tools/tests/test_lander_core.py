"""The lander's pure core (H3): the land decision, the record against the branch, and the lanes of a pass.

Example inputs in, values out. Nothing here touches git, the store, a forge or the clock: the glue that gathers these
inputs and acts on the answers is proven end to end by `smoke/test_lander_lanes.py`.
"""
from __future__ import annotations

import pytest

from agent_tools import land, lander, lander_cli, lander_ports
from agent_tools.lander import Drift, LandFacts, LandStep, land_step

URL, BRANCH = "https://forge.example/pull/7", "init/ship"


def facts(**fields) -> LandFacts:
    return LandFacts(url=URL, branch=BRANCH, **fields)


# -- the land decision ----------------------------------------------------------------------------------------------

def test_a_merged_pull_request_finishes_whatever_else_is_true():
    assert land_step(facts(merged=True, auto=False, stacked_on="below", checks=("pending", "x"))) == LandStep("finish")


@pytest.mark.parametrize(("given", "says"), [
    ({"stacked_on": "base-initiative"}, f"{URL} is stacked on base-initiative, which has not landed"),
    ({"auto": False}, f"{lander.PERSON_GATE_NOTE}: {URL}"),
    ({"prepare_only": True}, f"{lander.PREPARED_NOTE}: {URL}"),
])
def test_a_land_that_is_not_the_landers_to_merge_waits_and_says_why(given, says):
    assert land_step(facts(**given)) == LandStep("wait", says)


def test_whether_the_branch_has_ci_is_asked_first_then_the_checks_are_polled():
    assert land_step(facts()).action == "read_ci"
    assert land_step(facts(has_ci=True)).action == "poll"


def test_a_branch_with_no_ci_merges_on_its_gate_checks_and_never_polls():
    step = land_step(facts(has_ci=False))
    assert step.action == "merge" and step.note.startswith(f"no CI on {BRANCH}")


def test_pending_checks_wait_for_the_next_pass_instead_of_holding_this_one():
    step = land_step(facts(has_ci=True, checks=("pending", "checks pending: test")))
    assert step == LandStep("wait", f"checks on {URL} have not finished: checks pending: test")


def test_a_rerun_waits_and_green_merges():
    assert land_step(facts(has_ci=True, checks=("rerun", "failed runs rerun once: 9"))).action == "wait"
    assert land_step(facts(has_ci=True, checks=("green", "green"))) == LandStep("merge")


def test_red_checks_are_sent_back_with_the_forges_words():
    assert land_step(facts(has_ci=True, checks=("failed", "failing checks: lint"))) == LandStep("send_back", "failing checks: lint")


@pytest.mark.parametrize(("code", "output", "state"), [
    (0, "", "green"),
    (land.PENDING_RC, "checks pending: test", "pending"),
    (1, "no checks reported", "pending"),
    (1, "failing checks: lint", "failed"),
])
def test_one_poll_reads_as_a_state_and_no_check_yet_is_pending(code, output, state):
    assert land.check_state(code, output) == state


def test_checks_that_stay_unreadable_keep_the_land_waiting_with_the_reason():
    assert land_step(facts(has_ci=True, checks=("error", "checks unreadable 5 passes in a row: 401"))) == LandStep(
        "wait", f"checks on {URL} cannot be read: checks unreadable 5 passes in a row: 401")


def test_unreadable_checks_are_told_once_per_commit_and_reason():
    memo = {**land.poll_memo(None, "aaa"), "errors": land.POLL_ERROR_LIMIT, "unreadable": "HTTP 401"}
    told, reason = lander.tell_unreadable(memo, "error")
    assert reason == "HTTP 401" and told["told"] == "HTTP #"
    assert lander.tell_unreadable(told, "error") == (told, None)
    assert lander.tell_unreadable({**told, "unreadable": "bad gateway"}, "error")[1] == "bad gateway"
    assert lander.tell_unreadable(land.poll_memo(told, "bbb") | {"unreadable": "HTTP 401"}, "error")[1] == "HTTP 401"
    assert lander.tell_unreadable(memo, "pending") == (memo, None)


def test_the_same_error_with_a_new_request_id_time_or_count_is_not_told_again():
    first = "gh: HTTP 401 Bad credentials (request 9C1F:2B3A:1A2B3C4:5D6E7F8 at 2026-11-17T10:00:01Z, try 3 of 5)"
    later = "gh: HTTP 401 Bad credentials (request 7A2E:1F00:4C5D6E7:8F9A0B1 at 2026-11-17T10:04:59Z, try 5 of 5)"
    assert lander.reason_key(first) == lander.reason_key(later) == "gh: HTTP # Bad credentials (request # at #, try # of #)"
    memo = {**land.poll_memo(None, "aaa"), "unreadable": first}
    told, reason = lander.tell_unreadable(memo, "error")
    assert reason == first
    assert lander.tell_unreadable({**told, "unreadable": later}, "error")[1] is None
    assert lander.tell_unreadable({**told, "unreadable": "origin is not a Forgejo URL"}, "error")[1] is not None
    assert lander.reason_key("trace 123e4567-e89b-12d3-a456-426614174000 failed") == "trace # failed"


def test_a_queued_item_that_keeps_its_first_queueing_alarms_at_the_hour():
    item = {"id": "initiative_land:ship", "state": "queued", "queued_at": "2026-11-17T10:00:00Z",
            "last_error": "checks on x cannot be read: HTTP 401"}
    assert lander.stale_items([item], "2026-11-17T10:59:59Z") == []
    assert lander.stale_items([item], "2026-11-17T11:00:00Z") == [item]


def test_the_merge_guard_names_the_merged_phase_commits_the_branch_lacks():
    rec = record(p1=MERGED, p2={"state": "merged", "tip": "tip2", "merge_sha": "mrg2"}, p3={"state": "queued", "tip": "t3"})
    holds = {MERGED["tip"]: True, MERGED["merge_sha"]: True, "tip2": True}
    assert lander.unheld_shas(rec, holds) == ["mrg2"]
    assert lander.unheld_shas(rec, {**holds, "mrg2": True}) == []


# -- one poll per pass keeps wait_checks' bounds across passes --------------------------------------------------------

LIMIT = land.POLL_ERROR_LIMIT


def test_the_poll_memory_is_kept_for_its_commit_and_fresh_for_a_new_one():
    seen = {**land.poll_memo(None, "aaa"), "errors": 2, "accepted": ["9"]}
    assert land.poll_memo(seen, "aaa") == seen
    assert land.poll_memo(seen, "bbb") == land.poll_memo(None, "bbb")
    assert land.poll_memo(None, "bbb")["errors"] == 0


def test_unreadable_reads_are_pending_until_the_limit_then_an_error_with_the_reason():
    memo = land.poll_memo(None, "aaa")
    for n in range(1, LIMIT):
        state, detail, memo = land.unreadable_step(memo, "HTTP 502")
        assert state == "pending" and f"({n}/{LIMIT})" in detail
    state, detail, memo = land.unreadable_step(memo, "HTTP 502")
    assert (state, detail) == ("error", f"checks unreadable {LIMIT} passes in a row: HTTP 502")


def test_a_failure_with_runs_to_rerun_asks_for_the_rerun_of_runs_not_yet_taken():
    memo = {**land.poll_memo(None, "aaa"), "accepted": ["11"]}
    step = land.failed_poll_step(memo, {"c1", "c2"}, ("11", "12"), "failing checks: test")
    assert (step.state, step.todo) == ("rerun", ("12",))


def test_a_partial_rerun_is_recorded_so_no_run_is_rerun_twice_and_refusals_are_counted():
    memo = land.poll_memo(None, "aaa")
    state, detail, memo = land.rerun_step(memo, ("11", "12"), ["12"], {"c1"}, "failing checks: test")
    assert state == "pending" and memo["accepted"] == ["11"] and memo["rerun_tries"] == 1 and not memo["rerun_of"]
    assert "rerun refused for runs 12" in detail
    assert land.failed_poll_step(memo, {"c1"}, ("11", "12"), "failing checks: test").todo == ("12",)
    state, _, memo = land.rerun_step(memo, ("12",), [], {"c1"}, "failing checks: test")
    assert state == "rerun" and memo["accepted"] == ["11", "12"] and memo["rerun_of"] == ["c1"]


def test_refused_reruns_stop_at_the_limit_and_the_failure_is_final():
    memo = {**land.poll_memo(None, "aaa"), "rerun_tries": LIMIT}
    step = land.failed_poll_step(memo, {"c1"}, ("11",), "failing checks: test")
    assert step.state == "failed" and f"refused {LIMIT} times" in step.detail


def test_right_after_a_rerun_the_old_failures_are_pending_for_a_bounded_number_of_polls():
    memo = {**land.poll_memo(None, "aaa"), "rerun_of": ["c1", "c2"]}
    for _ in range(LIMIT):
        step = land.failed_poll_step(memo, {"c1"}, ("11",), "failing checks: test")
        assert step.state == "pending" and step.detail.startswith("rerun not yet registered")
        memo = step.memo
    assert land.failed_poll_step(memo, {"c1"}, ("11",), "failing checks: test").state == "failed"


def test_after_the_rerun_a_new_failure_is_final_and_never_rerun_again():
    memo = {**land.poll_memo(None, "aaa"), "rerun_of": ["c1"]}
    assert land.failed_poll_step(memo, {"c3"}, ("11",), "failing checks: test").state == "failed"


def test_a_startup_failure_or_a_failure_with_nothing_to_rerun_is_final():
    memo = land.poll_memo(None, "aaa")
    assert land.failed_poll_step(memo, {"c1"}, ("11",), f"{land.WORKFLOW_ISSUE}ci.yml").state == "failed"
    assert land.failed_poll_step(memo, {"c1"}, (), "failing checks: test").state == "failed"


@pytest.mark.parametrize(("green", "detail", "state"), [
    (True, "green", "green"),
    (False, "checks pending: x", "pending"),
    (False, "no checks reported within 1800s", "pending"),
    (False, "failing checks: lint", "failed"),
])
def test_a_forge_that_can_only_wait_is_read_as_a_poll(green, detail, state):
    assert lander_ports.waited_state(green, detail) == state


# -- the record against the branch ----------------------------------------------------------------------------------

def record(**phases) -> dict:
    return {"initiative": "ship", "branch": BRANCH, "base_sha": "base0", "phases": phases}


MERGED = {"state": "merged", "tip": "tip111111111111", "merge_sha": "mrg111111111111"}


def test_merged_entries_lists_only_merged_phases_in_natural_order():
    rec = record(p10=MERGED, p2=MERGED, p3={"state": "queued", "tip": "t"})
    assert [p for p, _, _ in lander.merged_entries(rec)] == ["p2", "p10"]


def test_a_branch_that_holds_what_the_record_says_has_no_drift():
    on = {MERGED["tip"]: True, MERGED["merge_sha"]: True}
    assert lander.phase_drift(record(p1=MERGED), on, on) == []


def test_record_merged_branch_missing_the_commit_and_no_copy_of_it_here_is_held_with_the_reason():
    """The efficiency case: the record says p1 merged, the branch does not hold it, and nothing here can put it back."""
    [drift] = lander.phase_drift(record(p1=MERGED), {}, {})
    assert drift.action == "hold" and drift.phase == "p1"
    assert drift.reason == (f"phase p1 is recorded merged at mrg111111111, but {BRANCH} does not hold tip111111111, "
                            "and this clone has no copy of its tip tip111111111 to merge again")


def test_a_missing_phase_whose_tip_is_here_is_merged_again():
    [drift] = lander.phase_drift(record(p1=MERGED), {}, {MERGED["tip"]: True})
    assert drift == Drift("p1", "remerge", f"phase p1 is recorded merged at mrg111111111, but {BRANCH} does not hold "
                                           "tip111111111; its tip tip111111111 is merged again", MERGED["tip"])


def test_a_lost_merge_commit_with_the_tip_still_on_the_branch_only_moves_the_record():
    [drift] = lander.phase_drift(record(p1=MERGED), {MERGED["tip"]: True}, {MERGED["tip"]: True})
    assert drift.action == "remerge" and "does not hold mrg111111111" in drift.reason


def test_a_merged_phase_with_no_tip_recorded_cannot_be_repaired():
    [drift] = lander.phase_drift(record(p1={"state": "merged", "merge_sha": "mrg2"}), {}, {})
    assert drift.action == "hold" and "no phase tip is recorded" in drift.reason


def test_a_repair_keeps_the_guard_on_every_sha_but_those_in_question():
    rec = record(p1=MERGED, p2={"state": "merged", "tip": "tip2", "merge_sha": "mrg2"})
    assert lander.kept_shas(rec, ["p1"]) == ["base0", "tip2", "mrg2"]


def test_a_hold_is_told_once_until_its_reason_changes_or_it_lifts():
    now = "2026-11-17T10:00:00Z"
    held, news = lander.held_after(None, "phase p1 is gone", now)
    assert held == {"reason": "phase p1 is gone", "since": now} and news
    assert lander.held_after(held, "phase p1 is gone", "later") == (held, False)
    assert lander.held_after(held, "phase p2 is gone", "later") == ({"reason": "phase p2 is gone", "since": "later"}, True)
    assert lander.held_after(held, None, "later") == (None, True)
    assert lander.held_after(None, None, "later") == (None, False)


def test_a_held_initiative_says_why_it_waits():
    assert lander.held_note({"initiative": "ship", "held": {"reason": "p1 is gone"}}) == "ship is held: p1 is gone"


def test_status_shows_the_hold():
    rec = {"initiative": "ship", "branch": BRANCH, "parent": "main", "state": "assembled", "held": {"reason": "p1 is gone"}}
    status = lander_cli.status_rows([rec], [], {}, "2026-11-17T10:00:00Z")
    assert status["initiatives"][0]["held"] == "p1 is gone"
    assert "  held: p1 is gone" in lander_cli.status_lines(status)


# -- lanes ----------------------------------------------------------------------------------------------------------

def test_a_pass_splits_into_one_lane_per_repository_in_queue_order():
    records = [{"initiative": "a1", "repo": "~/src/a"}, {"initiative": "b1", "repo": "~/src/b"}, {"initiative": "a2", "repo": "/home/user/src/a"}]
    items = [{"id": "land:b1", "initiative": "b1", "repo": "~/src/b"}, {"id": "phase:a2:p1", "initiative": "a2", "repo": "/home/user/src/a"},
             {"id": "land:a1", "initiative": "a1", "repo": "~/src/a"}, {"id": "land:z", "initiative": "z", "repo": "/home/user/src/z"}]
    where = {"a1": "/home/user/src/a", "a2": "/home/user/src/a", "b1": "/home/user/src/b"}
    found = lander.lanes(records, items, where)
    assert [(lane.repo, lane.initiatives, [i["id"] for i in lane.items]) for lane in found] == [
        ("/home/user/src/a", ("a1", "a2"), ["phase:a2:p1", "land:a1"]),
        ("/home/user/src/b", ("b1",), ["land:b1"]),
        ("/home/user/src/z", (), ["land:z"]),
    ]


@pytest.mark.parametrize(("rec", "judged"), [
    ({"state": "building"}, False),
    ({"state": "conflicted"}, False),
    ({"state": "assembled"}, True),
    ({"state": "pr_open", "pr": {"url": URL}}, True),
    ({"state": "conflicted", "pr": {"url": URL}}, True),
])
def test_only_a_published_initiative_is_judged_against_origin(rec, judged):
    assert lander.published(rec) is judged


def test_landed_and_per_task_records_are_not_live():
    assert lander.live({"state": "assembled"})
    assert not lander.live({"state": "landed"})
    assert not lander.live({"state": "building", "land_at": "task"})
    assert not lander.live({"state": "pr_open", "land_at": "epic"})


# -- a landing-record row that does not read ------------------------------------------------------------------------

def _unread(initiative: str, why: str = "phases is malformed"):
    from core import landing_record as lr

    return lr.Unreadable(initiative, why)


def test_an_unreadable_row_is_told_once_per_stall_and_forgotten_once_it_reads_again():
    bad = _unread("bad")
    tell, forget = lander.unreadable_notices({}, [bad])
    assert (tell, forget) == ([bad], [])
    told = {"unreadable:bad": lander.reason_key(lander.unreadable_reason(bad))}
    assert lander.unreadable_notices(told, [bad]) == ([], []), "the same stall is not told twice"
    changed = _unread("bad", "record_json is not JSON")
    assert lander.unreadable_notices(told, [changed]) == ([changed], []), "a new reason is news"
    assert lander.unreadable_notices(told, []) == ([], ["unreadable:bad"]), "it reads again: the next stall is new"
    assert lander.unreadable_notices({"other:x": "y"}, []) == ([], []), "another kind of notice is not this one's to forget"


def test_the_items_of_an_unreadable_initiative_are_left_out_of_the_pass_and_every_other_is_kept():
    items = [{"id": "phase_merge:bad:p1", "initiative": "bad"}, {"id": "initiative_land:good", "initiative": "good"}]
    assert lander.without_unreadable(items, [_unread("bad")]) == [items[1]]
    assert lander.without_unreadable(items, []) == items


def test_a_failed_pass_is_told_once_while_it_repeats_and_again_after_a_pass_that_succeeds():
    told, reason = lander.pass_failure_notice(None, "pass failed: OperationalError: the store went away at 10:00:01")
    assert reason is not None and told is not None
    assert lander.pass_failure_notice(told, "pass failed: OperationalError: the store went away at 10:00:31") == (told, None), (
        "the same failure at another time is not news")
    assert lander.pass_failure_notice(told, "pass failed: KeyError: 'branch'")[1] == "pass failed: KeyError: 'branch'"
    assert lander.pass_failure_notice(told, None) == (None, None)


def test_adopt_counts_an_unreadable_row_as_present_and_never_as_a_candidate():
    from agent_tools import lander_adopt
    from core import landing_record as lr

    assert lander_adopt.present_skip(None) is None
    assert lander_adopt.present_skip({"initiative": "a"}) == "it has a landing record already"
    skip = lander_adopt.present_skip(_unread("bad"))
    assert skip is not None and "cannot be read" in skip and "phases is malformed" in skip
    table = lr.Listing(({"initiative": "a"},), (_unread("bad"),))
    assert lander_adopt.unrecorded(["bad", "new", "a"], table) == ["new"]
    assert lander_adopt.left_alone(["bad", "new", "a"], table) == [f"bad: skipped: {skip}"], (
        "a person who named it is told it was left alone; a readable record is skipped quietly, as before")


def test_status_names_an_unreadable_row_with_why():
    status = lander_cli.status_rows([], [], {}, "2026-11-17T10:00:00Z", [_unread("bad")])
    assert status["unreadable"] == [{"initiative": "bad", "why": "phases is malformed"}]
    assert "bad: landing record cannot be read, left as it is: phases is malformed" in lander_cli.status_lines(status)
