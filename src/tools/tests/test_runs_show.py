"""`cox runs show` and `cox runs log`: where they read from is pure (`runs_show`); the commands run against a real sqlite
store in the runs directory and a real `file://` artifacts root, written by the save path's own `upload`."""

import hashlib

import pytest

from agent_tools import doctor
from agent_tools.cli import main
from agent_tools.cli_doctor import _doctor_artifacts_rows
from agent_tools.runs_show import BUCKET, RUN_FOLDER, Local, Stored, header, local_order, marked, plan, stored

AT = "2026-11-20T00:00:00Z"
TRANSCRIPT = b'{"type":"assistant","message":{"content":[{"type":"text","text":"built it"}]}}\n'
LOG = b"1 passed in 0.01s\n"


def _item(key: str, data: bytes, media: str) -> dict:
    return {"key": key, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "media": media}


T_ITEM = _item("v1/runs/demo-1/task__t1__build/transcript-build-1.jsonl@0c9e1d2a7b44", TRANSCRIPT, "application/x-ndjson")
P_ITEM = _item("v1/runs/demo-1/task__t1__build/patch.diff@3f1a9c0b2d4e", b"diff", "text/x-diff")
L_ITEM = _item("v1/runs/demo-1/task__t1__checks/01-pytest.log@a81f00c3e9d2", LOG, "text/plain")
L2_ITEM = _item("v1/runs/demo-1/task__t1__checks/02-lint.log@b81f00c3e9d2", b"clean\n", "text/plain")
ROWS = [{"step": "task/t1/build", "phase": "p1", "task_id": "t1", "artifacts_json": [P_ITEM, T_ITEM]},
        {"step": "task/t1/checks", "phase": "p1", "task_id": "t1", "artifacts_json": [L_ITEM, L2_ITEM]},
        {"step": "task/t1/plan", "artifacts_json": None}]
SHA = hashlib.sha256(LOG).hexdigest()


def test_show_takes_the_transcripts_and_log_the_check_logs_never_the_patch():
    assert stored(ROWS, "show") == (Stored("task/t1/build", "transcript-build-1.jsonl", T_ITEM, "p1", "t1"),)
    assert [s.name for s in stored(ROWS, "log")] == ["01-pytest.log", "02-lint.log"]


def _sources(copies):
    return [(c.label, c.source) for c in copies]


def test_each_copy_comes_from_the_run_folder_when_it_holds_it_and_the_rest_from_the_bucket():
    items = stored(ROWS, "log")
    copies, why = plan("demo-1", "log", [Local("demo-1/checks/p1/t1/1.log", SHA)], items, False)
    assert why == "" and _sources(copies) == [("demo-1/checks/p1/t1/1.log", RUN_FOLDER),
                                             ("task/t1/checks 02-lint.log", BUCKET)]
    assert copies[0].note == "" and copies[1].stored == items[1]
    whole, _ = plan("demo-1", "log", [], items, False)
    assert _sources(whole) == [("task/t1/checks 01-pytest.log", BUCKET), ("task/t1/checks 02-lint.log", BUCKET)]


def test_a_local_copy_of_other_bytes_than_its_stored_object_is_printed_with_a_note():
    copies, _ = plan("demo-1", "log", [Local("demo-1/checks/p1/t1/1.log", "0" * 64)], stored(ROWS, "log"), False)
    assert copies[0].source == RUN_FOLDER
    assert copies[0].note.startswith("note: demo-1/checks/p1/t1/1.log differs from the bucket's task/t1/checks "
                                     "01-pytest.log (sha256 000000000000 here,")
    scrubbed = ({"step": "task/t1/checks", "phase": "p1", "task_id": "t1", "artifacts_json": [{**L_ITEM, "scrubbed": True}]},)
    quiet, _ = plan("demo-1", "log", [Local("demo-1/checks/p1/t1/1.log", "0" * 64)], stored(scrubbed, "log"), False)
    assert quiet[0].note == ""  # a scrubbed copy differs by design


def test_a_trace_file_matches_its_transcript_and_a_level_log_matches_nothing():
    copies, _ = plan("demo-1", "show", [Local("demo-1-trace/build-1.jsonl", hashlib.sha256(TRANSCRIPT).hexdigest())],
                     stored(ROWS, "show"), False)
    assert _sources(copies) == [("demo-1-trace/build-1.jsonl", RUN_FOLDER)]
    level, _ = plan("demo-1", "log", [Local("demo-1/checks/p1/level/1.log", SHA)], stored(ROWS, "log"), False)
    assert [c.source for c in level] == [RUN_FOLDER, BUCKET, BUCKET]


