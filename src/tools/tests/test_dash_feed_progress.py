from agent_tools import dash_feed

LIVE = frozenset({("i7", "p2"), ("i11", "")})
STRANDED = frozenset({("i3", "p1", "t1")})
# Terminal store states per the feed: a phase is landed when each of its items is one of these.
LANDED = ("done", "dropped")


class CountingRows(list):
    """A list that counts each full iteration, so a per-initiative scan shows as a count that grows with N."""

    scans = 0

    def __iter__(self):
        type(self).scans += 1
        return super().__iter__()


def _task(initiative, phase, task_id, state, needs=(), **extra):
    return {
        "kind": "task", "initiative": initiative, "phase": phase, "task_id": task_id, "state": state,
        "needs": list(needs), "extra": extra,
    }


def _rows(count):
    """Per initiative: an initiative row (epic `e<n % 5>`, priority and land_at on some), then tasks across two or
    three phases, interleaved with other initiatives' rows so row order within a group is not the global order."""
    heads = [
        {
            "kind": "initiative", "initiative": f"i{n}", "state": "ready", "needs": [],
            "extra": {
                **({"epic": f"e{n % 5}"} if n % 3 else {}),
                **({"priority": n % 4} if n % 2 else {}),
                **({"land_at": "phase"} if n % 7 == 0 else {}),
            },
        }
        for n in range(count)
    ]
    tasks = [
        _task(f"i{n}", f"p{p}", f"t{p}-{k}", ("done", "ready", "approved", "dropped", "in_progress", "todo")[(n + p + k) % 6],
              needs=[f"t1-{k}"] if p == 2 else [])
        for p in (1, 2, 3)
        for k in (0, 1)
        for n in range(count)
        if p < 3 or n % 2
    ]
    return [*heads, *tasks]


def _own(rows, initiative):
    return [row for row in rows if row["initiative"] == initiative]


def _ref_progress(rows, initiative):
    by_phase = {}
    for row in _own(rows, initiative):
        if row["kind"] == "task":
            by_phase.setdefault(row["phase"], []).append(row["state"])
    phases = sorted(by_phase)
    flags = {p: all(s in LANDED for s in states) for p, states in by_phase.items()}
    current = next((p for p in phases if not flags[p]), phases[-1] if phases else None)
    return sum(flags.values()), len(phases), current


def _ref_states(rows, initiative):
    own = [row for row in _own(rows, initiative) if row["kind"] == "task"]
    landed = {row["task_id"] for row in own if row["state"] in ("done", "dropped")}
    land_at = next((r["extra"]["land_at"] for r in _own(rows, initiative) if r["kind"] == "initiative" and r["extra"].get("land_at")), "ticket")
    lane = {phase for i, phase in LIVE if i == initiative}
    out = []
    for row in own:
        live = bool(lane) and ("" in lane or row["phase"] in lane)
        written = row["extra"].get("land_at")
        if row["state"] == "done":
            state = "landed" if written is None or written == land_at else "merged"
        elif row["state"] == "dropped":
            state = "dropped"
        elif row["state"] == "approved" or (initiative, row["phase"], row["task_id"]) in STRANDED:
            state = "approved"
        elif row["state"] == "in_progress":
            state = "building" if live else "blocked"
        elif row["state"] == "ready":
            state = "blocked" if any(n not in landed for n in row["needs"]) else ("building" if live else "ready")
        else:
            state = None
        if state is not None:
            out.append((row["phase"], row["task_id"], state))
    return out


def test_group_rows_keeps_row_order_within_an_initiative():
    rows = [{"initiative": "a", "n": 1}, {"initiative": "b", "n": 2}, {"initiative": "a", "n": 3}, {"n": 4}]
    assert dash_feed._group_rows(rows) == {
        "a": [{"initiative": "a", "n": 1}, {"initiative": "a", "n": 3}],
        "b": [{"initiative": "b", "n": 2}],
        None: [{"n": 4}],
    }


def test_queue_states_equals_the_per_initiative_reference():
    rows = _rows(300)
    expected = {(i, p, t): s for i in (f"i{n}" for n in range(300)) for p, t, s in _ref_states(rows, i)}
    assert dash_feed._queue_states(rows, LIVE, STRANDED) == expected
    assert len(expected) > 1000


def test_epics_v1_equals_the_per_initiative_reference():
    rows = _rows(300)
    got = {entry["id"]: entry for record in dash_feed._epics_v1(rows) for entry in record["initiatives"]}
    assert {i: (e["phases_landed"], e["phases_total"]) for i, e in got.items()} == {
        f"i{n}": _ref_progress(rows, f"i{n}")[:2] for n in range(300) if n % 3
    }


def test_queue_progress_priority_and_phases_equal_the_reference(monkeypatch, tmp_path):
    rows = _rows(300)
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: rows)
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: [])
    monkeypatch.setattr(dash_feed, "_MAX_ROWS", 10_000)
    queue, total = dash_feed._queue(tmp_path, LIVE)
    expected = []
    for n in range(300):
        i = f"i{n}"
        landed, count, current = _ref_progress(rows, i)
        if landed < count:
            expected.append((i, rows[n]["extra"].get("priority"), landed, count, current))
    got = sorted(
        ((r["initiative"], r["priority"], r["phases_landed"], r["phases_total"], r["current_phase"]) for r in queue),
        key=lambda r: int(r[0][1:]),
    )
    assert (got, total) == (expected, len(expected))
    # Priority first (numeric ascending, absent last), then first appearance in row order.
    keys = [(0, r["priority"]) if r["priority"] is not None else (1, 0) for r in queue]
    assert keys == sorted(keys)
    sample = next(r for r in queue if r["initiative"] == "i7")
    assert [(p["id"], [(t["id"], t["state"]) for t in p["tasks"]]) for p in sample["phases"]] == [
        (phase, [(t, s) for p, t, s in _ref_states(rows, "i7") if p == phase]) for phase in ("p1", "p2", "p3")
    ]


def _scans(count, monkeypatch, tmp_path):
    rows = CountingRows(_rows(count))
    CountingRows.scans = 0
    dash_feed._queue_states(rows, LIVE, STRANDED)
    states = CountingRows.scans
    CountingRows.scans = 0
    dash_feed._epics_v1(rows)
    epics = CountingRows.scans
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: rows)
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: [])
    CountingRows.scans = 0
    dash_feed._queue(tmp_path, LIVE)
    return states, epics, CountingRows.scans


def test_rows_are_scanned_a_constant_number_of_times(monkeypatch, tmp_path):
    small = _scans(100, monkeypatch, tmp_path)
    large = _scans(400, monkeypatch, tmp_path)
    assert small == large
    assert max(large) <= 6
