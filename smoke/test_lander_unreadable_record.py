"""H2 follow-up: one malformed landing record stops only its own initiative. Real `cox lander once`, real git, the store.

Two initiatives are put in flight the way an operator does it (`cox route import`, then `cox lander adopt --apply`).
Then one landing-record row is made malformed in the store itself, as a bad write or a hand edit would leave it. The
lander's real pass must land the other initiative, tell the chair once about the bad one by name, and leave the bad
row exactly as it found it: the row is the evidence of what went wrong.
"""

from __future__ import annotations

import json

from smoke.test_lander_lanes import _in_flight, _on_origin_main, _profile_with_cartridge, _repository, _status

MALFORMED = '{"initiative": "beta-bad", "branch": "init/beta-bad", "phases": [1]}'


def _sql(sandbox, statement: str, params: tuple) -> object:
    """Run one statement through the lander's own store connection; print what a SELECT returns, as JSON."""
    return sandbox.python_json(
        "import json; from pathlib import Path; from agent_tools import lander_ports; "
        f"conn = lander_ports.connect(Path({str(sandbox.workspace / 'runs')!r})); "
        f"sql = {statement!r}.replace('?', conn.dialect.placeholder); "
        f"rows = conn.query_all(sql, {params!r}) if sql.startswith('SELECT') else conn.execute(sql, {params!r}); "
        "print(json.dumps(rows if isinstance(rows, list) else None))")


def _row(sandbox, initiative: str) -> list:
    return _sql(sandbox, "SELECT repo, state, record_json, created_at, updated_at FROM landing_records WHERE initiative = ?",
                (initiative,))


def _items(sandbox, initiative: str) -> list:
    return _sql(sandbox, "SELECT id, state, attempts, claimed_by, last_error, updated_at FROM land_queue WHERE initiative = ? "
                "ORDER BY id", (initiative,))


def _enqueue_land(sandbox, initiative: str) -> None:
    """An initiative_land item of the bad initiative, through the store's own enqueue: work a pass must leave alone."""
    sandbox.python_json(
        "import json; from pathlib import Path; from agent_tools import lander_ports; from harness import landing_store as ls; "
        f"conn = lander_ports.connect(Path({str(sandbox.workspace / 'runs')!r})); "
        f"ls.enqueue(conn, ls.INITIATIVE_LAND, {initiative!r}, repo='', now=lander_ports.now_stamp(), reason='smoke'); "
        "print(json.dumps(None))")


def test_one_malformed_landing_record_stops_only_its_initiative_and_the_chair_is_told_once(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path)
    good, good_origin = _repository(sandbox, tmp_path, "good", "exit 0\n")
    bad, bad_origin = _repository(sandbox, tmp_path, "bad", "exit 0\n")
    _in_flight(sandbox, bad, "bad", "beta-bad")
    _in_flight(sandbox, good, "good", "alpha-good")
    original = _row(sandbox, "beta-bad")[0][2]
    _sql(sandbox, "UPDATE landing_records SET record_json = ? WHERE initiative = ?", (MALFORMED, "beta-bad"))
    _enqueue_land(sandbox, "beta-bad")
    before, queued = _row(sandbox, "beta-bad"), _items(sandbox, "beta-bad")
    assert any(i[1] == "queued" for i in queued), queued
    assert json.loads(before[0][2]) == json.loads(MALFORMED), "the row is malformed in the store itself"

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "alpha-good recorded landed" in done.stdout, done.stdout
    assert _on_origin_main(sandbox, good_origin, "alpha-good.txt"), "the good initiative's pass proceeded"
    assert not _on_origin_main(sandbox, bad_origin, "beta-bad.txt")
    assert "unreadable: the landing record of beta-bad cannot be read" in done.stdout, done.stdout
    assert "lane failed" not in done.stdout, done.stdout
    assert _items(sandbox, "beta-bad") == queued, "its items were left out of the pass: none claimed, tried or failed"
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.returncode == 0, inbox.stdout + inbox.stderr
    assert inbox.stdout.count("landing record of beta-bad cannot be read") == 1, inbox.stdout
    assert _row(sandbox, "beta-bad") == before, "nothing wrote into the bad row"

    again = sandbox.cox("lander", "once")  # told once: the next pass says nothing new to the chair
    assert again.returncode == 0, again.stdout + again.stderr
    inbox = sandbox.cox("courier", "inbox", "--label", "chair").stdout
    assert inbox.count("landing record of beta-bad cannot be read") == 1, inbox
    assert _row(sandbox, "beta-bad") == before
    assert _items(sandbox, "beta-bad") == queued and "lane failed" not in again.stdout, again.stdout

    status = _status(sandbox)  # `cox lander status` reads the rest and names the bad row, instead of crashing
    assert [u["initiative"] for u in status["unreadable"]] == ["beta-bad"]
    adopted = sandbox.cox("lander", "adopt", "--apply", "beta-bad")  # adopt counts it as present: never a fresh record over it
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    assert "beta-bad: skipped: its landing record cannot be read and is left as it is" in adopted.stdout, adopted.stdout
    assert "record made" not in adopted.stdout, adopted.stdout
    assert _row(sandbox, "beta-bad") == before

    # Repaired by hand: the next pass reads it, the notice is forgotten, and a later stall would be told again.
    _sql(sandbox, "UPDATE landing_records SET record_json = ? WHERE initiative = ?", (original, "beta-bad"))
    assert _sql(sandbox, "SELECT key FROM lander_notices WHERE key = ?", ("unreadable:beta-bad",)) == [["unreadable:beta-bad"]]
    repaired = sandbox.cox("lander", "once")
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert "unreadable: the landing record of beta-bad reads again" in repaired.stdout, repaired.stdout
    assert _sql(sandbox, "SELECT key FROM lander_notices WHERE key = ?", ("unreadable:beta-bad",)) == []
    assert _status(sandbox)["unreadable"] == []
