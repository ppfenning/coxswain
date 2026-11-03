from agent_tools.dash_feed_tasks import counts, task_entries


def _row(task_id, state, phase="p1", needs=(), reason=""):
    return {"task_id": task_id, "phase": phase, "state": state, "needs": list(needs), "reason": reason}


def test_approved_and_quarantined_keep_their_own_states():
    rows = [_row("a", "approved"), _row("b", "quarantined", reason="tests red")]
    got = task_entries(rows, {}, {}, {"a": "approved", "b": "quarantined"})
    assert [(e["id"], e["state"]) for e in got] == [("a", "approved"), ("b", "quarantined")]
    assert got[1]["reason"] == "tests red"


def test_tries_counts_attempts_across_two_runs():
    attempts = {"a": [{"run": "r1"}, {"run": "r1"}, {"run": "r2"}]}
    latest = {"a": {"run": "r2", "node": "build", "turns": 7, "cost": 0.5}}
    [entry] = task_entries([_row("a", "approved")], attempts, latest, {})
    assert (entry["tries"], entry["run"], entry["node"], entry["turns"], entry["cost"]) == (3, "r2", "build", 7, 0.5)


def test_ready_task_shows_waiting_on_and_done_needs_do_not():
    rows = [_row("a", "approved"), _row("b", "done"), _row("c", "ready", needs=["a", "b"]), _row("d", "ready", needs=["b"])]
    got = {e["id"]: e for e in task_entries(rows, {}, {}, {})}
    assert got["c"]["waiting_on"] == ["a"]
    assert "waiting_on" not in got["d"]


def test_long_reason_is_cut_with_reason_full_holding_all():
    text = "x" * 200
    [entry] = task_entries([_row("a", "quarantined", reason=text)], {}, {}, {})
    assert (len(entry["reason"]), entry["reason_full"]) == (120, text)
    [short] = task_entries([_row("a", "quarantined", reason="short")], {}, {}, {})
    assert "reason_full" not in short


def test_run_verdict_appears_only_on_a_mismatch():
    rows = [_row("a", "ready"), _row("b", "done")]
    got = {e["id"]: e for e in task_entries(rows, {}, {}, {"a": "approved", "b": "done"})}
    assert got["a"]["run_verdict"] == "approved"
    assert got["a"]["state"] == "ready"
    assert "run_verdict" not in got["b"]


def test_counts_agree_with_the_entries():
    rows = [_row("a", "done"), _row("b", "merged"), _row("c", "quarantined"), _row("d", "approved")]
    assert counts(task_entries(rows, {}, {}, {})) == {"landed": 2, "quarantined": 1}


def test_phases_group_in_row_order():
    rows = [_row("a", "done", phase="p2"), _row("b", "done", phase="p1"), _row("c", "done", phase="p2")]
    assert [e["id"] for e in task_entries(rows, {}, {}, {})] == ["a", "c", "b"]
