import json
from pathlib import Path

from agent_tools import cli, remote_fetch, remote_lane, run_store, store_fill

RUN = "demo-1"
INITIATIVE_MD = "---\nid: demo\ntitle: Demo\nrepo: /r\n---\nwhy\n"
TASK_MD = "---\nid: t1\nstate: ready\ntitle: Do it\n---\nbody\n"
ENDED = (True, "2026-11-03T05:00:00Z")


def _destinations(argvs):
    return [argv[-1] for argv in argvs]


def test_an_epic_fetch_pulls_no_path_under_work():
    argvs = remote_fetch.pull_argvs("me@box:/hw/runs/demo-1", "me@box:/hw/runs/demo-1.log", "/cw/runs", RUN)
    assert [part for argv in argvs for part in argv if "/work/" in part] == []


def test_a_decompose_fetch_pulls_work_only_into_staging_never_into_the_workspace():
    argvs = remote_fetch.decompose_pull_argvs("me@box", "/hw", "/cw", "demo", RUN)
    assert _destinations(argvs) == ["/cw/runs/.fetch-staging/demo-1/work/demo/", "/cw/runs/"]
    assert not any(dest.startswith("/cw/work/") for dest in _destinations(argvs))


def _world(tmp_path, monkeypatch):
    """A chair workspace with a decompose record, an rsync edge that writes into whatever destination it is given, and a switchable in-memory store."""
    ws, host_ws = tmp_path / "chair", tmp_path / "host"
    (ws / "runs").mkdir(parents=True)
    record = remote_lane.remote_record("box", "2026-11-03T04:00:00Z", graph="decompose")
    (ws / "runs" / f"{RUN}.remote.json").write_text(json.dumps({**record, "initiative": "demo"}))
    host = cli.lane_hosts.LaneHost(name="box", ssh="me@box", workspace_dir=str(host_ws))
    store = {"up": True, "refuse": "", "drop": set(), "rows": []}

    def run(argv):
        if argv[-2].endswith("/work/demo/"):
            dest = Path(argv[-1])
            (dest / "p1").mkdir(parents=True, exist_ok=True)
            (dest / "initiative.md").write_text(INITIATIVE_MD)
            (dest / "p1" / "t1.md").write_text(TASK_MD)
        return 0

    def upsert(runs_dir, row):
        if store["refuse"]:
            return store["refuse"]
        if store["up"]:
            store["rows"].append(row)
        return ""

    monkeypatch.setattr(cli, "_remote_edge", lambda cwd: (run, None))
    monkeypatch.setattr(cli, "_remote_fetch_facts", lambda runs_dir, run_id: ENDED)
    monkeypatch.setattr(store_fill, "_read_store", lambda runs_dir: [r for r in store["rows"] if r["task_id"] not in store["drop"]] if store["up"] else None)
    monkeypatch.setattr(run_store, "upsert_row_detail", upsert)
    return ws, host, store


def _kept_staged(ws):
    return sorted(p.name for p in (ws / "runs" / ".fetch-staging" / RUN / "work" / "demo").rglob("*.md"))


def _assert_unfetched(ws):
    assert _kept_staged(ws) == ["initiative.md", "t1.md"]
    assert not (ws / "runs" / f"{RUN}.fetched.json").exists()
    assert not (ws / "work").exists()


def test_a_store_outage_keeps_the_staged_files_and_writes_no_marker_and_a_later_fetch_succeeds(tmp_path, monkeypatch):
    ws, host, store = _world(tmp_path, monkeypatch)
    store["up"] = False
    outcome, lines, _ = cli._fetch_one(ws / "runs", [host], RUN)
    assert outcome == "failed" and lines[0].startswith("fetch: store:")
    _assert_unfetched(ws)
    store["up"] = True
    outcome, _, _ = cli._fetch_one(ws / "runs", [host], RUN)
    assert outcome == "fetched"
    assert (ws / "work" / "demo" / "p1" / "t1.md").read_text() == TASK_MD
    assert {r["task_id"] for r in store["rows"]} == {"demo", "t1"}
    assert json.loads((ws / "runs" / f"{RUN}.fetched.json").read_text())["tasks"] == ["p1/t1.md"]
    assert not (ws / "runs" / ".fetch-staging").exists()


def test_a_refused_row_keeps_the_staged_files_and_writes_no_marker(tmp_path, monkeypatch):
    ws, host, store = _world(tmp_path, monkeypatch)
    store["refuse"] = "disk full"
    outcome, lines, _ = cli._fetch_one(ws / "runs", [host], RUN)
    assert outcome == "failed" and "disk full" in lines[0]
    _assert_unfetched(ws)


def test_a_row_the_read_back_does_not_return_is_not_success(tmp_path, monkeypatch):
    ws, host, store = _world(tmp_path, monkeypatch)
    store["drop"] = {"t1"}
    outcome, lines, _ = cli._fetch_one(ws / "runs", [host], RUN)
    assert outcome == "failed" and "not read back: demo/t1" in lines[0]
    _assert_unfetched(ws)
