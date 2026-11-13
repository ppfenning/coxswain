"""`_activity` gathers every operator session of the workspace's project folder, so the spend split does not depend on
who holds the chair lease. The shapes are fixed by tests/fixtures/dash_sessions_v1.json."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair_activity_read, dash_feed
from agent_tools.dash_feed import snapshot

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_sessions_v1.json").read_text())
_AT = datetime(2026, 10, 16, 18, tzinfo=UTC)  # the windows then start 2026-10-16T17:00Z and 2026-10-11T08:00Z
_SPEND = {
    "five_hour_fraction": 0.12, "five_hour_resets_at": "2026-10-16T22:00:00Z",
    "weekly_fraction": 0.57, "weekly_resets_at": "2026-10-18T08:00:00Z",
}
_CATALOG = "models:\n  m1:\n    price: {input: 1.0, output: 2.0}\n"
_SESSION_A = "5f0c2a9e-3b1d-4c7a-9e21-0a6b7d8c1f34"
_SESSION_B = "c81e4d57-92a0-4f6b-b3d5-71e2a9f04c68"
_SESSION_C = "9a7b6c5d-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
_NODE_KEYS = {"by_node", "cost_per_landed_task"}


def _line(message_id: str, timestamp: str, input_tokens: int = 0, output_tokens: int = 0) -> str:
    """One priced assistant line; model m1 costs $1 per million input tokens and $2 per million output tokens."""
    return json.dumps({
        "type": "assistant", "timestamp": timestamp,
        "message": {
            "id": message_id, "model": "m1",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    })


def _write_session(folder: Path, session_id: str, lines: list[str], agents: dict[str, tuple[dict, list[str]]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")
    subagents = folder / session_id / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    for agent_id, (meta, agent_lines) in agents.items():
        (subagents / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        (subagents / f"agent-{agent_id}.jsonl").write_text("\n".join(agent_lines) + "\n")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A workspace whose project folder holds session A, with lines today and earlier in the week, and session B, with
    lines only earlier in the week. Returns the workspace, the profile, an empty runs dir and the projects dir."""
    workspace = tmp_path / "ws"
    projects = tmp_path / "projects"
    folder = projects / chair_activity_read.project_folder_name(workspace)
    _write_session(
        folder, _SESSION_A,
        [_line("a1", "2026-10-12T08:00:00Z", 1_000_000), _line("a2", "2026-10-16T17:30:00Z", 1_000_000, 1_000_000)],
        {
            "g1": ({"agentType": "general-purpose", "description": "Survey the feeds"}, [_line("ag1", "2026-10-16T17:40:00Z", 0, 500_000)]),
            "e1": ({"agentType": "Explore", "description": "Find the callers"}, [_line("ae1", "2026-10-13T10:00:00Z", 1_500_000)]),
        },
    )
    _write_session(
        folder, _SESSION_B,
        [_line("b1", "2026-10-13T09:00:00Z", 2_000_000)],
        {"r1": ({"agentType": "general-purpose", "description": "Review the lease"}, [_line("br1", "2026-10-13T09:30:00Z", 0, 250_000)])},
    )
    catalog = tmp_path / "cartridges" / "providers"
    catalog.mkdir(parents=True)
    (catalog / "catalog.yaml").write_text(_CATALOG)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(dash_feed, "_PROJECTS_DIR", str(projects))
    return workspace, {"cartridges_dir": str(tmp_path / "cartridges")}, runs, projects


def _activity(world, session: str = "") -> dict:
    workspace, profile, runs, _ = world
    return dash_feed._activity(runs, profile, session, workspace, _SPEND, _AT, {})


def _row(built: dict, window: str, session_id: str) -> dict:
    return next(row for row in built["operator_sessions"][window] if row["session_id"] == session_id)


def test_both_sessions_appear_in_the_windows_their_lines_fall_in(world):
    built = _activity(world)

    assert [row["session_id"] for row in built["operator_sessions"]["five_hour"]] == [_SESSION_A]
    assert sorted(row["session_id"] for row in built["operator_sessions"]["weekly"]) == sorted([_SESSION_A, _SESSION_B])
    assert _row(built, "five_hour", _SESSION_A)["cost_usd"] == 3.0
    assert _row(built, "weekly", _SESSION_A)["cost_usd"] == 4.0
    assert _row(built, "weekly", _SESSION_B)["cost_usd"] == 2.0
    assert _row(built, "weekly", _SESSION_A)["started_at"] == "2026-10-12T08:00:00Z"
    assert _row(built, "weekly", _SESSION_A)["model"] == "m1"


def test_agents_appear_under_their_own_session_with_type_description_and_cost(world):
    built = _activity(world)

    assert _row(built, "five_hour", _SESSION_A)["agents"] == [
        {"agent_type": "general-purpose", "description": "Survey the feeds", "cost_usd": 1.0},
    ]
    assert _row(built, "weekly", _SESSION_A)["agents"] == [
        {"agent_type": "Explore", "description": "Find the callers", "cost_usd": 1.5},
        {"agent_type": "general-purpose", "description": "Survey the feeds", "cost_usd": 1.0},
    ]
    assert _row(built, "weekly", _SESSION_B)["agents"] == [
        {"agent_type": "general-purpose", "description": "Review the lease", "cost_usd": 0.5},
    ]
    assert _row(built, "weekly", _SESSION_A)["agent_count"] == 2


