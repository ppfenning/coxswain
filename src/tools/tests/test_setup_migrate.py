import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_tools import repo_path_migrate, setup_install, setup_migrate
from agent_tools.home import Locations
from agent_tools.setup_migrate import (
    CHECKOUTS,
    MARKER_NAME,
    OPS,
    ChairLease,
    FinishEdge,
    FinishFacts,
    HostRow,
    Lane,
    LeaseOutcome,
    LinkFact,
    cartridge_link_target,
    delete_marker,
    finish,
    marker_path,
    migrate_plan,
    observe_links,
    preflight_refusals,
    removal_decisions,
    remove_link,
    render_plan,
    resolve_old_root,
    stale_script_lines,
)
from agent_tools.setup_migrate_hosts import ObservedHost

LOCS = Locations(
    home="/h/coxswain",
    installs="/h/coxswain/installs",
    workspaces="/h/coxswain/workspaces",
    config="/u/.config",
    state="/u/.local/state/coxswain",
    profile="/u/.config/agent-tools/profile.yaml",
    record="/u/.config/agent-tools/home",
)
NEW = "/h/coxswain/installs"
CO = f"{NEW}/coxswain"
SRC_CART = f"{CO}/src/cartridges"
CART, GRAPHS, TOOLS = CHECKOUTS
CREW = "coxswain-crew"
LINK_BASE = "/u/ws/cartridges/base"
LINK_LOCAL = "/u/ws/cartridges/local"
OLD_LINKS = {
    LINK_BASE: "../../../h/coxswain/installs/coxswain-cartridges/cartridges/base",
    LINK_LOCAL: "../../../h/coxswain/installs/coxswain-cartridges/cartridges/local",
}
NEW_LINKS = {
    LINK_BASE: "../../../h/coxswain/installs/coxswain/src/cartridges/cartridges/base",
    LINK_LOCAL: "../../../h/coxswain/installs/coxswain/src/cartridges/cartridges/local",
}
OLD_SETTINGS = {
    "skills_roots": [f"{NEW}/{CART}/skills-plugins"],
    "provider_profile": f"{NEW}/{CART}/providers/claude-code.yaml",
    "harness_dir": f"{NEW}/{GRAPHS}",
    "umbrella_dir": CO,
}
NEW_SETTINGS = {
    "skills_roots": [f"{SRC_CART}/skills-plugins"],
    "provider_profile": f"{SRC_CART}/providers/claude-code.yaml",
    "harness_dir": f"{CO}/src/graphs",
    "umbrella_dir": CO,
}
OLD_BINS = {
    "coxswain-tools": {"installed": True, "editable": f"{NEW}/{TOOLS}", "extras": ["traces", "postgres"]},
    "agent-cartridges": {"installed": True, "editable": f"{NEW}/{CART}", "extras": []},
    "coxswain": {"installed": False, "editable": None, "extras": []},
}
NEW_BINS = {
    "coxswain-tools": {"installed": False, "editable": None, "extras": []},
    "agent-cartridges": {"installed": False, "editable": None, "extras": []},
    "coxswain": {"installed": True, "editable": CO, "extras": ["traces", "postgres"]},
}
TAG = "v2026.10.0"
RETIRED = f"{NEW}/.retired-2026-10-19"


def layout_047(**over):
    """The 0.47 layout: the umbrella at <installs>/coxswain, three component checkouts beside it."""
    base = {
        "checkout": {"kind": "dir", "monorepo": False, "dirty": False, "url": "u"},
        "target_tag": TAG,
        "components": {TOOLS: "dir", GRAPHS: "dir", CART: "dir", CREW: "absent"},
        "worktrees": {TOOLS: [], GRAPHS: [], CART: []},
        "venv_source": None,
        "install_bins": OLD_BINS,
        "settings": OLD_SETTINGS,
        "home_record": "/h/coxswain",
        "cartridge_links": OLD_LINKS,
        "claude_on_path": True,
        "marketplace": f"{NEW}/{CART}",
        "marketplace_plugins": ["local-skills@coxswain-cartridges"],
        "today": "2026-10-19",
    }
    return {**base, **over}


