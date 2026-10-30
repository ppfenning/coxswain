"""A cherry-pick that conflicts only in a declared generated file is resolved by regenerating it."""

import shutil
import subprocess as sp
import sys
from pathlib import Path

from agent_tools import cli, land
from agent_tools import readme_commands as rc

TOOLS = Path(rc.__file__).resolve().parent.parent
LINE = "cherry_pick: regenerated src/tools/README.md after a conflict in generated files only"
CLI_STUB = """from types import SimpleNamespace as NS

FLAGS = [
    "--first",
    "--m1",
    "--m2",
    "--m3",
    "--last",
]
ROW = NS(name="go", args=tuple(NS(flags=(f,), kwargs={}) for f in FLAGS), summary="goes", subcommands=())
COMMAND_TABLE = [(NS(name="grp", args=(), help="g"), [ROW])]
"""
README = f"intro\n\n{rc.BEGIN}\n```\n```\n{rc.END}\n\noutro\n"


def _git(repo, *args, check=True):
    return sp.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True).stdout.strip()


def _regenerate(repo):
    sp.run([sys.executable, "-m", "agent_tools.readme_commands"], cwd=repo / "src" / "tools", check=True)


def _branch(repo, name, before, flag, note=None):
    """A branch off `init` that adds `flag` after `before` in the stub CLI, regenerates the README, and may edit notes."""
    _git(repo, "checkout", "-q", "-b", name, "init")
    cli_py = repo / "src/tools/agent_tools/cli.py"
    cli_py.write_text(cli_py.read_text().replace(f'    "{before}",\n', f'    "{before}",\n    "{flag}",\n'))
    if note:
        (repo / "notes.txt").write_text(note)
    _regenerate(repo)
    _git(repo, "commit", "-qam", name)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path):
    repo = tmp_path / "repo"
    pkg = repo / "src/tools/agent_tools"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "cli.py").write_text(CLI_STUB)
    shutil.copy(TOOLS / "agent_tools" / "readme_commands.py", pkg / "readme_commands.py")
    (repo / "src/tools/README.md").write_text(README)
    (repo / "notes.txt").write_text("base\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    sp.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for args in (["config", "user.email", "t@e"], ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        _git(repo, *args)
    _regenerate(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "tag", "init")
    return repo


def _land_second(repo, first, second):
    """Fast-forward main to `first`, then cherry-pick `second` into a worktree on it."""
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", first)
    wt = repo.parent / "wt"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "main")
    out: list[str] = []
    ok, detail = cli._cherry_pick_generated(wt, second, None, echo=out.append)
    return wt, ok, detail, out


def test_two_branches_adding_different_flags_both_land_and_the_readme_passes_the_drift_check(tmp_path):
    repo = _repo(tmp_path)
    a = _branch(repo, "a", "--first", "--alpha")
    b = _branch(repo, "b", "--last", "--beta")
    wt, ok, detail, out = _land_second(repo, "a", b)
    assert ok, detail
    readme = (wt / "src/tools/README.md").read_text()
    assert "[--alpha ALPHA]" in readme and "[--beta BETA]" in readme and "<<<<" not in readme
    check = sp.run([sys.executable, "-m", "agent_tools.readme_commands", "--check"], cwd=wt / "src/tools", capture_output=True, text=True)
    assert check.returncode == 0, check.stdout
    assert LINE in out
    assert _git(wt, "status", "--porcelain") == "" and _git(wt, "rev-list", "--count", f"{a}..HEAD") == "1"


def test_a_conflict_in_a_file_that_is_not_generated_still_stops_the_land(tmp_path):
    repo = _repo(tmp_path)
    a = _branch(repo, "a", "--first", "--alpha", note="from a\n")
    b = _branch(repo, "b", "--last", "--beta", note="from b\n")
    wt, ok, detail, out = _land_second(repo, "a", b)
    assert not ok
    assert f"cherry-pick of {b[:8]} conflicts in: notes.txt, src/tools/README.md" in detail
    assert not any("regenerated" in line for line in out)
    assert _git(wt, "rev-parse", "HEAD") == _git(repo, "rev-parse", a)
    assert _git(wt, "status", "--porcelain") == ""


