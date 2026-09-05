"""The work store, and the DAG it refuses to run."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.cartridge import load
from core.skills import index_from_roots
from core.workstore import (
    WorkStoreError,
    phases,
    read_initiative,
    read_item,
    ready_tasks,
    record_attempt,
    set_state,
    validate_dag,
    write_item,
)

REPO = Path(__file__).resolve().parent.parent


def task(root: Path, phase: str, tid: str, *, needs=(), state="todo", surfaces=()) -> Path:
    return write_item(
        {
            "id": tid,
            "phase": phase,
            "state": state,
            "needs": list(needs),
            "surfaces": list(surfaces),
            "title": tid.replace("-", " "),
            "body": "What someone picking this up cold needs to know.",
        },
        root / phase / f"{tid}.md",
    )


@pytest.fixture
def initiative(tmp_path: Path) -> Path:
    root = tmp_path / "work" / "arrow-migration"
    (root).mkdir(parents=True)
    (root / "initiative.md").write_text(
        "---\nid: arrow-migration\ntitle: Arrow-native foundations\n---\n\nThe idea.\n", encoding="utf-8"
    )
    task(root, "p1-foundations", "t1-schema-probe")
    task(root, "p1-foundations", "t2-bench-harness")
    task(root, "p1-foundations", "t3-cutover", needs=["t1-schema-probe", "t2-bench-harness"])
    task(root, "p2-rollout", "t4-migrate", needs=["t3-cutover"], surfaces=["migration"])
    return root


# ── round-trip ─────────────────────────────────────────────────────────────


def test_an_item_round_trips(tmp_path: Path) -> None:
    path = task(tmp_path, "p1", "t1", needs=["t0"], surfaces=["schema"], state="ready")
    item = read_item(path)
    assert item["id"] == "t1"
    assert item["needs"] == ["t0"]
    assert item["surfaces"] == ["schema"]
    assert item["state"] == "ready"
    assert "picking this up cold" in item["body"]


def test_the_body_survives_a_state_change(tmp_path: Path) -> None:
    """The prose is the item. A state move must not eat it."""
    path = task(tmp_path, "p1", "t1")
    before = read_item(path)["body"]
    set_state(path, "done")
    after = read_item(path)
    assert after["state"] == "done"
    assert after["body"] == before


def test_an_item_with_two_attempts_reads_back_in_order(tmp_path: Path) -> None:
    entries = [
        {"run": "run-1", "phase": "p1", "reason": "quarantined: timeout", "ts": "2026-09-01T00:00:00Z"},
        {"run": "run-2", "phase": "p1", "reason": "quarantined: flaky test", "ts": "2026-09-02T00:00:00Z"},
    ]
    path = write_item(
        {
            "id": "t1",
            "phase": "p1",
            "state": "todo",
            "needs": [],
            "surfaces": [],
            "title": "t1",
            "attempts": entries,
            "body": "body",
        },
        tmp_path / "p1" / "t1.md",
    )
    item = read_item(path)
    assert item["attempts"] == entries


def test_record_attempt_twice_yields_two_entries_and_preserves_the_rest(tmp_path: Path) -> None:
    path = task(tmp_path, "p1", "t1", needs=["t0"], surfaces=["schema"], state="ready")
    before = read_item(path)

    record_attempt(path, run="run-1", phase="p1", reason="quarantined: timeout", ts="2026-09-01T00:00:00Z")
    after = record_attempt(path, run="run-2", phase="p1", reason="quarantined: flaky test", ts="2026-09-02T00:00:00Z")

    assert [a["run"] for a in after["attempts"]] == ["run-1", "run-2"]
    assert after["attempts"][0] == {
        "run": "run-1",
        "phase": "p1",
        "reason": "quarantined: timeout",
        "ts": "2026-09-01T00:00:00Z",
    }
    assert after["state"] == before["state"]
    assert after["needs"] == before["needs"]
    assert after["surfaces"] == before["surfaces"]
    assert after["body"] == before["body"]
    assert read_item(path)["attempts"] == after["attempts"]


def test_an_item_with_no_attempts_writes_no_attempts_line(tmp_path: Path) -> None:
    path = task(tmp_path, "p1", "t1")
    assert "attempts:" not in path.read_text(encoding="utf-8")


def test_a_malformed_attempts_value_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    path = tmp_path / "p1" / "t1.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nid: t1\nphase: p1\nstate: todo\nattempts: not-a-list\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert read_item(path)["attempts"] == []


def test_an_item_with_patterns_reads_back_and_writes_the_line(tmp_path: Path) -> None:
    path = write_item(
        {
            "id": "t1",
            "phase": "p1",
            "state": "todo",
            "needs": [],
            "surfaces": [],
            "patterns": ["docs_only"],
            "title": "t1",
            "body": "body",
        },
        tmp_path / "p1" / "t1.md",
    )
    assert read_item(path)["patterns"] == ["docs_only"]
    assert "patterns:" in path.read_text(encoding="utf-8")


def test_an_item_with_no_patterns_reads_empty_and_writes_no_line(tmp_path: Path) -> None:
    path = task(tmp_path, "p1", "t1")
    assert read_item(path)["patterns"] == []
    assert "patterns:" not in path.read_text(encoding="utf-8")


def test_a_non_list_patterns_value_reads_and_writes_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "p1" / "t1.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nid: t1\nphase: p1\nstate: todo\npatterns: docs_only\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert read_item(path)["patterns"] == []

    rewritten = write_item({**read_item(path), "patterns": "docs_only"}, path)
    assert "patterns:" not in rewritten.read_text(encoding="utf-8")
    assert read_item(rewritten)["patterns"] == []


def test_an_item_with_a_budget_reads_back_and_writes_the_line(tmp_path: Path) -> None:
    path = write_item(
        {
            "id": "t1",
            "phase": "p1",
            "state": "todo",
            "needs": [],
            "surfaces": [],
            "title": "t1",
            "budget_usd": 2.5,
            "body": "body",
        },
        tmp_path / "p1" / "t1.md",
    )
    assert read_item(path)["budget_usd"] == 2.5
    assert "budget_usd:" in path.read_text(encoding="utf-8")


def test_an_item_with_no_budget_has_no_key_and_writes_no_line(tmp_path: Path) -> None:
    path = task(tmp_path, "p1", "t1")
    assert "budget_usd" not in read_item(path)
    assert "budget_usd:" not in path.read_text(encoding="utf-8")


def test_a_non_numeric_or_zero_budget_reads_as_absent(tmp_path: Path) -> None:
    zero = tmp_path / "p1" / "t1.md"
    zero.parent.mkdir(parents=True)
    zero.write_text(
        "---\nid: t1\nphase: p1\nstate: todo\nbudget_usd: 0\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert "budget_usd" not in read_item(zero)

    non_numeric = tmp_path / "p2" / "t1.md"
    non_numeric.parent.mkdir(parents=True)
    non_numeric.write_text(
        "---\nid: t1\nphase: p2\nstate: todo\nbudget_usd: soon\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert "budget_usd" not in read_item(non_numeric)


def test_write_item_refuses_a_negative_budget_same_as_read_item(tmp_path: Path) -> None:
    path = write_item(
        {
            "id": "t1",
            "phase": "p1",
            "state": "todo",
            "needs": [],
            "surfaces": [],
            "title": "t1",
            "budget_usd": -1,
            "body": "body",
        },
        tmp_path / "p1" / "t1.md",
    )
    assert "budget_usd:" not in path.read_text(encoding="utf-8")
    assert "budget_usd" not in read_item(path)


def test_unknown_states_are_refused(tmp_path: Path) -> None:
    path = task(tmp_path, "p1", "t1")
    with pytest.raises(WorkStoreError, match="unknown state 'nearly'"):
        set_state(path, "nearly")


def test_base_cartridge_caps_the_build_budget_at_3_dollars() -> None:
    resolved = load("local", REPO / "cartridges", skill_index=index_from_roots([REPO / "skills-plugins"]))
    assert resolved["policy"]["build_budget_usd_max"] == 3.0


def test_frontmatter_must_be_present(tmp_path: Path) -> None:
    stray = tmp_path / "p1" / "loose.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("just prose, no header\n", encoding="utf-8")
    with pytest.raises(WorkStoreError, match="must open with a '---' frontmatter block"):
        read_item(stray)


def test_unclosed_frontmatter_is_refused(tmp_path: Path) -> None:
    stray = tmp_path / "p1" / "loose.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("---\nid: x\nbody without a closing fence\n", encoding="utf-8")
    with pytest.raises(WorkStoreError, match="never closed"):
        read_item(stray)


# ── the DAG ────────────────────────────────────────────────────────────────


def test_reads_an_initiative_with_its_phases(initiative: Path) -> None:
    read = read_initiative(initiative)
    assert read["title"] == "Arrow-native foundations"
    assert read["phases"] == ["p1-foundations", "p2-rollout"]
    assert len(read["items"]) == 4


def test_a_dangling_edge_is_refused(tmp_path: Path) -> None:
    items = [{"id": "a", "needs": ["ghost"]}]
    with pytest.raises(WorkStoreError, match="needs 'ghost', which does not exist"):
        validate_dag(items)


def test_a_cycle_is_refused(tmp_path: Path) -> None:
    """A cycle means nothing in it can ever be ready; the phase would just stall."""
    items = [{"id": "a", "needs": ["b"]}, {"id": "b", "needs": ["c"]}, {"id": "c", "needs": ["a"]}]
    with pytest.raises(WorkStoreError, match="dependency cycle"):
        validate_dag(items)


def test_a_self_edge_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WorkStoreError, match="dependency cycle"):
        validate_dag([{"id": "a", "needs": ["a"]}])


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    with pytest.raises(WorkStoreError, match="duplicate work item id 'a'"):
        validate_dag([{"id": "a", "needs": []}, {"id": "a", "needs": []}])


def test_a_valid_dag_passes(initiative: Path) -> None:
    validate_dag(read_initiative(initiative)["items"])  # must not raise


def test_the_dag_is_validated_at_read_not_at_run(initiative: Path) -> None:
    task(initiative, "p1-foundations", "t9-broken", needs=["does-not-exist"])
    with pytest.raises(WorkStoreError, match="does not exist"):
        read_initiative(initiative)


# ── what can run at once ───────────────────────────────────────────────────


def test_ready_returns_everything_unblocked(initiative: Path) -> None:
    """This set IS the parallelism — nothing in it depends on anything else in it."""
    items = read_initiative(initiative)["items"]
    assert [t["id"] for t in ready_tasks(items)] == ["t1-schema-probe", "t2-bench-harness"]


def test_finishing_a_dependency_unblocks_its_dependents(initiative: Path) -> None:
    set_state(initiative / "p1-foundations" / "t1-schema-probe.md", "done")
    set_state(initiative / "p1-foundations" / "t2-bench-harness.md", "done")
    items = read_initiative(initiative)["items"]
    assert [t["id"] for t in ready_tasks(items)] == ["t3-cutover"]


def test_ready_can_be_scoped_to_one_phase(initiative: Path) -> None:
    items = read_initiative(initiative)["items"]
    assert [t["id"] for t in ready_tasks(items, phase="p2-rollout")] == []


def test_done_work_is_never_ready(initiative: Path) -> None:
    set_state(initiative / "p1-foundations" / "t1-schema-probe.md", "done")
    items = read_initiative(initiative)["items"]
    assert "t1-schema-probe" not in [t["id"] for t in ready_tasks(items)]


def test_ready_is_ordered_so_a_run_is_replayable(initiative: Path) -> None:
    items = list(reversed(read_initiative(initiative)["items"]))
    assert [t["id"] for t in ready_tasks(items)] == ["t1-schema-probe", "t2-bench-harness"]


def test_phases_are_listed_in_order(initiative: Path) -> None:
    assert phases(read_initiative(initiative)["items"]) == ["p1-foundations", "p2-rollout"]
