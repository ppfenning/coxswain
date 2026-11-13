"""The sort's handled keys outlive a tick: a failure the service already annotated or rebuilt is not acted on again."""
import json
from pathlib import Path

from agent_tools import cli
from agent_tools.chair_facts import gather_facts
from tests.test_chair_facts import NOW, _deps


def test_a_handled_key_is_written_and_read_back_once(tmp_path: Path) -> None:
    cli._mark_handled_key(tmp_path, "k1")
    cli._mark_handled_key(tmp_path, "k2")
    cli._mark_handled_key(tmp_path, "k1")
    assert cli._read_handled_keys(tmp_path) == ["k1", "k2"]
    assert json.loads((tmp_path / "chair.handled_keys.json").read_text(encoding="utf-8")) == ["k1", "k2"]


def test_an_absent_or_damaged_handled_file_reads_as_none_handled(tmp_path: Path) -> None:
    assert cli._read_handled_keys(tmp_path) == []
    (tmp_path / "chair.handled_keys.json").write_text("{not json", encoding="utf-8")
    assert cli._read_handled_keys(tmp_path) == []


def test_the_facts_carry_the_handled_keys_the_deps_name() -> None:
    from dataclasses import replace

    facts = gather_facts(replace(_deps(), handled_keys=lambda: ["k1"]), NOW)
    assert facts["handled_keys"] == ["k1"]
    assert "handled_keys" not in gather_facts(_deps(), NOW) or gather_facts(_deps(), NOW)["handled_keys"] == []