def test_the_split_sums_every_session_whoever_holds_the_lease(world):
    built = _activity(world)

    for name, sessions_usd, agents_usd in (("five_hour", 3.0, 1.0), ("weekly", 6.0, 3.0)):
        window = built["spend_split"][name]
        assert window["chair_session_usd"] == sessions_usd
        assert window["chair_agents_usd"] == agents_usd
        parts = window["chair_session_usd"] + window["chair_agents_usd"] + window["cox_seats_usd"]
        assert window["total_usd"] == pytest.approx(parts)
    assert built["chair_agents"] == []


def test_a_service_chair_reads_none_and_the_sessions_are_counted(world):
    sources = _activity(world)["spend_sources"]

    assert sources == {"chair_session": "none", "price_catalog": "ok", "sessions_found": 2, "sessions_priced": "ok"}


def test_the_key_sets_equal_the_contract_fixtures(world):
    built = _activity(world)

    fixture_rows = _FIXTURE["operator_sessions"]
    for window in ("five_hour", "weekly"):
        for row in built["operator_sessions"][window]:
            assert row.keys() == fixture_rows[window][0].keys()
            assert all(agent.keys() == fixture_rows[window][0]["agents"][0].keys() for agent in row["agents"])
        assert set(built["spend_split"][window]) - _NODE_KEYS == set(_FIXTURE["spend_split"][window])
    assert built["operator_sessions"].keys() == fixture_rows.keys()
    assert built["spend_sources"].keys() == _FIXTURE["spend_sources"].keys()


def test_a_holder_found_only_outside_the_workspace_folder_is_counted_once(world):
    projects = world[3]
    _write_session(
        projects / "-some-other-project", _SESSION_C,
        [_line("c1", "2026-10-16T17:45:00Z", 1_000_000)],
        {"x1": ({"agentType": "Explore", "description": "Check the other tree"}, [_line("cx1", "2026-10-16T17:50:00Z", 0, 250_000)])},
    )

    built = _activity(world, _SESSION_C[:8])

    assert built["spend_sources"]["chair_session"] == "ok"
    assert built["spend_sources"]["sessions_found"] == 3
    assert [row["session_id"] for row in built["operator_sessions"]["weekly"]].count(_SESSION_C[:8]) == 1
    assert _row(built, "five_hour", _SESSION_C[:8])["cost_usd"] == 1.0
    assert [agent["agent_id"] for agent in built["chair_agents"]] == ["x1"]
    assert built["spend_split"]["five_hour"]["chair_session_usd"] == 4.0
    assert built["spend_split"]["five_hour"]["chair_agents_usd"] == 1.5


def test_a_holder_already_in_the_workspace_folder_is_not_added_again(world):
    built = _activity(world, _SESSION_A[:8])

    assert built["spend_sources"]["sessions_found"] == 2
    assert [row["session_id"] for row in built["operator_sessions"]["weekly"]].count(_SESSION_A) == 1
    assert built["spend_split"]["weekly"]["chair_session_usd"] == 6.0


def test_a_holder_that_is_not_found_adds_no_session_beside_the_folders_real_ones(world):
    built = _activity(world, "deadbeef")

    assert built["spend_sources"] == {
        "chair_session": "not_found", "price_catalog": "ok", "sessions_found": 2, "sessions_priced": "ok",
    }
    assert sorted(row["session_id"] for row in built["operator_sessions"]["weekly"]) == sorted([_SESSION_A, _SESSION_B])
    assert built["spend_split"]["weekly"]["chair_session_usd"] == 6.0


def test_an_ambiguous_holder_adds_no_session_beside_the_folders_real_ones(world):
    projects = world[3]
    for suffix in ("1111", "2222"):
        _write_session(projects / "-other", f"abcd1234-{suffix}", [_line(f"x{suffix}", "2026-10-16T17:45:00Z", 1_000_000)], {})

    built = _activity(world, "abcd1234")

    assert built["spend_sources"]["chair_session"] == "ambiguous"
    assert built["spend_sources"]["sessions_found"] == 2
    assert sorted(row["session_id"] for row in built["operator_sessions"]["weekly"]) == sorted([_SESSION_A, _SESSION_B])
    assert built["spend_split"]["weekly"]["chair_session_usd"] == 6.0


def test_the_snapshot_built_with_the_activity_carries_operator_sessions(world):
    built = _activity(world)

    carried = snapshot("2026-10-16T18:00:00Z", {}, {}, [], [], [], 0, [], 0, [], operator_sessions=built["operator_sessions"])
    omitted = snapshot("2026-10-16T18:00:00Z", {}, {}, [], [], [], 0, [], 0, [])

    assert carried["operator_sessions"] == built["operator_sessions"]
    assert "operator_sessions" not in omitted


def test_the_workspace_is_the_parent_of_a_work_directory_else_the_directory_itself():
    assert dash_feed._workspace(Path("/srv/ws/work")) == Path("/srv/ws")
    assert dash_feed._workspace(Path("/srv/ws")) == Path("/srv/ws")
