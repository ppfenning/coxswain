"""C6: `cox chair parity`'s judge (§5.3 step 3) and `cox chair status`'s lines. Pure; nothing is stubbed."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from agent_tools import chair_parity as parity
from agent_tools import chair_status as status
from agent_tools.chair_kinds import QUEUE_KINDS

TICK = ("chair 11-21 07:00 EST | lanes 0/2 | lands 0 | limits 5h 10% weekly 49% | dry run | "
        "would: housekeeping:sweep, request_triage:ship, launch_decompose:idea, needs_chair:chair-queue")


def _worker(kind: str, counts: str) -> str:
    return f"chair worker {kind} | holds nothing\nchair worker {kind} (dry run) 2026-11-21T12:00:00.000Z | start | {counts}"


def test_the_tick_reading_counts_its_would_segment_and_sets_its_asks_aside():
    read = parity.tick_reading(f"some log line\n{TICK}\n")
    assert read is not None
    assert read.counts == Counter({"housekeeping": 1, "request_triage": 1, "launch_decompose": 1})
    assert read.asks == ("chair-queue",)


def test_a_tick_with_nothing_to_do_is_an_empty_reading_and_no_status_line_is_none():
    assert parity.tick_reading("chair 11-21 07:00 EST | lanes 0/2 | idle\n") == parity.Reading(Counter())
    assert parity.tick_reading("chair run: refusing to start\n") is None


def test_the_worker_reading_counts_its_pass_line_and_sets_needs_chair_aside():
    read = parity.worker_reading("quarantine", _worker("quarantine", "planned 3 | 2x request_triage dry_run, 1x needs_chair dry_run"))
    assert read is not None and read.counts == Counter({"request_triage": 2}) and len(read.asks) == 1
    assert parity.worker_reading("quarantine", "chair worker quarantine | standby") is None
    assert parity.worker_reading("watch", _worker("watch", "nothing due")) == parity.Reading(Counter())


def _readings(**planned: dict[str, int]) -> dict[str, parity.Reading]:
    return {kind: parity.Reading(Counter(planned.get(kind, {}))) for kind in QUEUE_KINDS}


ALONE = parity.Reading(Counter({"housekeeping": 1, "request_triage": 1, "launch_decompose": 1}))
NONE = parity.Reading(Counter())


def test_the_workers_together_planning_what_the_tick_alone_did_is_a_match():
    workers = _readings(housekeeping={"housekeeping": 1}, quarantine={"request_triage": 1}, launch={"launch_decompose": 1})
    verdict = parity.judge(ALONE, NONE, workers)
    assert verdict.ok and verdict.problems == ()
    assert parity.verdict_lines(ALONE, NONE, workers, verdict)[-1] == "parity: match"


def test_an_action_no_worker_plans_is_lost():
    workers = _readings(housekeeping={"housekeeping": 1}, quarantine={"request_triage": 1})
    verdict = parity.judge(ALONE, NONE, workers)
    assert not verdict.ok and any("by no worker: 1x launch_decompose" in p for p in verdict.problems)


def test_an_action_planned_by_both_the_listed_tick_and_a_worker_is_doubled():
    workers = _readings(housekeeping={"housekeeping": 1}, quarantine={"request_triage": 1}, launch={"launch_decompose": 1})
    verdict = parity.judge(ALONE, parity.Reading(Counter({"housekeeping": 1})), workers)
    assert not verdict.ok
    assert any(p.startswith("housekeeping: the listed tick still plans housekeeping") for p in verdict.problems)
    assert any("planned twice: 1x housekeeping" in p for p in verdict.problems)


def test_what_the_listed_tick_still_plans_and_no_worker_does_is_stranded_even_when_the_counts_add_up():
    """C6: the old model's land slot had no worker. The listed tick planned its lands, the counts added up, and parity
    said match; but the cutover stops the tick, so those lands would never run."""
    alone = parity.Reading(Counter({"land_phase": 2, "fetch_exit": 1, "launch_epic": 1}))
    listed = parity.Reading(Counter({"land_phase": 2, "fetch_exit": 1}))
    verdict = parity.judge(alone, listed, _readings(launch={"launch_epic": 1}))
    assert not verdict.ok
    assert verdict.problems == (
        "the tick, every kind listed, still plans 1x fetch_exit, 2x land_phase, which no worker plans: once the tick is "
        "stopped nothing would",
    )
    owned = parity.judge(alone, NONE, _readings(watch={"land_phase": 2, "fetch_exit": 1}, launch={"launch_epic": 1}))
    assert owned.ok


def test_a_worker_planning_more_than_the_tick_alone_is_named():
    workers = _readings(housekeeping={"housekeeping": 2}, quarantine={"request_triage": 1}, launch={"launch_decompose": 1})
    verdict = parity.judge(ALONE, NONE, workers)
    assert any("housekeeping: plans more than the tick alone: housekeeping 2 > 1" in p for p in verdict.problems)


def test_a_worker_whose_pass_was_not_read_is_a_mismatch():
    workers = {k: v for k, v in _readings(housekeeping={"housekeeping": 1}).items() if k != "watch"}
    assert any(p == "watch: no dry-run pass was read" for p in parity.judge(NONE, NONE, workers).problems)


NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


def _row(kind: str, beat: str, expires: str, holder: str = "w@host:1") -> status.LeaseRow:
    return status.LeaseRow(f"chair:{kind}", f"{kind}-{holder}", 4, beat, expires)


def test_a_live_lease_shows_its_holder_epoch_beat_age_and_expiry():
    line = status.lease_line("chair:watch", status.kind_standing(_row("watch", "2026-11-21T11:59:55Z", "2026-11-21T12:00:55Z"), NOW))
    assert line == "chair:watch: live, held by watch-w@host:1 at epoch 4, beat 5s ago, expires in 55s"


def test_an_expired_released_and_absent_lease():
    expired = status.kind_standing(_row("watch", "2026-11-21T11:50:00Z", "2026-11-21T11:51:00Z"), NOW)
    assert expired.state == "expired" and status.lease_line("x", expired).endswith("expired 9m00s ago")
    released = status.kind_standing(status.LeaseRow("chair:watch", "", 4, "2026-11-21T11:50:00Z", "1970-01-01T00:00:00Z"), NOW)
    assert released.state == "released"
    assert status.lease_line("x", status.kind_standing(None, NOW)) == "x: no lease row"


def test_the_status_names_every_kind_the_chair_lease_the_units_and_what_is_not_running():
    queued = frozenset(QUEUE_KINDS)
    rows = [_row(k, "2026-11-21T11:59:58Z", "2026-11-21T12:00:58Z") for k in QUEUE_KINDS if k != "decompose"]
    units = {"coxswain-chair@housekeeping.service": "active", "coxswain-chair.service": "inactive"}
    lines = status.status_lines(queued, None, rows, NOW, units)
    assert lines[0] == "chair.queues: housekeeping, watch, quarantine, decompose, launch"
    assert lines[2] == "chair (the tick's lease): no lease row"
    assert sum(1 for line in lines if line.startswith("  chair:")) == len(QUEUE_KINDS)
    assert "  coxswain-chair.service: inactive" in lines
    assert any(line.startswith("queues: no depth or oldest age") for line in lines)
    assert lines[-1] == "NOT RUNNING: decompose (listed in chair.queues, no live worker)"
    standings = {k: status.kind_standing(r, NOW) for k, r in ((r.name.removeprefix("chair:"), r) for r in rows)}
    assert not status.all_live(queued, standings) and status.all_live(queued - {"decompose"}, standings)


def test_with_nothing_listed_the_status_says_the_tick_plans_every_kind():
    lines = status.status_lines(frozenset(), None, [], NOW)
    assert lines[0] == "chair.queues: none (the tick plans every kind)" and not any("NOT RUNNING" in l for l in lines)
    assert status.all_live(frozenset(), {})


PROFILE = """team: pat
workspace_dir: ~/repos/workspace
provider_profile: ~/.config/agent-tools/provider.yaml
chair:
  queues: [housekeeping]
  housekeeping_hours: 6
notify: {ntfy: https://ntfy.example/cox}
"""


def test_the_scratch_profile_points_at_the_scratch_store_and_workspace_and_lists_the_kinds():
    from agent_tools import route

    text = parity.scratch_profile(PROFILE, "/tmp/p/provider.yaml", "/tmp/p/workspace", QUEUE_KINDS)
    read = route.parse_profile(text)
    assert read["workspace_dir"] == "/tmp/p/workspace" and read["provider_profile"] == "/tmp/p/provider.yaml"
    assert list(read["chair"]["queues"]) == list(QUEUE_KINDS) and float(read["chair"]["housekeeping_hours"]) == 6
    assert read["team"] == "pat" and read["notify"] == {"ntfy": "https://ntfy.example/cox"}
    empty = route.parse_profile(parity.scratch_profile(PROFILE, "/p", "/w", ()))
    assert list(empty["chair"]["queues"]) == []
