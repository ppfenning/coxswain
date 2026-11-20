"""harness.artifacts: the key layout, the write-once rule and what each step carries are pure; the uploads run against
a real local filesystem root and a real sqlite store, with no stand-ins."""

import hashlib
import json

import pytest

from harness.artifacts import (
    ArtifactConflict,
    Source,
    artifacts_root,
    attach,
    decide,
    entry,
    key,
    put_object,
    sources,
    step_artifacts,
    upload,
)
from harness.store_migrate import open_store
from harness.store_steps import put_step, step_row, steps_for, steps_of_result

AT = "2026-11-20T00:00:00Z"
PATCH = "--- a/NOTE.md\n+++ b/NOTE.md\n@@ -0,0 +1 @@\n+ok\n"
RESULT = {
    "run_id": "ship-1:p1:t1",
    "plan": {"steps": ["write the note"], "files_expected": ["NOTE.md"]},
    "build": {"patch": PATCH, "files_touched": ["NOTE.md"]},
    "review": {"verdict": "approve", "findings": []},
    "change_facts": {
        "checks": [
            {"name": "unit tests", "passed": True, "exit_code": 0, "log": "checks/p1/t1/1.log"},
            {"name": "lint", "passed": True, "exit_code": 0},
        ],
    },
}


def rows_of(result=RESULT):
    return steps_of_result(result, run_id="ship-1", initiative="ship", phase="p1", task="t1", at=AT)


def by_step(rows):
    return {r["step"]: r for r in rows}


# --- key ------------------------------------------------------------------------------------------------------------


def test_a_key_puts_the_step_under_the_run_with_its_slashes_as_double_underscores():
    assert key("ship-7", "task/t1/build", "patch.diff") == "v1/runs/ship-7/task__t1__build/patch.diff"
    assert key("ship-7", "phase/review/1", "transcript.jsonl") == "v1/runs/ship-7/phase__review__1/transcript.jsonl"


@pytest.mark.parametrize(("run_id", "step", "name"), [
    ("", "task/t1/build", "patch.diff"),
    ("ship-7", "task//build", "patch.diff"),
    ("ship-7", "task/../build", "patch.diff"),
    ("ship-7", "task/t1/build", "a/b"),
    ("ship/7", "task/t1/build", "patch.diff"),
    ("ship-7", "task/t1/build", ".."),
])
def test_a_key_refuses_a_part_that_could_name_another_place(run_id, step, name):
    with pytest.raises(ValueError, match="not a plain name"):
        key(run_id, step, name)


# --- entry and decide -----------------------------------------------------------------------------------------------


def test_an_entry_records_the_size_and_the_sha256_of_the_bytes():
    data = b"hello\n"
    assert entry("v1/runs/r/s/n", data, "text/plain") == {
        "key": "v1/runs/r/s/n", "bytes": 6, "sha256": hashlib.sha256(data).hexdigest(), "media": "text/plain"}


def test_a_key_is_written_when_empty_left_when_it_holds_the_same_bytes_and_refused_when_it_holds_others():
    assert decide("k", b"a", None) == "write"
    assert decide("k", b"a", b"a") == "same"
    with pytest.raises(ArtifactConflict, match="k already holds different content"):
        decide("k", b"a", b"b")


# --- sources and attach ---------------------------------------------------------------------------------------------


def test_the_build_carries_its_patch_the_checks_each_logged_check_and_each_role_its_transcripts(tmp_path):
    calls = [("plan", tmp_path / "plan-1.jsonl"), ("build", tmp_path / "build-2.jsonl"),
             ("review_charter", tmp_path / "review_charter-3.jsonl"), ("validate_phase", tmp_path / "v-4.jsonl")]
    found = sources(rows_of(), RESULT, run_dir=tmp_path / "ship-1", calls=calls)
    assert [(s.step, s.name) for s in found] == [
        ("task/t1/plan", "transcript-plan-1.jsonl"),
        ("task/t1/build", "patch.diff"),
        ("task/t1/build", "transcript-build-2.jsonl"),
        ("task/t1/review", "transcript-review_charter-3.jsonl"),
        ("task/t1/checks", "01-unit-tests.log"),
    ]
    patch = next(s for s in found if s.name == "patch.diff")
    assert (patch.text, patch.path, patch.media) == (PATCH, None, "text/x-diff")
    log = next(s for s in found if s.name.endswith(".log"))
    assert log.path == tmp_path / "ship-1" / "checks" / "p1" / "t1" / "1.log"


def test_a_build_with_no_patch_and_checks_with_no_log_carry_nothing(tmp_path):
    bare = {**RESULT, "build": {"patch": "  \n", "quarantine": "no patch"}, "change_facts": {"checks": [{"name": "x"}]}}
    assert sources(rows_of(bare), bare, run_dir=tmp_path) == ()


def test_attach_appends_the_entries_and_leaves_the_rest_of_the_row():
    row = step_row(run_id="r", step="task/t1/build", initiative="i", phase="p", task_id="t1", kind="build", round_=1,
                   ok=True, verdict="", result={}, finished_at=AT)
    listed = attach(row, [{"key": "k"}])
    assert listed["artifacts_json"] == [{"key": "k"}]
    assert {**listed, "artifacts_json": []} == row
    assert row["artifacts_json"] == []


# --- the edges, on a real root --------------------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path):
    pytest.importorskip("pyarrow")
    found = artifacts_root({"artifacts_url": f"file://{tmp_path / 'artifacts'}"}, {})
    assert found is not None
    return found


