"""L2: the three check levels, named in one `.agent-checks` file and read by the three gates. Pure, no mocks.

The old model is the guard: a file with no `# level:` marker parses, resolves and reaches every gate exactly as it
did before levels existed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.check_gate import GATE_LEVELS, GATES, gate_checks, gate_commands
from harness.checks import LEVELS, check_timeout, fixable_checks, for_level, old_model_checks, repo_checks
from harness.epic import Ctx
from harness.epic_branches import _merge_checks

ROOT = Path(__file__).resolve().parents[3]

# This repository's `.agent-checks` as it stood before L2 (origin/main at #1299): no marker anywhere.
UNMARKED = """\
# The component suites, each from its own folder (their tests import sibling test modules by name).
cd src/tools && pytest -q -n auto
cd src/graphs && pytest -q -n auto
cd src/cartridges && pytest -q -n auto
# The smoke suite (docs/smoke.md): real cox from this worktree against real git and a throwaway Postgres container.
pytest -q -rs smoke
python .github/check_clean.py . --require-names
fmt-tool --check  # fix: fmt-tool
pytest -q -rs smoke
python .github/check_types.py
"""
# What `repo_checks` gave for it then: one `{name, cmd}` per line (and `fix`), the repeat dropped, no other key.
UNMARKED_PARSED = [
    {"name": "cd src/tools && pytest -q -n auto", "cmd": "cd src/tools && pytest -q -n auto"},
    {"name": "cd src/graphs && pytest -q -n auto", "cmd": "cd src/graphs && pytest -q -n auto"},
    {"name": "cd src/cartridges && pytest -q -n auto", "cmd": "cd src/cartridges && pytest -q -n auto"},
    {"name": "pytest -q -rs smoke", "cmd": "pytest -q -rs smoke"},
    {"name": "python .github/check_clean.py . --require-names", "cmd": "python .github/check_clean.py . --require-names"},
    {"name": "fmt-tool --check  # fix: fmt-tool", "cmd": "fmt-tool --check", "fix": "fmt-tool"},
    {"name": "python .github/check_types.py", "cmd": "python .github/check_types.py"},
]

MARKED = """\
lint
# level: phase
smoke -m "not forge"
lint
# level: initiative
unit-all
smoke
# level: task
types
"""

CARTRIDGE = {"checks": [{"name": "unit", "cmd": "pytest -q"}], "level_checks": [{"name": "it", "cmd": "make it"}],
             "ticket_checks": [{"name": "e2e", "cmd": "make e2e"}]}


def _ctx(tmp_path: Path, landing: dict, parsed: list, *, levels: bool = False) -> Ctx:
    return Ctx(repo=tmp_path, cartridge={"landing_areas": landing}, runner=None, specs={}, run_id="r1",
               date="2026-11-20", max_parallel=1, ledger_path=tmp_path / "ledger", provider_profile="p",
               runs_dir=tmp_path / "runs", worktree_root=tmp_path / "wt", assume=None, fix_attempts=None,
               initiative_id="i", default_ref="main", repo_checks=parsed, check_levels=levels)


# -- the old model, untouched -----------------------------------------------------------------------------------------

def test_an_unmarked_file_parses_exactly_as_before_levels():
    assert repo_checks(UNMARKED) == UNMARKED_PARSED
    assert for_level(UNMARKED_PARSED, "task") == UNMARKED_PARSED
    assert for_level(UNMARKED_PARSED, "phase") == [] and for_level(UNMARKED_PARSED, "initiative") == []


def test_an_unmarked_file_reaches_only_the_task_gate_and_every_gate_reads_as_the_cartridge_alone(tmp_path):
    parsed = repo_checks(UNMARKED)
    for landing in (CARTRIDGE, {"checks": CARTRIDGE["checks"]}, {}, {**CARTRIDGE, "land_checks": "ci"}):
        for gate in GATES:
            assert gate_checks(landing, gate, parsed if gate != "task" else ()) == gate_checks(landing, gate)
        for levels in (False, True):  # an unmarked file reads the same on either model
            ctx = _ctx(tmp_path, dict(landing), parsed, levels=levels)
            # Ctx.checks: the cartridge's, then every line of the file it does not already run, as before.
            cartridge = list(landing.get("checks") or [])
            assert ctx.checks == cartridge + [c for c in UNMARKED_PARSED if c["cmd"] not in {x["cmd"] for x in cartridge}]
            assert ctx.level_checks == list(landing.get("level_checks") or [])
            assert ctx.gate_commands == gate_commands(landing)  # what the runner was handed before levels
    assert old_model_checks(parsed) == UNMARKED_PARSED


# What this repository's `.agent-checks` ran on main before levels: every command line, in order (origin/main at #1299).
MAIN_COMMANDS = [
    "cd src/tools && pytest -q -n auto", "cd src/graphs && pytest -q -n auto", "cd src/cartridges && pytest -q -n auto",
    "pytest -q -rs smoke", "python .github/check_clean.py . --require-names", "python .github/check_unwired.py",
    "python .github/check_types.py",
]


def _real() -> list:
    return repo_checks((ROOT / ".agent-checks").read_text(encoding="utf-8-sig"))


def test_an_old_model_run_of_this_repository_checks_exactly_what_main_checked(tmp_path):
    """The marked file, on the old model: every task runs the same seven commands main ran (the full smoke among them,
    in its fix loop), the level gate and the merge's check run nothing, and the runner's gate map is the cartridge's.
    Only the order moves: the smoke, now an initiative line, comes after the task lines."""
    landing = {"land_checks": "ci"}  # this repository's overlay
    ctx = _ctx(tmp_path, landing, _real())
    cmds = [str(c["cmd"]) for c in ctx.checks]
    assert sorted(cmds) == sorted(MAIN_COMMANDS) and len(cmds) == len(MAIN_COMMANDS)
    assert all("level" not in c for c in ctx.checks)
    assert ctx.level_checks == [] and _merge_checks(ctx, "p1") is None
    assert ctx.gate_commands == gate_commands(landing) == {}


def test_the_old_model_reads_the_widest_level_and_never_runs_a_suite_twice():
    widest = repo_checks("a\n# level: phase\ns\n# level: initiative\na\nall\n")
    assert [c["cmd"] for c in old_model_checks(widest)] == ["a", "all"]
    assert [c["cmd"] for c in old_model_checks(repo_checks("a\n# level: phase\ns\n"))] == ["a", "s"]
    fixed = old_model_checks(repo_checks("# level: initiative\nfmt --check  # fix: fmt\n"))
    assert fixed == [{"name": "fmt --check  # fix: fmt", "cmd": "fmt --check", "fix": "fmt"}]


# -- the markers ------------------------------------------------------------------------------------------------------

def test_markers_split_one_file_into_the_three_levels_and_a_repeat_is_dropped_only_within_its_level():
    parsed = repo_checks(MARKED)
    assert for_level(parsed, "task") == [{"name": "lint", "cmd": "lint"}, {"name": "types", "cmd": "types"}]
    assert for_level(parsed, "phase") == [{"name": 'smoke -m "not forge"', "cmd": 'smoke -m "not forge"', "level": "phase"},
                                          {"name": "lint", "cmd": "lint", "level": "phase"}]
    assert [c["cmd"] for c in for_level(parsed, "initiative")] == ["unit-all", "smoke"]
    assert "level" not in for_level(parsed, "task")[1]  # `# level: task` is the default level, written out


def test_a_marker_naming_another_level_is_a_failing_task_check_and_its_lines_run_nowhere():
    parsed = repo_checks("lint\n# level: smoke\nsecret-suite\n# level: phase\nsmoke\n")
    task = for_level(parsed, "task")
    assert [c["cmd"] for c in task][:1] == ["lint"] and len(task) == 2
    assert task[1]["name"] == ".agent-checks line 2: unknown level 'smoke', expected one of task, phase, initiative"
    assert task[1]["cmd"].endswith("exit 2")
    assert all("secret-suite" not in c["cmd"] for c in parsed)
    assert [c["cmd"] for c in for_level(parsed, "phase")] == ["smoke"]


def test_a_marker_must_be_the_whole_comment_line():
    parsed = repo_checks("# levels: phase\n#level:phase\n  # level:   initiative  \nfull\n# level: phase extra\nx\n")
    assert [(c["cmd"], c.get("level")) for c in parsed] == [("full", "initiative"), ("x", "initiative")]


def test_a_timeout_mark_sets_the_lines_own_timeout_and_never_reaches_the_shell_or_the_name():
    parsed = repo_checks("# level: phase\nsmoke -n 4  # timeout: 2400\n"
                         "# level: initiative\nfmt --check  # fix: fmt  # timeout: 90  # new model only\nall\n")
    assert for_level(parsed, "phase") == [{"name": "smoke -n 4", "cmd": "smoke -n 4", "level": "phase", "timeout": 2400}]
    fmt, every = for_level(parsed, "initiative")
    assert fmt == {"name": "fmt --check  # fix: fmt", "cmd": "fmt --check", "fix": "fmt", "level": "initiative",
                   "timeout": 90, "new_model_only": True}
    assert "timeout" not in every  # an unmarked line keeps the runner's default
    assert check_timeout(for_level(parsed, "phase")[0], 600) == 2400 and check_timeout(every, 600) == 600


def test_a_timeout_mark_carries_into_the_old_models_list_and_into_a_fix_entry():
    """The old model takes the widest section's lines whole, their own timeout with them: the command set is as
    before, and a line given more time than the default can only run where it would have timed out."""
    taken = old_model_checks(repo_checks("a\n# level: initiative\nsuite  # timeout: 3600\n"))
    assert taken == [{"name": "a", "cmd": "a"}, {"name": "suite", "cmd": "suite", "timeout": 3600}]
    assert fixable_checks(repo_checks("fmt --check  # fix: fmt  # timeout: 90\n")) == [
        {"name": "fmt --check  # fix: fmt", "cmd": "fmt", "timeout": 90}]


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5", "2400s"])
def test_a_bad_timeout_is_a_failing_task_check_and_its_line_runs_nowhere(value):
    parsed = repo_checks(f"lint\n# level: phase\nsmoke  # timeout: {value}\n")
    task = for_level(parsed, "task")
    assert [c["cmd"] for c in task][:1] == ["lint"] and len(task) == 2
    assert task[1]["name"] == (f".agent-checks line 3: bad timeout {value!r}, expected a positive whole number of "
                               "seconds")
    assert task[1]["cmd"].endswith("exit 2") and for_level(parsed, "phase") == []


def test_for_level_refuses_a_level_that_does_not_exist():
    assert LEVELS == ("task", "phase", "initiative")
    with pytest.raises(ValueError, match="unknown check level 'ticket'"):
        for_level([], "ticket")


# -- the gates --------------------------------------------------------------------------------------------------------

def test_each_gate_runs_the_cartridges_list_then_its_levels_section_without_a_repeat():
    assert GATE_LEVELS == {"task": "task", "level": "phase", "ticket": "initiative"}
    parsed = repo_checks("pytest -q\nlint\n# level: phase\nmake it\nsmoke\n# level: initiative\nmake e2e\nall\n")
    assert [c["cmd"] for c in gate_checks(CARTRIDGE, "task", parsed)] == ["pytest -q", "lint"]
    assert [c["cmd"] for c in gate_checks(CARTRIDGE, "level", parsed)] == ["make it", "smoke"]
    assert [c["cmd"] for c in gate_checks(CARTRIDGE, "ticket", parsed)] == ["make e2e", "all"]
    # The runner's gate map: the task gate stays the cartridge's (a run joins the file's task lines itself).
    assert gate_commands(CARTRIDGE, parsed) == {"task": ["pytest -q"], "level": ["make it", "smoke"],
                                                "ticket": ["make e2e", "all"]}
    assert gate_commands({}, repo_checks("lint\n")) == {}


def test_the_phase_lanes_context_holds_each_level_to_its_gate(tmp_path):
    parsed = repo_checks("lint\n# level: phase\nsmoke\n# level: initiative\nall\n")
    ctx = _ctx(tmp_path, {"checks": CARTRIDGE["checks"]}, parsed, levels=True)
    assert [c["cmd"] for c in ctx.checks] == ["pytest -q", "lint"]  # a task build never sees the smoke
    assert [c["cmd"] for c in ctx.level_checks] == ["smoke"]
    assert ctx.gate_commands == {"task": ["pytest -q"], "level": ["smoke"], "ticket": ["all"]}


def test_this_repositorys_file_names_the_three_levels():
    parsed = _real()
    task = [c["cmd"] for c in for_level(parsed, "task")]
    assert not [c for c in task if "smoke" in c]  # no phase-model task runs the smoke
    assert [c["cmd"] for c in for_level(parsed, "phase")] == ['pytest -q -rs smoke -m "not forge" -n 4']
    # E1: each wider line carries its own timeout, well past the runner's 600 s default the serial smoke outran.
    assert [c.get("timeout") for c in for_level(parsed, "phase")] == [2400]
    assert [c.get("timeout") for c in for_level(parsed, "initiative")] == [1800, 1800, 1800, 3600, 1800]
    assert [c["cmd"] for c in for_level(parsed, "initiative")] == [
        "cd src/tools && pytest -q -n auto", "cd src/graphs && pytest -q -n auto",
        "cd src/cartridges && pytest -q -n auto", "pytest -q -rs smoke", "pytest -q -n auto"]
    # D2: the root suite is the new model's alone (`# new model only`); the old model runs it in CI's `unit (root)`.
    assert [c["cmd"] for c in for_level(parsed, "initiative") if c.get("new_model_only")] == ["pytest -q -n auto"]