def migrated(**over):
    """The same host after the plan ran."""
    final = {
        "checkout": {"kind": "dir", "monorepo": True, "dirty": False, "url": "u"},
        "target_tag": None,
        "components": {TOOLS: "absent", GRAPHS: "absent", CART: "absent", CREW: "absent"},
        "worktrees": {},
        "venv_source": CO,
        "venv_current": True,
        "install_bins": NEW_BINS,
        "settings": NEW_SETTINGS,
        "cartridge_links": NEW_LINKS,
        "marketplace": SRC_CART,
    }
    return layout_047(**{**final, **over})


def ops(steps):
    return [(s["op"], s.get("key") or s.get("path") or s.get("src") or " ".join(s.get("argv", []))) for s in steps]


def test_the_047_layout_plans_every_rule_in_order():
    assert ops(migrate_plan(layout_047(), LOCS)) == [
        ("checkout", CO),
        ("run", "uv sync --frozen --all-extras -q"),
        ("rewrite_profile_key", "skills_roots"),
        ("rewrite_profile_key", "provider_profile"),
        ("rewrite_profile_key", "harness_dir"),
        ("symlink", LINK_BASE),
        ("symlink", LINK_LOCAL),
        ("run", "claude plugin marketplace remove coxswain-cartridges"),
        ("run", f"claude plugin marketplace add {SRC_CART}"),
        ("run", "claude plugin install local-skills@coxswain-cartridges"),
        ("run", "uv tool uninstall coxswain-tools"),
        ("run", "uv tool uninstall agent-cartridges"),
        ("run", f"uv tool install --force -q --editable {CO}[traces,postgres]"),
        ("move", f"{NEW}/{TOOLS}"),
        ("move", f"{NEW}/{GRAPHS}"),
        ("move", f"{NEW}/{CART}"),
        ("print", ""),
    ]


@pytest.mark.parametrize(("harness_dir", "folder"), [
    ("/r/coxswain-graphs", "/r"), ("/r/coxswain/src/graphs/", "/r"), ("~/r/coxswain-graphs", "/u/r"), ("/r/graphs", None)])
def test_the_profile_harness_dir_names_the_install_folder(harness_dir, folder):
    assert setup_migrate.profile_install_folder(harness_dir, "/u") == folder


def test_a_root_install_migrates_where_it_sits_only_when_its_umbrella_is_there():
    assert setup_migrate.install_locations(LOCS, "/r", umbrella_present=True).installs == "/r"
    assert setup_migrate.install_locations(LOCS, "/r", umbrella_present=False) == LOCS


def test_a_root_install_retires_beside_itself_and_writes_no_home_record():
    locs = replace(LOCS, installs="/r")
    settings = {"harness_dir": "/r/coxswain-graphs", "umbrella_dir": "/r/coxswain"}
    steps = migrate_plan(layout_047(checkout={"kind": "dir", "monorepo": True}, home_record=None, settings=settings,
                                    cartridge_links={}), locs)
    assert ("rewrite_profile_key", "harness_dir") in ops(steps) and ("move", "/r/coxswain-tools") in ops(steps)
    assert not [s for s in steps if s["op"] in ("rewrite_setting", "checkout", "refuse")]


def test_a_second_run_over_the_result_plans_one_skip():
    assert [s["op"] for s in migrate_plan(migrated(), LOCS)] == ["skip"]


def test_the_umbrella_checks_out_the_target_tag_in_place():
    step = migrate_plan(layout_047(), LOCS)[0]
    assert step["argvs"] == [["git", "-C", CO, "fetch", "--tags"], ["git", "-C", CO, "checkout", TAG]]


def test_an_absent_checkout_is_cloned_at_the_target_tag():
    step = migrate_plan(layout_047(checkout={"kind": "absent", "url": "https://x/coxswain.git"}), LOCS)[0]
    assert step["argvs"] == [["git", "clone", "--branch", TAG, "https://x/coxswain.git", CO]]


