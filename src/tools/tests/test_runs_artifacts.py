"""`cox runs artifacts`: the listing is pure; the command reads a real sqlite store in the runs directory."""

import json

from agent_tools.cli import main
from agent_tools.runs_artifacts import lines, listing

AT = "2026-11-20T00:00:00Z"
ITEM = {"key": "v1/runs/demo-1/task__t1__build/patch.diff", "bytes": 42, "sha256": "ab" * 32, "media": "text/x-diff"}


def test_listing_gives_one_entry_per_artifact_in_step_order_and_skips_rows_with_none():
    rows = [{"step": "task/t1/build", "artifacts_json": [ITEM]}, {"step": "task/t1/plan", "artifacts_json": []},
            {"step": "task/t1/review", "artifacts_json": None}]
    assert listing(rows) == [{"step": "task/t1/build", **ITEM}]


def test_a_missing_mark_is_listed_with_its_error():
    rows = [{"step": "task/t1/build", "artifacts_json": [ITEM], "result_json": {"artifacts_error": "OSError: down"}}]
    found = listing(rows)
    assert found[0] == {"step": "task/t1/build", **ITEM}
    assert found[1] == {"step": "task/t1/build", "missing": True, "error": "OSError: down"}
    assert lines("demo-1", found)[1] == "task/t1/build: MISSING artifacts: OSError: down"


def test_a_scrubbed_item_keeps_its_flag_and_its_line_says_so():
    found = listing([{"step": "task/t1/checks", "artifacts_json": [{**ITEM, "scrubbed": True}, ITEM]}])
    assert found[0]["scrubbed"] is True and "scrubbed" not in found[1]
    assert [line.endswith("  scrubbed") for line in lines("demo-1", found)] == [True, False]


def test_a_patch_that_holds_a_secret_is_listed_with_its_mark():
    found = listing([{"step": "task/t1/build", "artifacts_json": [{**ITEM, "holds_secret": True}]}])
    assert found[0]["holds_secret"] is True
    assert lines("demo-1", found)[0].endswith("  HOLDS A SECRET (stored raw)")


def test_lines_name_each_key_with_its_size_and_digest_or_say_there_are_none():
    assert lines("demo-1", [{"step": "s", **ITEM}]) == [f"{ITEM['key']}  42 B  {'ab' * 6}"]
    assert lines("demo-1", []) == ["demo-1: no artifacts recorded (no step rows list any; is artifacts_url set?)"]


def _stored(runs_dir):
    from harness.store_migrate import open_store
    from harness.store_steps import put_step, step_row

    conn = open_store(f"sqlite:///{runs_dir / 'cox.db'}", AT)
    put_step(conn, step_row(run_id="demo-1", step="task/t1/build", initiative="demo", phase="p1", task_id="t1",
                            kind="build", round_=1, ok=True, verdict="", result={}, finished_at=AT, artifacts=[ITEM]))
    conn.close()


def test_the_command_prints_the_keys_the_runs_step_rows_list(tmp_path, monkeypatch, capsys):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path / "no-profile.yaml"))
    _stored(runs_dir)
    assert main(["runs", "artifacts", "demo-1", "--runs-dir", str(runs_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"run": "demo-1", "artifacts": [{"step": "task/t1/build", **ITEM}]}
    assert main(["runs", "artifacts", "demo-1", "--runs-dir", str(runs_dir)]) == 0
    assert capsys.readouterr().out == f"{ITEM['key']}  42 B  {'ab' * 6}\n"
