import json
from pathlib import Path

from agent_tools import dash_feed

FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_epics_v1.json"
TITLES = {
    "cartridge-bounds": {"title": "Bound cartridge cost per run"},
    "dash-waves": {"title": "Show waves of phases on the dash"},
    "initiative-landing": {"title": "Group initiatives and land them on a shared branch"},
}


def _initiative(slug: str, epic: str | None, branch: str | None = None) -> dict:
    extra = {k: v for k, v in (("epic", epic), ("target_branch", branch)) if v is not None}
    return {"kind": "initiative", "initiative": slug, "task_id": slug, "phase": None, "state": "ready", "extra": extra}


def _tasks(slug: str, landed: int, total: int) -> list[dict]:
    return [
        {"kind": "task", "initiative": slug, "task_id": f"{slug}-{n}", "phase": f"p{n}", "state": "done" if n < landed else "todo"}
        for n in range(total)
    ]


ROWS = [
    _initiative("landing-feed", "initiative-landing", "develop"),
    *_tasks("landing-feed", 1, 3),
    _initiative("waves-render", "dash-waves", "main"),
    *_tasks("waves-render", 1, 4),
    _initiative("bounds-router", "cartridge-bounds"),
    *_tasks("bounds-router", 2, 2),
    _initiative("landing-cores", "initiative-landing", "develop"),
    *_tasks("landing-cores", 2, 2),
    _initiative("bounds-ledger", "cartridge-bounds"),
    *_tasks("bounds-ledger", 3, 3),
    _initiative("solo", None),
    *_tasks("solo", 1, 1),
]


def test_epics_equal_the_fixture():
    assert dash_feed._epics_v1(ROWS, TITLES) == json.loads(FIXTURE.read_text())["epics"]


def test_title_is_the_id_without_definitions():
    assert [e["title"] for e in dash_feed._epics_v1(ROWS)] == ["cartridge-bounds", "dash-waves", "initiative-landing"]


def test_initiative_without_phases_is_not_landed():
    (epic,) = dash_feed._epics_v1([_initiative("empty", "e")])
    assert (epic["landed"], epic["complete"], epic["initiatives"][0]["phases_total"]) == (0, False, 0)


def test_no_epic_feed_gives_an_empty_list():
    assert dash_feed._epics_v1([_initiative("solo", None), *_tasks("solo", 1, 1)]) == []
    assert dash_feed.snapshot("2026-11-05T00:00:00Z", {}, {}, [], [], [], 0, [], 0, [])["epics"] == []
