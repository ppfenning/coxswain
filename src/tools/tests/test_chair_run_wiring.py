from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from agent_tools import chair_facts, chair_report, chair_resolve, chair_run, cli, host_cmd

# Fields the dataclass carries as plain metadata, not as wired sources: never callable, never unwired.
_FACTS_DEPS_METADATA_FIELDS = frozenset({"session", "pid", "host"})

# `chair_facts.gather_facts` reads every optional field through an `is not None` (or truthy) guard before
# falling back, `queue` and `stranded_records` included (chair_facts.py:300,303,308,322-331) — that guard shape
# is shared and does not by itself distinguish this pair. What distinguishes them is what `_chair_run_deps`
# (agent_tools/cli.py:5747-5776) actually wires: `queue` and `stranded_records` are the only two `FactsDeps`
# keywords it never passes there, so they sit at the dataclass's own `None` default, while every other optional
# field is passed a real callable, e.g. `missing_repos=missing_repos` (line 5760) and
# `history=lambda: chair_read_housekeeping.read_last_housekeeping(runs_dir)` (line 5774). That is a fact about
# what `_chair_run_deps` wires today, not an assumption: if a future change starts passing `queue=` or
# `stranded_records=`, the assertion below fails and names the field to drop from this set.
# `decomposed_intake` and `item_counts` are wired and so no longer listed here.
# `merge_queue` is wired: it returns the raw profile value, like `max_launches_per_hour`.
# `live_phases` is declared but not wired yet: the wiring task passes a reader and drops it from this set.
_FACTS_DEPS_NONE_GUARDED_FIELDS = frozenset(
    {"queue", "stranded_records", "live_phases"}
)

# Steward sources whose dataclass default is a working callable (`[]`, `""`, `False`), so only identity shows them unwired.
_FACTS_DEPS_STEWARD_SOURCES = ("steward_entries", "ticket_head", "level_merged")


def test_every_facts_deps_field_the_dataclass_declares_is_wired(tmp_path) -> None:
    """Iterates `FactsDeps`' own fields, so a fact added later is checked with no edit here.

    This replaces a hand-kept set of field names that let `remote_unfetched` and housekeeping ship unwired:
    a name missing from a hand-copied list is a name never checked.
    """
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    for f in dataclasses.fields(chair_facts.FactsDeps):
        value = getattr(deps.facts_deps, f.name)
        if f.name in _FACTS_DEPS_METADATA_FIELDS:
            continue
        if f.name in _FACTS_DEPS_NONE_GUARDED_FIELDS:
            # Checked, not skipped: today's real deps leave these `None`, which `gather_facts` guards for.
            assert value is None, f"{f.name} is no longer None; drop it from _FACTS_DEPS_NONE_GUARDED_FIELDS"
            continue
        assert callable(value), f"{f.name} is not callable: {value!r}"
        assert value is not None, f"{f.name} is None"
        assert not isinstance(value, cli._ChairUnwired), f"{f.name} is unwired: {value!r}"
    # A default is callable and not None, so the loop above accepts it: the steward sources must differ from it.
    declared = {f.name: f.default for f in dataclasses.fields(chair_facts.FactsDeps)}
    for name in _FACTS_DEPS_STEWARD_SOURCES:
        assert getattr(deps.facts_deps, name) is not declared[name], f"{name} is still the dataclass default"
    # `_ChairUnwired` is one sentinel shared by both bundles: `_chair_unwired_sources` (agent_tools/cli.py:5545-5552)
    # refuses to start while either `deps.facts_deps` or `deps.exec_deps` holds one. The replaced `TWELVE` set
    # named "run_id" and "record", both `exec_deps` fields, so checking `exec_deps` here too keeps that coverage
    # rather than narrowing it to `FactsDeps` alone.
    for f in dataclasses.fields(cli.chair_exec.Deps):
        exec_value = getattr(deps.exec_deps, f.name)
        assert not isinstance(exec_value, cli._ChairUnwired), f"exec_deps.{f.name} is unwired: {exec_value!r}"


def test_tick_calls_plan_with_the_tick_s_own_now() -> None:
    """`tick` takes `now` as its own argument and must hand that exact value to `plan`, not re-derive one."""
    calls: list[tuple] = []

    def fake_plan(facts, now):
        calls.append((facts, now))
        return []

    known_now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    deps = chair_run.RunDeps(
        facts_deps=object(),  # type: ignore[arg-type]
        exec_deps=object(),  # type: ignore[arg-type]
        report_deps=object(),  # type: ignore[arg-type]
        beat=lambda: None,
        current_epoch=lambda: 0,
        holds=lambda: False,
        release=lambda: None,
        sleep=lambda _seconds: None,
        now=lambda: known_now,
        gather=lambda facts_deps, now: {},  # type: ignore[arg-type,return-value]
        plan=fake_plan,
        perform=lambda actions, exec_deps, current_epoch, dry_run: [],
    )

    chair_run.tick(deps, False, known_now)

    assert len(calls) == 1
    assert calls[0][1] == known_now


def test_the_recorder_is_built_with_a_store_runner_and_the_lease_holder(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.chair_read_record, "recorder", lambda *args, **kwargs: seen.update(kwargs))
    cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert seen["holder"] == cli.chair.lease_holder("chair", 1, "h")
    assert callable(seen["store"])