def test_with_neither_the_sentence_says_why_and_names_an_upload_failure_when_a_row_records_one():
    assert plan("demo-1", "show", [], (), False) == ((), (
        "demo-1: no transcripts in the run folder, and no step row lists any in object storage (is artifacts_url set?)"))
    assert "upload failed (cox runs artifacts shows it)" in plan("demo-1", "log", [], (), True)[1]


def test_marked_reads_the_upload_error_a_row_records():
    assert marked([{"result_json": {"artifacts_error": "OSError: down"}}]) is True
    assert marked([{"result_json": {}}, {"result_json": None}, {}]) is False


def test_local_copies_come_in_natural_order_and_each_has_a_header():
    assert local_order(["r/checks/p1/t1/10.log", "r/checks/p1/t1/2.log", "r/checks/p1/t1/1.log"]) == (
        "r/checks/p1/t1/1.log", "r/checks/p1/t1/2.log", "r/checks/p1/t1/10.log")
    assert header("task/t1/checks 01-pytest.log", "from file:///a") == "== task/t1/checks 01-pytest.log (from file:///a)"


# --- the commands, against a real store and a real file:// root ----------------------------------------------------


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A runs directory with a sqlite store and a provider profile whose artifacts_url is a file:// folder, holding one
    run whose build and checks were saved through `upload`, as the epic driver saves them."""
    pytest.importorskip("pyarrow")
    from harness.artifacts import Source, artifacts_root, upload
    from harness.store_migrate import open_store
    from harness.store_steps import put_step, step_row

    runs, root_dir = tmp_path / "runs", tmp_path / "artifacts"
    (runs / "demo-1" / "checks" / "p1" / "t1").mkdir(parents=True)
    (runs / "demo-1" / "checks" / "p1" / "t1" / "1.log").write_bytes(LOG)
    (runs / "demo-1-trace").mkdir()
    (runs / "demo-1-trace" / "build-1.jsonl").write_bytes(TRANSCRIPT)
    provider = tmp_path / "provider.yaml"
    provider.write_text(f"artifacts_url: file://{root_dir}\n", encoding="utf-8")
    routing = tmp_path / "profile.yaml"
    routing.write_text(f"provider_profile: {provider}\nworkspace_dir: {tmp_path}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(routing))
    root = artifacts_root({"artifacts_url": f"file://{root_dir}"}, {}, runs)
    assert root is not None
    rows = [step_row(run_id="demo-1", step=f"task/t1/{kind}", initiative="demo", phase="p1", task_id="t1", kind=kind,
                     round_=1, ok=True, verdict="", result={}, finished_at=AT) for kind in ("build", "checks")]
    done, error = upload(root, "demo-1", rows, (
        Source("task/t1/build", "transcript-build-1.jsonl", "application/x-ndjson", path=runs / "demo-1-trace" / "build-1.jsonl"),
        Source("task/t1/checks", "01-pytest.log", "text/plain", path=runs / "demo-1" / "checks" / "p1" / "t1" / "1.log"),
    ))
    assert error is None
    conn = open_store(f"sqlite:///{runs / 'cox.db'}", AT)
    for row in done:
        put_step(conn, row)
    conn.close()
    return runs, root_dir, routing


def _removed(path):
    for p in sorted(path.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    path.rmdir()


def test_with_the_run_folder_the_commands_print_its_copies(lane, capsys):
    runs, _, _ = lane
    assert main(["runs", "log", "demo-1", "--runs-dir", str(runs)]) == 0
    assert capsys.readouterr().out == f"== demo-1/checks/p1/t1/1.log (run folder)\n{LOG.decode()}"
    assert main(["runs", "show", "demo-1", "--runs-dir", str(runs)]) == 0
    assert capsys.readouterr().out == f"== demo-1-trace/build-1.jsonl (run folder)\n{TRANSCRIPT.decode()}"


def test_once_the_run_folder_is_gone_the_commands_read_the_bucket(lane, capsys):
    runs, root_dir, _ = lane
    _removed(runs / "demo-1")
    _removed(runs / "demo-1-trace")
    assert main(["runs", "log", "demo-1", "--runs-dir", str(runs)]) == 0
    assert capsys.readouterr().out == f"== task/t1/checks 01-pytest.log (from file://{root_dir})\n{LOG.decode()}"
    assert main(["runs", "show", "demo-1", "--runs-dir", str(runs)]) == 0
    out = capsys.readouterr().out
    assert out == f"== task/t1/build transcript-build-1.jsonl (from file://{root_dir})\n{TRANSCRIPT.decode()}"


def test_a_folder_missing_one_copy_reads_that_one_from_the_bucket_and_notes_a_local_copy_that_differs(lane, capsys):
    runs, root_dir, _ = lane
    (runs / "demo-1-trace" / "build-1.jsonl").unlink()
    (runs / "demo-1" / "checks" / "p1" / "t1" / "1.log").write_bytes(b"edited by hand\n")
    assert main(["runs", "show", "demo-1", "--runs-dir", str(runs)]) == 0
    out = capsys.readouterr().out
    assert out == f"== task/t1/build transcript-build-1.jsonl (from file://{root_dir})\n{TRANSCRIPT.decode()}"
    assert main(["runs", "log", "demo-1", "--runs-dir", str(runs)]) == 0
    done = capsys.readouterr()
    assert done.out == "== demo-1/checks/p1/t1/1.log (run folder)\nedited by hand\n"
    assert "differs from the bucket's task/t1/checks 01-pytest.log" in done.err


def test_a_bucket_object_that_no_longer_matches_its_row_fails_the_read(lane, capsys):
    runs, root_dir, _ = lane
    _removed(runs / "demo-1")
    stored_log = next(root_dir.rglob("01-pytest.log@*"))
    stored_log.chmod(0o644)
    stored_log.write_bytes(LOG.upper())
    assert main(["runs", "log", "demo-1", "--runs-dir", str(runs)]) == 1
    assert "ArtifactCorrupt" in capsys.readouterr().err


def test_a_run_with_nothing_anywhere_exits_2_and_says_so(lane, capsys):
    runs, _, _ = lane
    assert main(["runs", "log", "other-1", "--runs-dir", str(runs)]) == 2
    assert "no check logs in the run folder" in capsys.readouterr().out


def test_rows_that_name_objects_with_no_artifacts_url_to_read_them_from_fail(lane, capsys, tmp_path):
    runs, _, routing = lane
    _removed(runs / "demo-1")
    (tmp_path / "provider.yaml").write_text("{}\n", encoding="utf-8")
    assert main(["runs", "log", "demo-1", "--runs-dir", str(runs)]) == 1
    assert "sets no artifacts_url" in capsys.readouterr().err


# --- the doctor's artifacts root row --------------------------------------------------------------------------------


def test_the_artifacts_row_names_the_place_and_the_stage_that_failed_never_the_url():
    assert doctor.artifacts_rows(None, None, None) == []
    ok = doctor.artifacts_rows("s3://cox-artifacts/a", None, None)[0]
    assert ok["ok"] and ok["detail"] == "s3 bucket cox-artifacts: a probe object was written, read back and removed"
    bad = doctor.artifacts_rows("file:///srv/a", None, ("write", "PermissionError"))[0]
    assert not bad["ok"] and bad["detail"].startswith("local folder: the probe object's write failed (PermissionError)")
    assert "/srv/a" not in bad["detail"]
    assert doctor.artifacts_rows("s3://b/a", "unknown scheme", None)[0]["detail"] == "artifacts_url is refused: unknown scheme"


def test_the_doctor_probes_a_real_root_and_leaves_nothing_behind(lane, tmp_path):
    _, root_dir, routing = lane
    profile = {"provider_profile": str(tmp_path / "provider.yaml"), "workspace_dir": str(tmp_path)}
    before = sorted(p.relative_to(root_dir) for p in root_dir.rglob("*") if p.is_file())
    [row] = _doctor_artifacts_rows(profile)
    assert (row["check"], row["ok"]) == ("artifacts root", True), row
    assert sorted(p.relative_to(root_dir) for p in root_dir.rglob("*") if p.is_file()) == before
    assert not (root_dir / "v1" / "probe").exists()


def test_the_doctor_fails_a_root_it_cannot_write(tmp_path):
    pytest.importorskip("pyarrow")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the root's folder should be", encoding="utf-8")
    provider = tmp_path / "provider.yaml"
    provider.write_text(f"artifacts_url: file://{blocker}/root\n", encoding="utf-8")
    [row] = _doctor_artifacts_rows({"provider_profile": str(provider), "workspace_dir": str(tmp_path)})
    assert not row["ok"] and "write failed" in row["detail"] and str(blocker) not in row["detail"], row


def test_without_artifacts_url_the_doctor_has_no_artifacts_row(tmp_path):
    provider = tmp_path / "provider.yaml"
    provider.write_text("{}\n", encoding="utf-8")
    assert _doctor_artifacts_rows({"provider_profile": str(provider), "workspace_dir": str(tmp_path)}) == []
