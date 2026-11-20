"""`policy.dispatch.lane_model` reaches the store: `cox route store-fill` stamps a new initiative row with the team
cartridge's model, against a real Postgres store and a resolved cartridge.

The unit tests (src/tools/tests/test_lane_model.py) prove the pure stamp; this proves the edge reads the cartridge the
profile names and that the stamp survives the store's own round trip. A repeat fill writes nothing, so the stamp never
changes after the first write."""

import json

import pytest

from smoke import plan
from smoke.conftest import TEAM

INITIATIVE = "work/ship/initiative.md"
TASK = "work/ship/p1/t1.md"


def _with_lane_model(launchable, model: str | None) -> None:
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    if model is not None:
        cartridge["policy"] = {"dispatch": {"lane_model": model}}
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")


def _fill(sandbox):
    sandbox.write(INITIATIVE, plan.work_item_text({"id": "ship", "title": "Ship it"}, "Why"))
    sandbox.write(TASK, plan.work_item_text({"state": "ready", "title": "First", "id": "t1", "phase": "p1"}, "Body"))
    return sandbox.cox("route", "store-fill")


@pytest.mark.parametrize(("model", "stored"), [("phase", "phase"), ("task", None), (None, None)])
def test_store_fill_stamps_the_cartridge_lane_model_once(launchable, model, stored):
    sandbox = launchable.sandbox
    _with_lane_model(launchable, model)
    first = _fill(sandbox)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "inserted 2," in first.stdout
    assert (sandbox.store_row("ship", "ship").get("extra") or {}).get("lane_model") == stored
    assert "lane_model" not in (sandbox.store_row("ship", "t1").get("extra") or {})
    again = sandbox.cox("route", "store-fill")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "inserted 0, filled 0, unchanged 2" in again.stdout
