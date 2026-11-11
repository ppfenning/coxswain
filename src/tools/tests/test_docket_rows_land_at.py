from agent_tools.chair_read_docket import docket_from_rows

NOW = "2026-11-11T00:00:00Z"


def _row(task_id: str, state: str, needs: tuple[str, ...] = (), phase: str = "p1") -> dict:
    return {
        "kind": "task", "initiative": "epic", "task_id": task_id, "phase": phase, "state": state,
        "needs": list(needs), "title": task_id, "surfaces": [], "body": "", "extra": {},
        "holder": None, "epoch": 0, "expires_at": "",
    }


ROWS = [_row("a", "approved"), _row("b", "ready", ("a",), phase="p2")]


def _ids(docket: list[dict], key: str) -> list[str]:
    return [t["id"] for i in docket for t in i[key]]


def test_a_ticket_initiative_offers_a_task_whose_need_is_approved_and_unmerged():
    docket = docket_from_rows(ROWS, NOW, land_at={"epic": "ticket"})
    assert _ids(docket, "ready_tasks") == ["b"]
    assert _ids(docket, "waiting_tasks") == []


def test_a_task_initiative_still_waits_on_an_approved_unmerged_need():
    docket = docket_from_rows(ROWS, NOW, land_at={"epic": "task"})
    assert _ids(docket, "ready_tasks") == []
    assert _ids(docket, "waiting_tasks") == ["b"]


def test_no_map_and_an_id_missing_from_the_map_keep_the_default():
    default = docket_from_rows(ROWS, NOW)
    assert _ids(default, "waiting_tasks") == ["b"]
    assert docket_from_rows(ROWS, NOW, land_at={"other": "ticket"}) == default
