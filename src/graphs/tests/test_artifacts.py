"""harness.artifacts: the key layout, the write-once rule, the scrub and what each step carries are pure; the uploads
run against a real local filesystem root and a real store (sqlite, and Postgres when COXSWAIN_TEST_PG_URL is set),
through the epic driver's own `record_steps`, with no stand-ins."""

import hashlib
import os
from pathlib import Path

import pytest

from harness.artifacts import (
    ERROR_FIELD,
    ArtifactConflict,
    ArtifactCorrupt,
    ArtifactsRoot,
    Source,
    artifacts_root,
    attach,
    decide,
    entry,
    key,
    locate,
    mark_missing,
    name_of,
    pick,
    prepared,
    probe,
    probe_key,
    put_object,
    read_object,
    scrub,
    secret_values,
    sources,
    step_artifacts,
    upload,
    verified,
)
from harness.epic import Ctx
from harness.epic_records import record_steps
from harness.store_steps import step_row, steps_for, steps_of_result
from harness.store_write import Store

AT = "2026-11-20T00:00:00Z"
PATCH = "--- a/NOTE.md\n+++ b/NOTE.md\n@@ -0,0 +1 @@\n+ok\n"
SHA = hashlib.sha256(PATCH.encode()).hexdigest()
RESULT = {
    "run_id": "ship-1:p1:t1",
    "plan": {"steps": ["write the note"], "files_expected": ["NOTE.md"]},
    "build": {"patch": PATCH, "files_touched": ["NOTE.md"]},
    "review": {"verdict": "approve", "findings": []},
    "change_facts": {
        "checks_run": "ship-1",
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


def keys_of(row):
    return [e.get("key") for e in row["artifacts_json"]]


# --- key ------------------------------------------------------------------------------------------------------------


def test_a_key_puts_the_step_under_the_run_and_names_the_content():
    assert key("ship-7", "task/t1/build", "patch.diff", SHA) == f"v1/runs/ship-7/task__t1__build/patch.diff@{SHA[:12]}"
    assert key("ship-7", "phase/review/1", "t.jsonl", SHA).startswith("v1/runs/ship-7/phase__review__1/t.jsonl@")


@pytest.mark.parametrize(("run_id", "step", "name"), [
    ("", "task/t1/build", "patch.diff"),
    ("ship-7", "task//build", "patch.diff"),
    ("ship-7", "task/../build", "patch.diff"),
    ("ship-7", "task/t1/build", "a/b"),
    ("ship/7", "task/t1/build", "patch.diff"),
    ("ship-7", "task/t1/build", ".."),
    ("ship-7", "task/t1__x/build", "patch.diff"),
    ("ship-7", "task/t1/build", "a__b.log"),
])
def test_a_key_refuses_a_part_that_could_name_another_place(run_id, step, name):
    with pytest.raises(ValueError, match="not a plain name"):
        key(run_id, step, name, SHA)


def test_a_key_refuses_a_digest_that_is_not_a_sha256():
    with pytest.raises(ValueError, match="not a sha256"):
        key("ship-7", "task/t1/build", "patch.diff", "abc")


# --- entry, decide and scrub ----------------------------------------------------------------------------------------


def test_an_entry_records_the_size_and_the_sha256_of_the_bytes():
    data = b"hello\n"
    assert entry("v1/runs/r/s/n", data, "text/plain") == {
        "key": "v1/runs/r/s/n", "bytes": 6, "sha256": hashlib.sha256(data).hexdigest(), "media": "text/plain"}


def test_a_key_is_written_when_empty_left_at_the_same_size_and_refused_at_another():
    assert decide("k", 3, None) == "write"
    assert decide("k", 3, 3) == "same"
    with pytest.raises(ArtifactConflict, match="k already holds 4 bytes, not 3"):
        decide("k", 3, 4)


def test_secret_values_are_the_long_values_of_every_variable_named_like_a_secret():
    env = {"GH_TOKEN": "gho_placeholder0", "CLAUDE_CODE_OAUTH_TOKEN": "sk-placeholder-oauth",
           "PGPASSWORD": "placeholder-pw", "DB_PASSWD": "placeholder-pw2", "AWS_SECRET_ACCESS_KEY": "placeholder-sk",
           "GARAGE_ACCESS_KEY_ID": "GKplaceholder1", "api_key": "lowercase-key", "PATH": "/usr/bin:/bin",
           "SHORT_TOKEN": "abc"}
    assert secret_values(env) == ("sk-placeholder-oauth", "gho_placeholder0", "placeholder-pw2", "GKplaceholder1",
                                  "placeholder-pw", "placeholder-sk", "lowercase-key")


def test_scrub_replaces_each_secret_in_its_escaped_forms_and_leaves_short_ones():
    assert scrub("key=GKabcdef123 x=abc repr='s3cr\\tvalue'", ["GKabcdef123", "s3cr\tvalue", "abc"]) == (
        "key=*** x=abc repr='***'")


def test_scrub_removes_the_userinfo_and_query_passwords_of_every_url():
    text = ("store postgresql://worker:placeholder@db.example.internal:5432/cox?sslpassword=x9 and "
            "https://gho_placeholder@github.com/o/r.git and plain https://example.internal/path?a=1")
    assert scrub(text, []) == (
        "store postgresql://***@db.example.internal:5432/cox?sslpassword=*** and "
        "https://***@github.com/o/r.git and plain https://example.internal/path?a=1")


def test_a_check_that_prints_its_environment_uploads_no_secret_of_it():
    env = {"GH_TOKEN": "gho_placeholder0", "COX_STORE_URL": "postgresql://w:placeholder-pw9@h/db",
           "PGPASSWORD": "placeholder-pw9", "HOME": "/var/empty/placeholder"}
    printed = "\n".join(f"{k}={v}" for k, v in env.items())
    out = scrub(printed, secret_values(env))
    assert all(secret not in out for secret in ("gho_placeholder0", "placeholder-pw9")), out
    assert "HOME=/var/empty/placeholder" in out


# --- sources, attach and mark_missing -------------------------------------------------------------------------------


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
    log = next(s for s in found if s.name.endswith(".log"))
    assert log.path == tmp_path / "ship-1" / "checks" / "p1" / "t1" / "1.log"


def test_a_name_with_a_double_underscore_is_folded_so_its_key_holds():
    rows = rows_of()
    weird = {**RESULT, "change_facts": {**RESULT["change_facts"], "checks": [{"name": "a__b", "log": "x.log"}]}}
    names = [s.name for s in sources(rows, weird, run_dir=Path("/r"), calls=[("build", Path("/t/b__1.jsonl"))])]
    assert names == ["patch.diff", "transcript-b_1.jsonl", "01-a_b.log"]
    assert all(key("ship-1", "task/t1/x", n, SHA) for n in names)


def test_a_build_with_no_patch_and_checks_with_no_log_carry_nothing(tmp_path):
    bare = {**RESULT, "build": {"patch": "  \n", "quarantine": "no patch"},
            "change_facts": {"checks_run": "ship-1", "checks": [{"name": "x"}]}}
    assert sources(rows_of(bare), bare, run_dir=tmp_path) == ()


def test_attach_lists_items_and_mark_missing_records_the_error_in_its_own_field():
    row = step_row(run_id="r", step="task/t1/build", initiative="i", phase="p", task_id="t1", kind="build", round_=1,
                   ok=True, verdict="", result={"patch_ok": True}, finished_at=AT)
    listed = mark_missing(attach(row, [{"key": "k"}]), "boom")
    assert listed["artifacts_json"] == [{"key": "k"}]
    assert listed["result_json"] == {"patch_ok": True, ERROR_FIELD: "boom"}
    assert {**listed, "artifacts_json": [], "result_json": {"patch_ok": True}} == row
    assert row["artifacts_json"] == [] and row["result_json"] == {"patch_ok": True}


# --- the root -------------------------------------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path):
    pytest.importorskip("pyarrow")
    found = artifacts_root({"artifacts_url": f"file://{tmp_path / 'artifacts'}"}, {}, tmp_path / "runs")
    assert found is not None
    return found


def test_no_artifacts_url_is_no_root(tmp_path):
    assert artifacts_root({}, {}, tmp_path) is None
    assert artifacts_root({"artifacts_url": ""}, {}, tmp_path) is None


def test_a_bad_artifacts_url_is_refused_under_its_own_name(tmp_path):
    with pytest.raises(ValueError, match="artifacts url must not carry credentials"):
        artifacts_root({"artifacts_url": "s3://user:pw@bucket/a"}, {}, tmp_path)


def test_a_relative_path_is_under_the_runs_directory_and_an_absolute_one_stays(tmp_path):
    pytest.importorskip("pyarrow")
    relative = artifacts_root({"artifacts_url": "artifacts"}, {}, tmp_path / "runs")
    absolute = artifacts_root({"artifacts_url": str(tmp_path / "elsewhere")}, {}, tmp_path / "runs")
    assert relative is not None and relative.path == str(tmp_path / "runs" / "artifacts")
    assert absolute is not None and absolute.path == str(tmp_path / "elsewhere")


def test_the_root_scrubs_the_secret_values_of_the_lanes_environment(tmp_path):
    pytest.importorskip("pyarrow")
    block = {"endpoint": "http://127.0.0.1:1", "access_key_env": "GARAGE_ACCESS_KEY_ID", "secret_key_env": "GARAGE_SECRET_ACCESS_KEY"}
    env = {"GARAGE_ACCESS_KEY_ID": "GKplaceholder1", "GARAGE_SECRET_ACCESS_KEY": "placeholder-secret",
           "GH_TOKEN": "gho_placeholder0", "HOME": "/var/empty/placeholder"}
    found = artifacts_root({"artifacts_url": "s3://bucket/a", "object_store": block}, env, tmp_path)
    assert found is not None and found.secrets == secret_values(env)
    assert "gho_placeholder0" in found.secrets and "/var/empty/placeholder" not in found.secrets


# --- put_object, on a real local root -------------------------------------------------------------------------------


def test_put_object_writes_once_repeats_as_a_no_op_and_refuses_another_size(root, tmp_path):
    assert put_object(root, "v1/runs/r/s/n.log@0", b"one") is True
    assert put_object(root, "v1/runs/r/s/n.log@0", b"one") is False
    with pytest.raises(ArtifactConflict):
        put_object(root, "v1/runs/r/s/n.log@0", b"three")
    folder = tmp_path / "artifacts" / "v1" / "runs" / "r" / "s"
    assert (folder / "n.log@0").read_bytes() == b"one"
    assert sorted(p.name for p in folder.iterdir()) == ["n.log@0"]  # no temporary file is left behind


def test_a_write_cut_off_part_way_leaves_no_key_and_the_retry_writes_it(root, tmp_path):
    folder = tmp_path / "artifacts" / "v1" / "runs" / "r" / "s"
    folder.mkdir(parents=True)
    (folder / ".n.log@0.abc.part").write_bytes(b"on")  # what a process killed mid-write leaves
    assert put_object(root, "v1/runs/r/s/n.log@0", b"one") is True
    assert (folder / "n.log@0").read_bytes() == b"one"


def test_a_key_another_writer_made_meanwhile_is_the_same_content_or_a_conflict(root, tmp_path):
    folder = tmp_path / "artifacts" / "v1" / "runs" / "r" / "s"
    folder.mkdir(parents=True)
    os.link(_write(tmp_path / "other", b"one"), folder / "n.log@0")
    assert put_object(root, "v1/runs/r/s/n.log@0", b"one") is False


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# --- upload ---------------------------------------------------------------------------------------------------------


def test_upload_writes_each_source_then_lists_it_on_its_row_and_skips_one_gone_or_empty(root, tmp_path):
    log = _write(tmp_path / "1.log", b"passed\n")
    found = (Source("task/t1/build", "patch.diff", "text/x-diff", text=PATCH),
             Source("task/t1/checks", "01-unit-tests.log", "text/plain", path=log),
             Source("task/t1/checks", "02-gone.log", "text/plain", path=tmp_path / "gone.log"),
             Source("task/t1/checks", "03-empty.log", "text/plain", text=""))
    rows, error = upload(root, "ship-1", rows_of(), found)
    listed = by_step(rows)
    assert error is None
    assert keys_of(listed["task/t1/build"]) == [f"v1/runs/ship-1/task__t1__build/patch.diff@{SHA[:12]}"]
    log_key = keys_of(listed["task/t1/checks"])[0]
    assert log_key.startswith("v1/runs/ship-1/task__t1__checks/01-unit-tests.log@")
    assert listed["task/t1/plan"]["artifacts_json"] == []
    assert (tmp_path / "artifacts" / log_key).read_bytes() == b"passed\n"


def test_a_rebuild_with_another_patch_writes_another_key_and_is_never_locked_out(root):
    first, _ = upload(root, "ship-1", rows_of(), (Source("task/t1/build", "patch.diff", "text/x-diff", text=PATCH),))
    other = PATCH + "+more\n"
    second, error = upload(root, "ship-1", rows_of(), (Source("task/t1/build", "patch.diff", "text/x-diff", text=other),))
    assert error is None
    assert keys_of(by_step(first)["task/t1/build"]) != keys_of(by_step(second)["task/t1/build"])


def test_uploads_are_scrubbed_flagged_and_listed_by_what_was_written(tmp_path):
    pytest.importorskip("pyarrow")
    from pyarrow.fs import LocalFileSystem

    secret_root = ArtifactsRoot(LocalFileSystem(), str(tmp_path / "a"), "file://a", ("placeholder-secret",))
    rows, _ = upload(secret_root, "ship-1", rows_of(),
                     (Source("task/t1/checks", "01-env.log", "text/plain", text="token placeholder-secret\n"),
                      Source("task/t1/checks", "02-clean.log", "text/plain", text="passed\n")))
    dirty, clean = by_step(rows)["task/t1/checks"]["artifacts_json"]
    data = (tmp_path / "a" / dirty["key"]).read_bytes()
    assert data == b"token ***\n" and dirty["sha256"] == hashlib.sha256(data).hexdigest()
    assert dirty["scrubbed"] is True and "scrubbed" not in clean


PATCH_WITH_URL = "--- a/conftest.py\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+URL = 'postgresql://cox:cox@localhost/cox_test'\n"


def test_the_patch_is_stored_byte_for_byte_even_when_it_holds_a_url_with_a_password_or_a_secret(tmp_path):
    pytest.importorskip("pyarrow")
    from pyarrow.fs import LocalFileSystem

    patch = PATCH_WITH_URL + "+TOKEN = 'placeholder-secret'\n"
    secret_root = ArtifactsRoot(LocalFileSystem(), str(tmp_path / "a"), "file://a", ("placeholder-secret",))
    rows, error = upload(secret_root, "ship-1", rows_of(), (Source("task/t1/build", "patch.diff", "text/x-diff", text=patch),))
    item = by_step(rows)["task/t1/build"]["artifacts_json"][0]
    assert error is None and (tmp_path / "a" / item["key"]).read_bytes() == patch.encode()
    assert item["sha256"] == hashlib.sha256(patch.encode()).hexdigest() and "scrubbed" not in item


def test_prepared_keeps_a_patch_as_written_and_says_when_the_scrub_changed_anything_else():
    data = PATCH_WITH_URL.encode()
    assert prepared("text/x-diff", data, ("placeholder-secret",)) == (data, False)
    stored, scrubbed = prepared("text/plain", data, ())
    assert scrubbed is True and b"cox:cox@" not in stored and b"postgresql://***@localhost/cox_test" in stored
    assert prepared("text/plain", b"passed\n", ("placeholder-secret",)) == (b"passed\n", False)
    assert prepared("application/x-ndjson", b"placeholder-secret", ("placeholder-secret",)) == (b"***", True)


def test_an_entry_says_scrubbed_only_when_it_was():
    assert "scrubbed" not in entry("k", b"x", "text/plain")
    assert entry("k", b"x", "text/plain", scrubbed=True)["scrubbed"] is True


def test_a_broken_root_stops_the_uploads_and_marks_each_row_that_had_artifacts(tmp_path):
    pytest.importorskip("pyarrow")
    from pyarrow.fs import LocalFileSystem

    blocker = _write(tmp_path / "blocker", b"a file where the root's folder should be")
    broken = ArtifactsRoot(LocalFileSystem(), str(blocker / "root"), "file://blocker/root")
    found = (Source("task/t1/build", "patch.diff", "text/x-diff", text=PATCH),
             Source("task/t1/checks", "01-unit-tests.log", "text/plain", text="passed\n"))
    rows, error = upload(broken, "ship-1", rows_of(), found)
    listed = by_step(rows)
    assert error
    for step in ("task/t1/build", "task/t1/checks"):
        assert listed[step]["artifacts_json"] == [] and listed[step]["result_json"][ERROR_FIELD] == error
    assert ERROR_FIELD not in listed["task/t1/plan"]["result_json"]


def test_a_filesystem_with_no_hard_links_marks_the_save_missing(root):
    def no_links(src: str, dst: str) -> None:
        raise PermissionError(1, "Operation not permitted", dst)

    rows, error = upload(root, "ship-1", rows_of(), (Source("task/t1/build", "patch.diff", "text/x-diff", text=PATCH),),
                         link=no_links)
    build = by_step(rows)["task/t1/build"]
    assert error is not None and "Operation not permitted" in error
    assert build["artifacts_json"] == [] and build["result_json"][ERROR_FIELD] == error


# --- the save path: record_steps on a real store --------------------------------------------------------------------


def _ctx(tmp_path, conn, artifacts):
    return Ctx(repo=tmp_path, cartridge={}, runner=None, specs={}, run_id="ship-1", date="2026-11-20", max_parallel=1,
               ledger_path=tmp_path / "ledger", provider_profile="p", runs_dir=tmp_path / "runs",
               worktree_root=tmp_path / "wt", assume=None, fix_attempts=None, initiative_id="ship",
               default_ref="main", store=Store(conn), artifacts=artifacts)


def _lane_files(tmp_path, store):
    """The check log the lane wrote, and a build transcript recorded as the runner records one: a trace file, named in
    the node call's detail."""
    log = tmp_path / "runs" / "ship-1" / "checks" / "p1" / "t1" / "1.log"
    log.parent.mkdir(parents=True)
    log.write_text("1 passed\n", encoding="utf-8")
    trace = tmp_path / "runs" / "ship-1-trace" / "build-1.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"type": "result"}\n', encoding="utf-8")
    store.record_call({"id": "c1", "task_id": "t1", "role": "build", "ok": True, "trace": str(trace)},
                      run_id="ship-1", seq=1, phase_id="p1")
    return log, trace


def test_without_a_root_the_save_path_gets_its_rows_back_unchanged(tmp_path, store_conn):
    rows = rows_of()
    assert step_artifacts(None, store_conn, rows, RESULT, runs_dir=tmp_path, run_id="ship-1", phase="p1",
                          task="t1") == rows


def test_record_steps_uploads_the_patch_the_log_and_the_transcript_and_the_rows_list_them(tmp_path, store_conn, root):
    c = _ctx(tmp_path, store_conn, root)
    log, _ = _lane_files(tmp_path, c.store)
    record_steps(c, RESULT, phase="p1", task="t1")
    stored = by_step(steps_for(store_conn, "ship-1"))
    build = keys_of(stored["task/t1/build"])
    assert [k.rpartition("@")[0] for k in build] == [
        "v1/runs/ship-1/task__t1__build/patch.diff", "v1/runs/ship-1/task__t1__build/transcript-build-1.jsonl"]
    assert [k.rpartition("@")[0] for k in keys_of(stored["task/t1/checks"])] == [
        "v1/runs/ship-1/task__t1__checks/01-unit-tests.log"]
    for row in stored.values():
        for item in row["artifacts_json"]:
            data = (tmp_path / "artifacts" / str(item["key"])).read_bytes()
            assert (item["bytes"], item["sha256"]) == (len(data), hashlib.sha256(data).hexdigest())

    # A second save, after the local log changed, leaves the stored rows as they were.
    log.write_text("rewritten\n", encoding="utf-8")
    record_steps(c, RESULT, phase="p1", task="t1")
    assert by_step(steps_for(store_conn, "ship-1")) == stored


def test_a_broken_bucket_still_writes_every_row_and_marks_those_that_carried_artifacts(tmp_path, store_conn):
    pytest.importorskip("pyarrow")
    from pyarrow.fs import LocalFileSystem

    blocker = _write(tmp_path / "blocker", b"x")
    c = _ctx(tmp_path, store_conn, ArtifactsRoot(LocalFileSystem(), str(blocker / "root"), "file://blocker/root"))
    _lane_files(tmp_path, c.store)
    record_steps(c, RESULT, phase="p1", task="t1")
    stored = by_step(steps_for(store_conn, "ship-1"))
    assert sorted(stored) == ["task/t1/build", "task/t1/checks", "task/t1/plan", "task/t1/review"]
    for step in ("task/t1/build", "task/t1/checks"):
        assert stored[step]["artifacts_json"] == [] and stored[step]["result_json"][ERROR_FIELD], stored[step]
    for step in ("task/t1/plan", "task/t1/review"):
        assert stored[step]["artifacts_json"] == [] and ERROR_FIELD not in stored[step]["result_json"], stored[step]


# --- reading back: pick and verified are pure; locate and probe run against a real local root ---------------------

ITEM = {"key": f"v1/runs/ship-1/task__t1__build/patch.diff@{SHA[:12]}", "bytes": len(PATCH), "sha256": SHA,
        "media": "text/x-diff"}


def test_name_of_is_the_last_segment_without_its_content_suffix():
    assert name_of(ITEM["key"]) == "patch.diff"
    assert name_of("v1/runs/r/s/transcript-build-1.jsonl@0c9e1d2a7b44") == "transcript-build-1.jsonl"


def test_pick_finds_the_item_a_step_lists_under_a_name_and_the_last_when_it_lists_two():
    later = {**ITEM, "key": "v1/runs/ship-1/task__t1__build/patch.diff@ffffffffffff"}
    rows = [{"step": "task/t1/plan", "artifacts_json": [ITEM]}, {"step": "task/t1/build", "artifacts_json": [ITEM, later]}]
    assert pick(rows, "task/t1/build", "patch.diff") == later
    assert pick(rows, "task/t1/plan", "patch.diff") == ITEM
    assert pick(rows, "task/t1/build", "01-unit-tests.log") is None
    assert pick([{"step": "task/t1/build", "artifacts_json": None}], "task/t1/build", "patch.diff") is None


def test_verified_passes_the_bytes_the_row_records_and_refuses_any_other():
    assert verified(ITEM, PATCH.encode()) == PATCH.encode()
    with pytest.raises(ArtifactCorrupt):
        verified(ITEM, PATCH.encode() + b"x")
    with pytest.raises(ArtifactCorrupt):
        verified(ITEM, PATCH.upper().encode())  # same size, another digest


def test_locate_reads_back_what_upload_wrote_from_the_rows_alone(root, tmp_path):
    log = _write(tmp_path / "1.log", b"passed\n")
    rows, _ = upload(root, "ship-1", rows_of(), (Source("task/t1/build", "patch.diff", "text/x-diff", text=PATCH),
                                                 Source("task/t1/checks", "01-unit-tests.log", "text/plain", path=log)))
    log.unlink()  # the local copy is gone: the rows and the root are all a reader has
    assert locate(root, rows, "task/t1/build", "patch.diff") == PATCH.encode()
    assert locate(root, rows, "task/t1/checks", "01-unit-tests.log") == b"passed\n"
    assert locate(root, rows, "task/t1/checks", "02-lint.log") is None


def test_an_object_changed_after_its_row_was_written_is_refused_on_read(root, tmp_path):
    rows, _ = upload(root, "ship-1", rows_of(), (Source("task/t1/build", "patch.diff", "text/x-diff", text=PATCH),))
    item = by_step(rows)["task/t1/build"]["artifacts_json"][0]
    path = tmp_path / "artifacts" / item["key"]
    path.chmod(0o644)
    path.write_bytes(PATCH.upper().encode())
    with pytest.raises(ArtifactCorrupt):
        read_object(root, item)


def test_the_probe_key_sits_beside_the_runs_and_refuses_a_path():
    assert probe_key("doctor-h-1") == "v1/probe/doctor-h-1"
    with pytest.raises(ValueError):
        probe_key("../runs")


def test_the_probe_writes_reads_back_and_removes_its_object(root, tmp_path):
    assert probe(root, "doctor-h-1") is None
    assert not (tmp_path / "artifacts" / probe_key("doctor-h-1")).exists()
    assert probe(root, "doctor-h-1") is None  # repeatable: nothing is left to collide with


def test_the_probe_says_the_write_failed_on_a_root_it_cannot_write_or_link_in(tmp_path):
    pytest.importorskip("pyarrow")
    from pyarrow.fs import LocalFileSystem

    blocker = _write(tmp_path / "blocker", b"a file where the root's folder should be")
    broken = ArtifactsRoot(LocalFileSystem(), str(blocker / "root"), "file://blocker/root")
    stage, what = probe(broken, "doctor-h-1") or ("", "")
    assert stage == "write" and what and "blocker" not in what

    def no_links(src: str, dst: str) -> None:
        raise PermissionError(1, "Operation not permitted")  # what SMB answers a hard link with

    linkless = ArtifactsRoot(LocalFileSystem(), str(tmp_path / "smb"), "file://smb")
    assert probe(linkless, "doctor-h-1", link=no_links) == ("write", "PermissionError")

