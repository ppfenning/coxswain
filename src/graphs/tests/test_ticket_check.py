import subprocess
from pathlib import Path

import pytest

from harness.surface_heal import heal_new_markers
from harness.ticket_check import GitStdout, LookupUnavailable, check_ticket, ticket_lookups

PATHS = frozenset({"src/cli.py", "src/a.py", "src/b.py"})
HITS = {"old_fn": [("src/a.py", 3, "def old_fn():"), ("src/b.py", 9, "    old_fn()")]}
DEFINED = {"real_fn": ["src/a.py"]}


def defined(name: str) -> list[str]:
    return DEFINED.get(name, [])


def search(word: str) -> list[tuple[str, int, str]]:
    return HITS.get(word, [])


def test_a_clean_ticket_returns_unchanged_with_no_corrections() -> None:
    task = {"id": "t", "surfaces": ["src/a.py"], "body": "Change `real_fn()` in src/a.py."}
    assert check_ticket(task, PATHS, defined, search) == (task, ())


def test_a_body_named_file_outside_the_surfaces_is_added_with_a_lint_note() -> None:
    task = {"surfaces": ["src/a.py"], "lint": [], "body": "Update src/b.py to match."}
    healed, corrections = check_ticket(task, PATHS, defined, search)
    assert healed["surfaces"] == ["src/a.py", "src/b.py"]
    assert healed["lint"] == ["added to surfaces: src/b.py was named in the body and healed by the ticket check"]
    assert corrections == ()


def test_a_missing_surface_gets_the_new_marker_once_heal_runs() -> None:
    task = {"surfaces": ["src/a.py", "src/fresh.py"], "body": "Add the module."}
    checked, corrections = check_ticket(task, PATHS, defined, search)
    assert corrections == ()
    assert heal_new_markers(checked, PATHS)["surfaces"] == ["src/a.py", "src/fresh.py (new)"]


def test_a_false_no_caller_claim_gives_a_correction_carrying_the_hits() -> None:
    task = {"surfaces": ["src/a.py"], "body": "Remove it. No callers of `old_fn`."}
    healed, corrections = check_ticket(task, PATHS, defined, search)
    assert corrections == ("claim 'no caller of old_fn' is false, 1 hit: src/b.py:9: old_fn()",)
    assert healed["lint"] == [f"correction: {corrections[0]}"]
    assert healed["surfaces"] == task["surfaces"]


def test_a_nonexistent_function_gives_a_correction() -> None:
    task = {"surfaces": ["src/a.py"], "body": "Rework `ghost_fn()` to take a path."}
    healed, corrections = check_ticket(task, PATHS, defined, search)
    assert corrections == ("ghost_fn was not found in the tree. No def or class of that name exists.",)
    assert healed["lint"] == [f"correction: {corrections[0]}"]


def test_a_bare_filename_is_resolved_and_noted_before_heal_can_mark_it_new() -> None:
    task = {"surfaces": ["cli.py"], "body": "One file."}
    healed, corrections = check_ticket(task, PATHS, defined, search)
    assert healed["surfaces"] == ["src/cli.py"]
    assert healed["lint"] == ["cli.py: resolved to src/cli.py, healed by the ticket check"]
    assert corrections == ()


def test_a_repeated_check_does_not_repeat_a_correction_in_the_lint() -> None:
    task = {"surfaces": ["src/a.py"], "body": "No callers of `old_fn`."}
    once, _ = check_ticket(task, PATHS, defined, search)
    twice, _ = check_ticket(once, PATHS, defined, search)
    assert twice == once


def test_an_unreadable_commit_reports_nothing_instead_of_a_missing_name() -> None:
    def unreadable(*args, **kwargs):
        return None

    for commit in ("", "abc123"):
        found, hits = ticket_lookups("/repo", commit, unreadable)
        assert hits("old_fn") == ()
        with pytest.raises(LookupUnavailable):
            found("ghost_fn")
        named = {"surfaces": ["src/a.py"], "body": "Rework `ghost_fn()`."}
        with pytest.raises(LookupUnavailable):
            check_ticket(named, PATHS, found, hits)
        claimed = {"surfaces": ["src/a.py"], "body": "No callers of `old_fn`."}
        assert check_ticket(claimed, PATHS, found, hits) == (claimed, ())


def test_lookups_stop_at_the_time_budget_and_answer_a_repeat_from_memory() -> None:
    now = [0.0]
    calls: list[tuple] = []

    def slow(*args, **kwargs):
        calls.append(args)
        now[0] += 100.0
        return ""

    found, hits = ticket_lookups("/repo", "abc", slow, clock=lambda: now[0], budget_seconds=60.0)
    assert hits("one") == () and hits("one") == ()
    assert len(calls) == 1
    assert hits("two") == ()
    assert len(calls) == 1
    with pytest.raises(LookupUnavailable):
        found("two")


def _repo_at_head(tmp_path: Path, files: dict[str, str]) -> tuple[str, GitStdout]:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout

    run("init", "-q")
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")

    def git_stdout(*args: str, timeout: int = 120, ok_codes: tuple[int, ...] = (0,)) -> str | None:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)
        return proc.stdout if proc.returncode in ok_codes else None

    return run("rev-parse", "HEAD").strip(), git_stdout


def test_the_lookups_read_definitions_and_hits_at_the_commit(tmp_path: Path) -> None:
    commit, git_stdout = _repo_at_head(tmp_path, {"use.py": "def old_fn():\n    pass\n\n\nold_fn()\n"})
    found, hits = ticket_lookups(str(tmp_path), commit, git_stdout)
    assert found("old_fn") == ("use.py",)
    assert found("ghost_fn") == ()
    assert hits("old_fn") == (("use.py", 1, "def old_fn():"), ("use.py", 5, "old_fn()"))
    assert hits("ghost_fn") == ()


def test_definitions_in_other_syntaxes_are_found_so_no_false_missing_name(tmp_path: Path) -> None:
    commit, git_stdout = _repo_at_head(
        tmp_path,
        {
            "frame.rs": "impl Frame {\n    pub(crate) fn render_frame(&self) {}\n    pub async fn draw_all() {}\n}\n",
            "tool.go": "func (r *Runner) Execute() error {\n    return nil\n}\n",
            "ui.ts": "export async function load_all() {}\nexport default class Panel {}\nexport const arrow_fn = () => 1;\n",
            "obj.js": "class Box {\n  static async open_box(x) {\n  }\n}\n",
            "a.py": "class K:\n    async def aio(self):\n        pass\n",
            "Other.java": "class Other { void java_helper() {} }\n",
            "notes.md": "mentions doc_only_name here\n",
        },
    )
    found, _ = ticket_lookups(str(tmp_path), commit, git_stdout)
    assert found("render_frame") == ("frame.rs",)
    assert found("draw_all") == ("frame.rs",)
    assert found("Execute") == ("tool.go",)
    assert found("load_all") == ("ui.ts",)
    assert found("Panel") == ("ui.ts",)
    assert found("arrow_fn") == ("ui.ts",)
    assert found("open_box") == ("obj.js",)
    assert found("aio") == ("a.py",)
    assert found("java_helper") == ("Other.java",)
    assert found("doc_only_name") == ()
    assert found("render_fram") == ()
