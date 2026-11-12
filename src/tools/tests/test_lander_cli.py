"""`cox lander`: the unit text, the status view, and one real pass over a temporary workspace."""
from __future__ import annotations

from pathlib import Path

from agent_tools import cli, lander_cli

NOW = "2026-11-12T12:00:00Z"


def test_the_unit_runs_the_lander_loop_beside_the_chair() -> None:
    text = lander_cli.unit_text("/opt/cox/bin/cox", "/srv/ws", 30, "/srv/profile.yaml", "/opt/cox/bin:/usr/bin", "/srv/env")
    assert "ExecStart=/opt/cox/bin/cox lander run --interval 30 --profile /srv/profile.yaml\n" in text
    assert "WorkingDirectory=/srv/ws\n" in text and "Environment=PATH=/opt/cox/bin:/usr/bin\n" in text
    assert "EnvironmentFile=-/srv/env\n" in text and "Restart=on-failure\n" in text
    bare = lander_cli.unit_text("cox", "/srv/ws", 45)
    assert "ExecStart=cox lander run --interval 45\n" in bare and "Environment" not in bare
    assert lander_cli.unit_path(Path("/srv/u")) == Path("/srv/u/.config/systemd/user/coxswain-lander.service")
    assert lander_cli.systemctl_lines()[-1] == "systemctl --user enable --now coxswain-lander.service"


def test_age_reads_minutes_hours_and_days() -> None:
    assert lander_cli.age("2026-11-12T11:57:00Z", NOW) == "3m"
    assert lander_cli.age("2026-11-12T09:55:00Z", NOW) == "2h05m"
    assert lander_cli.age("2026-11-08T10:00:00Z", NOW) == "4d02h"
    assert lander_cli.age("", NOW) == "?"


def test_status_shows_live_initiatives_by_derived_state_and_the_open_queue_with_its_age() -> None:
    records = [
        {"initiative": "demo", "state": "building", "branch": "init/demo", "parent": "main",
         "phases": {"p1": {"state": "merged", "tasks": ["a"]}}},
        {"initiative": "old", "state": "landed", "branch": "init/old", "parent": "main", "phases": {}},
    ]
    tasks = {"demo": [{"id": "a", "phase": "p1", "state": "approved", "needs": []},
                      {"id": "b", "phase": "p2", "state": "approved", "needs": ["a"]}]}
    items = [
        {"id": "phase_merge:demo:p2", "kind": "phase_merge", "state": "queued", "queued_at": "2026-11-12T09:55:00Z",
         "attempts": 2, "claimed_by": "", "last_error": "conflict in shared.txt", "reason": "phase built"},
        {"id": "phase_merge:demo:p1", "kind": "phase_merge", "state": "done", "queued_at": "2026-11-12T09:00:00Z",
         "attempts": 1, "claimed_by": "", "last_error": "", "reason": "phase built"},
    ]
    status = lander_cli.status_rows(records, items, tasks, NOW)
    assert status["initiatives"] == [
        {"initiative": "demo", "state": "building", "branch": "init/demo", "parent": "main",
         "phases": {"p1": "merged", "p2": "built"}, "pr": None},
    ]
    assert [(q["id"], q["waiting"], q["last_error"]) for q in status["queue"]] == [
        ("phase_merge:demo:p2", "2h05m", "conflict in shared.txt"),
    ]
    assert lander_cli.status_lines(status) == [
        "demo: building on init/demo -> main",
        "  phases: p1 merged, p2 built",
        "",
        "phase_merge:demo:p2: queued, waiting 2h05m, 2 attempt(s): conflict in shared.txt",
    ]
    assert lander_cli.status_lines({"initiatives": [], "queue": []}) == ["no initiative is in flight", "", "the land queue is empty"]


def test_the_lander_group_is_in_the_command_table_with_its_four_commands() -> None:
    group, rows = cli._table_entry("lander")
    assert group.name == "lander"
    assert [row.name for row in rows] == ["once", "run", "status", "service"]
    parsed = cli.build_parser().parse_args(["lander", "run", "--interval", "5"])
    assert parsed.interval == 5.0 and parsed.profile is None
