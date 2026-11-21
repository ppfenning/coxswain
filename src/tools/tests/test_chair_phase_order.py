"""C5: the phase queue's order, pure (`chair_phase_queue.order`): the hotfix and bug band, first in first out inside a
band, a release's initiatives claimed together, and nothing serialised across repositories. The claim side is the
worker's own rule (`phase_pick.choices`) over the rows the chair writes. No mocks."""

from agent_tools import chair_phase_queue as cpq
from harness import phase_pick

NOW = "2026-11-21T12:00:00Z"


def _claim_order(placed: dict, requires: dict | None = None, capabilities: tuple[str, ...] = ("repo:app",)) -> list:
    """The keys in the order one free worker claims them, one claim at a time, by `phase_pick.choices`."""
    rows = [{"initiative": k[0], "phase": k[1], "state": "ready", "ready_at": NOW, "band": b, "seq": s,
             "requires_json": list((requires or {}).get(k, ["repo:app"]))} for k, (b, s) in placed.items()]
    taken: list = []
    while picks := phase_pick.choices([r for r in rows if (r["initiative"], r["phase"]) not in taken], capabilities, 1, NOW):
        taken.append((picks[0]["initiative"], picks[0]["phase"]))
    return taken


def test_a_hotfix_and_a_bug_go_in_band_0_and_everything_else_in_band_1():
    assert [cpq.band_of(k) for k in ("hotfix", "bug", " Hotfix ", "BUG")] == [0, 0, 0, 0]
    assert [cpq.band_of(k) for k in ("idea", "feature", "", "chore")] == [1, 1, 1, 1]


def test_the_smoke_case_a_then_a_hotfix_b_then_c_in_as_release_reads_b_a_c():
    phases = [("a", "p1"), ("b", "p1"), ("c", "p1")]
    placed = cpq.order(phases, {"a": "2026.12.1", "c": "2026.12.1"}, {"b": "hotfix"},
                       {"a": "2026-11-21T10:00:00Z", "b": "2026-11-21T10:01:00Z", "c": "2026-11-21T10:02:00Z"})
    assert placed == {("b", "p1"): (0, 0), ("a", "p1"): (1, 0), ("c", "p1"): (1, 1)}
    assert _claim_order(placed) == [("b", "p1"), ("a", "p1"), ("c", "p1")]


def test_inside_a_band_the_first_queued_initiative_goes_first_whatever_the_docket_says():
    phases = [("late", "p1"), ("early", "p1"), ("middle", "p1")]
    seen = {"early": "2026-11-21T09:00:00Z", "middle": "2026-11-21T09:30:00Z", "late": "2026-11-21T10:00:00Z"}
    placed = cpq.order(phases, {}, {}, seen)
    assert _claim_order(placed) == [("early", "p1"), ("middle", "p1"), ("late", "p1")]


def test_a_tie_on_first_queued_keeps_docket_order_and_an_unseen_initiative_goes_last():
    placed = cpq.order([("x", "p1"), ("y", "p1"), ("z", "p1")], {}, {}, {"x": NOW, "y": NOW})
    assert _claim_order(placed) == [("x", "p1"), ("y", "p1"), ("z", "p1")]


def test_a_hotfix_filed_last_still_goes_ahead_of_older_ordinary_work_and_bugs_keep_fifo_among_themselves():
    seen = {"old": "2026-11-20T00:00:00Z", "bug1": "2026-11-21T01:00:00Z", "fix": "2026-11-21T02:00:00Z"}
    placed = cpq.order([("old", "p1"), ("fix", "p1"), ("bug1", "p1")], {}, {"fix": "hotfix", "bug1": "bug"}, seen)
    assert _claim_order(placed) == [("bug1", "p1"), ("fix", "p1"), ("old", "p1")]
    assert placed[("bug1", "p1")] == (0, 0) and placed[("old", "p1")] == (1, 0)  # seqs count inside each band


def test_initiatives_sharing_a_release_are_claimed_together_at_the_earliest_members_place():
    seen = {"a": "2026-11-21T10:00:00Z", "d": "2026-11-21T10:01:00Z", "e": "2026-11-21T10:02:00Z",
            "c": "2026-11-21T10:03:00Z"}
    releases = {"a": "2026.12.1", "c": "2026.12.1", "e": "2026.12.2"}
    placed = cpq.order([("a", "p1"), ("d", "p1"), ("e", "p1"), ("c", "p1")], releases, {}, seen)
    assert _claim_order(placed) == [("a", "p1"), ("c", "p1"), ("d", "p1"), ("e", "p1")]