def test_no_artifacts_url_is_no_root():
    assert artifacts_root({}, {}) is None
    assert artifacts_root({"artifacts_url": ""}, {}) is None


def test_a_bad_artifacts_url_is_refused_under_its_own_name():
    with pytest.raises(ValueError, match="artifacts url must not carry credentials"):
        artifacts_root({"artifacts_url": "s3://user:pw@bucket/a"}, {})


def test_a_plain_path_is_a_local_root(tmp_path):
    pytest.importorskip("pyarrow")
    found = artifacts_root({"artifacts_url": str(tmp_path)}, {})
    assert found is not None and found.path == str(tmp_path)


def test_put_object_writes_once_repeats_as_a_no_op_and_refuses_new_bytes(root, tmp_path):
    assert put_object(root, "v1/runs/r/s/n.log", b"one") is True
    assert put_object(root, "v1/runs/r/s/n.log", b"one") is False
    with pytest.raises(ArtifactConflict):
        put_object(root, "v1/runs/r/s/n.log", b"two")
    assert (tmp_path / "artifacts" / "v1" / "runs" / "r" / "s" / "n.log").read_bytes() == b"one"


def test_upload_writes_each_source_then_lists_it_on_its_row_and_skips_one_gone_or_empty(root, tmp_path):
    rows = rows_of()
    log = tmp_path / "1.log"
    log.write_text("passed\n", encoding="utf-8")
    found = (Source("task/t1/build", "patch.diff", "text/x-diff", text=PATCH),
             Source("task/t1/checks", "01-unit-tests.log", "text/plain", path=log),
             Source("task/t1/checks", "02-gone.log", "text/plain", path=tmp_path / "gone.log"),
             Source("task/t1/checks", "03-empty.log", "text/plain", text=""))
    listed = by_step(upload(root, "ship-1", rows, found))
    assert [e["key"] for e in listed["task/t1/build"]["artifacts_json"]] == ["v1/runs/ship-1/task__t1__build/patch.diff"]
    assert [e["key"] for e in listed["task/t1/checks"]["artifacts_json"]] == ["v1/runs/ship-1/task__t1__checks/01-unit-tests.log"]
    assert listed["task/t1/plan"]["artifacts_json"] == []
    base = tmp_path / "artifacts" / "v1" / "runs" / "ship-1"
    assert (base / "task__t1__build" / "patch.diff").read_text(encoding="utf-8") == PATCH
    assert (base / "task__t1__checks" / "01-unit-tests.log").read_text(encoding="utf-8") == "passed\n"


def test_an_upload_conflict_raises_before_any_row_comes_back(root):
    put_object(root, "v1/runs/ship-1/task__t1__build/patch.diff", b"another patch")
    with pytest.raises(ArtifactConflict):
        upload(root, "ship-1", rows_of(), (Source("task/t1/build", "patch.diff", "text/x-diff", text=PATCH),))


def _store(tmp_path):
    return open_store(f"sqlite:///{tmp_path / 'store.db'}", AT)


def test_without_a_root_the_save_path_gets_its_rows_back_unchanged(tmp_path):
    conn = _store(tmp_path)
    rows = rows_of()
    assert step_artifacts(None, conn, rows, RESULT, runs_dir=tmp_path, run_id="ship-1", phase="p1", task="t1") == rows
    conn.close()


def test_the_save_path_uploads_each_new_steps_artifacts_and_its_stored_rows_list_them(root, tmp_path):
    conn = _store(tmp_path)
    log = tmp_path / "runs" / "ship-1" / "checks" / "p1" / "t1" / "1.log"
    log.parent.mkdir(parents=True)
    log.write_text("1 passed\n", encoding="utf-8")
    trace = tmp_path / "runs" / "ship-1-trace" / "build-1.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"type": "result"}\n', encoding="utf-8")
    conn.execute(
        "INSERT INTO node_calls (call_id, run_id, seq, phase_id, task_id, role, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("c1", "ship-1", 1, "p1", "t1", "build", json.dumps({"trace": str(trace)})),
    )

    def save():
        for row in step_artifacts(root, conn, rows_of(), RESULT, runs_dir=tmp_path / "runs", run_id="ship-1",
                                  phase="p1", task="t1"):
            put_step(conn, row)

    save()
    stored = by_step(steps_for(conn, "ship-1"))
    assert [e["key"] for e in stored["task/t1/build"]["artifacts_json"]] == [
        "v1/runs/ship-1/task__t1__build/patch.diff", "v1/runs/ship-1/task__t1__build/transcript-build-1.jsonl"]
    assert [e["key"] for e in stored["task/t1/checks"]["artifacts_json"]] == [
        "v1/runs/ship-1/task__t1__checks/01-unit-tests.log"]
    for row in stored.values():
        for item in row["artifacts_json"]:
            data = (tmp_path / "artifacts" / str(item["key"])).read_bytes()
            assert (item["bytes"], item["sha256"]) == (len(data), hashlib.sha256(data).hexdigest())

    # A second save, after the local log changed, leaves the stored rows and the written objects as they were.
    log.write_text("rewritten\n", encoding="utf-8")
    save()
    assert by_step(steps_for(conn, "ship-1")) == stored
    assert (tmp_path / "artifacts" / "v1/runs/ship-1/task__t1__checks/01-unit-tests.log").read_text() == "1 passed\n"
    conn.close()
