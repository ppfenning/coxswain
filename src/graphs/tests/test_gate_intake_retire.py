"""The workstore arm's `retire` route: stamp an intake file's frontmatter and move it, in code."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harness.gate import auto_apply, stamp_frontmatter, workstore_route

INTAKE = "---\nid: idea-1\nstate: ready\ntitle: An idea\n---\n\nBody with a rule\n---\nand a tail.\n\n"


class NoRunner:
    def run(self, **kwargs):
        pytest.fail(f"the model arm must not run: {kwargs.get('role')}")


CARTRIDGE = {"context": [], "write_kinds": {"state_move": {"risk": "low", "ramp": "gated", "apply_arm": "workstore"}}}


def proposal(**apply) -> dict:
    return {
        "kind": "state_move",
        "risk": "low",
        "target": "idea-1",
        "evidence": [{"check": "c", "output": "o"}],
        "rationale": "because",
        "suggested_action": "retire it",
        "apply": apply,
    }


def test_route_retire() -> None:
    apply = {"path": "a.md", "retire_to": "done/a.md", "fields": {"initiative": "x"}}
    assert workstore_route("state_move", apply) == (
        "retire",
        {"path": "a.md", "retire_to": "done/a.md", "fields": {"initiative": "x"}},
    )


def test_route_state_only_is_unchanged() -> None:
    assert workstore_route("state_move", {"path": "a.md", "state": "done"}) == (
        "state_move",
        {"path": "a.md", "state": "done"},
    )


@pytest.mark.parametrize(
    "apply",
    [
        {"path": "a.md", "fields": {"initiative": "x"}},
        {"path": "a.md", "retire_to": "done/a.md"},
        {"path": "a.md", "retire_to": 3, "fields": {"initiative": "x"}},
        {"path": "a.md", "retire_to": "done/a.md", "fields": ["initiative"]},
        {"retire_to": "done/a.md", "fields": {"initiative": "x"}},
        {"path": 1, "retire_to": "done/a.md", "fields": {"initiative": "x"}},
    ],
)
def test_route_other_shapes_fall_back(apply: dict) -> None:
    assert workstore_route("state_move", apply) == ("fallback", {})


def test_retire_is_only_for_state_move() -> None:
    apply = {"path": "a.md", "retire_to": "done/a.md", "fields": {"initiative": "x"}}
    assert workstore_route("item_update", apply) == ("fallback", {})


def test_stamp_adds_a_key_and_keeps_the_body() -> None:
    got = stamp_frontmatter(INTAKE, {"initiative": "init-2"})
    assert (
        got
        == "---\nid: idea-1\nstate: ready\ntitle: An idea\ninitiative: init-2\n---\n\nBody with a rule\n---\nand a tail.\n\n"
    )


def test_stamp_overwrites_an_existing_key() -> None:
    got = stamp_frontmatter("---\ninitiative: old\n---\nbody", {"initiative": "new"})
    assert got == "---\ninitiative: new\n---\nbody"


def test_stamp_keeps_crlf_in_the_body() -> None:
    got = stamp_frontmatter("---\nid: a\n---\r\nline\r\n", {"initiative": "n"})
    assert got == "---\nid: a\ninitiative: n\n---\nline\r\n"


@pytest.mark.parametrize(
    "text", ["no frontmatter\n", "---\nid: a\nnever closed\n", "---\n- a\n- b\n---\nbody", "---\n: [\n---\n"]
)
def test_stamp_refuses_text_without_a_mapping_block(text: str) -> None:
    with pytest.raises(ValueError):
        stamp_frontmatter(text, {"initiative": "n"})


def test_auto_apply_retires_the_intake_file(tmp_path: Path) -> None:
    source = tmp_path / "intake" / "idea-1.md"
    source.parent.mkdir()
    source.write_text(INTAKE, encoding="utf-8")
    target = tmp_path / "intake" / "done" / "idea-1.md"
    got = auto_apply(
        proposal(path=str(source), retire_to=str(target), fields={"initiative": "init-2"}),
        cartridge=CARTRIDGE,
        runner=NoRunner(),
    )
    assert got[0] is True
    assert not source.exists()
    text = target.read_text(encoding="utf-8")
    meta = yaml.safe_load(text.split("---\n")[1])
    assert meta == {"id": "idea-1", "state": "ready", "title": "An idea", "initiative": "init-2"}
    assert text.endswith("\n---\n\nBody with a rule\n---\nand a tail.\n\n")


def test_auto_apply_refuses_when_the_destination_exists(tmp_path: Path) -> None:
    source = tmp_path / "idea-1.md"
    source.write_text(INTAKE, encoding="utf-8")
    target = tmp_path / "done" / "idea-1.md"
    target.parent.mkdir()
    target.write_text("already here", encoding="utf-8")
    applied, detail = auto_apply(
        proposal(path=str(source), retire_to=str(target), fields={"initiative": "init-2"}),
        cartridge=CARTRIDGE,
        runner=NoRunner(),
    )
    assert applied is False
    assert "already exists" in detail
    assert source.read_text(encoding="utf-8") == INTAKE
    assert target.read_text(encoding="utf-8") == "already here"


def test_auto_apply_refuses_when_the_source_is_missing(tmp_path: Path) -> None:
    target = tmp_path / "done" / "idea-1.md"
    applied, detail = auto_apply(
        proposal(path=str(tmp_path / "gone.md"), retire_to=str(target), fields={"initiative": "init-2"}),
        cartridge=CARTRIDGE,
        runner=NoRunner(),
    )
    assert applied is False
    assert "does not exist" in detail
    assert not target.exists()


def test_auto_apply_returns_an_error_value_for_a_file_without_frontmatter(tmp_path: Path) -> None:
    source = tmp_path / "plain.md"
    source.write_text("no frontmatter\n", encoding="utf-8")
    target = tmp_path / "done" / "plain.md"
    applied, detail = auto_apply(
        proposal(path=str(source), retire_to=str(target), fields={"initiative": "init-2"}),
        cartridge=CARTRIDGE,
        runner=NoRunner(),
    )
    assert applied is False
    assert "frontmatter" in detail
    assert source.exists()
    assert not target.exists()