def test_a_monorepo_checkout_is_never_moved_even_without_a_target():
    steps = migrate_plan(migrated(venv_source=None), LOCS)
    assert [s["op"] for s in steps] == ["run", "print"] and steps[0]["argv"][:2] == ["uv", "sync"]


@pytest.mark.parametrize(
    ("checkout", "tag", "reason"),
    [
        ({"kind": "file"}, TAG, "has a file at"),
        ({"kind": "dir", "monorepo": False, "dirty": True}, TAG, "is dirty at"),
        ({"kind": "dir", "monorepo": False}, None, "has no release tag to move to"),
        ({"kind": "absent", "url": None}, TAG, "no repository was named to clone"),
    ],
)
def test_the_checkout_refuses_and_nothing_else_is_planned(checkout, tag, reason):
    steps = migrate_plan(layout_047(checkout=checkout, target_tag=tag), LOCS)
    assert [s["op"] for s in steps] == ["refuse"] and reason in steps[0]["why"]


def test_unobserved_cartridge_links_refuse():
    observed = layout_047()
    del observed["cartridge_links"]
    assert [s["checkout"] for s in migrate_plan(observed, LOCS)] == ["cartridge_links"]


def test_a_component_to_retire_with_unobserved_worktrees_refuses():
    steps = migrate_plan(layout_047(worktrees={TOOLS: [], GRAPHS: []}), LOCS)
    assert [(s["op"], s["checkout"]) for s in steps] == [("refuse", CART)]


def test_the_venv_is_synced_only_when_coxswain_is_not_editable_from_the_checkout():
    assert not [s for s in migrate_plan(migrated(venv_source=CO), LOCS) if s.get("cwd") == CO]
    sync = [s for s in migrate_plan(migrated(venv_source=f"{NEW}/{TOOLS}"), LOCS) if s.get("cwd") == CO]
    assert sync == [setup_install.sync_step(CO)]


def test_the_venv_is_synced_when_it_is_behind_the_lock_or_the_plan_touches_the_host():
    assert ops(migrate_plan(migrated(venv_current=False), LOCS))[0] == ("run", "uv sync --frozen --all-extras -q")
    assert ops(migrate_plan(migrated(home_record="/old/home"), LOCS))[0] == ("run", "uv sync --frozen --all-extras -q")


def test_profile_keys_move_into_the_checkout_and_a_teams_own_provider_profile_stays():
    settings = {**OLD_SETTINGS, "provider_profile": "/u/ws/providers/team.yaml"}
    rewrites = {s["key"]: s["new"] for s in migrate_plan(layout_047(settings=settings), LOCS) if s["op"] == "rewrite_profile_key"}
    assert rewrites == {"skills_roots": NEW_SETTINGS["skills_roots"], "harness_dir": NEW_SETTINGS["harness_dir"]}


def test_tilde_form_settings_are_expanded_before_comparing():
    locs = replace(LOCS, home="/u/.local/share/coxswain", installs="/u/.local/share/coxswain/installs")
    settings = {"harness_dir": "~/.local/share/coxswain/installs/coxswain-graphs", "umbrella_dir": "~/.local/share/coxswain/installs/coxswain"}
    steps = migrate_plan(migrated(settings=settings, home_record=locs.home), locs)
    assert [(s["key"], s["new"]) for s in steps if s["op"] == "rewrite_profile_key"] == [
        ("harness_dir", "/u/.local/share/coxswain/installs/coxswain/src/graphs")]


def test_a_missing_umbrella_dir_is_added_with_no_old_value():
    settings = {key: value for key, value in NEW_SETTINGS.items() if key != "umbrella_dir"}
    steps = migrate_plan(migrated(settings=settings), LOCS)
    assert [(s["key"], s["old"], s["new"]) for s in steps if s["op"] == "rewrite_profile_key"] == [("umbrella_dir", None, CO)]


