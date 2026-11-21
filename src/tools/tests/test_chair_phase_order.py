"""C5: the phase queue's order, pure (`chair_phase_queue.order`): the hotfix and bug band, explicit priority then
first in first out inside a band, a release's initiatives claimed together, nothing serialised across repositories,
and one pass over every claimable row so no two share a place (`placements`). The claim side is the
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
    out = cpq.ordered(wanted, cpq.placements(wanted, stored, {}, {}, NOW))
    assert [(r["initiative"], r["band"], r["seq"]) for r in out] == [("new", 1, 1), ("old", 1, 0)]


# -- explicit priority inside a band (`cox route priority`, the docket's) --------------------------------------------


def test_inside_a_band_explicit_priority_goes_first_then_fifo():
    seen = {"old": "2026-11-21T09:00:00Z", "mid": "2026-11-21T09:30:00Z", "top": "2026-11-21T10:00:00Z"}
    placed = cpq.order([("old", "p1"), ("mid", "p1"), ("top", "p1")], {}, {}, seen, {"top": 5})
    assert _claim_order(placed) == [("top", "p1"), ("old", "p1"), ("mid", "p1")]


def test_priority_never_crosses_a_band_a_hotfix_still_goes_ahead_of_a_top_priority_idea():
    seen = {"idea": "2026-11-21T09:00:00Z", "fix": "2026-11-21T10:00:00Z"}
    placed = cpq.order([("idea", "p1"), ("fix", "p1")], {}, {"fix": "hotfix"}, seen, {"idea": 99})
    assert placed == {("fix", "p1"): (0, 0), ("idea", "p1"): (1, 0)}


def test_priority_orders_urgent_work_among_itself_too():
    seen = {"bug1": "2026-11-21T09:00:00Z", "bug2": "2026-11-21T10:00:00Z"}
    placed = cpq.order([("bug1", "p1"), ("bug2", "p1")], {}, {"bug1": "bug", "bug2": "bug"}, seen, {"bug2": 1})
    assert _claim_order(placed) == [("bug2", "p1"), ("bug1", "p1")]


def test_a_release_follows_its_first_member_in_priority_then_fifo_order():
    """Release grouping comes after priority and FIFO: the release sits where its first member does, and pulls its
    other members (here `new`, filed last) forward with it."""
    seen = {"a": "2026-11-21T09:00:00Z", "b": "2026-11-21T09:30:00Z", "new": "2026-11-21T11:00:00Z",
            "top": "2026-11-21T10:00:00Z"}
    placed = cpq.order([("a", "p1"), ("b", "p1"), ("top", "p1"), ("new", "p1")], {"a": "r1", "new": "r1"}, {}, seen,
                       {"top": 3})
    assert _claim_order(placed) == [("top", "p1"), ("a", "p1"), ("new", "p1"), ("b", "p1")]


def test_route_priority_top_moves_a_later_initiative_ahead_through_the_tick():
    queue = {"rows": [], "readable": True, "lane_prereqs": False, "phase_initiatives": ["a", "b"], "closed": [],
             "priorities": {"b": 10}}
    facts = {"phase_queue": queue, "initiatives": [_initiative(i) for i in ("a", "b")], "limits": {"hard_stop": False}}
    [action] = cpq.plan(facts, NOW)  # type: ignore[arg-type]
    inserted = {(c["initiative"], c["seq"]) for c in action["changes"] if c["op"] == "insert"}  # type: ignore[index]
    assert inserted == {("b", 0), ("a", 1)}


def test_the_read_takes_each_initiatives_highest_priority_over_its_open_tasks():
    from agent_tools.chair_read_phase_queue import initiative_priorities

    heads = [{"kind": "task", "initiative": "a", "state": "ready", "priority": 3},
             {"kind": "task", "initiative": "a", "state": "todo", "priority": "7"},
             {"kind": "task", "initiative": "a", "state": "done", "priority": 50},  # finished work does not count
             {"kind": "task", "initiative": "b", "state": "ready", "priority": 0},
             {"kind": "task", "initiative": "c", "state": "ready", "priority": "high"},
             {"kind": "initiative", "initiative": "d", "state": "ready", "priority": 9}]
    assert initiative_priorities(heads) == {"a": 7}


# -- one order pass over every claimable row: no two share a (band, seq) ---------------------------------------------


def _stored(initiative: str, phase: str, state: str, band: int, seq: int, **extra: object) -> dict:
    return {"initiative": initiative, "phase": phase, "state": state, "band": band, "seq": seq, "attempts": 0,
            "ready_at": NOW, "queued_at": "2026-11-21T08:00:00Z", "requires_json": ["repo:app"], "repo": "~/src/app",
            "lease_until": "2026-11-21T13:00:00Z", "claim_epoch": 1, **extra}


def _after(stored: list[dict], found: list[dict]) -> list[dict]:
    """The stored rows once `found`'s band and seq writes are applied, as the store's update and review ops do."""
    by_key = {(r["initiative"], r["phase"]): dict(r) for r in stored}
    for c in found:
        key = (c["initiative"], c["phase"])
        if c["op"] == "insert":
            by_key[key] = {**c, "state": "ready", "ready_at": NOW, "queued_at": NOW, "requires_json": c["requires"]}
        elif c["op"] in ("update", "review", "reopen") and "band" in c:
            by_key[key] = {**by_key[key], "band": c["band"], "seq": c["seq"],
                           **({"state": "ready"} if c["op"] != "update" else {})}
    return list(by_key.values())


def _places(rows: list[dict]) -> list[tuple]:
    return [(r["band"], r["seq"]) for r in rows if r["state"] in ("ready", "waiting", "claimed", "built")]


def test_a_built_row_made_ready_by_its_review_is_placed_and_never_clashes_with_a_new_row():
    """L6's `review` makes a parked `built` row ready. Left at its old seq 0 it would share (1, 0) with the new row the
    pass numbers from 0; the pass places it, and the review carries the place."""
    stored = [_stored("old", "p1", "built", 1, 0)]
    wanted = [{"initiative": "new", "phase": "p1", "kind": "build", "repo": "~/src/app", "requires": ["repo:app"],
               "band": 1, "seq": 0}]
    placed = cpq.placements(wanted, stored, {}, {}, NOW)
    found = cpq.changes(cpq.ordered(wanted, placed), stored, NOW, placed=placed)
    review = next(c for c in found if c["op"] == "review")
    assert (review["band"], review["seq"]) == (1, 0)  # queued first, so first in line
    assert next(c for c in found if c["op"] == "insert")["seq"] == 1
    places = _places(_after(stored, found))
    assert len(places) == len(set(places)), places


def test_a_ready_row_nobody_wants_this_pass_is_renumbered_with_its_requirements_kept():
    stored = [_stored("gone", "p1", "ready", 1, 0, requires_json=["repo:web", "docker"], repo="~/src/web")]
    wanted = [{"initiative": "hot", "phase": "p1", "kind": "build", "repo": "~/src/app", "requires": ["repo:app"],
               "band": 1, "seq": 0}]
    placed = cpq.placements(wanted, stored, {}, {"hot": "hotfix"}, NOW, {"gone": 1})
    found = cpq.changes(cpq.ordered(wanted, placed), stored, NOW, placed=placed)
    assert {(c["op"], c["initiative"], c["band"], c["seq"]) for c in found} == {("insert", "hot", 0, 0)}  # (1, 0) kept
    stored2 = [*stored, _stored("idea", "p1", "ready", 1, 0, queued_at="2026-11-21T07:00:00Z")]
    placed2 = cpq.placements([], stored2, {}, {}, NOW)
    found2 = cpq.changes([], stored2, NOW, placed=placed2)
    [update] = [c for c in found2 if c["initiative"] == "gone"]
    assert update == {"op": "update", "initiative": "gone", "phase": "p1", "kind": "build", "repo": "~/src/web",
                      "requires": ["docker", "repo:web"], "band": 1, "seq": 1}
    places = _places(_after(stored2, found2))
    assert len(places) == len(set(places)), places


def test_a_claimed_row_keeps_its_place_and_the_pass_numbers_round_it():
    stored = [_stored("busy", "p1", "claimed", 1, 0), _stored("busy", "p2", "claimed", 1, 2,
                                                              lease_until="2026-11-21T11:00:00Z")]  # lapsed: claimable
    wanted = [{"initiative": i, "phase": "p1", "kind": "build", "repo": "~/src/app", "requires": ["repo:app"],
               "band": 1, "seq": 0} for i in ("a", "b", "c", "busy")]
    placed = cpq.placements(wanted, stored, {}, {}, NOW)
    assert placed[("busy", "p1")] == (1, 0) and placed[("busy", "p2")] == (1, 2)
    found = cpq.changes(cpq.ordered(wanted, placed), stored, NOW, placed=placed)
    assert not [c for c in found if c["initiative"] == "busy"]  # a claimed row is the worker's
    assert sorted(c["seq"] for c in found if c["op"] == "insert") == [1, 3, 4]
    places = _places(_after(stored, found))
    assert len(places) == len(set(places)), places


def test_a_withdrawn_or_exhausted_row_takes_no_place():
    stored = [_stored("held", "p1", "ready", 1, 0), _stored("spent", "p1", "ready", 1, 1, attempts=99)]
    placed = cpq.placements([], stored, {}, {}, NOW, leaving={("held", "p1"), ("spent", "p1")})
    assert placed == {}


def test_every_pass_of_the_tick_leaves_the_claimable_rows_on_distinct_places():
    """Through `plan`: a built row from an older worker, a ready row the docket dropped, a claimed row, a reopened
    phase and two new ones. After the pass's writes, no two rows a worker could claim share a place, and a second
    pass over the result plans no band or seq change."""
    stored = [_stored("parked", "p1", "built", 1, 0), _stored("dropped", "p1", "ready", 1, 1),
              _stored("busy", "p1", "claimed", 1, 2),
              _stored("back", "p1", "finished", 1, 0, outcome="approved", queued_at="2026-11-21T06:00:00Z")]
    queue = {"rows": stored, "readable": True, "lane_prereqs": False, "closed": [],
             "phase_initiatives": ["parked", "dropped", "busy", "back", "n1", "n2"]}
    initiatives = [{**_initiative(i), "repo": "~/src/app"} for i in ("back", "n1", "n2")]
    facts = {"phase_queue": queue, "initiatives": initiatives, "limits": {"hard_stop": False}}
    [action] = cpq.plan(facts, NOW)  # type: ignore[arg-type]
    after = _after(stored, action["changes"])  # type: ignore[arg-type]
    places = _places(after)
    assert len(places) == len(set(places)) == 6, places
    again = cpq.plan({**facts, "phase_queue": {**queue, "rows": after}}, NOW)  # type: ignore[arg-type]
    assert not [c for a in again for c in a.get("changes", []) if c["op"] in ("update", "review", "insert", "reopen")]


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
