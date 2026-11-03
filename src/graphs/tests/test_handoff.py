from harness.handoff import (
    HANDOFF_KEYS,
    build_handoff,
    files_from_patch,
    parse_handoff,
    plan_context,
    render_handoff,
    serialise_handoff,
)

PATCH = (
    "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    "diff --git a/src/b.py b/src/b.py\n--- /dev/null\n+++ b/src/b.py\n@@ -0,0 +1 @@\n+z\n"
    "diff --git a/src/gone.py b/src/gone.py\n--- a/src/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-w\n"
)


def _record(**over):
    return {
        "files_changed": ["src/a.py"],
        "interfaces_added": ["a.run"],
        "decisions": ["kept the flat layout"],
        "branch": "lane-a",
        "commit": "abc123",
        **over,
    }


def test_the_record_from_a_literal_patch_has_exactly_the_five_keys():
    build = {"patch": PATCH, "summary": "s", "files_touched": ["ignored"], "decisions": ["d1"]}
    record = build_handoff(build, "lane-a", "abc123")
    assert set(record) == set(HANDOFF_KEYS)
    assert record["files_changed"] == ["src/a.py", "src/b.py", "src/gone.py"]
    assert record["decisions"] == ["d1"]
    assert record["interfaces_added"] == []
    assert (record["branch"], record["commit"]) == ("lane-a", "abc123")


def test_a_patch_names_each_file_once_in_first_seen_order():
    assert files_from_patch(PATCH + "+++ b/src/a.py\n") == ["src/a.py", "src/b.py", "src/gone.py"]


def test_serialisation_round_trips_and_keeps_only_the_five_keys():
    record = _record()
    assert parse_handoff(serialise_handoff(record)) == record
    assert set(parse_handoff(serialise_handoff({**record, "transcript": "t"}))) == set(HANDOFF_KEYS)


def test_a_siblings_record_never_appears_in_the_plan_context():
    item = {"id": "t2", "body": "do the second thing", "needs": ["t1"]}
    handoffs = {"t1": _record(), "t3": _record(branch="lane-sibling", commit="def456")}
    text = plan_context(item, handoffs)
    assert text.startswith("do the second thing")
    assert "lane-a" in text and "abc123" in text
    assert "lane-sibling" not in text and "def456" not in text


def test_a_record_carrying_a_transcript_renders_without_it():
    item = {"id": "t2", "body": "body", "needs": ["t1"]}
    leaky = _record(transcript="SECRET lane transcript", messages=["SECRET message"])
    assert "SECRET" not in render_handoff("t1", leaky)
    assert "SECRET" not in plan_context(item, {"t1": leaky})
