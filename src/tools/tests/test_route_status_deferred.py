import json

import pytest

from agent_tools import release_gate, route, run_store
from agent_tools.cli import main


def _intake(task_id: str, state: str = "queued", **extra: object) -> dict:
    return {"kind": "intake", "state": state, "task_id": task_id, "initiative": "", "extra": extra}


def _initiative(name: str, **extra: object) -> dict:
    return {"kind": "initiative", "state": "open", "task_id": name, "initiative": name, "extra": extra}


def test_intake_and_initiative_share_one_heading() -> None:
    queue = [_intake("i-1", target_release="2.0"), _initiative("init-a", target_release="2.0")]
    deferred = route.deferred_by_target(queue, "1.5")
    assert deferred == {"2.0": ["i-1", "init-a"]}
    assert route.render_status([], deferred=deferred) == "deferred to 2.0:\n  i-1\n  init-a"


def test_targets_sort_ascending_by_version() -> None:
    queue = [_initiative("late", target_release="1.10"), _initiative("soon", target_release="1.2")]
    deferred = route.deferred_by_target(queue, "1.0")
    assert list(deferred) == ["1.2", "1.10"]
    assert route.render_status([], deferred=deferred) == "deferred to 1.2:\n  soon\ndeferred to 1.10:\n  late"


def test_initiative_at_or_below_current_is_not_listed() -> None:
    queue = [_initiative("at", target_release="1.5"), _initiative("below", target_release="1.4")]
    assert route.deferred_by_target(queue, "1.5") == {}


def test_targets_that_parse_alike_share_one_heading() -> None:
    assert route.group_deferred({"a": "v1.2"}, {"b": "1.2"}) == {"1.2": ["a", "b"]}


def test_intake_naming_a_deferred_initiative_is_listed_once_under_the_initiative() -> None:
    queue = [_intake("i-1", initiative="init-a", target_release="2.0"), _initiative("init-a")]
    assert route.deferred_by_target(queue, "1.0") == {"2.0": ["init-a"]}


def test_an_intake_and_an_initiative_with_one_id_are_both_kept_when_targets_differ() -> None:
    queue = [_intake("same", target_release="2.0"), _initiative("same", target_release="3.0")]
    assert route.deferred_by_target(queue, "1.0") == {"2.0": ["same"], "3.0": ["same"]}


def test_no_deferred_work_leaves_output_unchanged() -> None:
    held = [{"id": "h-1", "reason": "wait"}]
    plain = route.render_status([], held=held, problems=["x"], gate_level="open")
    assert route.render_status([], held=held, problems=["x"], gate_level="open", deferred={}) == plain
    assert route.render_status([], held=held, problems=["x"], gate_level="open", deferred=None) == plain


def test_deferred_block_sits_after_held_and_before_problems() -> None:
    text = route.render_status([], held=[{"id": "h-1", "reason": "wait"}], problems=["p"], deferred={"2.0": ["a"]})
    assert text == "held:\n  h-1: wait\ndeferred to 2.0:\n  a\nproblem: p"


def test_held_initiative_entries_carry_hold_reason() -> None:
    own = _initiative("own", held="true", hold_reason="own reason")
    via = _intake("i-2", "decomposed", initiative="named", held="true", hold_reason="via intake")
    queue = [own, via]
    assert route.held_initiative_entries(queue, release_gate.held_initiatives(queue)) == [
        {"id": "named", "reason": "via intake"},
        {"id": "own", "reason": "own reason"},
    ]


def test_held_initiative_named_by_a_listed_held_intake_is_not_repeated() -> None:
    listed = _intake("i-3", initiative="named", held="true", hold_reason="why")
    queue = [listed, _initiative("named"), _initiative("other", held="true")]
    assert route.held_initiative_entries(queue, release_gate.held_initiatives(queue), [listed]) == [
        {"id": "other", "reason": ""}
    ]


@pytest.fixture
def status(tmp_path, monkeypatch, capsys):
    """Runs `route status` over a literal queue at version 1.0; no tracker, store or harness is touched."""
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\nprovider_profile: /opt/providers/acme.yaml\n"
    )
    queue: list[dict] = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: list(queue))
    monkeypatch.setattr(run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr("agent_tools.version_info.package_version", lambda *_a, **_k: "1.0")

    def run(rows: list[dict], *flags: str) -> str:
        queue[:] = rows
        capsys.readouterr()
        assert main(["route", "status", "--profile", str(profile), *flags]) == 0
        return capsys.readouterr().out

    return run


def test_status_text_lists_deferred_work_after_held(status) -> None:
    out = status([_intake("held-1", held=True, hold_reason="parked"), _intake("later", target_release="2.0")])
    assert "held:\n  held-1: parked\ndeferred to 2.0:\n  later" in out


def test_status_json_adds_deferred_beside_the_existing_keys(status) -> None:
    doc = json.loads(status([_intake("later", target_release="2.0"), _initiative("init-a", target_release="3.0")], "--json"))
    assert doc["deferred"] == {"2.0": ["later"], "3.0": ["init-a"]}
    assert "waiting" not in doc
    assert "held" not in doc


def test_status_json_held_only_queue_keeps_both_existing_keys(status) -> None:
    doc = json.loads(status([_intake("held-1", held=True)], "--json"))
    assert (doc["waiting"], doc["held"]) == ({}, [{"id": "held-1", "reason": ""}])
    assert "deferred" not in doc


def test_status_json_waiting_only_queue_keeps_both_existing_keys(status) -> None:
    doc = json.loads(status([_intake("waits", after=["nothing-landed"])], "--json"))
    assert (doc["waiting"], doc["held"]) == ({"waits": ["nothing-landed"]}, [])
    assert "deferred" not in doc


def test_status_json_lists_a_held_initiative_with_its_reason(status) -> None:
    doc = json.loads(status([_initiative("init-a", held=True, hold_reason="paused")], "--json"))
    assert doc["held"] == [{"id": "init-a", "reason": "paused"}]


def test_status_json_with_no_queue_facts_is_the_bare_list(status) -> None:
    assert json.loads(status([], "--json")) == []
