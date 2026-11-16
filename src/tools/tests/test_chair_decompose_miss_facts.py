"""A decompose launch that finds no idea file is recorded as a miss, and the misses are read back into chair facts."""

import json
from dataclasses import replace

from test_chair_exec import _deps as _exec_deps
from test_chair_facts import NOW
from test_chair_facts import _deps as _facts_deps

from agent_tools import chair_exec
from agent_tools.chair_decompose_miss import DecomposeMiss
from agent_tools.chair_facts import decompose_miss_facts, decompose_misses_reported_keys, gather_facts
from agent_tools.chair_read_decompose_miss import intake_fingerprint

PATH = "intake/2026-11-11-some-idea.md"
MISS_OUTPUT = f"routing: no idea file at {PATH}\n"
LAUNCH = {"kind": "launch_decompose", "intake_ids": [PATH], "epoch": 1}


def test_the_miss_path_is_read_off_the_launch_output() -> None:
    assert chair_exec.idea_miss_path(MISS_OUTPUT) == PATH
    assert chair_exec.idea_miss_path(f"some noise\n{MISS_OUTPUT}more\n") == PATH
    assert chair_exec.idea_miss_path("routing: intake x is a draft; ready it with `cox route ready x`\n") is None
    assert chair_exec.idea_miss_path("") is None


def _perform(output: str, code: int = 2, fingerprint: str = "fp-1") -> list[dict]:
    recorded: list[dict] = []
    deps = replace(_exec_deps([], output=output, code=code), record=recorded.append, intake_fingerprint=lambda path: fingerprint)
    chair_exec.perform([dict(LAUNCH)], deps, lambda: 1, False)
    return recorded


def test_a_missing_idea_file_is_recorded_on_the_failed_launch_with_the_intake_fingerprint() -> None:
    [row] = _perform(MISS_OUTPUT)
    assert row["status"] == "failed"
    assert (row["idea_missing"], row["fingerprint"]) == (PATH, "fp-1")


def test_an_unrelated_failure_records_no_miss() -> None:
    [row] = _perform("routing: launch failed: boom\n")
    assert row["status"] == "failed"
    assert "idea_missing" not in row and "fingerprint" not in row


def test_a_launch_that_succeeds_records_no_miss() -> None:
    [row] = _perform("run some-idea-1\n", code=0)
    assert row["status"] == "done" and "idea_missing" not in row


def _row(ts: str, status: str = "failed", *, fingerprint: str = "fp-1", missing: bool = True, path: str = PATH) -> dict:
    doc = {"kind": "launch_decompose", "intake_ids": [path], "epoch": 1, "status": status, "ts": ts}
    if missing:
        doc |= {"idea_missing": path, "fingerprint": fingerprint}
    return {"ts": ts, "kind": "launch_decompose", "target": path, "status": status, "action_json": json.dumps(doc)}


def test_one_miss_counts_one_and_a_repeat_on_an_unchanged_intake_counts_two() -> None:
    once = decompose_miss_facts([_row("2026-11-11T10:00:00Z")])
    assert once == {PATH: DecomposeMiss("2026-11-11-some-idea", PATH, 1, "2026-11-11T10:00:00Z", "fp-1")}
    twice = decompose_miss_facts([_row("2026-11-11T10:00:00Z"), _row("2026-11-11T10:05:00Z")])
    assert (twice[PATH].count, twice[PATH].last_miss_at) == (2, "2026-11-11T10:05:00Z")


def test_rows_are_read_in_time_order_whatever_order_they_come_in() -> None:
    misses = decompose_miss_facts([_row("2026-11-11T10:05:00Z", fingerprint="fp-2"), _row("2026-11-11T10:00:00Z")])
    assert (misses[PATH].count, misses[PATH].fingerprint) == (1, "fp-2")


def test_a_changed_intake_restarts_the_count() -> None:
    misses = decompose_miss_facts([_row("2026-11-11T10:00:00Z"), _row("2026-11-11T10:05:00Z", fingerprint="fp-2")])
    assert misses[PATH].count == 1


def test_a_launch_that_got_past_the_idea_file_clears_the_miss() -> None:
    rows = [_row("2026-11-11T10:00:00Z"), _row("2026-11-11T10:05:00Z", status="done", missing=False)]
    assert decompose_miss_facts(rows) == {}


def test_an_unrelated_failure_leaves_the_count_as_it_was() -> None:
    rows = [_row("2026-11-11T10:00:00Z"), _row("2026-11-11T10:05:00Z", missing=False), _row("2026-11-11T10:10:00Z")]
    assert decompose_miss_facts(rows)[PATH].count == 2


def test_the_reported_keys_are_the_intake_and_fingerprint_of_each_missing_idea_needs_chair() -> None:
    doc = {"kind": "needs_chair", "cause": "decompose_missing_idea", "intake": PATH, "fingerprint": "fp-1"}
    other = {"kind": "needs_chair", "cause": "decompose_stalled", "initiative": "i", "run": "i-1"}
    rows = [{"kind": "needs_chair", "action_json": json.dumps(d)} for d in (doc, other)]
    assert decompose_misses_reported_keys(rows) == [f"{PATH}|fp-1"]


def test_gather_reads_the_misses_their_current_fingerprints_and_the_reported_keys() -> None:
    reported = {"kind": "needs_chair", "cause": "decompose_missing_idea", "intake": PATH, "fingerprint": "fp-1", "status": "recorded"}
    rows = [_row("2026-11-11T10:00:00Z"), {"ts": "2026-11-11T10:01:00Z", "kind": "needs_chair", "action_json": json.dumps(reported)}]
    deps = replace(_facts_deps(), actions=lambda: rows, intake_fingerprint=lambda path: f"now:{path}")
    facts = gather_facts(deps, NOW)
    assert facts["decompose_misses"][PATH].count == 1
    assert facts["decompose_miss_fingerprints"] == {PATH: f"now:{PATH}"}
    assert facts["decompose_misses_reported"] == [f"{PATH}|fp-1"]


def test_with_no_fingerprint_reader_the_miss_facts_are_empty() -> None:
    deps = replace(_facts_deps(), actions=lambda: [_row("2026-11-11T10:00:00Z")])
    facts = gather_facts(deps, NOW)
    assert (facts["decompose_misses"], facts["decompose_miss_fingerprints"]) == ({}, {})


def test_the_fingerprint_changes_when_the_file_appears_or_the_row_changes(tmp_path) -> None:
    row = {"kind": "intake", "initiative": "", "task_id": "2026-11-11-some-idea", "state": "queued", "title": "t", "body": "b", "extra": {}}
    missing = intake_fingerprint(tmp_path, [row], PATH)
    assert missing == intake_fingerprint(tmp_path, [row], PATH)
    assert missing != intake_fingerprint(tmp_path, [{**row, "extra": {"stage": "ready"}}], PATH)
    assert missing != intake_fingerprint(tmp_path, [row], "intake/elsewhere.md")
    (tmp_path / "intake").mkdir()
    (tmp_path / PATH).write_text("---\ntitle: t\n---\nb\n", encoding="utf-8")
    assert missing != intake_fingerprint(tmp_path, [row], PATH)


def test_the_fingerprint_ignores_other_rows(tmp_path) -> None:
    row = {"kind": "intake", "task_id": "2026-11-11-some-idea", "state": "queued", "extra": {}}
    other = {"kind": "intake", "task_id": "another", "state": "queued", "extra": {}}
    assert intake_fingerprint(tmp_path, [row], PATH) == intake_fingerprint(tmp_path, [row, other], PATH)
