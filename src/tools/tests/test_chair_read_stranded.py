from agent_tools import land_manual
from agent_tools.chair_read_stranded import keep_stranded_keys, read_missing_repos, read_stranded, stranded_from_rows


def test_a_full_row_keeps_exactly_the_six_keys():
    row = {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": "cox runs land r1 --task t1 --repo /x", "expected": True}
    assert keep_stranded_keys([row]) == [row]


def test_extra_keys_on_the_input_row_are_dropped():
    row = {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": None, "review": {"verdict": "approve"}, "repo": "/x"}
    assert keep_stranded_keys([row]) == [{"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": None, "expected": None}]


def test_an_empty_input_gives_an_empty_list():
    assert keep_stranded_keys([]) == []


def _stranded_record(run: str, repo: str) -> dict:
    return {
        "run": run, "task": run, "phase": "p1", "branch": "b1", "repo": repo,
        "review": {"verdict": "approve"}, "arbitration": "arbiter: skipped (both approved)",
        "landed": False,
    }


def _stranded_item(run: str) -> dict:
    return {"id": run, "state": "ready"}


def test_read_stranded_drops_a_row_whose_repo_the_predicate_rejects():
    records = [_stranded_record("r1", "/gone"), _stranded_record("r2", "/here")]
    items = [_stranded_item("r1"), _stranded_item("r2")]
    repo_exists = {"/gone": False, "/here": True}.__getitem__
    assert read_stranded(records, items, repo_exists) == [
        {"run": "r2", "task": "r2", "phase": "p1", "branch": "b1", "remedy": "cox runs land r2 --task r2 --repo /here", "expected": False},
    ]


def test_stranded_from_rows_reads_rebuilt_from_off_the_row_extra():
    record = {**_stranded_record("x-1", "/here"), "task": "t1"}
    row = {"kind": "task", "task_id": "t1", "initiative": "x", "phase": "p1", "state": "ready", "extra": {"rebuilt_from": ["x-1"]}}
    assert stranded_from_rows([row], [record]) == []
    assert [r["run"] for r in stranded_from_rows([{**row, "extra": {}}], [record])] == ["x-1"]


def test_read_missing_repos_returns_the_rejected_repo_once():
    records = [_stranded_record("r1", "/gone"), _stranded_record("r2", "/here")]
    items = [_stranded_item("r1"), _stranded_item("r2")]
    repo_exists = {"/gone": False, "/here": True}.__getitem__
    assert read_missing_repos(records, items, repo_exists) == ["/gone"]


def test_read_stranded_keeps_the_six_key_row_for_a_live_present_repo():
    records = [_stranded_record("r1", "/here")]
    items = [_stranded_item("r1")]
    assert read_stranded(records, items, lambda repo: True) == [
        {"run": "r1", "task": "r1", "phase": "p1", "branch": "b1", "remedy": "cox runs land r1 --task r1 --repo /here", "expected": False},
    ]


def test_read_stranded_drops_a_row_whose_branch_the_predicate_reports_present():
    records = [{**_stranded_record("r1", "/here"), "branch": "epic/acme/p1"}]
    items = [_stranded_item("r1")]
    assert read_stranded(records, items, lambda repo: True, lambda repo, branch: True) == []


def test_read_stranded_keeps_a_row_whose_branch_the_predicate_reports_gone():
    records = [{**_stranded_record("r1", "/here"), "branch": "epic/acme/p1"}]
    items = [_stranded_item("r1")]
    assert read_stranded(records, items, lambda repo: True, lambda repo, branch: False) == [
        {"run": "r1", "task": "r1", "phase": "p1", "branch": "epic/acme/p1",
         "remedy": "cox runs land r1 --task r1 --repo /here", "expected": False},
    ]
def _record(**over):
    base = {
        "run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
        "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
        "landed": False,
    }
    return {**base, **over}


def _row(state, **over):
    base = {
        "kind": "task", "initiative": "acme", "task_id": "t1", "phase": "p1", "state": state,
        "needs": [], "title": "", "surfaces": [], "body": "", "extra": {},
    }
    return {**base, **over}


def test_a_ready_row_for_an_approved_unlanded_record_is_stranded():
    assert stranded_from_rows([_row("ready")], [_record()]) == [
        {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": None, "expected": False}
    ]


def test_a_store_row_for_a_record_in_a_manual_repo_is_expected_and_carries_the_manual_remedy():
    record = _record(repo="/manual")
    (row,) = stranded_from_rows([_row("ready")], [record], manual_of=lambda repo: repo == "/manual")
    assert (row["expected"], row["remedy"]) == (True, land_manual.REMEDY)
    (row,) = stranded_from_rows([_row("ready")], [record], manual_of=lambda repo: False)
    assert row["expected"] is False


def test_a_store_row_reads_the_records_repo_overlay_by_default(tmp_path):
    manual = tmp_path / "manual"
    (manual / ".agent").mkdir(parents=True)
    (manual / ".agent" / "cartridge.yaml").write_text("land: manual\n")
    auto = tmp_path / "auto"
    auto.mkdir()
    assert [r["expected"] for r in stranded_from_rows([_row("ready")], [_record(repo=str(manual))])] == [True]
    assert [r["expected"] for r in stranded_from_rows([_row("ready")], [_record(repo=str(auto))])] == [False]


def test_a_done_row_for_the_same_task_is_not_stranded():
    assert stranded_from_rows([_row("done")], [_record()]) == []


_LOG_LINE = "approved but not landed: t1 — cox runs land r1 --task t1 --repo /here"
_LAND = "cox runs land r1 --task t1 --repo /here"


def _fetched_record(**over):
    return {**_stranded_record("r1", "/here"), "task": "t1", **over}


def _fetched_item(state, **over):
    return {"id": "t1", "state": state, "phase": "p1", "repo": "/here", **over}


def _read(records, items, **kw):
    return read_stranded(records, items, lambda repo: True, lambda repo, branch: False, **kw)


def test_a_fetched_run_with_an_approved_task_record_gives_a_row_for_that_run_and_task():
    assert _read([_fetched_record()], [_fetched_item("approved")], log_texts={}) == [
        {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": _LAND, "expected": False},
    ]


def test_a_log_line_alone_gives_a_stranded_row_with_the_line_remedy():
    assert _read([], [_fetched_item("ready")], log_texts={}) == []
    assert _read([], [_fetched_item("ready")], log_texts={"r1": _LOG_LINE + "\n"}) == [
        {"run": "r1", "task": "t1", "phase": "p1", "branch": None, "remedy": _LAND, "expected": False},
    ]


def test_a_task_in_both_the_records_and_the_log_appears_once():
    rows = _read([_fetched_record()], [_fetched_item("ready")], log_texts={"r1": _LOG_LINE})
    assert [(r["run"], r["task"], r["branch"]) for r in rows] == [("r1", "t1", "b1")]


def test_a_task_whose_record_a_code_quarantine_superseded_gets_no_row_from_a_stale_log_line():
    record = _fetched_record(cause="code")
    assert _read([record], [_fetched_item("ready")], log_texts={"r1": _LOG_LINE}) == []


def test_a_task_waiting_on_its_carried_phase_branch_gets_no_row_from_its_log_line():
    record = _fetched_record(branch="epic/acme/p1")
    rows = read_stranded([record], [_fetched_item("ready")], lambda repo: True, lambda repo, branch: True, log_texts={"r1": _LOG_LINE})
    assert rows == []


def test_a_log_line_for_an_item_rebuilt_from_the_run_or_sent_back_by_a_land_conflict_gets_no_row():
    assert _read([], [_fetched_item("ready", rebuilt_from=["r1"])], log_texts={"r1": _LOG_LINE}) == []
    assert _read([], [_fetched_item("ready", land_conflict="x")], log_texts={"r1": _LOG_LINE}) == []


def test_a_log_line_for_a_task_in_two_repos_gets_no_row():
    items = [_fetched_item("ready"), _fetched_item("ready", repo="/there")]
    assert _read([], items, log_texts={"r1": _LOG_LINE}) == []


def test_a_log_line_for_a_manual_repo_gets_the_manual_remedy(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".agent").mkdir(parents=True)
    (repo / ".agent" / "cartridge.yaml").write_text("land: manual\n")
    rows = _read([], [_fetched_item("ready", repo=str(repo))], log_texts={"r1": _LOG_LINE})
    assert [(r["remedy"], r["expected"]) for r in rows] == [(land_manual.REMEDY, True)]


def test_a_record_row_for_a_manual_repo_is_expected_and_one_for_an_automatic_repo_is_not(tmp_path):
    manual = tmp_path / "manual"
    (manual / ".agent").mkdir(parents=True)
    (manual / ".agent" / "cartridge.yaml").write_text("land: manual\n")
    auto = tmp_path / "auto"
    auto.mkdir()
    records = [_stranded_record("r1", str(manual)), _stranded_record("r2", str(auto))]
    rows = _read(records, [_stranded_item("r1"), _stranded_item("r2")], log_texts={})
    assert [(r["run"], r["expected"]) for r in rows] == [("r1", True), ("r2", False)]
    assert rows[0]["remedy"] == land_manual.REMEDY


def test_the_default_edge_reads_fetched_runs_logs_and_skips_missing_and_landed_elsewhere(tmp_path):
    for run in ("r1", "r2", "r3"):
        (tmp_path / f"{run}.fetched.json").write_text("{}")
    (tmp_path / "r3.fetched.json").write_text('{"landed_elsewhere": true}')
    for run in ("r1", "r3"):
        (tmp_path / f"{run}.log").write_text(_LOG_LINE.replace("r1", run) + "\n")
    assert [r["run"] for r in _read([], [_fetched_item("ready")], runs_dir=tmp_path)] == ["r1"]