def test_repo_map_values_naming_a_component_follow_it_to_its_retired_folder():
    repo_map = {"o/coxswain-tools": f"{NEW}/{TOOLS}", "o/other": "/u/src/other"}
    settings = {**OLD_SETTINGS, "repo_map": repo_map}
    step = next(s for s in migrate_plan(layout_047(settings=settings), LOCS) if s.get("key") == "repo_map")
    assert step["new"] == {"o/coxswain-tools": f"{RETIRED}/{TOOLS}", "o/other": "/u/src/other"}


def test_the_home_record_is_rewritten_when_it_names_another_home():
    steps = migrate_plan(migrated(home_record="/old/home"), LOCS)
    assert [(s["op"], s["new"]) for s in steps if s["op"] == "rewrite_setting"] == [("rewrite_setting", "/h/coxswain")]


def test_cartridge_links_are_repointed_relative_into_the_checkout():
    assert cartridge_link_target(LINK_BASE, LOCS) == NEW_LINKS[LINK_BASE]
    targets = {s["path"]: s["target"] for s in migrate_plan(layout_047(), LOCS) if s["op"] == "symlink"}
    assert targets == NEW_LINKS


def test_claude_absent_yields_one_not_checked_print_and_no_claude_step():
    steps = migrate_plan(layout_047(claude_on_path=False), LOCS)
    assert not [s for s in steps if s.get("argv", [""])[0] == "claude"]
    assert [s["item"] for s in steps if s["op"] == "print"][-1] == "marketplace not checked"


def test_unobserved_install_bins_plan_no_tool_step():
    observed = layout_047()
    del observed["install_bins"]
    assert not [s for s in migrate_plan(observed, LOCS) if s.get("argv", [""])[:2] == ["uv", "tool"]]


def test_a_leftover_old_tool_is_uninstalled_even_when_coxswain_is_already_installed():
    bins = {**NEW_BINS, "agent-cartridges": {"installed": True, "editable": None, "extras": ["x"]}}
    steps = [s["argv"] for s in migrate_plan(migrated(install_bins=bins), LOCS) if s["op"] == "run"]
    assert steps == [
        ["uv", "sync", "--frozen", "--all-extras", "-q"],
        ["uv", "tool", "uninstall", "agent-cartridges"],
        ["uv", "tool", "install", "--force", "-q", "--editable", f"{CO}[traces,postgres,x]"],
    ]


def test_the_tool_install_stops_the_plan_on_failure():
    install = next(s for s in migrate_plan(layout_047(), LOCS) if s.get("argv", [])[:3] == ["uv", "tool", "install"])
    assert install["warn_only"] is False


def test_component_checkouts_retire_aside_with_their_worktrees_repaired():
    steps = migrate_plan(layout_047(worktrees={TOOLS: ["/w/lane-1"], GRAPHS: [], CART: []}), LOCS)
    moves = [(s["src"], s["dst"]) for s in steps if s["op"] == "move"]
    assert moves == [(f"{NEW}/{n}", f"{RETIRED}/{n}") for n in (TOOLS, GRAPHS, CART)]
    assert [s["argv"] for s in steps if s["op"] in ("worktree_repair", "verify")] == [
        ["git", "-C", f"{RETIRED}/{TOOLS}", "worktree", "repair", "/w/lane-1"],
        ["git", "-C", f"{RETIRED}/{TOOLS}", "worktree", "list", "--porcelain"],
    ]


def test_crew_retires_too_when_it_was_installed():
    observed = layout_047(components={TOOLS: "absent", GRAPHS: "absent", CART: "absent", CREW: "dir"}, worktrees={CREW: []})
    assert [s["dst"] for s in migrate_plan(observed, LOCS) if s["op"] == "move"] == [f"{RETIRED}/{CREW}"]


BARE_UNIT = "[Service]\nExecStart=/usr/bin/cox chair run\n"
OLD_PATH = f"Environment=PATH={NEW}/{TOOLS}/.venv/bin:{NEW}/{GRAPHS}/.venv/bin:/usr/bin\n"


def test_a_unit_naming_a_component_checkout_is_regenerated():
    steps = migrate_plan(migrated(chair_unit="[Service]\n" + OLD_PATH), LOCS)
    assert [s["op"] for s in steps] == ["run", "chair_unit", "print"]


