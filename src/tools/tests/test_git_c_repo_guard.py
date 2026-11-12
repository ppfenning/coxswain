"""Guard: a `git -C <repo-named variable>` argv in agent_tools must go through repo_path or be allowlisted.

A recorded repository may be a tilde path; git does not expand it. A site that passes one to `-C` without
`repo_path.expand` or `repo_path.git_argv` works on the author's machine and fails on a host with another home.

The scan sees only the literal `"-C",` followed by `repo`, `repo_dir`, `home_repo`, `where` or `host_repo`. It does not
see `'-C'`, `str(repo)`, `Path(...)` or other variable names, so a clean scan means those five names, nothing wider.
An allowlist reason that starts with DEFECT marks a site that needs the helper: it is listed, not blessed.
A clean scan says the tree matches the list. It does not say a listed site is safe: the reasons are prose for a reviewer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

AGENT_TOOLS_DIR = Path(__file__).resolve().parent.parent / "agent_tools"

# The literal "-C", then a bare repository-named variable: not `repo.name`, `repo(...)` or `repo[...]`.
UNEXPANDED = re.compile(r'"-C",\s*(?:repo|repo_dir|home_repo|where|host_repo)\b(?![.(\[])')

FACTS = "repo is the land trigger's or revert action's repo, expanded upstream by chair_facts.read_initiative_repos"
PRUNED = "repo is `where` from _pruned: _local_repo for the local clear, _host_repo for the host run (chair_exec.py:690, 695)"
HOME_REPO = (
    "home_repo is set once in _carry_forward: _local_repo here, _host_repo over ssh, host-relative while deps.home is set"
    " (production sets it, chair_exec.py:1893) and the recorded repo unchanged when it is empty"
)
ORIGIN_HEAD = (
    "DEFECT: cox runs stranded passes base_name to runs_stranded as base_of, which gets the recorded repo from initiative"
    " frontmatter unexpanded (cli.py:1064, 1077, 1105); configured_base expands it, origin_head does not"
)
HOST_REPOS = "repo comes from cli._host_repos, absolute through expanduser, and the run here is local"
SSH_CHAIR_PATH = (
    "DEFECT: sent over ssh as the chair's absolute path from cli._host_repos, not repo_path.host_form;"
    " breaks where the host's home differs"
)
SWEEP_OVER_SSH = (
    "DEFECT: _over_ssh binds delete_branches_with to the ssh door and chair_apply_review passes step.repo unconverted,"
    " so the host gets the chair's path, not repo_path.host_form; the local door expands at chair_exec.py:1880"
)
BASE_CALLERS = (
    "reached only through base_ref, whose callers pass an expanded path: chair_exec home_repo, cli expanduser and"
    " repo_path.expand; origin_head is listed apart because base_name has other callers"
)
LAND_EXPANDED = "merged_on_origin rebinds repo through repo_path.expand before the fetch and _contained"
FETCH_EXPANDED = "repos come from chair_repo_path, expanded under the home cli passes to fetch_run"

# (file relative to agent_tools, stripped source line, exact count of that line, one-line reason).
# fmt: off
SITES: Sequence[tuple[str, str, int, str]] = [
    ("base_branch.py", 'code, out = run(["git", "-C", repo, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"])', 1, ORIGIN_HEAD),
    ("base_branch.py", 'code, out = run(["git", "-C", repo, "remote"])', 1, BASE_CALLERS),
    ("base_branch.py", 'code, out = run(["git", "-C", repo, "fetch", "origin"])', 1, BASE_CALLERS),
    ("base_branch.py", 'if run(["git", "-C", repo, "rev-parse", "--verify", "-q", f"refs/remotes/{ref}"])[0] != 0:', 1, BASE_CALLERS),
    ("chair_exec.py", 'code, listing = deps.run(["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}"])', 1, PRUNED),
    ("chair_exec.py", 'outcomes = [(name, deps.run(["git", "-C", repo, "branch", "-D", name])) for name in names]', 1, PRUNED),
    ("chair_exec.py", 'code, listing = deps.run(["git", "-C", where, "worktree", "list", "--porcelain"])', 1, PRUNED),
    ("chair_exec.py", 'code, output = deps.run(["git", "-C", where, *argv[1:]])', 1, PRUNED),
    ("chair_exec.py", 'on_host, host_out = at_home(["git", "-C", host_repo or repo, "rev-parse", "--verify", "--quiet", ref])', 1, "host_repo is home_repo, host-relative over ssh while deps.home is set (chair_exec.py:1893); the `or repo` fallback and an empty deps.home send the recorded repo unchanged"),
    ("chair_exec.py", 'code, out = at_home(["git", "-C", home_repo, "worktree", "add", tmp, branch])', 1, HOME_REPO),
    ("chair_exec.py", 'at_home(["git", "-C", home_repo, "worktree", "remove", "--force", tmp])', 1, HOME_REPO),
    ("chair_exec.py", 'rcode, rout = at_home(["git", "-C", home_repo, "worktree", "remove", "--force", tmp])', 2, HOME_REPO),
    ("chair_exec.py", 'code, listing = run(["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}"])', 1, SWEEP_OVER_SSH),
    ("chair_exec.py", 'outcomes = [(name, run(["git", "-C", repo, "branch", "-D", name])) for name in listing.split() if name.startswith(prefix)]', 1, SWEEP_OVER_SSH),
    ("chair_revert_green.py", 'sha == commit or runner(["git", "-C", repo, "merge-base", "--is-ancestor", commit, sha])[0] == 0', 1, FACTS),
    ("chair_revert_ports.py", '_checked(self._run, ["git", "-C", repo, "fetch", "origin", "main"])', 1, FACTS),
    ("chair_revert_ports.py", 'self._run(["git", "-C", repo, "worktree", "remove", "--force", worktree])', 1, FACTS),
    ("chair_revert_ports.py", 'self._run(["git", "-C", repo, "branch", "-D", branch])', 1, FACTS),
    ("chair_revert_ports.py", 'parents = parent_count(_checked(self._run, ["git", "-C", repo, "rev-list", "--parents", "-n", "1", commit]))', 1, FACTS),
    ("chair_revert_ports.py", '_checked(self._run, ["git", "-C", repo, "worktree", "prune"])', 1, FACTS),
    ("chair_revert_ports.py", '_checked(self._run, ["git", "-C", repo, "worktree", "add", "-B", branch, tree[0], "origin/main"])', 1, FACTS),
    ("chair_revert_ports.py", 'if not self._tip(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref]):', 1, FACTS),
    ("chair_revert_ports.py", '_checked(self._run, ["git", "-C", repo, "update-ref", ref, tip])', 1, FACTS),
    ("chair_revert_ports.py", 'git = ["git", "-C", repo]', 2, FACTS),
    ("chair_revert_ports.py", '_checked(self._run, ["git", "-C", repo, "push", "origin", branch])', 1, FACTS),
    ("chair_smoke.py", '"checkout": ["git", "-C", repo, "checkout", "-b", branch, "origin/main"],', 1, FACTS),
    ("chair_smoke.py", '"revert": ["git", "-C", repo, "revert", "--no-edit", land["commit"]],', 1, FACTS),
    ("chair_smoke.py", '"push": ["git", "-C", repo, "push", "origin", branch],', 1, FACTS),
    ("host_cmd.py", 'branch = _out(run, ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"]) or ""', 1, HOST_REPOS),
    ("host_cmd.py", 'behind_raw = _out(run, ["git", "-C", repo, "rev-list", "--count", "HEAD..origin/main"])', 1, HOST_REPOS),
    ("host_cmd.py", 'lock_hash = _out(run, ["git", "-C", repo, "hash-object", LOCKFILE_NAME])', 1, HOST_REPOS),
    ("host_cmd.py", 'git = ["git", "-C", repo]', 1, SSH_CHAIR_PATH),
    ("host_cmd.py", '_, short = run(ssh_argv(ssh, ["git", "-C", repo, "rev-parse", "--short", "HEAD"]))', 1, SSH_CHAIR_PATH),
    ("land_manual.py", 'if run(["git", "-C", repo, "merge-base", "--is-ancestor", b, t])[0] == 0:', 1, LAND_EXPANDED),
    ("land_manual.py", 'code, out = run(["git", "-C", repo, "merge-tree", "--write-tree", t, b])', 1, LAND_EXPANDED),
    ("land_manual.py", 'tree = run(["git", "-C", repo, "rev-parse", f"{t}^{{tree}}"])', 1, LAND_EXPANDED),
    ("land_manual.py", 'code, out = run(["git", "-C", repo, "fetch", "origin", f"+refs/heads/{n}:refs/remotes/origin/{n}"])', 1, LAND_EXPANDED),
    ("remote_fetch.py", 'code = run_cmd(["git", "-C", repo, *fetch[1:]])', 1, FETCH_EXPANDED),
    ("remote_fetch.py", 'if run_cmd(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"]) == 0', 1, FETCH_EXPANDED),
    ("remote_fetch.py", 'found = run_cmd(["git", "-C", repo, "ls-remote", "--exit-code", ".", f"refs/heads/agents/{run}/*"])', 1, FETCH_EXPANDED),
    ("remote_fetch.py", 'run_cmd(["git", "-C", repo, *fetch[1:]])', 1, FETCH_EXPANDED),
    ("remote_fetch.py", 'return run_cmd(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]) == 0', 1, FETCH_EXPANDED),
    ("remote_fetch.py", 'return git(["git", "-C", repo, *args])', 1, FETCH_EXPANDED + "; the carry fetch passes _GitEdge.local_repo, already _local_repo-expanded"),
]
# fmt: on

# (file, stripped line) to (count, reason). A site whose line changes, moves or is added fails the guard, and so does
# an entry whose line is gone, so a fixed or replaced site cannot hide behind an old entry.
ALLOWLIST: Mapping[tuple[str, str], tuple[int, str]] = {(f, line): (n, why) for f, line, n, why in SITES}


def scan(files: Sequence[tuple[str, str]], allowlist: Mapping[tuple[str, str], tuple[int, str]]) -> list[str]:
    """Findings for `"-C", repo` sites beyond their (file, line) allowlist count, and for entries seen fewer times.

    `files` are (path relative to agent_tools, source text) pairs.
    """
    found: dict[tuple[str, str], list[int]] = {}
    for path, text in files:
        lines = text.splitlines()
        for m in UNEXPANDED.finditer(text):
            number = text.count("\n", 0, m.start()) + 1
            site = " ".join(s.strip() for s in lines[number - 1 : number + m.group(0).count("\n")])
            found.setdefault((path, site), []).append(number)
    excess = [
        f"{path}:{number}: {line} passes a repository without repo_path"
        for (path, line), numbers in found.items()
        for number in numbers[allowlist.get((path, line), (0, ""))[0] :]
    ]
    stale = [
        f"{path}: allowlisted line seen {len(found.get(key, []))} of {count} time(s), update the entry: {line}"
        for key, (count, _reason) in allowlist.items()
        for path, line in [key]
        if len(found.get(key, [])) < count
    ]
    return [*excess, *stale]


def read_tree(root: Path) -> list[tuple[str, str]]:
    """Edge. Every module under `root` as (posix path relative to root, source text), in path order."""
    return [(p.relative_to(root).as_posix(), p.read_text(encoding="utf-8")) for p in sorted(root.rglob("*.py"))]


UNEXPANDED_SITE = 'code, out = run(["git", "-C", repo, "status"])\n'


def test_unexpanded_site_is_reported() -> None:
    findings = scan([("mod.py", UNEXPANDED_SITE)], {})
    assert len(findings) == 1
    assert findings[0].startswith("mod.py:1: code, out = run(")


def test_helper_routed_site_is_not_reported() -> None:
    routed = 'code, out = run(repo_path.git_argv(repo, home, "status"))\n'
    expanded = 'code, out = run(["git", "-C", repo_path.expand(repo, home), "status"])\n'
    assert scan([("mod.py", routed), ("other.py", expanded)], {}) == []


def test_allowlisted_site_is_not_reported() -> None:
    allowed = {("mod.py", UNEXPANDED_SITE.strip()): (1, "expanded upstream")}
    assert scan([("mod.py", UNEXPANDED_SITE)], allowed) == []
    swapped = 'code, out = run(["git", "-C", repo, "reset", "--hard"])\n'
    assert len(scan([("mod.py", swapped)], allowed)) == 2, (
        "a different site in the same file is reported, and the old entry is stale"
    )
    split = 'run([\n    "git", "-C",\n    repo, "status"])\n'
    split_allowed = {("mod.py", '"git", "-C", repo, "status"])'): (1, "expanded upstream")}
    assert scan([("mod.py", split)], split_allowed) == []
    assert len(scan([("mod.py", split.replace("status", "reset"))], split_allowed)) == 2, "a split site is keyed whole"


def test_real_tree_has_no_unlisted_repo_named_git_c_site() -> None:
    files = read_tree(AGENT_TOOLS_DIR)
    assert files, f"no modules found under {AGENT_TOOLS_DIR}"
    assert scan(files, ALLOWLIST) == []
