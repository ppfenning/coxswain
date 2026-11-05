"""The chair lands an initiative on its resolved target branch; with none resolved, on the repository default."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools import chair_exec

INITIATIVE_ROW = {"kind": "initiative", "initiative": "i1", "extra": {"epic": "e1", "target_branch": "release/1"}}
EPIC_ONLY_ROW = {"kind": "initiative", "initiative": "i2", "extra": {"epic": "e1"}}
BARE_ROW = {"kind": "initiative", "initiative": "i3", "extra": {}}
EPICS = {"e1": {"id": "e1", "title": None, "target_branch": "epic-line"}}
BAD_EPIC = "---\nid: other\ntarget_branch: 'bad branch..'\n---\n"


def test_the_initiative_beats_the_epic_beats_the_cartridge() -> None:
    assert chair_exec.resolved_target("cart", "epic", "init") == "init"
    assert chair_exec.resolved_target("cart", "epic", None) == "epic"
    assert chair_exec.resolved_target("cart", None, None) == "cart"
    assert chair_exec.resolved_target(None, None, None) is None


def test_target_for_reads_the_row_then_the_epic() -> None:
    rows = [INITIATIVE_ROW, EPIC_ONLY_ROW, BARE_ROW]
    assert chair_exec.target_for("i1", rows, EPICS, None) == "release/1"
    assert chair_exec.target_for("i2", rows, EPICS, None) == "epic-line"
    assert chair_exec.target_for("i3", rows, EPICS, None) is None
    assert chair_exec.target_for("i3", rows, EPICS, "cart") == "cart"
    assert chair_exec.target_for("missing", rows, EPICS, None) is None


def store(tmp_path: Path, monkeypatch, rows: list[dict], epics: dict[str, str]) -> None:
    (tmp_path / "epics").mkdir()
    for name, text in epics.items():
        (tmp_path / "epics" / f"{name}.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(chair_exec.run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: rows)


def edge(tmp_path: Path, initiative: str, cartridge: str | None = None) -> str | None:
    return chair_exec._target_branch_edge(tmp_path, tmp_path, cartridge, initiative)


def test_the_edge_reads_an_epic_override_from_the_work_store(tmp_path: Path, monkeypatch) -> None:
    store(tmp_path, monkeypatch, [EPIC_ONLY_ROW], {"e1": "---\nid: e1\ntarget_branch: epic-line\n---\n"})
    assert edge(tmp_path, "i2") == "epic-line"


def test_an_unrelated_bad_epic_file_does_not_hide_a_valid_target(tmp_path: Path, monkeypatch) -> None:
    rows = [INITIATIVE_ROW, EPIC_ONLY_ROW]
    store(tmp_path, monkeypatch, rows, {"e1": "---\nid: e1\ntarget_branch: epic-line\n---\n", "other": BAD_EPIC})
    assert edge(tmp_path, "i1") == "release/1"
    assert edge(tmp_path, "i2") == "epic-line"


def test_a_bad_file_for_the_initiatives_own_epic_refuses_rather_than_defaults(tmp_path: Path, monkeypatch) -> None:
    store(tmp_path, monkeypatch, [EPIC_ONLY_ROW], {"e1": BAD_EPIC.replace("other", "e1")})
    with pytest.raises(chair_exec.TargetUnreadable):
        edge(tmp_path, "i2")


def test_a_bad_cartridge_value_refuses(tmp_path: Path, monkeypatch) -> None:
    store(tmp_path, monkeypatch, [BARE_ROW], {})
    with pytest.raises(chair_exec.TargetUnreadable):
        edge(tmp_path, "i3", "bad branch..")


def test_an_initiative_file_with_no_row_refuses_but_no_file_means_no_target(tmp_path: Path, monkeypatch) -> None:
    store(tmp_path, monkeypatch, [], {})
    assert edge(tmp_path, "i4") is None
    assert edge(tmp_path, "i4", "cart") == "cart"
    (tmp_path / "work" / "i4").mkdir(parents=True)
    (tmp_path / "work" / "i4" / "initiative.md").write_text("---\n---\n", encoding="utf-8")
    with pytest.raises(chair_exec.TargetUnreadable):
        edge(tmp_path, "i4")


def test_a_stub_with_no_target_branch_has_none() -> None:
    assert chair_exec._target_of(SimpleNamespace(), "i1") is None
    assert chair_exec._target_of(SimpleNamespace(target_branch=None), "i1") is None
    assert chair_exec._target_of(SimpleNamespace(target_branch=lambda i: "release/1"), "") is None
    assert chair_exec._target_of(SimpleNamespace(target_branch=lambda i: "release/1"), "i1") == "release/1"


def opened_base(tmp_path: Path, monkeypatch, target: str | None) -> str | None:
    seen: dict[str, str | None] = {}

    def open_pr(repo, title, body, *, head=None, base=None):
        seen["base"] = base
        return True, "https://example.test/pr/1"

    monkeypatch.setattr(chair_exec.forge_auto, "open_pr", open_pr)
    git = chair_exec._GitEdge(tmp_path, tmp_path, str(tmp_path), str(tmp_path), target)  # tmp_path is no git repo: default_branch() is "main"
    chair_exec._ForgeEdge(git).open_pr("carry/x", "t", "b")
    return seen["base"]


def test_an_initiative_with_a_target_lands_its_pr_on_it(tmp_path: Path, monkeypatch) -> None:
    assert opened_base(tmp_path, monkeypatch, "release/1") == "release/1"


def test_an_initiative_without_a_target_lands_on_the_old_default(tmp_path: Path, monkeypatch) -> None:
    assert opened_base(tmp_path, monkeypatch, None) == "main"


def test_edge_deps_carry_ports_land_pr_merge_and_ancestry_on_the_epic_target(tmp_path: Path, monkeypatch) -> None:
    store(tmp_path, monkeypatch, [EPIC_ONLY_ROW, BARE_ROW], {"e1": "---\nid: e1\ntarget_branch: epic-line\n---\n"})
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, "s", 1, lambda a: "r", lambda a: str(tmp_path), lambda a: None, "h", "python"
    )
    bases: list[object] = []
    monkeypatch.setattr(chair_exec.forge_auto, "open_pr", lambda repo, t, b, *, head=None, base=None: (bases.append(base) or True, ""))
    monkeypatch.setattr(chair_exec.forge_auto, "merge", lambda repo, info: (bases.append(info["default_branch"]) or True, ""))
    argvs: list[list[str]] = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, **kw: (argvs.append(argv) or 1, ""))
    git, forge, _ = deps.carry_ports({"initiative": "i2", "repo": str(tmp_path)})
    forge.open_pr("carry/x", "t", "b")
    forge.merge("1")
    git.already_on_main("abc")
    assert bases == ["epic-line", "epic-line"]
    assert argvs[-1][-1] == "origin/epic-line"
    plain, _, _ = deps.carry_ports({"initiative": "i3", "repo": str(tmp_path)})
    assert plain.landing_branch() == "main"


def test_carry_phase_refuses_when_the_target_is_unreadable() -> None:
    def ports(action):
        raise chair_exec.TargetUnreadable("store down")

    result = chair_exec._carry_phase({"kind": "carry_phase", "phase": "p"}, SimpleNamespace(carry_ports=ports))  # type: ignore[arg-type]
    assert result["status"] == "failed"
    assert "store down" in result["reason"]


def test_carry_forward_passes_the_target_as_the_base_flag(monkeypatch) -> None:
    flags: list[str | None] = []

    def base_ref(repo, flag, configured, run):
        flags.append(flag)
        return f"origin/{flag or 'main'}"

    monkeypatch.setattr(chair_exec.base_branch, "base_ref", base_ref)
    merges: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        if "merge" in argv:
            merges.append(argv)
        return 0, ""

    for target, expected in ((lambda initiative: "release/1", "origin/release/1"), (lambda initiative: None, "origin/main")):
        deps = SimpleNamespace(run=run, target_branch=target)
        merged, needs, failed = chair_exec._carry_forward({"kind": "clear"}, deps, "/repo", "i1", ["p"])  # type: ignore[arg-type]
        assert (merged, needs, failed) == (["p"], None, None)
        assert merges[-1][-1] == expected
    assert flags == ["release/1", None]


def test_carry_forward_fails_when_the_target_is_unreadable() -> None:
    def target(initiative: str) -> str:
        raise chair_exec.TargetUnreadable("store down")

    deps = SimpleNamespace(run=lambda argv: (0, ""), target_branch=target)
    merged, needs, failed = chair_exec._carry_forward({"kind": "clear"}, deps, "/repo", "i1", ["p"])  # type: ignore[arg-type]
    assert (merged, needs) == ([], None)
    assert failed is not None and failed["status"] == "failed" and "store down" in failed["reason"]


def test_finish_task_passes_the_target_as_the_base_name_flag(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(chair_exec.base_branch, "base_name", lambda repo, flag, configured, run: seen.setdefault("flag", flag) or "main")
    lm = chair_exec.land_manual
    monkeypatch.setattr(lm, "checked_recently", lambda stamp: False)
    monkeypatch.setattr(lm, "record_branch", lambda runs_dir, run, task: None)
    monkeypatch.setattr(lm, "pr_branches", lambda initiative, phase, task, recorded: [])
    monkeypatch.setattr(lm, "merge_targets", lambda base, initiative, phase: seen.setdefault("base", base) and [base])
    monkeypatch.setattr(lm, "merged_on_origin", lambda repo, branches, targets, run: ("open", "not merged"))
    monkeypatch.setattr(lm, "mark_checked", lambda stamp: None)
    action = {"kind": "finish_task", "initiative": "i1", "task_id": "t", "run": "r", "repo": "/repo", "phase": "p"}
    deps = SimpleNamespace(runs_dir=tmp_path, run=lambda argv: (0, ""), target_branch=lambda initiative: "release/1")
    assert chair_exec._finish_task(action, deps)["status"] == "skipped"  # type: ignore[arg-type]
    assert (seen["flag"], seen["base"]) == ("release/1", "release/1")