def _commit_on(repo, branch, files, message):
    _git(repo, "checkout", "-q", branch)
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def test_a_generator_that_fails_stops_the_land_with_its_output_and_commits_nothing(tmp_path):
    repo = _repo(tmp_path)
    a = _branch(repo, "a", "--first", "--alpha")
    b = _branch(repo, "b", "--last", "--beta")
    broken = "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n"
    _commit_on(repo, "a", {"src/tools/agent_tools/readme_commands.py": broken}, "break the generator")
    wt, ok, detail, out = _land_second(repo, "a", b)
    assert not ok
    assert f"cherry-pick of {b[:8]} conflicts in: src/tools/README.md" in detail
    assert "exited 3: boom" in detail
    assert not any("regenerated" in line or "add/add" in line for line in out)
    assert _git(wt, "rev-parse", "HEAD") == _git(repo, "rev-parse", "a") != a
    assert _git(wt, "status", "--porcelain") == ""


def test_a_failing_generate_step_after_the_regeneration_prints_no_regenerated_line(tmp_path):
    repo = _repo(tmp_path)
    _branch(repo, "a", "--first", "--alpha")
    b = _branch(repo, "b", "--last", "--beta")
    _commit_on(repo, "a", {".agent-generate": "false\n"}, "add a failing generate step")
    wt, ok, detail, out = _land_second(repo, "a", b)
    assert not ok and "generate failed" in detail
    assert not any("regenerated" in line for line in out)


def test_the_real_generator_rewrites_a_conflicted_real_readme_in_a_worktree(tmp_path):
    repo = tmp_path / "repo"
    tools = repo / "src" / "tools"
    shutil.copytree(TOOLS / "agent_tools", tools / "agent_tools", ignore=shutil.ignore_patterns("__pycache__"))
    original = (TOOLS / "README.md").read_text()
    (tools / "README.md").write_text(original)
    (repo / ".gitignore").write_text("__pycache__/\n")
    sp.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for args in (["config", "user.email", "t@e"], ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        _git(repo, *args)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "tag", "init")
    first_row = original.split(rc.BEGIN + "\n```\n", 1)[1].split("\n", 1)[0]
    shas = {}
    for name in ("a", "b"):
        _git(repo, "checkout", "-q", "-b", name, "init")
        (tools / "README.md").write_text(original.replace(first_row, f"stale {name}", 1))
        _git(repo, "commit", "-qam", name)
        shas[name] = _git(repo, "rev-parse", "HEAD")
    wt, ok, detail, out = _land_second(repo, "a", shas["b"])
    assert ok, detail
    assert (wt / "src/tools/README.md").read_text() == original
    assert LINE in out


def test_the_generated_list_is_the_readme_run_from_its_tools_directory():
    spec = land.GENERATED_FILES["src/tools/README.md"]
    assert list(land.GENERATED_FILES) == ["src/tools/README.md"]
    assert (spec.cwd, spec.command, spec.begin, spec.end) == (
        "src/tools", ("-m", "agent_tools.readme_commands", "--readme"), rc.BEGIN, rc.END
    )
    assert land.generated_conflict_only(["src/tools/README.md"])
    assert not land.generated_conflict_only(["src/tools/README.md", "notes.txt"])
    assert not land.generated_conflict_only([])


def _conflict(inside: str, ours: str, theirs: str) -> str:
    hunk = f"<<<<<<< HEAD\n{ours}||||||| base\nold\n=======\n{theirs}>>>>>>> pick\n"
    return f"intro\n{hunk}" if inside == "outside" else f"{rc.BEGIN}\n```\n{hunk}```\n{rc.END}\n"


def test_a_hunk_inside_the_generated_span_resolves_to_ours():
    text = _conflict("inside", "mine\n", "theirs\n")
    assert land.resolve_block_conflicts(text, rc.BEGIN, rc.END) == f"{rc.BEGIN}\n```\nmine\n```\n{rc.END}\n"


def test_a_hunk_outside_the_generated_span_is_not_resolved():
    assert land.resolve_block_conflicts(_conflict("outside", "mine\n", "theirs\n"), rc.BEGIN, rc.END) is None


def test_a_hunk_holding_the_span_markers_is_not_resolved():
    text = f"{rc.BEGIN}\n<<<<<<< HEAD\n{rc.END}\n||||||| base\n=======\nx\n>>>>>>> pick\n"
    assert land.resolve_block_conflicts(text, rc.BEGIN, rc.END) is None