def test_a_dropin_naming_a_component_checkout_is_rewritten_to_the_one_venv():
    drop_ins = [("10-path.conf", "[Service]\n" + OLD_PATH)]
    steps = migrate_plan(migrated(chair_unit=BARE_UNIT, chair_unit_drop_ins=drop_ins, chair_active=True), LOCS)
    step = next(s for s in steps if s["op"] == "chair_unit_dropin")
    assert (step["op"], step["dropin"], step["restart"]) == ("chair_unit_dropin", "10-path.conf", True)
    rewritten = setup_migrate.rewrite_dropin_path(OLD_PATH, tuple(tuple(p) for p in step["pairs"]))
    assert rewritten == f"Environment=PATH={CO}/.venv/bin:{CO}/.venv/bin:/usr/bin\n"


def test_a_unit_naming_only_the_dash_checkout_is_left_alone():
    unit = f"[Service]\nEnvironment=PATH={NEW}/coxswain-dash/bin:/usr/bin\n"
    assert [s["op"] for s in migrate_plan(migrated(chair_unit=unit), LOCS)] == ["skip"]


def test_pure_rewrite_changes_only_the_old_folders_on_path_lines():
    text = (
        '[Service]\nEnvironment="OTHER=/u/repos/coxswain-tools"\n'
        "Environment=PATH=/u/repos/coxswain-tools/.venv/bin:/u/repos/coxswain-tools-x/bin:/usr/bin\n"
    )
    pairs = (("/u/repos/coxswain-tools", f"{NEW}/coxswain-tools"),)
    assert setup_migrate.rewrite_dropin_path(text, pairs) == (
        '[Service]\nEnvironment="OTHER=/u/repos/coxswain-tools"\n'
        f"Environment=PATH={NEW}/coxswain-tools/.venv/bin:/u/repos/coxswain-tools-x/bin:/usr/bin\n"
    )


def test_every_step_has_a_known_op_and_a_why_and_is_json_serialisable():
    steps = migrate_plan(layout_047(), LOCS)
    assert all(s["op"] in OPS and s["why"] for s in steps)
    assert json.loads(json.dumps(steps)) == steps


def test_render_plan_is_one_line_per_step():
    steps = migrate_plan(layout_047(), LOCS)
    lines = render_plan(steps).splitlines()
    assert len(lines) == len(steps) and lines[0] == f"1. checkout {CO} at {TAG} - {steps[0]['why']}"


def test_is_monorepo_pyproject_reads_the_distribution_name():
    assert setup_migrate.is_monorepo_pyproject('[project]\nname = "coxswain"\n')
    assert not setup_migrate.is_monorepo_pyproject('[project]\nname = "coxswain-tools"\n')
    assert not setup_migrate.is_monorepo_pyproject("not toml [")


def test_observe_install_bins_reads_receipts_and_records_missing_ones_as_not_installed(tmp_path):
    (tmp_path / "coxswain-tools").mkdir()
    (tmp_path / "coxswain-tools" / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "coxswain-tools", editable = "/i/coxswain-tools", extras = ["lake"] }]\n'
    )
    (tmp_path / "agent-cartridges").mkdir()
    (tmp_path / "agent-cartridges" / "uv-receipt.toml").write_text("not toml [")
    assert setup_migrate.observe_install_bins(str(tmp_path)) == {
        "coxswain-tools": {"installed": True, "editable": "/i/coxswain-tools", "extras": ["lake"]},
        "agent-cartridges": {"installed": False, "editable": None, "extras": []},
        "coxswain": {"installed": False, "editable": None, "extras": []},
    }


