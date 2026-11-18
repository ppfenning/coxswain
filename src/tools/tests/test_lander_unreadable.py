"""One landing-record row that does not read, through the lander's own passes: a real repo, a real store, a fake forge.

The bug lived here: `plan_pass` listed every record, one malformed row raised for the whole table, and no initiative
in the store landed while nobody was told. Now the row stops only its own initiative, the chair is told once, and
nothing writes into the row.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_tools import lander, lander_adopt, lander_cli
from harness import landing_ops as ops
from harness import landing_store as ls
from tests.test_lander import World, task

BAD_JSON = '{"initiative": "bad", "branch": "init/bad", "phases": [1]}'


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _corrupt(world: World, initiative: str, text: str = BAD_JSON) -> None:
    p = world.conn.dialect.placeholder
    world.conn.execute(f"UPDATE landing_records SET record_json = {p} WHERE initiative = {p}", (text, initiative))


def _raw(world: World, initiative: str) -> tuple:
    p = world.conn.dialect.placeholder
    return world.conn.query_one(f"SELECT repo, state, record_json, created_at, updated_at FROM landing_records "
                                f"WHERE initiative = {p}", (initiative,))


def _told(world: World) -> list[dict]:
    return [fields for kind, fields in world.notes if kind == "needs_chair"]


def test_a_malformed_row_stops_only_its_own_initiative_and_the_chair_is_told_once(world: World) -> None:
    world.start("good", [task("a", "p1")])
    world.start("bad", [task("b", "p1")])
    world.build("good", "p1")
    world.build("bad", "p1")
    assert ops.queue_phase(world.conn, world.record("bad"), "p1", now=world.now(), reason="built")
    _corrupt(world, "bad")
    before = _raw(world, "bad")

    lander.run_once(world.ports)

    assert world.record("good")["state"] == "landed", "the good initiative's pass proceeds"
    [told] = _told(world)
    assert told["initiative"] == "bad" and "cannot be read" in told["reason"] and "phases" in told["reason"]
    assert _raw(world, "bad") == before, "the bad row is the evidence: nothing writes into it"
    assert ls.get_item(world.conn, "phase_merge:bad:p1")["state"] == "queued", "its item waits for the row to be repaired"

    lander.run_once(world.ports)
    assert len(_told(world)) == 1, "one notice per stall"
    assert _raw(world, "bad") == before


def test_a_row_that_reads_again_is_forgotten_and_its_next_stall_is_told_again(world: World) -> None:
    world.start("bad", [task("b", "p1")])
    good_json = _raw(world, "bad")[2]
    _corrupt(world, "bad")
    lander.run_once(world.ports)
    assert len(_told(world)) == 1

    _corrupt(world, "bad", good_json)
    lander.run_once(world.ports)
    assert "unreadable: the landing record of bad reads again" in world.logged
    assert ls.told(world.conn, lander.UNREADABLE_KEY) == {}

    _corrupt(world, "bad", "{not json")
    lander.run_once(world.ports)
    assert [t["initiative"] for t in _told(world)] == ["bad", "bad"], "a new stall is told"


def test_a_land_stacked_on_a_layer_whose_row_does_not_read_waits_and_writes_nothing(world: World) -> None:
    world.start("low", [task("l", "p1")])
    world.rows["up"] = [task("u", "p1")]
    record, why = ops.start_record(world.conn, world.repo, initiative="up", branch="init/up", parent="init/low",
                                   parent_ref="init/low", now=world.now())
    assert why == "" and record is not None
    world.build("up", "p1")
    _corrupt(world, "low", '{"initiative": "low", "branch": "init/low", "phases": [1]}')

    outcomes = lander.run_once(world.ports)

    land = next(o for o in outcomes if o.item == "initiative_land:up")
    assert land.status == "waiting" and "landing record of low" in land.detail and "cannot be read" in land.detail
    assert world.record("up")["parent"] == "init/low", "not moved down onto a parent nobody can read"
    assert not [c for c in world.forge.calls if c[0] in ("pr_create", "merge", "retarget")], "nothing is cut, opened or merged"


def test_an_epic_whose_member_may_be_an_unreadable_row_does_not_open_its_pull_request(world: World) -> None:
    """Finding 2: `meta` of an initiative with no file is `{}`, which read as "not in this epic": the epic opened early."""
    ls.put_record(world.conn, {"initiative": "a", "repo": str(world.repo), "branch": "init/a", "parent": "epic-x",
                               "state": "landed", "epic": "x", "phases": {}})
    ls.put_record(world.conn, {"initiative": "m", "repo": str(world.repo), "branch": "init/m", "parent": "epic-x",
                               "state": "building", "epic": "x", "phases": {}})
    epic_meta = {"epic_stack": True, "epic_branch": "epic-x", "epic_parent": "main", "epic_title": "Epic x"}
    world.ports.meta = lambda initiative: dict(epic_meta) if initiative == "a" else {}
    world.ports.repo = lambda record: world.repo
    _corrupt(world, "m", "{not json")

    events = lander.land_epics(world.ports)

    assert events == ["epic x: waits: the landing record of m cannot be read, and may be one of its initiatives"]
    assert not [c for c in world.forge.calls if c[0] == "pr_create"] and not ls.has_record(world.conn, "epic:x")
    ls.delete_record(world.conn, "m")
    assert any(e.startswith("epic x: every initiative has landed; opened ") for e in lander.land_epics(world.ports)), (
        "with the row gone the same epic opens: the hold was the unreadable row's")


def test_adopt_leaves_an_initiative_whose_row_does_not_read_and_never_writes_over_it(world: World) -> None:
    world.start("bad", [task("b", "p1", state="approved")])
    _corrupt(world, "bad")
    before = _raw(world, "bad")
    world.ports.repo = lambda record: world.repo

    assert lander_adopt.candidates(world.ports, ["bad"]) == []
    found = lander_adopt.plan(world.ports, "bad")
    assert found.skip is not None and "cannot be read" in found.skip
    assert lander_adopt.apply(world.ports, found, lambda *_: None) == [f"bad: skipped: {found.skip}"]
    assert _raw(world, "bad") == before


def test_a_failed_pass_is_told_to_the_chair_once_per_distinct_failure(world: World) -> None:
    told = lander_cli.tell_pass_failure(world.ports, None, "pass failed: OperationalError: gone at 10:00:01")
    told = lander_cli.tell_pass_failure(world.ports, told, "pass failed: OperationalError: gone at 10:00:31")
    assert len(_told(world)) == 1 and "pass failed" in _told(world)[0]["reason"]
    told = lander_cli.tell_pass_failure(world.ports, told, None)
    lander_cli.tell_pass_failure(world.ports, told, "pass failed: OperationalError: gone at 10:05:00")
    assert len(_told(world)) == 2, "after a pass that succeeded, the same failure is news again"