def test_decomposed_intake_and_item_counts_are_provided(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert callable(deps.facts_deps.decomposed_intake)
    assert callable(deps.facts_deps.item_counts)


def _decomposed_workspace(tmp_path, monkeypatch, tasks: tuple[str, ...], intake_id: str = ""):
    """An initiative `cited` naming intake `cited.md`, one plain initiative, and `tasks` under `cited`; runs are faked."""
    for name, text in (("cited", "---\nintake: cited.md\n---\n"), ("plain", "---\nrepo: /x\n---\n")):
        (tmp_path / "work" / name / "p1").mkdir(parents=True)
        (tmp_path / "work" / name / "initiative.md").write_text(text, encoding="utf-8")
    for task in tasks:
        (tmp_path / "work" / "cited" / "p1" / f"{task}.md").write_text(f"---\nid: {task}\n---\nbody\n", encoding="utf-8")
    if intake_id:
        (tmp_path / "cited.md").write_text(f"---\nid: {intake_id}\n---\nbody\n", encoding="utf-8")
    seen: list[str] = []

    def fake_runs(runs_dir, intake, cache=None):
        seen.append(intake)
        return [SimpleNamespace(run_id="cited-1"), SimpleNamespace(run_id="cited-2")]

    monkeypatch.setattr(cli.chair_decompose_runs, "read_decompose_runs", fake_runs)
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    return deps, seen


def test_the_wired_facts_report_a_decomposed_initiative_with_no_tasks_exactly_once(tmp_path, monkeypatch) -> None:
    deps, _ = _decomposed_workspace(tmp_path, monkeypatch, ())
    counts, decomposed = deps.facts_deps.item_counts(), deps.facts_deps.decomposed_intake()  # type: ignore[misc]
    assert counts == {"cited": 0}
    assert chair_facts.empty_decompose_facts(decomposed, set(), counts) == [{"initiative": "cited", "run": "cited-2"}]


def test_an_initiative_with_tasks_is_counted_and_its_decompose_runs_are_never_read(tmp_path, monkeypatch) -> None:
    deps, seen = _decomposed_workspace(tmp_path, monkeypatch, ("a", "b"))
    counts, decomposed = deps.facts_deps.item_counts(), deps.facts_deps.decomposed_intake()  # type: ignore[misc]
    assert (counts, decomposed, seen) == ({"cited": 2}, {}, [])
    assert chair_facts.empty_decompose_facts(decomposed, set(), counts) == []


def test_decomposed_intake_is_keyed_by_the_initiative_when_the_intake_id_differs(tmp_path, monkeypatch) -> None:
    deps, seen = _decomposed_workspace(tmp_path, monkeypatch, (), intake_id="other-id")
    assert deps.facts_deps.decomposed_intake() == {"cited": "cited-2"}  # type: ignore[misc]
    assert seen == ["other-id"]


def test_newest_run_ids_takes_the_last_of_each_list_and_drops_an_empty_one() -> None:
    assert cli._newest_run_ids({"a": ["a-1", "a-2"], "b": [], "c": ["c-1"]}) == {"a": "a-2", "c": "c-1"}


def test_task_item_counts_counts_task_rows_per_initiative() -> None:
    tickets = [{"initiative": "a"}, {"initiative": "a", "kind": "task"}, {"initiative": "b", "kind": "intake"}]
    assert cli._task_item_counts(tickets, ["a", "empty"]) == {"a": 2, "empty": 0}


def test_initiative_intakes_keeps_only_initiatives_that_name_an_intake() -> None:
    texts = {Path("w/work/a/initiative.md"): "---\nintake: x.md\n---\n", Path("w/work/b/initiative.md"): "---\nrepo: r\n---\n"}
    assert cli._initiative_intakes(texts) == {"a": "x.md"}


def test_remote_unfetched_history_and_housekeeping_hours_are_wired_not_none(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert callable(deps.facts_deps.remote_unfetched)
    assert callable(deps.facts_deps.history)
    assert callable(deps.facts_deps.housekeeping_hours)


def test_remote_unfetched_is_wired_to_the_docket_s_initiative_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.chair_facts, "docket_by_land_at", lambda *a, **k: {"initiatives": [{"id": "alpha"}, {"id": "beta"}]})
    seen: dict = {}

    def fake_read(runs_dir, initiatives):
        seen["runs_dir"] = runs_dir
        seen["initiatives"] = list(initiatives)
        return {"alpha": "run-1"}

    monkeypatch.setattr(cli.chair_read_remote_unfetched, "read_remote_unfetched", fake_read)
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.remote_unfetched() == {"alpha": "run-1"}
    assert seen == {"runs_dir": runs, "initiatives": ["alpha", "beta"]}


def test_history_is_wired_to_read_last_housekeeping_at_runs_dir(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_history(runs_dir):
        seen["runs_dir"] = runs_dir
        return "2026-09-27T00:00:00Z"

    monkeypatch.setattr(cli.chair_read_housekeeping, "read_last_housekeeping", fake_history)
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.history() == "2026-09-27T00:00:00Z"
    assert seen == {"runs_dir": runs}


def test_housekeeping_hours_reads_the_profile_s_chair_namespace(tmp_path) -> None:
    runs = tmp_path / "runs"
    profile = {"chair": {"housekeeping_hours": 6}}
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.housekeeping_hours() == 6


def test_housekeeping_hours_is_none_with_no_chair_namespace_in_the_profile(tmp_path) -> None:
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.housekeeping_hours() is None


def test_idle_stall_calls_the_four_readers_and_reads_the_configured_minutes(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    backlog = {"free_lanes": 2, "ready": 1, "queued": 0, "last_progress_at": None, "empty_stubs": [], "blocked_ready": []}

    def fake_backlog(ws, mode, max_in_flight, now, *_held, **reused):
        seen["backlog"] = (ws, mode, now)
        seen["reused"] = sorted(reused)  # the round's intake, work items and live runs, not a second read of each
        return backlog

    monkeypatch.setattr(cli.chair_read_idle_backlog, "read_idle_backlog", fake_backlog)
    monkeypatch.setattr(cli.chair_read_idle_hosts, "read_idle_hosts", lambda runs_dir, hosts: [{"host": "h2", "ok": False, "detail": "x"}])
    monkeypatch.setattr(cli.chair_read_idle_lands, "read_lands_waiting", lambda runs_dir, now, read_checks=None: [])
    monkeypatch.setattr(cli.chair_read_idle_open, "read_idle_open", lambda ws, actions=None: ("host:h2", "text"))
    runs = tmp_path / "runs"
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    configured = cli._chair_run_deps(runs, {"chair": {"idle_stall_minutes": 20.0}}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    unset = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    inputs = configured.facts_deps.idle_stall(now)
    assert (inputs["stall_minutes"], inputs["free_lanes"], inputs["open_signature"], inputs["hosts"][0]["host"]) == (20, 2, "host:h2", "h2")
    assert seen["backlog"] == (tmp_path, "files", now)
    assert seen["reused"] == ["items", "land_at", "live", "queued"]
    assert unset.facts_deps.idle_stall(now)["stall_minutes"] == 15


def test_the_steward_stays_off_whatever_the_profile_sets_for_chair_steward(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()

    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    def deps(profile: dict) -> chair_facts.FactsDeps:
        return cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files").facts_deps

    # The steward is retired: the profile value still reaches the wired setting, where `steward_asked_for` reads it
    # for the doctor row, and it adds no Facts key.
    asked = deps({"chair": {"steward": True}})
    assert chair_facts.steward_asked_for(asked.steward_enabled()) is True
    assert chair_facts.resolve_steward_enabled(asked.steward_enabled()) is False
    assert "steward" not in chair_facts.gather_facts(asked, now)
    assert "steward" not in chair_facts.gather_facts(deps({}), now)
    # The malformed and empty forms are cheap to check on the wired setting alone.
    for profile in ({"chair": {}}, {"chair": {"steward": "banana"}}):
        assert chair_facts.steward_asked_for(deps(profile).steward_enabled()) is False
        assert chair_facts.resolve_steward_enabled(deps(profile).steward_enabled()) is False


def test_a_profile_chair_steward_scope_reaches_the_facts_deps(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()

    def scope(profile: dict) -> object:
        return cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files").facts_deps.steward_scope()

    assert (scope({"chair": {"steward_scope": "epics"}}), scope({})) == ("epics", None)


def test_harness_python_is_the_configured_harness_venv(tmp_path) -> None:
    runs = tmp_path / "runs"
    profile = {"harness_dir": str(tmp_path / "harness")}
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.exec_deps.harness_python == str(Path(profile["harness_dir"]) / ".venv" / "bin" / "python")


def test_harness_python_falls_back_to_the_bare_interpreter_with_no_harness_dir(tmp_path) -> None:
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.exec_deps.harness_python == "python"


def test_the_dispatch_facts_offer_the_lane_hosts_named_in_the_profile_file(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: host-a\n    ssh: host-a\n    workspace_dir: /w\n", encoding="utf-8")
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {  # type: ignore[misc]
        "max_in_flight": 3, "live_runs": 0, "hosts": [{"name": "host-a", "live_runs": 0, "capabilities": []}],
    }


HOSTS_DDL = ("CREATE TABLE hosts (name TEXT PRIMARY KEY, ssh TEXT, capacity INTEGER, state TEXT, beat_at TEXT,"
             " versions_json TEXT, updated_at TEXT, updated_by TEXT)")
BEAT = '{"login_ok": true, "workspace_dir": "/srv/ws"}'


def _hosts_table(runs, *rows) -> None:
    """The graphs-hosts-table columns; each row is (name, ssh, capacity, state, versions_json)."""
    runs.mkdir()
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute(HOSTS_DDL)
    conn.executemany(
        "INSERT INTO hosts VALUES (?, ?, ?, ?, '2026-09-26T11:58:00Z', ?, '2026-09-26T11:58:00Z', 'chair')", rows,
    )
    conn.commit()
    conn.close()


def _cartridge(root: Path, name: str, text: str) -> None:
    (root / name).mkdir()
    (root / name / "cartridge.yaml").write_text(text, encoding="utf-8")


def test_a_missing_local_host_row_is_registered_before_dispatch_with_the_cartridge_cap(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    _hosts_table(runs)
    _cartridge(tmp_path, "example-team", "policy:\n  dispatch:\n    max_in_flight: 8\n")
    profile = {"cartridges_dir": str(tmp_path), "team": "example-team"}
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.store_cli, "runner", lambda runs_dir: (lambda argv: (calls.append(argv), (0, "{}"))[1]))

    cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    assert calls == [host_cmd.local_host_add_argv("h", 8, "chair")]


def test_a_local_host_row_already_present_writes_nothing(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    _hosts_table(runs, ("h", "h", 5, "active", BEAT))
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.store_cli, "runner", lambda runs_dir: (lambda argv: (calls.append(argv), (0, "{}"))[1]))

    cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    assert calls == []


def test_a_local_host_row_capacity_is_the_free_lane_cap_not_the_cartridge_max(tmp_path) -> None:
    runs = tmp_path / "runs"
    _hosts_table(runs, ("h", "h", 2, "active", BEAT))
    _cartridge(tmp_path, "example-team", "policy:\n  dispatch:\n    max_in_flight: 8\n")
    profile = {"cartridges_dir": str(tmp_path), "team": "example-team"}

    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    assert deps.facts_deps.docket()["max_in_flight"] == 2  # type: ignore[misc]


def test_the_dispatch_facts_name_only_the_active_table_hosts_when_the_table_has_rows(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: other\n    ssh: other\n    workspace_dir: /w\n", encoding="utf-8")
    runs = tmp_path / "runs"
    _hosts_table(runs, ("host-a", "host-a", 8, "active", BEAT), ("pi", "pi", 2, "draining", BEAT))
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {  # type: ignore[misc]
        "max_in_flight": 3, "live_runs": 0,
        "hosts": [{"name": "host-a", "live_runs": 0, "capacity": 8, "capabilities": []}],
    }


def test_the_dispatch_facts_carry_weight_and_capabilities_from_the_hosts_row(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute(HOSTS_DDL.replace("updated_by TEXT)", "updated_by TEXT, weight INTEGER, capabilities TEXT)"))
    conn.execute(
        "INSERT INTO hosts VALUES ('host-a', 'host-a', 8, 'active', '2026-09-26T11:58:00Z', ?, '2026-09-26T11:58:00Z', 'chair', 2, ?)",
        (BEAT, '["go"]'),
    )
    conn.commit()
    conn.close()
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3})["hosts"] == [  # type: ignore[misc]
        {"name": "host-a", "live_runs": 0, "capacity": 8, "weight": 2, "capabilities": ["go"]}
    ]


def test_an_active_table_host_with_no_workspace_dir_is_not_dispatched_and_the_chair_says_why(tmp_path, monkeypatch) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: other\n    ssh: other\n    workspace_dir: /w\n", encoding="utf-8")
    runs = tmp_path / "runs"
    _hosts_table(runs, ("host-a", "host-a", 8, "active", BEAT), ("fresh", "fresh", 4, "active", None))
    # "h" (this test's local host) carries no row here, so `_chair_run_deps` now tries to register it;
    # stub the write to succeed so that attempt adds no line to `said`, which asserts an exact list below.
    monkeypatch.setattr(cli.store_cli, "runner", lambda runs_dir: (lambda argv: (0, "{}")))
    said: list[str] = []
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, said.append, profile, "files")
    assert [h["name"] for h in deps.facts_deps.dispatch({"max_in_flight": 3})["hosts"]] == ["host-a"]  # type: ignore[misc]
    assert said == [
        "chair run: not dispatching to fresh: no workspace_dir; run `cox host beat` on each",
        "chair run: profile lane_hosts not in the hosts table are not lane hosts while it has rows: other;"
        " add them with `cox host add`",
    ]


def test_the_local_hosts_own_row_is_never_a_dispatch_target_and_its_workspace_dir_resolves_not_refuses(tmp_path) -> None:
    # "h" (this test's local host) carries no versions_json, so with no fix it would land in the startup
    # `unreachable` list; the routing profile's own top-level `workspace_dir` resolves it instead.
    runs = tmp_path / "runs"
    _hosts_table(runs, ("host-a", "host-a", 8, "active", BEAT), ("h", "h", 4, "active", None))
    profile = {"workspace_dir": "/srv/ws"}
    said: list[str] = []
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, said.append, tmp_path / "profile.yaml", "files")
    assert [h["name"] for h in deps.facts_deps.dispatch({"max_in_flight": 3})["hosts"]] == ["host-a"]  # type: ignore[misc]
    assert said == []


def test_the_dispatch_facts_fall_back_to_the_profile_host_when_the_table_has_no_rows(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: other\n    ssh: other\n    workspace_dir: /w\n", encoding="utf-8")
    runs = tmp_path / "runs"
    _hosts_table(runs)
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3})["hosts"] == [  # type: ignore[misc]
        {"name": "other", "live_runs": 0, "capabilities": []}
    ]


def test_a_live_pidfile_run_with_no_store_lane_still_counts_as_a_local_lane(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r1.pid").write_text("123", encoding="utf-8")
    monkeypatch.setattr("agent_tools.epic.run_live", lambda *args, **kwargs: True)
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {"max_in_flight": 3, "live_runs": 1, "hosts": []}  # type: ignore[misc]


def test_lanes_on_an_unlisted_host_and_live_pidfiles_all_count_as_local() -> None:
    lanes = [cli.run_store.Lane("r1", host, "t", "t") for host in ("host-a", "elsewhere", "host-b")]
    assert cli._dispatch_counts(lanes, "host-b", ["host-a"], 2) == {"": 4, "host-a": 1}


def test_live_lanes_count_per_host_with_the_local_machine_under_the_empty_name() -> None:
    lanes = [cli.run_store.Lane("r1", host, "t", "t") for host in (None, "host-b", "host-a")]
    assert cli._live_by_host(lanes, "host-b") == {"": 2, "host-a": 1}


def test_the_deps_weekly_reader_passes_a_store_spend_that_reads_the_store_from_since(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *args, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(cli.run_store, "cost_since", lambda runs_dir, since: (runs_dir, since))
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    deps.facts_deps.weekly()
    assert seen["store_spend"]("2026-08-29") == (runs, "2026-08-29")
    assert seen["reset"] is None


def test_the_deps_weekly_reader_passes_the_profiles_parsed_weekly_reset(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *args, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(cli.run_store, "cost_since", lambda runs_dir, since: (runs_dir, since))
    runs = tmp_path / "runs"
    profile = {"weekly_reset": "Sun 04:00 America/New_York"}
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    deps.facts_deps.weekly()
    assert seen["reset"] == cli.usage_window.parse_weekly_reset("Sun 04:00 America/New_York")


class _FakeStore:
    """A store whose lease row names one holder; each renewal by that holder moves the heartbeat counter."""

    def __init__(self, holder: str) -> None:
        self.holder = holder
        self.heartbeat = 0

    def renew(self, holder: str, epoch: int):
        if holder != self.holder:
            return cli.chair.store_cli.LeaseRefused(epoch + 1, self.holder)
        self.heartbeat += 1
        return cli.chair.store_cli.LeaseGranted(epoch, holder)


_OLD = "2020-01-01T00:00:00+00:00"


def _chair_on_disk(tmp_path, monkeypatch, store_holder: str):
    """A record file with an old heartbeat, a lease sidecar for chair-x@h:1, and deps whose tick stops after the beat."""
    runs = tmp_path / "runs"
    runs.mkdir()
    store = _FakeStore(store_holder)
    monkeypatch.setattr(cli.chair.store_cli, "lease_renew", lambda runs_dir, name, who, epoch, ttl: store.renew(who, epoch))
    (runs / cli.chair.LEASE_FILENAME).write_text(json.dumps({"holder": "chair-x@h:1", "epoch": 3}), encoding="utf-8")
    cli.chair.write(runs, {"session": "chair-x", "pid": 1, "host": "h", "taken_at": _OLD, "heartbeat_at": _OLD, "runs": [], "claude_session": None})
    deps = cli._chair_run_deps(runs, {}, "chair-x", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    def stop_after_beat(*_args):
        raise RuntimeError("beat done")

    quiet = dataclasses.replace(deps.report_deps, echo=lambda line: None)
    return runs, store, dataclasses.replace(deps, gather=stop_after_beat, report_deps=quiet)


def test_one_loop_tick_moves_the_store_lease_heartbeat_and_the_record_file_heartbeat(tmp_path, monkeypatch) -> None:
    runs, store, deps = _chair_on_disk(tmp_path, monkeypatch, "chair-x@h:1")

    chair_run.run(True, 0, False, deps)

    assert store.heartbeat == 1
    assert cli.chair.read(runs)["heartbeat_at"] > _OLD


def test_a_tick_that_lost_the_lease_leaves_the_record_file_untouched(tmp_path, monkeypatch) -> None:
    runs, store, deps = _chair_on_disk(tmp_path, monkeypatch, "other@h:9")
    before = cli.chair.chair_path(runs).read_bytes()

    chair_run.run(True, 0, False, deps)

    assert store.heartbeat == 0
    assert cli.chair.chair_path(runs).read_bytes() == before


def test_a_beat_during_an_active_takeover_until_still_advances_the_record_file_heartbeat(tmp_path, monkeypatch) -> None:
    """The lease sidecar already names this loop (chair-x@h:1) -- reclaimed, e.g., by a take_lease action after an
    expired takeover -- but the record file still names the prior holder, with a takeover `until` still ahead.
    A tick's beat must still move heartbeat_at forward for the holder the lease actually names."""
    runs = tmp_path / "runs"
    runs.mkdir()
    store = _FakeStore("chair-x@h:1")
    monkeypatch.setattr(cli.chair.store_cli, "lease_renew", lambda runs_dir, name, who, epoch, ttl: store.renew(who, epoch))
    (runs / cli.chair.LEASE_FILENAME).write_text(json.dumps({"holder": "chair-x@h:1", "epoch": 3}), encoding="utf-8")
    cli.chair.write(runs, {
        "session": "alice", "pid": 9, "host": "h", "taken_at": _OLD, "heartbeat_at": _OLD,
        "runs": [], "claude_session": None, "until": "2099-01-01T00:00:00+00:00",
    })
    deps = cli._chair_run_deps(runs, {}, "chair-x", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    def stop_after_beat(*_args):
        raise RuntimeError("beat done")

    quiet = dataclasses.replace(deps.report_deps, echo=lambda line: None)
    deps = dataclasses.replace(deps, gather=stop_after_beat, report_deps=quiet)

    chair_run.run(True, 0, False, deps)

    assert store.heartbeat == 1
    assert cli.chair.read(runs)["heartbeat_at"] > _OLD


def _stranded_workspace_with_a_missing_repo(tmp_path):
    """A `runs`/`work` pair whose one live, approved, unlanded record names a repo that does not exist."""
    runs = tmp_path / "runs"
    missing_repo = str(tmp_path / "gone")
    tasks = runs / "r1" / "tasks" / "p1"
    tasks.mkdir(parents=True)
    record = {
        "run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
        "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
        "landed": False, "repo": missing_repo,
    }
    (tasks / "t1.json").write_text(json.dumps(record), encoding="utf-8")
    work = tmp_path / "work" / "acme" / "p1"
    work.mkdir(parents=True)
    (work / "t1.md").write_text("---\nid: t1\nstate: ready\n---\nbody\n", encoding="utf-8")
    (tmp_path / "work" / "acme" / "initiative.md").write_text("---\nrepo: /unused\n---\nbody\n", encoding="utf-8")
    return runs, missing_repo


def test_a_missing_repo_is_reported_on_the_first_tick_and_not_the_second(tmp_path) -> None:
    runs, missing_repo = _stranded_workspace_with_a_missing_repo(tmp_path)
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    first_missing = deps.facts_deps.missing_repos()  # type: ignore[misc]
    first_reported = deps.facts_deps.reported_repos()  # type: ignore[misc]
    assert cli.chair_facts.new_missing_repos(first_missing, first_reported) == [missing_repo]

    second_missing = deps.facts_deps.missing_repos()  # type: ignore[misc]
    second_reported = deps.facts_deps.reported_repos()  # type: ignore[misc]
    assert cli.chair_facts.new_missing_repos(second_missing, second_reported) == []


def test_the_record_file_is_beaten_only_by_a_live_run_with_no_refusal() -> None:
    assert [
        cli._record_beat_wanted(False, ""),
        cli._record_beat_wanted(False, "chair: held by other@h:9 (store lease)"),
        cli._record_beat_wanted(True, ""),
    ] == [True, False, False]


def test_the_real_deps_read_run_exits_from_the_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.chair_read_exits, "exits", lambda runs_dir: {"i": True, "runs": str(runs_dir)})
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.run_exited() == {"i": True, "runs": str(runs)}  # type: ignore[misc]


def _tick_status_deps(tmp_path, writer, recorded: list, echoed: list, now: datetime) -> chair_run.RunDeps:
    return chair_run.RunDeps(
        facts_deps=object(),  # type: ignore[arg-type]
        exec_deps=SimpleNamespace(record=recorded.append),  # type: ignore[arg-type]
        report_deps=chair_report.Deps(echo=echoed.append),
        beat=lambda: None,
        current_epoch=lambda: 7,
        holds=lambda: False,
        release=lambda: None,
        sleep=lambda _seconds: None,
        now=lambda: now,
        gather=lambda facts_deps, at: {},  # type: ignore[arg-type,return-value]
        plan=lambda facts, at: [],
        perform=lambda actions, exec_deps, current_epoch, dry_run: [],
        meter_doc=lambda: None,
        runs_dir=tmp_path,
        read_action=lambda runs_dir: None,
        write_tick_status=writer,
    )


def test_a_tick_writes_its_status_line_and_now_to_the_lease_once_under_the_held_epoch(tmp_path) -> None:
    now = datetime(2026, 10, 5, 12, 30, 15, tzinfo=UTC)
    calls: list[tuple] = []
    echoed: list = []
    deps = _tick_status_deps(tmp_path, lambda *args: calls.append(args) or True, [], echoed, now)

    chair_run._attempt(deps, False)

    assert len(calls) == 1
    runs_dir, epoch, text = calls[0]
    assert (runs_dir, epoch) == (tmp_path, 7)
    assert json.loads(text) == {"tick_at": "2026-10-05T12:30:15Z", "status": echoed[1], "current_action": None}
    assert echoed[1].strip()


def test_a_dry_run_tick_writes_no_tick_status(tmp_path) -> None:
    calls: list[tuple] = []
    now = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)
    deps = _tick_status_deps(tmp_path, lambda *args: calls.append(args) or True, [], [], now)

    chair_run._attempt(deps, True)

    assert calls == []


def test_a_writer_returning_false_neither_stops_the_tick_nor_skips_the_local_record(tmp_path) -> None:
    calls: list[tuple] = []
    recorded: list = []
    echoed: list = []
    now = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)
    deps = _tick_status_deps(tmp_path, lambda *args: calls.append(args) or False, recorded, echoed, now)

    chair_run._attempt(deps, False)

    assert len(calls) == 1
    assert [r["kind"] for r in recorded] == ["status"]
    assert recorded[0]["line"] == echoed[1]


def test_a_writer_that_raises_does_not_stop_the_tick(tmp_path) -> None:
    def boom(*_args) -> bool:
        raise RuntimeError("store down")

    recorded: list = []
    echoed: list = []
    deps = _tick_status_deps(tmp_path, boom, recorded, echoed, datetime(2026, 10, 5, 12, 30, tzinfo=UTC))

    chair_run._attempt(deps, False)

    assert len(recorded) == 1
    assert len(echoed) == 2


def test_a_live_run_wires_release_requested_and_a_dry_run_leaves_it_none(tmp_path) -> None:
    live = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    dry = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", True, print, tmp_path / "profile.yaml", "files")

    assert callable(live.release_requested)
    assert dry.release_requested is None


def _fake_git(head: str, branch: str = "refs/heads/main"):
    def run(args: list[str]) -> str | None:
        return head if "rev-parse" in args else branch

    return run


def _deps(tmp_path, dry_run: bool) -> chair_run.RunDeps:
    return cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", dry_run, print, tmp_path / "profile.yaml", "files")


def test_the_built_deps_carry_the_checkout_read_once_at_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_chair_git", _fake_git("abc123\n"))
    deps = _deps(tmp_path, True)

    assert deps.started_checkout == cli.chair_restart.CheckoutState(head="abc123", on_branch=True)

    monkeypatch.setattr(cli, "_chair_git", _fake_git("def456\n"))
    assert deps.started_checkout.head == "abc123"
    assert deps.checkout_state().head == "def456"


def test_an_unreadable_checkout_leaves_started_checkout_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_chair_git", lambda _args: None)
    deps = _deps(tmp_path, True)

    assert deps.started_checkout is None and deps.checkout_state() is None


def test_the_git_edge_returns_none_on_failure_and_stdout_on_success(tmp_path) -> None:
    assert cli._chair_git(["-C", str(tmp_path / "missing"), "rev-parse", "HEAD"]) is None
    assert cli._chair_git(["--version"]).startswith("git version")


def test_a_dry_run_keeps_the_default_lands_in_flight(tmp_path) -> None:
    assert _deps(tmp_path, True).lands_in_flight is chair_run.no_lands_in_flight


def test_a_live_chairs_lands_in_flight_reflects_the_land_worker(tmp_path) -> None:
    deps = _deps(tmp_path, False)
    release = threading.Event()
    assert deps.lands_in_flight() is False

    deps.exec_deps.lands.submit({"repo": "r"}, lambda: release.wait(5) and {"action": {}, "status": "done", "reason": ""})
    assert deps.lands_in_flight() is True

    release.set()
    deadline = time.monotonic() + 5
    while deps.lands_in_flight() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert deps.lands_in_flight() is False


def _loop_on_disk(tmp_path, monkeypatch, *, release: bool):
    """A live loop `chair-x` (pid 1, host h) with a record and a lease sidecar; optionally a takeover's release request stamped on the record."""
    runs = tmp_path / "runs"
    runs.mkdir()
    store = _FakeStore("chair-x@h:1")
    monkeypatch.setattr(cli.chair.store_cli, "lease_renew", lambda runs_dir, name, who, epoch, ttl: store.renew(who, epoch))
    monkeypatch.setattr(cli.chair.store_cli, "lease_release", lambda runs_dir, name, who, epoch: cli.chair.store_cli.LeaseReleased())
    (runs / cli.chair.LEASE_FILENAME).write_text(json.dumps({"holder": "chair-x@h:1", "epoch": 3}), encoding="utf-8")
    record = {"session": "chair-x", "pid": 1, "host": "h", "taken_at": _OLD, "heartbeat_at": _OLD, "runs": [], "claude_session": None}
    cli.chair.write(runs, cli.chair.request_release(record, "taker", datetime.now(UTC)) if release else record)
    deps = cli._chair_run_deps(runs, {}, "chair-x", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    quiet = dataclasses.replace(deps.report_deps, echo=lambda line: None)
    return runs, store, dataclasses.replace(deps, report_deps=quiet)


def test_a_tick_honours_a_stamped_release_request_and_drops_the_lease_and_the_lock(tmp_path, monkeypatch) -> None:
    runs, _store, deps = _loop_on_disk(tmp_path, monkeypatch, release=True)

    line = chair_run.tick(deps, False, datetime.now(UTC))

    assert "released at takeover request" in line
    assert not (runs / cli.chair.LEASE_FILENAME).exists()
    assert not cli.chair.chair_path(runs).exists()
    assert cli.chair.read(runs) is None


def test_a_takeover_succeeds_inside_the_grace_once_the_loop_releases_on_its_tick(tmp_path, monkeypatch) -> None:
    runs, _store, deps = _loop_on_disk(tmp_path, monkeypatch, release=False)
    monkeypatch.setattr(cli.chair.store_cli, "lease_acquire", lambda runs_dir, name, who, ttl, steal: cli.chair.store_cli.LeaseGranted(4, who))
    forced: list[str] = []
    real_release = cli.chair.release_lease
    monkeypatch.setattr(cli.chair, "release_lease", lambda runs_dir, session, pid, host: (forced.append(session), real_release(runs_dir, session, pid, host)))
    now = [1000.0]
    ticks: list[str] = []

    def fake_sleep(seconds: float) -> None:
        now[0] += seconds
        ticks.append(chair_run.tick(deps, False, datetime.now(UTC)))

    ok, message = chair_resolve.takeover(
        runs, "taker", 2, "h", None, None, heartbeat_minutes=10, clock=lambda: now[0], sleep=fake_sleep,
    )

    assert ok, message
    assert now[0] - 1000.0 < chair_resolve.TAKEOVER_GRACE_SECONDS
    assert ["released at takeover request" in t for t in ticks] == [True]
    assert forced == ["chair-x"]  # the loop's own release inside the tick; the fallback found no record and released nothing
    record = cli.chair.read(runs)
    assert record["session"] == "taker"
    assert record["taken_over_from"] == "chair-x"


def test_a_tick_with_no_release_request_still_beats_and_keeps_the_lease(tmp_path, monkeypatch) -> None:
    runs, store, deps = _loop_on_disk(tmp_path, monkeypatch, release=False)

    def stop_after_beat(*_args):
        raise RuntimeError("beat done")

    chair_run.run(True, 0, False, dataclasses.replace(deps, gather=stop_after_beat))

    assert store.heartbeat == 1
    assert (runs / cli.chair.LEASE_FILENAME).exists()
    assert cli.chair.read(runs)["heartbeat_at"] > _OLD


_DASH = "/w/coxswain-dash"
_REBUILD = [
    ["git", "-C", _DASH, "pull", "--ff-only"],
    ["cargo", "build", "--release", "--manifest-path", f"{_DASH}/Cargo.toml"],
    ["ln", "-sf", f"{_DASH}/target/release/towpath", "/b/towpath"],
    ["ln", "-sf", f"{_DASH}/target/release/coxtop", "/b/coxtop"],
]


def _landed(repo: str, commit: str = "c1") -> dict:
    return {
        "action": {"kind": "land", "repo": repo}, "status": "landed",
        "reason": "pr_create: https://x/y/pull/3\n", "commit": commit,
    }


def _rebuild_wrapper(tmp_path, results: list, run, echo=lambda _l: None):
    return cli._chair_perform_with_smoke(
        tmp_path, lambda *_a: results, dash_dir=_DASH, bin_dir="/b", toolchain_found=True, run=run, echo=echo,
    )


def test_a_landed_dash_checkout_runs_the_pull_build_and_link_argvs_once(tmp_path) -> None:
    seen: list[list[str]] = []
    results = [_landed(_DASH, "a"), _landed(_DASH, "b")]

    _rebuild_wrapper(tmp_path, results, lambda argv: seen.append(argv) or (0, ""))([], object(), lambda: 1, False)

    assert seen == _REBUILD


def test_a_landed_coxswain_checkout_runs_no_rebuild(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.chair_smoke, "run_smoke_commands", lambda _repo: [])
    seen: list[list[str]] = []

    _rebuild_wrapper(tmp_path, [_landed("/w/coxswain")], lambda argv: seen.append(argv) or (0, ""))(
        [], object(), lambda: 1, False,
    )

    assert seen == []


def test_a_dry_run_runs_no_rebuild(tmp_path) -> None:
    seen: list[list[str]] = []

    _rebuild_wrapper(tmp_path, [_landed(_DASH)], lambda argv: seen.append(argv) or (0, ""))([], object(), lambda: 1, True)

    assert seen == []


def test_a_wrapper_with_no_dash_inputs_plans_nothing(tmp_path, monkeypatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_argv", lambda argv: ran.append(argv) or (0, "", ""))
    results = [_landed(_DASH)]

    assert cli._chair_perform_with_smoke(tmp_path, lambda *_a: results)([], object(), lambda: 1, False) == results
    assert ran == []


def test_a_failing_rebuild_is_one_echoed_line_and_the_smoke_still_runs(tmp_path, monkeypatch) -> None:
    smoked: list[str] = []
    monkeypatch.setattr(cli.chair_smoke, "run_smoke_commands", lambda repo: smoked.append(repo) or [])
    lines: list[str] = []
    results = [_landed(_DASH), _landed("/w/coxswain-tools")]

    got = _rebuild_wrapper(tmp_path, results, lambda _argv: (1, "boom"), lines.append)([], object(), lambda: 1, False)

    assert got == results
    assert lines == [f"chair run: towpath rebuild failed (exit 1) for {_DASH}"]
    assert smoked == ["/w/coxswain-tools"]
    assert cli.chair_smoke.read_hold(str(tmp_path)) is None


def test_a_runner_that_raises_does_not_raise_out_of_the_wrapper(tmp_path) -> None:
    def explode(_argv: list[str]) -> tuple[int, str]:
        raise OSError("no cargo")

    lines: list[str] = []
    results = [_landed(_DASH)]

    assert _rebuild_wrapper(tmp_path, results, explode, lines.append)([], object(), lambda: 1, False) == results
    assert lines == [f"chair run: towpath rebuild failed for {_DASH}: no cargo"]


def _wired_kwargs(tmp_path, monkeypatch, branch: str) -> dict:
    wired: dict = {}
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(cli, "_chair_git", _fake_git("abc\n", branch=branch))
    monkeypatch.setattr(cli, "_chair_perform_with_smoke", lambda _runs, _perform, **kw: wired.update(kw))
    _deps(tmp_path, False)
    return wired


def test_a_development_install_passes_the_dash_inputs(tmp_path, monkeypatch) -> None:
    wired = _wired_kwargs(tmp_path, monkeypatch, "refs/heads/main")

    assert wired["dash_dir"] == str(cli.chair_restart.checkout_root().parent / "coxswain-dash")
    assert (wired["bin_dir"], wired["toolchain_found"]) == ("/opt/bin", True)


def test_a_detached_checkout_passes_no_dash_inputs(tmp_path, monkeypatch) -> None:
    wired = _wired_kwargs(tmp_path, monkeypatch, "")

    assert "dash_dir" not in wired


def _live_deps(tmp_path, profile: dict | None = None, dry_run: bool = False) -> chair_run.RunDeps:
    return cli._chair_run_deps(tmp_path / "runs", profile or {}, "chair", 1, "h", dry_run, print, tmp_path / "profile.yaml", "files")


_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
_REAL_CARTRIDGES = Path(__file__).resolve().parents[2] / "cartridges"  # src/cartridges, the real `local` team cartridge
_AGENT_CHECKS = ("cargo fmt --check", "cargo test")


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_GIT_ENV)
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


def _steward_repo(tmp_path: Path, initiative: str = "epic-x") -> Path:
    """A real git repository with `.agent-checks`, recorded as `initiative`'s repo in the workspace `tmp_path`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / ".agent-checks").write_text("\n".join(_AGENT_CHECKS) + "\n", encoding="utf-8")
    _commit(repo, "a.txt")
    (tmp_path / "work" / initiative).mkdir(parents=True)
    (tmp_path / "work" / initiative / "initiative.md").write_text(f"---\nrepo: {repo}\n---\n", encoding="utf-8")
    return repo


def test_a_live_bundle_wires_the_steward_executor_and_its_checks_from_the_cartridge_and_agent_checks(tmp_path) -> None:
    repo = _steward_repo(tmp_path)
    profile = {"team": "local", "cartridges_dir": str(_REAL_CARTRIDGES / "cartridges"), "skills_roots": [str(_REAL_CARTRIDGES / "skills-plugins")]}
    for dry_run in (False, True):
        deps = _live_deps(tmp_path, profile, dry_run)
        assert deps.exec_deps.steward_exec is not None
        assert deps.exec_deps.steward_checks({"kind": "run_gate", "epic": "epic-x"}) == [
            "ruff check .", "pytest -q", *_AGENT_CHECKS,
        ]
    assert deps.exec_deps.steward_checks({"kind": "run_gate", "epic": "no-such-epic"}) == []
    assert repo.is_dir()


def test_a_run_gate_that_cannot_resolve_its_cartridge_raises_and_a_merge_level_does_not(tmp_path) -> None:
    _steward_repo(tmp_path)
    profile = {"team": "no-such-team", "cartridges_dir": str(_REAL_CARTRIDGES / "cartridges"), "skills_roots": []}
    checks = _live_deps(tmp_path, profile).exec_deps.steward_checks
    assert checks({"kind": "merge_level", "epic": "epic-x"}) == list(_AGENT_CHECKS)
    try:
        checks({"kind": "run_gate", "epic": "epic-x"})
    except RuntimeError as exc:
        assert "cartridge unresolved" in str(exc)
    else:
        raise AssertionError("a gate on short checks must not pass")


def test_the_wired_steward_runs_a_gate_in_a_temporary_worktree_of_the_ticket_branch(tmp_path) -> None:
    repo = _steward_repo(tmp_path)
    _git(repo, "branch", "pr/epic-x")
    run = _live_deps(tmp_path).exec_deps.steward_exec
    assert run is not None
    passed = run({"kind": "run_gate", "epic": "epic-x", "fields": {"k": "v"}}, ("test -f a.txt",))
    failed = run({"kind": "run_gate", "epic": "epic-x", "fields": {}}, ("exit 1",))
    assert (passed["event"], passed["fields"], failed["event"]) == ("gate_passed", {"k": "v"}, "gate_failed")
    assert not cli._land_worktree(repo, "pr/epic-x").exists()
    assert "pr/epic-x" in _git(repo, "branch", "--list", "pr/epic-x")


def test_steward_phase_of_indexes_the_phases_in_natural_order() -> None:
    rows = [{"kind": "task", "initiative": i, "phase": p} for i, p in (("e", "p10"), ("e", "p2"), ("e", "p1"), ("o", "z"))]
    phase_of = cli._steward_phase_of(lambda: rows)
    assert [phase_of("e", n) for n in range(3)] == ["p1", "p2", "p10"]


def test_level_merged_and_ticket_head_read_a_real_git_repository(tmp_path) -> None:
    repo = _steward_repo(tmp_path)
    (tmp_path / "work" / "no-branch").mkdir()
    (tmp_path / "work" / "no-branch" / "initiative.md").write_text(f"---\nrepo: {repo}\n---\n", encoding="utf-8")
    _git(repo, "branch", "pr/epic-x")
    _git(repo, "checkout", "-b", "epic/epic-x/p1")
    _commit(repo, "level.txt")
    _git(repo, "checkout", "pr/epic-x")
    facts = _live_deps(tmp_path).facts_deps

    assert facts.ticket_head("epic-x") == _git(repo, "rev-parse", "pr/epic-x")
    assert (facts.ticket_head("no-branch"), facts.ticket_head("no-initiative")) == ("", "")
    assert facts.level_merged("epic-x", "p1") is False
    _git(repo, "merge", "--no-ff", "-m", "merge level", "epic/epic-x/p1")
    assert facts.level_merged("epic-x", "p1") is True
    assert facts.ticket_head("epic-x") == _git(repo, "rev-parse", "pr/epic-x")
    assert facts.level_merged("no-initiative", "p1") is False


def test_the_wired_steward_ports_resolve_a_target_branch_for_a_recorded_repo_and_an_empty_one_without(tmp_path) -> None:
    repo = _steward_repo(tmp_path)
    (tmp_path / "work" / "no-repo").mkdir()
    (tmp_path / "work" / "no-repo" / "initiative.md").write_text("---\ntitle: t\n---\n", encoding="utf-8")
    home = os.path.expanduser("~")
    target = cli._steward_target_branch_of({}, tmp_path, cli._repo_for(tmp_path, home))
    assert (target("epic-x"), target("no-repo"), target("no-initiative")) == ("main", "", "")

    run = _live_deps(tmp_path).exec_deps.steward_exec
    assert run is not None
    missing = run({"kind": "merge_level", "epic": "no-repo", "level": 0}, ())
    assert missing["event"] == "merge_failed" and "no target branch is known" in missing["reason"]
    run({"kind": "merge_level", "epic": "epic-x", "level": 0}, ())
    assert _git(repo, "rev-parse", "pr/epic-x") == _git(repo, "rev-parse", "main")


def test_steward_entries_reads_the_courier_bus_in_the_workspace(tmp_path) -> None:
    facts = _live_deps(tmp_path).facts_deps
    assert list(facts.steward_entries()) == []
    (tmp_path / "courier.jsonl").write_text(
        json.dumps({"id": "m1", "topic": "epic-x", "event": "relaunched", "fields": {"level": "0"}}) + "\n", encoding="utf-8"
    )
    assert [e["event"] for e in facts.steward_entries()] == ["relaunched"]


def _tracked_caches(monkeypatch) -> list:
    made: list = []

    class Tracked(cli.chair_read_cache.StampCache):
        def __init__(self, max_entries: int) -> None:
            super().__init__(max_entries)
            made.append(self)

    monkeypatch.setattr(cli.chair_read_cache, "StampCache", Tracked)
    return made


def test_the_first_work_files_read_after_a_restart_is_served_from_the_saved_cache(tmp_path, monkeypatch) -> None:
    task = tmp_path / "work" / "init" / "p1" / "t1.md"
    task.parent.mkdir(parents=True)
    task.write_text("---\nid: t1\n---\nbody\n", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    saved = cli.chair_read_cache.StampCache(512)
    cached_text = "---\nid: t1\n---\ntext only the saved cache holds\n"
    saved.get(task, cli.chair_read_cache.file_stamp(task), lambda: cached_text)
    cli.chair_read_cache.save_cache(saved, tmp_path / "runs" / "chair-cache.json")
    made = _tracked_caches(monkeypatch)
    real_read = cli.chair_read_quarantined.read_work_files
    returned: list = []

    def spy(root, cache=None):
        returned.append((cache, real_read(root, cache)))
        return returned[-1][1]

    monkeypatch.setattr(cli.chair_read_quarantined, "read_work_files", spy)

    deps = _live_deps(tmp_path)
    assert deps.facts_deps.item_counts() == {"init": 1}  # type: ignore[misc]

    files_cache = made[0]
    assert returned[0][0] is files_cache
    assert returned[0][1][task] == cached_text  # the disk text differs, so no read callback fired
    assert (files_cache.hits, files_cache.misses) == (1, 0)


def test_save_cache_writes_the_work_and_store_files_and_keeps_an_unread_decompose_file(tmp_path) -> None:
    (tmp_path / "runs").mkdir()
    kept = tmp_path / "runs" / "chair-cache-decompose.json"
    kept.write_text("previous chair", encoding="utf-8")
    deps = _live_deps(tmp_path)

    assert not (tmp_path / "runs" / "chair-cache.json").exists()
    deps.save_cache()  # type: ignore[misc]

    written = sorted(p.name for p in (tmp_path / "runs").glob("chair-cache*.json"))
    assert written == ["chair-cache-decompose.json", "chair-cache-store.json", "chair-cache.json"]
    assert kept.read_text(encoding="utf-8") == "previous chair"  # no intake was read, so nothing replaces it


def test_a_restart_record_on_disk_gives_the_note_and_record_restart_bumps_it(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    path = cli.chair_restart_log.record_path(runs)
    stamp = datetime.now(UTC).isoformat()
    cli.chair_restart_log.write_record(path, cli.chair_restart_log.RestartRecord(times=(stamp,), reason="code moved"))

    deps = _live_deps(tmp_path)

    assert deps.restart_note == "restarts 1 in 24h, last: code moved"
    assert "| tick error: " not in deps.restart_note
    deps.record_restart("newer code")  # type: ignore[misc]
    bumped = cli.chair_restart_log.read_record(path)
    assert bumped is not None and len(bumped.times) == 2 and bumped.reason == "newer code"


def test_a_record_restart_with_no_record_on_disk_writes_the_first_one(tmp_path) -> None:
    (tmp_path / "runs").mkdir()
    deps = _live_deps(tmp_path)

    assert deps.restart_note == ""
    deps.record_restart("first")  # type: ignore[misc]
    record = cli.chair_restart_log.read_record(cli.chair_restart_log.record_path(tmp_path / "runs"))
    assert record is not None and len(record.times) == 1


def test_the_profile_sets_the_restart_window_and_defaults_apply_otherwise(tmp_path) -> None:
    set_by_profile = _live_deps(tmp_path, {"chair": {"restart_window_s": 30, "restart_max_defer_s": 90.5}})
    absent = _live_deps(tmp_path, {})
    non_numeric = _live_deps(tmp_path, {"chair": {"restart_window_s": "soon", "restart_max_defer_s": True}})

    assert (set_by_profile.restart_window_s, set_by_profile.restart_max_defer_s) == (30.0, 90.5)
    assert (absent.restart_window_s, absent.restart_max_defer_s) == (120.0, 600.0)
    assert (non_numeric.restart_window_s, non_numeric.restart_max_defer_s) == (120.0, 600.0)
    assert callable(absent.changed_files)


def test_a_zero_restart_window_is_kept_and_a_negative_one_falls_back(tmp_path) -> None:
    zero = _live_deps(tmp_path, {"chair": {"restart_window_s": 0, "restart_max_defer_s": 0.0}})
    negative = _live_deps(tmp_path, {"chair": {"restart_window_s": -5}})

    assert (zero.restart_window_s, zero.restart_max_defer_s) == (0.0, 0.0)
    assert negative.restart_window_s == 120.0


def _two_intake_workspace(tmp_path, monkeypatch, dry_run: bool):
    for name in ("a", "b"):
        (tmp_path / "work" / name / "p1").mkdir(parents=True)
        (tmp_path / "work" / name / "initiative.md").write_text(f"---\nintake: {name}.md\n---\n", encoding="utf-8")
        (tmp_path / f"{name}.md").write_text(f"---\nid: {name}\n---\nbody\n", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    seen: list = []

    def fake_runs(runs_dir, intake, cache=None):
        seen.append((intake, cache))
        cache.get(("facts", intake), (1, 1), lambda: f"log of {intake}")
        return [SimpleNamespace(run_id=f"{intake}-1")]

    monkeypatch.setattr(cli.chair_decompose_runs, "read_decompose_runs", fake_runs)
    return _live_deps(tmp_path, dry_run=dry_run), seen


def test_each_intake_gets_its_own_decompose_cache_and_keeps_it_across_ticks(tmp_path, monkeypatch) -> None:
    deps, seen = _two_intake_workspace(tmp_path, monkeypatch, True)

    deps.facts_deps.decomposed_intake()  # type: ignore[misc]
    deps.beat()  # type: ignore[misc]
    deps.facts_deps.decomposed_intake()  # type: ignore[misc]

    by_intake: dict = {}
    for intake, cache in seen:
        by_intake.setdefault(intake, []).append(cache)
    assert sorted(by_intake) == ["a", "b"]
    assert all(len({id(c) for c in caches}) == 1 for caches in by_intake.values())
    assert by_intake["a"][0] is not by_intake["b"][0]


def test_the_saved_decompose_file_holds_every_intakes_facts(tmp_path, monkeypatch) -> None:
    deps, _ = _two_intake_workspace(tmp_path, monkeypatch, False)
    deps.facts_deps.decomposed_intake()  # type: ignore[misc]

    deps.save_cache()  # type: ignore[misc]

    reloaded = cli.chair_read_cache.StampCache(64)
    assert cli.chair_read_cache.load_cache(reloaded, tmp_path / "runs" / "chair-cache-decompose.json") == 2
    assert reloaded.get(("facts", "a"), (1, 1), lambda: "miss") == "log of a"
    assert reloaded.get(("facts", "b"), (1, 1), lambda: "miss") == "log of b"


def test_a_dry_run_sets_no_save_or_record_hooks_and_loads_no_cache(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    cli.chair_restart_log.write_record(
        cli.chair_restart_log.record_path(runs),
        cli.chair_restart_log.RestartRecord(times=(datetime.now(UTC).isoformat(),), reason="r"),
    )
    loaded: list = []
    monkeypatch.setattr(cli.chair_read_cache, "load_cache", lambda cache, path: loaded.append(path) or 0)

    deps = _live_deps(tmp_path, dry_run=True)

    assert (deps.save_cache, deps.record_restart, deps.restart_note, loaded) == (None, None, "", [])