UHOME = "/u"
FINST = f"{UHOME}/coxswain/installs"
FOLD = f"{UHOME}/repos"
MARKER = json.dumps({"old_root": FOLD, "new_root": FINST})
OK_ROWS = [{"check": "home root", "ok": True, "detail": ""}]
BEAT = ObservedHost("m", "m", "/w", {"coxswain": "/m/x/coxswain/installs/coxswain"}, None, "beat")
OLD_ROW = {"kind": "task", "initiative": "i", "task_id": "t", "extra": {"repo": f"~/repos/{TOOLS}"}, "surfaces": []}
OLD_FILE = {"work/i/p/t.md": f"---\nrepo: ~/repos/{GRAPHS}\n---\nbody\n"}
RIGHT = LinkFact(TOOLS, f"{FOLD}/{TOOLS}", "symlink", f"{FINST}/{TOOLS}")


def facts(**over):
    base = FinishFacts(
        FOLD, MARKER, FINST, UHOME, [{"op": "skip", "why": "x."}], OK_ROWS, [BEAT], [OLD_ROW], OLD_FILE, [RIGHT]
    )
    return base._replace(**over)


class Spy:
    """Records edge calls in order. `write` rewrites the rows it holds; `stuck` makes it a no-op."""

    def __init__(self, rows, files, stuck=False):
        self.rows, self.files, self.stuck, self.events = list(rows), dict(files), stuck, []

    def edge(self):
        return FinishEdge(self.write, self.rescan, self.remove, self.delete_marker)

    def write(self, moves):
        self.events.append("write")
        if not self.stuck:
            for old, new in moves:
                self.rows = [
                    m or r for r in self.rows for m in [repo_path_migrate.migrate_row(r, UHOME, move=(old, new))]
                ]
                self.files = {
                    k: repo_path_migrate.migrate_text(t, UHOME, move=(old, new)) or t for k, t in self.files.items()
                }

    def rescan(self):
        self.events.append("rescan")
        return self.rows, self.files

    def remove(self, path):
        self.events.append(f"remove {path}")

    def delete_marker(self):
        self.events.append("delete_marker")


def run_finish(f=None, apply=True, stuck=False):
    f = f or facts()
    spy = Spy(f.rows, f.files, stuck)
    return finish(f, apply, spy.edge()), spy


def test_no_marker_and_no_explicit_root_exits_two():
    result, spy = run_finish(facts(old_root=None, marker_text=None))
    assert (result.code, result.lines) == (2, ("finish: no old root known; pass --old-root",))
    assert spy.events == []


def test_no_marker_with_explicit_root_proceeds():
    result, _ = run_finish(facts(marker_text=None), apply=False)
    assert result.code == 0
    assert "finish: would delete the marker" not in result.lines


def test_marker_supplies_the_old_root_when_none_is_given():
    assert resolve_old_root(None, MARKER) == FOLD
    assert resolve_old_root("/x", MARKER) == "/x"
    assert resolve_old_root(None, "not json") is None
    assert resolve_old_root(None, "{}") is None


def test_gate_local_plan_not_single_skip():
    result, spy = run_finish(facts(plan=[{"op": "symlink", "why": "x."}]))
    assert (result.code, result.lines) == (1, ("finish: waiting on the local plan to reach a single skip step",))
    assert spy.events == []


def test_gate_failing_home_row():
    result, spy = run_finish(facts(home_rows=[{"check": "home root", "ok": False, "detail": "skipped"}]))
    assert (result.code, result.lines) == (1, ("finish: waiting on home check home root",))
    assert spy.events == []


def test_gate_host_without_a_beat():
    result, _ = run_finish(facts(hosts=[replace(BEAT, source="fallback")]))
    assert (result.code, result.lines) == (1, ("finish: waiting on host m to beat",))


def test_gate_host_not_migrated():
    old = replace(BEAT, checkouts={TOOLS: "/m/x/coxswain/installs/coxswain-tools"})
    result, spy = run_finish(facts(hosts=[old]))
    assert (result.code, result.lines) == (1, ("finish: waiting on host m to migrate",))
    assert spy.events == []


def test_plan_form_lists_and_writes_nothing():
    result, spy = run_finish(apply=False)
    assert result.code == 0
    assert spy.events == []
    assert f"finish: would remove {FOLD}/{TOOLS}" in result.lines
    assert "finish: would delete the marker" in result.lines
    assert sorted(c["key"] for c in result.rewritten) == ["task:i/t", "work/i/p/t.md"]


