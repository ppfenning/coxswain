import re
import sys
import time
from pathlib import Path

from agent_tools.chair_land_proc import (
    LandOutcome,
    LandSpec,
    count_by_repo,
    dead_outcome,
    encode_outcome,
    encode_spec,
    land_key,
    main,
    parse_outcome,
    parse_spec,
    paths_for,
)


def _spec(argv: list[str] | None = None, repo: str = "r", key: str = "k", timeout_s: float = 30.0) -> LandSpec:
    return LandSpec(
        key,
        {"kind": "land_task", "task_id": "t1"},
        ["true"] if argv is None else argv,
        repo,
        timeout_s,
        1.5e9,
        "h",
        4242,
    )


def test_spec_round_trip() -> None:
    assert parse_spec(encode_spec(_spec())) == _spec()


def test_outcome_round_trip() -> None:
    assert parse_outcome(encode_outcome(LandOutcome(3, "boom"))) == LandOutcome(3, "boom")


def test_parse_garbage_returns_none() -> None:
    whole = encode_spec(_spec())
    for text in ["", "not json", whole[:20], "[]", "null", '{"key": "k"}', whole.replace('"argv": [', '"argv": [1,')]:
        assert parse_spec(text) is None
    for text in ["", "{", "[]", '{"code": "x", "output": ""}', '{"code": 1}']:
        assert parse_outcome(text) is None


def test_land_key_stable_and_safe() -> None:
    action = {"run": "i 1/x", "task_id": "t:1"}
    assert land_key(action) == land_key(dict(action))
    assert re.fullmatch(r"[A-Za-z0-9._-]+", land_key(action))
    assert land_key({"run": "r", "phase": "p"}).startswith("r--p--")


def test_land_key_distinct_actions_do_not_collide() -> None:
    keys = [
        land_key({"run": "r", "task_id": "a b"}),
        land_key({"run": "r", "task_id": "a:b"}),
        land_key({"run": "r", "task_id": "x"}),
        land_key({"run": "r", "phase": "x"}),
        land_key({}),
        land_key({"kind": "other"}),
    ]
    assert len(set(keys)) == len(keys)


def test_paths_for() -> None:
    spec_path, outcome_path = paths_for(Path("/s"), "k")
    assert (spec_path, outcome_path) == (Path("/s/k.spec.json"), Path("/s/k.outcome.json"))


def test_dead_outcome() -> None:
    assert dead_outcome(_spec(), 4242) == LandOutcome(-1, "land process 4242 exited without an outcome")


def test_count_by_repo() -> None:
    assert count_by_repo([_spec(repo="a"), _spec(repo="a"), _spec(repo="b")]) == {"a": 2, "b": 1}
    assert count_by_repo([]) == {}


def test_main_writes_failing_outcome(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "print('boom'); raise SystemExit(3)"]
    spec_path, outcome_path = paths_for(tmp_path, "k")
    spec_path.write_text(encode_spec(_spec(argv)))
    assert main(str(spec_path)) == 0
    outcome = parse_outcome(outcome_path.read_text())
    assert outcome is not None and outcome.code == 3 and "boom" in outcome.output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k.outcome.json", "k.spec.json"]


def test_main_timeout_writes_124(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "import time; time.sleep(5)"]
    spec_path, outcome_path = paths_for(tmp_path, "k")
    spec_path.write_text(encode_spec(_spec(argv, timeout_s=0.3)))
    assert main(str(spec_path)) == 0
    outcome = parse_outcome(outcome_path.read_text())
    assert outcome is not None and outcome.code == 124 and "timed out" in outcome.output


def test_main_timeout_keeps_partial_output_and_kills_grandchildren(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-survived"
    grandchild = f"import time; time.sleep(1.0); open({str(marker)!r}, 'w').close()"
    code = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); print('partial', flush=True); time.sleep(30)"
    spec_path, outcome_path = paths_for(tmp_path, "k")
    spec_path.write_text(encode_spec(_spec([sys.executable, "-c", code], timeout_s=0.5)))
    assert main(str(spec_path)) == 0
    outcome = parse_outcome(outcome_path.read_text())
    assert outcome is not None and outcome.code == 124 and "partial" in outcome.output
    time.sleep(1.5)
    assert not marker.exists()


def test_main_non_utf8_output_still_writes_outcome(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end'); raise SystemExit(5)"]
    spec_path, outcome_path = paths_for(tmp_path, "k")
    spec_path.write_text(encode_spec(_spec(argv)))
    assert main(str(spec_path)) == 0
    outcome = parse_outcome(outcome_path.read_text())
    assert (
        outcome is not None
        and outcome.code == 5
        and outcome.output.startswith("ok ")
        and outcome.output.endswith(" end")
    )


def test_main_empty_argv_still_writes_outcome(tmp_path: Path) -> None:
    spec_path, outcome_path = paths_for(tmp_path, "k")
    spec_path.write_text(encode_spec(_spec([])))
    assert main(str(spec_path)) == 0
    outcome = parse_outcome(outcome_path.read_text())
    assert outcome is not None and outcome.code == 127 and "could not start" in outcome.output


def test_main_unreadable_spec_writes_nothing(tmp_path: Path) -> None:
    spec_path = tmp_path / "k.spec.json"
    spec_path.write_text("{")
    assert main(str(spec_path)) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["k.spec.json"]