def test_a_releases_phases_get_consecutive_seqs_across_its_initiatives():
    seen = {"a": "2026-11-21T10:00:00Z", "d": "2026-11-21T10:01:00Z", "c": "2026-11-21T10:02:00Z"}
    phases = [("a", "p1"), ("a", "p2"), ("d", "p1"), ("c", "p1"), ("c", "p2")]
    placed = cpq.order(phases, {"a": "r1", "c": "r1"}, {}, seen)
    assert [placed[k][1] for k in [("a", "p1"), ("a", "p2"), ("c", "p1"), ("c", "p2"), ("d", "p1")]] == [0, 1, 2, 3, 4]


def test_an_urgent_initiative_is_not_pulled_down_to_join_its_release_and_its_mates_stay_in_band_1():
    seen = {"a": "2026-11-21T10:00:00Z", "fix": "2026-11-21T10:05:00Z", "b": "2026-11-21T10:06:00Z"}
    placed = cpq.order([("a", "p1"), ("b", "p1"), ("fix", "p1")], {"a": "r1", "fix": "r1"}, {"fix": "hotfix"}, seen)
    assert placed == {("fix", "p1"): (0, 0), ("a", "p1"): (1, 0), ("b", "p1"): (1, 1)}


def test_a_blank_release_groups_nothing():
    seen = {"a": "2026-11-21T10:00:00Z", "b": "2026-11-21T10:01:00Z", "c": "2026-11-21T10:02:00Z"}
    placed = cpq.order([("a", "p1"), ("b", "p1"), ("c", "p1")], {"a": " ", "c": ""}, {}, seen)
    assert _claim_order(placed) == [("a", "p1"), ("b", "p1"), ("c", "p1")]


def test_each_phase_is_its_own_row_and_nothing_serialises_unrelated_repositories():
    """Two repositories: every phase gets its own seq (none shares a row with another, or waits on its repository's
    earlier phase), and a worker that meets only the second repository claims that repository's row at once, ahead of
    the first repository's rows it cannot run."""
    seen = {"app1": "2026-11-21T10:00:00Z", "app2": "2026-11-21T10:01:00Z", "web1": "2026-11-21T10:02:00Z"}
    phases = [("app1", "p1"), ("app1", "p2"), ("app2", "p1"), ("web1", "p1")]
    placed = cpq.order(phases, {}, {}, seen)
    assert len(set(placed.values())) == len(phases)
    requires = {k: ["repo:web"] if k[0].startswith("web") else ["repo:app"] for k in phases}
    assert _claim_order(placed, requires, ("repo:web",)) == [("web1", "p1")]
    assert _claim_order(placed, requires, ("repo:app",)) == [("app1", "p1"), ("app1", "p2"), ("app2", "p1")]


def test_the_first_queued_time_is_the_earliest_row_of_the_initiative_finished_ones_included():
    stored = [{"initiative": "a", "phase": "p2", "queued_at": "2026-11-21T10:05:00Z", "state": "ready"},
              {"initiative": "a", "phase": "p1", "queued_at": "2026-11-21T10:00:00Z", "state": "finished"},
              {"initiative": "b", "phase": "p1", "queued_at": "", "state": "ready"}]
    assert cpq.first_queued(stored) == {"a": "2026-11-21T10:00:00Z"}


def test_ordered_rows_put_a_new_initiative_behind_the_ones_already_waiting():
    wanted = [{"initiative": i, "phase": "p1", "band": 1, "seq": n * 1000} for n, i in enumerate(["new", "old"])]
    stored = [{"initiative": "old", "phase": "p0", "queued_at": "2026-11-21T09:00:00Z", "state": "finished"}]
    out = cpq.ordered(wanted, stored, {}, {}, NOW)
    assert [(r["initiative"], r["band"], r["seq"]) for r in out] == [("new", 1, 1), ("old", 1, 0)]


def _initiative(name: str) -> dict:
    task = {"id": "t1", "requires": []}
    return {"id": name, "repo": "~/src/app", "lane_model": "phase", "phases": [{"phase": "p1", "ready_tasks": [task]}]}


def test_the_tick_writes_the_order_into_the_rows_it_queues():
    queue = {"rows": [{"initiative": "a", "phase": "p0", "state": "finished", "outcome": "approved",
                       "queued_at": "2026-11-21T09:00:00Z"}],
             "readable": True, "lane_prereqs": False, "phase_initiatives": ["a", "b", "c"], "closed": [],
             "releases": {"a": "2026.12.1", "c": "2026.12.1"}, "kinds": {"b": "hotfix"}}
    facts = {"phase_queue": queue, "initiatives": [_initiative(i) for i in ("c", "b", "a")], "limits": {"hard_stop": False}}
    [action] = cpq.plan(facts, NOW)  # type: ignore[arg-type]
    inserted = {(c["initiative"], c["band"], c["seq"]) for c in action["changes"] if c["op"] == "insert"}  # type: ignore[index]
    assert inserted == {("b", 0, 0), ("a", 1, 0), ("c", 1, 1)}