def test_rewrite_then_clean_rescan_then_removal():
    result, spy = run_finish()
    assert result.code == 0
    assert spy.rows[0]["extra"]["repo"] == f"~/coxswain/installs/{TOOLS}"
    assert f"repo: ~/coxswain/installs/{GRAPHS}" in spy.files["work/i/p/t.md"]
    assert result.removed == (f"{FOLD}/{TOOLS}",)


def test_leftover_value_stops_before_any_removal():
    result, spy = run_finish(stuck=True)
    assert result.code == 1
    assert result.lines == (f"finish: 2 value(s) still under {FOLD}",)
    assert spy.events == ["write", "rescan"]


def test_marker_is_deleted_only_after_the_removals():
    _, spy = run_finish()
    assert spy.events == ["write", "rescan", f"remove {FOLD}/{TOOLS}", "delete_marker"]


def test_real_directory_and_wrong_link_are_kept():
    wrong = LinkFact(GRAPHS, f"{FOLD}/{GRAPHS}", "symlink", "/elsewhere")
    real = LinkFact(CART, f"{FOLD}/{CART}", "dir")
    result, spy = run_finish(facts(links=[RIGHT, wrong, real]))
    assert result.removed == (f"{FOLD}/{TOOLS}",)
    assert result.kept == (
        f"{FOLD}/{GRAPHS}: a link to /elsewhere, not {FINST}/{GRAPHS}",
        f"{FOLD}/{CART}: a real directory",
    )
    assert [e for e in spy.events if e.startswith("remove")] == [f"remove {FOLD}/{TOOLS}"]
    assert spy.events[-1] == "delete_marker"


def test_second_run_is_a_noop(tmp_path):
    installs, old = tmp_path / "installs", tmp_path / "repos"
    installs.mkdir()
    old.mkdir()
    for name in CHECKOUTS:
        (installs / name).mkdir()
        (old / name).symlink_to(installs / name)
    marker = tmp_path / MARKER_NAME
    marker.write_text(MARKER)
    links = observe_links(str(old), UHOME)
    assert [(link.kind, link.target) for link in links] == [("symlink", str(installs / n)) for n in CHECKOUTS]
    removals, kept = removal_decisions(links, str(installs))
    assert len(removals) == 3 and kept == ()
    for path in removals:
        remove_link(path)
    delete_marker(str(marker))
    assert not marker.exists() and not any(old.iterdir())
    again = facts(
        old_root=str(old),
        marker_text=None,
        installs=str(installs),
        rows=[],
        files={},
        links=observe_links(str(old), UHOME),
    )
    result, spy = run_finish(again)
    assert (result.code, result.lines) == (0, ("finish: nothing to do",))
    assert spy.events == []
    delete_marker(str(marker))


def test_observe_links_expands_only_a_tilde_root():
    tilde = observe_links("~/repos", UHOME)
    assert [(link.path, link.kind) for link in tilde] == [(f"/u/repos/{n}", "absent") for n in CHECKOUTS]
    absolute = observe_links("/nowhere/repos", UHOME)
    assert [link.path for link in absolute] == [f"/nowhere/repos/{n}" for n in CHECKOUTS]


def test_marker_path_sits_in_the_state_folder():
    assert marker_path(LOCS) == "/u/.local/state/coxswain/migrate.json"


def test_module_imports_only_standard_library_and_home():
    tree = ast.parse(Path(setup_migrate.__file__).read_text())
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert imported <= {"__future__", "collections", "dataclasses", "json", "pathlib", "posixpath", "re", "tomllib", "typing", "agent_tools"}
    assert "agent_tools.home" in {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}


ME = "box1"
FREE_CHAIR = ChairLease(released=True, stale=False, host=ME)


def refusals(leases=(), lanes=(), chair=FREE_CHAIR, hosts=()):
    return preflight_refusals(tuple(leases), tuple(lanes), ME, chair, tuple(hosts))


