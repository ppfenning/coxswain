"""An intake's release or requeue resets its empty-decompose streak: runs launched before `released_at` do not count."""

from datetime import UTC, datetime

from agent_tools import chair_decompose_runs, route_hold, route_requeue, run_store
from agent_tools.chair_decompose_streak import DecomposeRun, DecomposeStreak, at_limit, streak
from agent_tools.chair_facts import decompose_streak_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

PATH = "2026-10-06-some-idea.md"
RELEASED = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
BEFORE_1 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
BEFORE_2 = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
AFTER_1 = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)
AFTER_2 = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)


def _empty(run_id: str, at: datetime | None) -> DecomposeRun:
    return DecomposeRun(run_id, False, None, launched_at=at)


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [PATH],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {},
        "lost_runs": {},
        "last_housekeeping_at": None,
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _kinds(actions, kind):
    return [a for a in actions if a["kind"] == kind]


def _plan(runs: list[DecomposeRun], cutoff: datetime | None):
    streaks = decompose_streak_facts([PATH], lambda p: runs, lambda p: cutoff)
    return plan_tick(_facts(decompose_streaks=streaks))


# the streak, pure


def test_runs_launched_before_the_cutoff_do_not_count():
    s = streak([_empty("i-1", BEFORE_1), _empty("i-2", BEFORE_2)], since=RELEASED)
    assert s == DecomposeStreak(empty_count=0, run_ids=(), first_refusal=None)


def test_runs_launched_after_the_cutoff_count_as_before():
    s = streak([_empty("i-1", BEFORE_1), _empty("i-2", AFTER_1), _empty("i-3", AFTER_2)], since=RELEASED)
    assert s.run_ids == ("i-2", "i-3") and at_limit(s)


def test_no_cutoff_counts_every_run():
    assert streak([_empty("i-1", BEFORE_1), _empty("i-2", BEFORE_2)]).empty_count == 2


def test_a_run_with_no_launch_time_still_counts():
    assert streak([_empty("i-1", None), _empty("i-2", None)], since=RELEASED).empty_count == 2


# the gate, end to end through the facts


def test_a_released_intake_with_two_older_empty_runs_is_launchable():
    actions = _plan([_empty("some-idea-1", BEFORE_1), _empty("some-idea-2", BEFORE_2)], RELEASED)
    assert _kinds(actions, "launch_decompose") == [{"kind": "launch_decompose", "intake_ids": [PATH], "epoch": 7}]
    assert _kinds(actions, "needs_chair") == []


def test_two_empty_runs_after_the_release_still_gate_it():
    runs = [_empty("some-idea-1", BEFORE_1), _empty("some-idea-2", AFTER_1), _empty("some-idea-3", AFTER_2)]
    actions = _plan(runs, RELEASED)
    assert _kinds(actions, "launch_decompose") == []
    stalled = _kinds(actions, "needs_chair")
    assert [a["cause"] for a in stalled] == ["decompose_stalled"]
    assert stalled[0]["run"] == "some-idea-3"


def test_one_empty_run_after_the_release_does_not_gate_it():
    actions = _plan([_empty("some-idea-1", BEFORE_1), _empty("some-idea-2", BEFORE_2), _empty("some-idea-3", AFTER_1)], RELEASED)
    assert len(_kinds(actions, "launch_decompose")) == 1


def test_without_a_cutoff_reader_the_streak_gates_as_before():
    runs = [_empty("some-idea-1", BEFORE_1), _empty("some-idea-2", BEFORE_2)]
    assert PATH in decompose_streak_facts([PATH], lambda p: runs)


# the edge: launch times and the recorded cutoff


def test_launch_times_parse_naive_as_utc_and_garbage_as_none():
    assert chair_decompose_runs.parse_when("2026-11-16T13:00:00.123456+00:00") == datetime(2026, 11, 16, 13, 0, 0, 123456, tzinfo=UTC)
    assert chair_decompose_runs.parse_when("2026-11-16T13:00:00") == AFTER_1
    assert chair_decompose_runs.parse_when("") is None
    assert chair_decompose_runs.parse_when("not a time") is None
    assert chair_decompose_runs.parse_when(None) is None


def test_the_cutoff_is_read_from_the_intake_frontmatter(tmp_path):
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "a.md").write_text('---\nid: a\nreleased_at: "2026-11-16T12:00:00+00:00"\n---\nBody\n')
    (tmp_path / "intake" / "b.md").write_text("---\nid: b\n---\nBody\n")
    assert chair_decompose_runs.read_released_at(tmp_path, "intake/a.md") == RELEASED
    assert chair_decompose_runs.read_released_at(tmp_path, "intake/b.md") is None
    assert chair_decompose_runs.read_released_at(tmp_path, "intake/missing.md") is None


# release and requeue record the cutoff


FREE = "---\ntitle: Idea\nstage: ready\n---\nBody\n"
STAMPED = '---\ntitle: Idea\nstage: ready\nreleased_at: "2026-11-16T12:00:00+00:00"\n---\nBody\n'


def test_release_records_released_at_in_the_row_and_the_file(tmp_path, monkeypatch):
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "idea.md").write_text(FREE)
    sent = []
    monkeypatch.setattr(route_hold.run_store, "upsert_row", lambda runs_dir, row: sent.append(row) or True)
    monkeypatch.setattr(route_hold.queue_export, "record_written_paths", lambda w, paths: None)
    rows = [{"kind": "intake", "state": "queued", "task_id": "idea", "initiative": "intake", "extra": {"title": "Idea", "stage": "ready"}}]
    assert route_hold.release(tmp_path, tmp_path / "runs", "idea", rows, now=RELEASED) == 0
    assert sent[0]["extra"]["released_at"] == "2026-11-16T12:00:00+00:00"
    assert (tmp_path / "intake" / "idea.md").read_text() == STAMPED
    assert chair_decompose_runs.read_released_at(tmp_path, "intake/idea.md") == RELEASED


def test_requeue_records_released_at_in_the_row_and_the_file(tmp_path, monkeypatch):
    intake = {"kind": "intake", "initiative": "intake", "task_id": "idea", "state": "decomposed", "extra": {"initiative": "demo"}}
    folder = {"kind": "initiative", "initiative": "demo", "task_id": "demo", "state": "todo", "extra": {"intake": "intake/idea.md"}}
    writes = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: [intake, folder])
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: writes.append(row) or "")
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "idea.md").write_text("---\nid: idea\ninitiative: demo\ntitle: Idea\n---\nBody\n")
    code, _ = route_requeue.requeue(tmp_path, tmp_path / "runs", "demo", now=RELEASED)
    assert code == 0
    assert writes[0]["extra"] == {"released_at": "2026-11-16T12:00:00+00:00"}
    assert chair_decompose_runs.read_released_at(tmp_path, "intake/idea.md") == RELEASED
