"""`policy.dispatch.lane_model` end to end: `cox route store-fill` stamps a new initiative row with the team cartridge's
model, `cox route status` names it, and `cox chair run --once --dry-run` plans no launch for it.

The unit tests (src/tools/tests/test_lane_model.py) prove the pure stamp and the planner; this proves the edges read
the cartridge the profile names, that the stamp survives the store's own round trip, and that the installed chair reads
it back. A repeat fill writes nothing, so the stamp never changes after the first write."""

import json

import pytest

from smoke import plan
from smoke.conftest import TEAM

INITIATIVE = "work/ship/initiative.md"
TASK = "work/ship/p1/t1.md"
HEADING = "lane model phase (waits for a phase worker; every other initiative is task):"


def _with_lane_model(launchable, model: str | None) -> None:
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    if model is not None:
        cartridge["policy"] = {"dispatch": {"lane_model": model}}
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")


def _fill(launchable):
    sandbox = launchable.sandbox
    sandbox.write(INITIATIVE, plan.work_item_text({"id": "ship", "title": "Ship it", "repo": "app"}, "Why"))
    fields = {"state": "ready", "title": "First", "id": "t1", "phase": "p1", "repo": "app"}
    sandbox.write(TASK, plan.work_item_text(fields, "Body"))
    return sandbox.cox("route", "store-fill")


@pytest.mark.parametrize(("model", "stored"), [("phase", "phase"), ("task", None), (None, None)])
def test_store_fill_stamps_the_cartridge_lane_model_once(launchable, model, stored):
    sandbox = launchable.sandbox
    _with_lane_model(launchable, model)
    first = _fill(launchable)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "inserted 2," in first.stdout
    assert (sandbox.store_row("ship", "ship").get("extra") or {}).get("lane_model") == stored
    assert "lane_model" not in (sandbox.store_row("ship", "t1").get("extra") or {})
    again = sandbox.cox("route", "store-fill")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "inserted 0, filled 0, unchanged 2" in again.stdout


@pytest.mark.parametrize("model", ["phase", None])
def test_status_names_a_phase_initiative_and_the_chair_plans_no_launch_for_it(launchable, model):
    sandbox = launchable.sandbox
    _with_lane_model(launchable, model)
    assert _fill(launchable).returncode == 0
    status = sandbox.cox("route", "status")
    assert status.returncode == 0, status.stdout + status.stderr
    tick = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    if model == "phase":
        assert f"{HEADING}\n  ship" in status.stdout, status.stdout
        assert "waits for a phase worker" in tick.stdout, tick.stdout
        assert "launch_epic" not in tick.stdout, tick.stdout
    else:
        assert HEADING not in status.stdout, status.stdout
        assert "waits for a phase worker" not in tick.stdout, tick.stdout
