import json
from pathlib import Path

from agent_tools import cli, remote_lane, run_store, store_fill

RUN = "demo-1"
INITIATIVE_MD = "---\nid: demo\ntitle: Demo\nrepo: /r\n---\nwhy\n"
TASK_MD = "---\nid: t1\nstate: ready\ntitle: Do it\n---\nbody\n"
ENDED = (True, "2026-11-03T05:00:00Z")
LIVE = (False, None)


def _world(tmp_path, monkeypatch, *, graph: str, facts=ENDED):
    """A chair workspace with a remote record, the rsync edge recording argvs and the store faked in memory."""
    ws, host_ws = tmp_path / "chair", tmp_path / "host"
    (ws / "runs").mkdir(parents=True)
    record = remote_lane.remote_record("box", "2026-11-03T04:00:00Z", graph=graph, **({"repo": "/r"} if graph == "epic" else {}))
    (ws / "runs" / f"{RUN}.remote.json").write_text(json.dumps({**record, **({"initiative": "demo"} if graph == "decompose" else {})}))
    host = cli.lane_hosts.LaneHost(name="box", ssh="me@box", workspace_dir=str(host_ws))
    argvs: list[list[str]] = []

    def run(argv):
        argvs.append(argv)
        if any(part.endswith("/work/demo/") for part in argv) and "--list-only" not in argv:
            (ws / "work" / "demo" / "p1").mkdir(parents=True, exist_ok=True)
            (ws / "work" / "demo" / "initiative.md").write_text(INITIATIVE_MD)
            (ws / "work" / "demo" / "p1" / "t1.md").write_text(TASK_MD)
        return 0

    rows: list[dict] = []
    monkeypatch.setattr(cli, "_remote_edge", lambda cwd: (run, None))
    monkeypatch.setattr(cli, "_remote_fetch_facts", lambda runs_dir, run_id: facts)
    monkeypatch.setattr(store_fill, "_read_store", lambda runs_dir: list(rows))
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: rows.append(row) or "")
    return ws, host, argvs, rows


def test_a_fetched_decompose_leaves_its_files_in_the_workspace_and_the_store(tmp_path, monkeypatch):
    ws, host, argvs, rows = _world(tmp_path, monkeypatch, graph="decompose")
    outcome, _, name = cli._fetch_one(ws / "runs", [host], RUN)
    assert (outcome, name) == ("fetched", "box")
    assert (ws / "work" / "demo" / "initiative.md").read_text() == INITIATIVE_MD
    assert (ws / "work" / "demo" / "p1" / "t1.md").read_text() == TASK_MD
    assert {(r["kind"], r["initiative"], r["task_id"]) for r in rows} == {("initiative", "demo", "demo"), ("task", "demo", "t1")}
    marker = json.loads((ws / "runs" / f"{RUN}.fetched.json").read_text())
    assert (marker["initiative"], marker["tasks"]) == ("demo", ["p1/t1.md"])
    assert [argv[0] for argv in argvs] == ["rsync", "rsync"]


def test_a_decompose_fetch_records_the_pulled_files_as_written(tmp_path, monkeypatch):
    ws, host, _, _ = _world(tmp_path, monkeypatch, graph="decompose")
    written = []
    monkeypatch.setattr(cli.queue_export, "record_written_paths", lambda workspace, paths: written.append((workspace, [Path(p) for p in paths])))
    cli._fetch_one(ws / "runs", [host], RUN)
    assert written == [(ws, [ws / "work/demo/initiative.md", ws / "work/demo/p1/t1.md"])]


def test_a_live_decompose_is_not_fetched(tmp_path, monkeypatch):
    ws, host, argvs, rows = _world(tmp_path, monkeypatch, graph="decompose", facts=LIVE)
    outcome, lines, _ = cli._fetch_one(ws / "runs", [host], RUN)
    assert outcome == "live"
    assert "still live on box" in lines[0]
    assert (argvs, rows, (ws / "runs" / f"{RUN}.fetched.json").exists()) == ([], [], False)


def test_a_failed_store_fill_leaves_the_decompose_unfetched(tmp_path, monkeypatch):
    ws, host, _, _ = _world(tmp_path, monkeypatch, graph="decompose")
    monkeypatch.setattr(store_fill, "_read_store", lambda runs_dir: None)
    outcome, lines, _ = cli._fetch_one(ws / "runs", [host], RUN)
    assert outcome == "failed" and lines[0].startswith("fetch: store:")
    assert not (ws / "runs" / f"{RUN}.fetched.json").exists()


def test_fetch_all_picks_up_an_ended_decompose(tmp_path, monkeypatch, capsys):
    ws, host, _, rows = _world(tmp_path, monkeypatch, graph="decompose")
    assert cli._runs_fetch_all(ws / "runs", [host]) == 0
    assert f"{RUN}: fetched from box" in capsys.readouterr().out
    assert len(rows) == 2


def test_an_epic_record_takes_the_old_path(tmp_path, monkeypatch):
    ws, host, argvs, rows = _world(tmp_path, monkeypatch, graph="epic")
    monkeypatch.setattr(cli, "_fetch_decompose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("epic must not take it")))
    outcome, _, _ = cli._fetch_one(ws / "runs", [host], RUN)
    assert outcome == "fetched"
    assert any(argv[:1] == ["git"] for argv in argvs)
    assert json.loads((ws / "runs" / f"{RUN}.fetched.json").read_text())["repos"] == ["/r"]
    assert rows == []