def test_preflight_clean_returns_empty_list():
    granted = LeaseOutcome("/u/repos/coxswain-tools", granted=True)
    assert refusals(leases=[granted]) == []


def test_preflight_refused_lease_names_path_and_holder():
    lease = LeaseOutcome("/u/repos/coxswain-tools", granted=False, holder="run-7")
    assert refusals(leases=[lease]) == ["migrate: refusing, run-7 holds /u/repos/coxswain-tools"]


def test_preflight_errored_lease_names_path():
    lease = LeaseOutcome("/u/repos/coxswain-graphs", granted=False, error="timeout")
    assert refusals(leases=[lease]) == [
        "migrate: refusing, an unknown holder (lease error: timeout) holds /u/repos/coxswain-graphs"
    ]


def test_preflight_local_live_lane_counts():
    assert refusals(lanes=[Lane("lane-a", ME)]) == ["migrate: refusing, live lane lane-a holds a run on this machine"]


def test_preflight_lane_with_no_host_counts():
    assert refusals(lanes=[Lane("lane-b", None)]) == ["migrate: refusing, live lane lane-b holds a run on this machine"]


def test_preflight_remote_lane_is_ignored():
    assert refusals(lanes=[Lane("lane-c", "box2")]) == []


def test_preflight_chair_lease_held_here_counts():
    held = ChairLease(released=False, stale=False, host=ME)
    assert refusals(chair=held) == ["migrate: refusing, the chair holds its lease on box1"]


@pytest.mark.parametrize(
    "chair",
    [
        ChairLease(released=True, stale=False, host=ME),
        ChairLease(released=False, stale=True, host=ME),
        ChairLease(released=False, stale=False, host="box2"),
        ChairLease(released=False, stale=False, host=None),
    ],
)
def test_preflight_chair_lease_not_held_here_is_ignored(chair):
    assert refusals(chair=chair) == []


def test_preflight_active_host_row_asks_for_drain():
    assert refusals(hosts=[HostRow(ME, "active")]) == [
        "migrate: refusing, host box1 holds active state, run cox host drain box1 first"
    ]


@pytest.mark.parametrize("row", [HostRow(ME, "draining"), HostRow(ME, "drained"), HostRow("box2", "active")])
def test_preflight_other_host_rows_are_ignored(row):
    assert refusals(hosts=[row]) == []


def test_preflight_orders_leases_lanes_chair_host_row():
    lines = refusals(
        leases=[LeaseOutcome("/p", granted=False, holder="h")],
        lanes=[Lane("l", None)],
        chair=ChairLease(released=False, stale=False, host=ME),
        hosts=[HostRow(ME, "active")],
    )
    assert [line.split(", ", 1)[1].split(" holds")[0] for line in lines] == [
        "h",
        "live lane l",
        "the chair",
        "host box1",
    ]


OLD_TOOLS = "/u/src/coxswain-tools"
STALE_LINE = f"migrate: run.sh still names {OLD_TOOLS}; edit it by hand"


def test_stale_script_lines_absolute_form():
    assert stale_script_lines({"run.sh": f"bash {OLD_TOOLS}/run.sh"}, [OLD_TOOLS], "/u") == [STALE_LINE]


def test_stale_script_lines_tilde_form():
    assert stale_script_lines({"run.sh": "cd ~/src/coxswain-tools"}, [OLD_TOOLS], "/u") == [STALE_LINE]


def test_stale_script_lines_home_variable_form():
    assert stale_script_lines({"run.sh": 'cd "$HOME/src/coxswain-tools"'}, [OLD_TOOLS], "/u") == [STALE_LINE]


def test_stale_script_lines_prefix_only_sibling_is_not_a_match():
    assert stale_script_lines({"run.sh": f"{OLD_TOOLS}-old/x"}, [OLD_TOOLS], "/u") == []


def test_stale_script_lines_no_match_is_empty():
    assert stale_script_lines({"run.sh": "echo hello"}, [OLD_TOOLS], "/u") == []
