"""`cox runs land`, the steps: checks, cherry-picks, regenerated files, merges and the records a land writes."""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_tools.cli import _LazyHandler
from agent_tools.cli_common import _read_text_or_none
from agent_tools.cli_lazy import (
    bounded_run,
    cleanup,
    forge,
    forge_github,
    generate,
    land,
    land_batch_squash,
    land_merge_lock,
    queue_export,
    review_pr_close,
    route,
    route_edit,
    route_sync,
    route_sync_gh,
    run_store,
    store_cli,
)
from agent_tools.cli_route_sync import _route_sync, _sync_skipped, _tracker_scope
from agent_tools.cli_runs_fetch import _UNLABELED, _runs_fetch


def _git_out(repo: Path, *args: str) -> str | None:
    """Stripped stdout of `git -C repo <args>`, or `None` when git exits non-zero."""
    r = bounded_run.completed(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _land_push(repo: Path, branch: str, forge_module=forge_github) -> tuple[bool, str]:
    """Edge. Push `branch`. A rebuilt task pushes the name its closed PR left on origin with unrelated history, which
    a plain push rejects; when every PR on that remote branch is closed, replace exactly the tip seen. A branch with
    an open or merged PR, or whose PRs cannot be listed, gets the plain push and git's own refusal."""
    listing = _git_out(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    tip = listing.split()[0] if listing else None
    prs = forge.branch_prs(forge_module, repo, branch) if tip else []
    decision = land.push_lease(tip, [] if isinstance(prs, str) else prs)
    if decision["kind"] != "lease":
        return forge_module.push(repo, branch)
    ok, detail = forge_module.push(repo, branch, expected_tip=decision["tip"])
    closed = ", ".join(f"#{n}" for n in decision["prs"])
    return (True, f"{detail} (replaced {decision['tip'][:8]} left by closed {closed})") if ok else (False, detail)


def _checks_env(cwd: Path, cores: int = 1, capacity: int | None = None) -> dict[str, str] | None:
    """Edge. The environment a check runs in: `cwd/.venv/bin` first on PATH when the worktree has one (the venv
    `_link_venv` links), so a bare `pytest` is the project's, not a standalone tool's. With a `capacity` of 1 or more
    the lane's xdist worker count rides on top (`harness.lane_workers.check_env`). None, the inherited environment,
    when there is neither a venv nor a capacity."""
    from harness.lane_workers import check_env
    venv_bin = Path(cwd) / ".venv" / "bin"
    base = {**os.environ, "PATH": os.pathsep.join([str(venv_bin), os.environ.get("PATH", "")])} if venv_bin.is_dir() else None
    capped = check_env(base if base is not None else os.environ, cores, capacity)
    return capped if capped is not None else base


def _git_common_dir(cwd: Path) -> Path | None:
    """Edge. The repository's git common directory, shared by every worktree, or None when git cannot say."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"], capture_output=True, text=True,
                             check=True, timeout=30).stdout.strip()
    except subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError:
        return None
    return (Path(cwd) / out).resolve() if out else None


def _run_checks(checks: list[tuple[str, str]], cwd: Path, cores: int = 1, capacity: int | None = None) -> tuple[bool, str]:
    """Each `(name, shell line)` pair in order, run by a shell in `cwd` as the builders run it (`harness.checks`),
    stopping at the first launch error or failure and naming it by `name`, never by its command.

    A check that already passed on this exact tree and command is not run again (`harness.check_reuse`); the detail
    names it. `cores` and `capacity` are the land's, read by the caller. With a `capacity` the passing detail names
    the xdist worker count the checks ran with."""
    from harness.check_reuse import run_checks_reusing
    from harness.checks import run_checks
    from harness.lane_workers import XDIST_VAR
    if not checks:
        return True, "none declared"
    env = _checks_env(cwd, cores, capacity)
    workers = env.get(XDIST_VAR) if env is not None and capacity is not None and capacity >= 1 else None
    note = f" (xdist workers: {workers})" if workers else ""
    common = _git_common_dir(Path(cwd))
    skipped: list[str] = []
    reused: list[str] = []
    flaky: list[str] = []
    for name, line in checks:
        one = [{"name": name, "cmd": line}]
        [result] = (run_checks_reusing(Path(cwd), one, ledger_dir=common, env=env) if common is not None
                    else run_checks(Path(cwd), one, env=env))
        if result.get("reused"):
            reused.append(name)
        if result.get("note"):
            flaky.append(result["note"])
        if result["outcome"] == "unrunnable":
            return False, land.checks_detail(f"{land.LAUNCH_ERROR}{name}: {result['error']}", skipped)
        if result["outcome"] == "timed_out":
            return False, land.checks_detail(f"{name}: {result['error']}", skipped)
        verdict = land.check_exit(line, result["exit_code"])
        if verdict == "fail":
            return False, land.checks_detail(f"{name}: {result['output_tail'].strip()}", skipped, flaky)
        if verdict == "skip":
            skipped.append(name)
    return True, land.checks_detail(f"{len(checks)} checks passed{note}", skipped, flaky, reused)


def _land_worktree(repo: Path, branch: str) -> Path:
    """Carries this process's pid: on 2026-09-27 two concurrent lands of one branch, the chair's and the loop's, deleted each other's worktree mid-check."""
    return Path(tempfile.gettempdir()) / f"cox-land-{Path(repo).resolve().name}-{branch.replace('/', '-')}-{os.getpid()}"


def _link_venv(repo: Path, wt: Path) -> None:
    """Point `wt/.venv` at the checkout's, so a check naming `.venv/bin/python` runs the worktree's code in the repo's environment."""
    venv = Path(repo).resolve() / ".venv"
    if venv.is_dir() and not (wt / ".venv").exists():
        (wt / ".venv").symlink_to(venv, target_is_directory=True)


_GENERATE_FAILED = "generate failed: "


def _generate_and_amend(wt: Path, umbrella: str | None, echo: Callable[[str], None] = print) -> tuple[bool, str]:
    """Run the worktree's `.agent-generate`, then fold any change it made into the one commit.

    `(True, "")` when the file is absent or nothing changed; `(False, output)` on the first non-zero exit."""
    spec = wt / ".agent-generate"
    if not spec.is_file():
        return True, ""
    plan = generate.plan_commands(generate.parse_generate_file(spec.read_text(encoding="utf-8")), umbrella)
    for _, note in plan:
        if note is not None:
            echo(f"land: {note}")
    env = {**os.environ, **generate.build_env(str(wt), umbrella)}
    for command in (c for c, note in plan if note is None):
        r = bounded_run.completed(command, bounded_run.STEP_TIMEOUT_SECONDS, shell=True, cwd=wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode != 0:
            return False, f"{_GENERATE_FAILED}{command!r} exited {r.returncode}: {r.stdout.strip()}"
    # `_link_venv`'s `.venv` is a symlink, which a `.venv/` ignore rule does not match: keep it out of the commit.
    outside_venv = ["--", ".", ":(exclude).venv"]
    if not _git_out(wt, "status", "--porcelain", *outside_venv):
        return True, ""
    # Not `add -A` with the exclude: git 2.55 refuses an exclude pathspec that names an ignored path. Stage all,
    # then take the linked `.venv` back out, which covers a git that ignores the symlink and one that does not.
    staged = bounded_run.completed(["git", "-C", str(wt), "add", "-A", "--", "."], capture_output=True, text=True)
    bounded_run.completed(["git", "-C", str(wt), "reset", "-q", "--", ".venv"], capture_output=True, text=True)
    changed = (_git_out(wt, "diff", "--cached", "--name-only") or "").split()
    amend = bounded_run.completed(["git", "-C", str(wt), "commit", "--amend", "--no-edit"], capture_output=True, text=True)
    failed = next((r for r in (staged, amend) if r.returncode != 0), None)
    if failed is not None:
        return False, f"{_GENERATE_FAILED}{failed.stderr.strip() or failed.stdout.strip()}"
    echo(f"land: regenerated {' '.join(changed)}")
    return True, ""


def _skip_empty_pick(wt: Path, sha: str, cp: subprocess.CompletedProcess, echo: Callable[[str], None] = print) -> bool:
    """Edge. True when the stopped pick `cp` of `sha` in `wt` was empty, not a conflict and not a commit authored empty:
    git's skip cleared it and the skip line was echoed. False leaves the stopped pick for the caller's handling."""
    conflicted = bounded_run.completed(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True).stdout
    if not land.is_empty_pick(cp.stderr, cp.stdout, bool(conflicted.split())):
        return False
    if not _git_out(wt, "diff-tree", "--no-commit-id", "--name-only", "-r", sha):
        return False  # a commit authored empty is not already on the base: it fails as it always did
    subject = _git_out(wt, "log", "-1", "--format=%s", sha) or ""
    if bounded_run.completed(["git", "-C", str(wt), "cherry-pick", "--skip"], capture_output=True, text=True).returncode != 0:
        return False
    echo(land.skip_note(sha, subject, land.SKIP_EMPTY))
    return True


def _patch_id_skipped(repo: Path, step: dict) -> str | None:
    """Edge. For a `cherry_pick` step whose range is empty: the all-skipped detail when the base holds the branch's
    commits by patch-id, after echoing each with its reason. None for a branch with no commits ahead of its base."""
    raw = _git_out(repo, "rev-list", "--reverse", "--no-merges", "--right-only", f"{step['from']}...{step['branch']}")
    dropped = (raw or "").split()
    for sha in dropped:
        print(land.skip_note(sha, _git_out(repo, "log", "-1", "--format=%s", sha) or "", land.SKIP_PATCH_ID))
    return land.skipped_all_detail(len(dropped), step["from"]) if dropped else None


def _drop_pr_branch(repo: Path, onto: str) -> None:
    """Edge. An all-skipped pick built nothing to keep: drop its land worktree and the local pr branch, so a rerun can cut it again."""
    _remove_land_worktree(repo, onto)
    bounded_run.completed(["git", "-C", str(repo), "branch", "-D", onto], capture_output=True, text=True)


def _range_detail(step: dict, shas: list[str], skipped: list[str]) -> str:
    """Pure. The success detail of a `cherry_pick` step: the all-skipped detail when every commit was skipped."""
    if shas and len(skipped) == len(shas):
        return land.skipped_all_detail(len(skipped), step["from"])
    return f"cherry-picked {len(shas) - len(skipped)} commits onto {step['onto']}"


def _cherry_pick_generated(wt: Path, sha: str, umbrella: str | None, echo: Callable[[str], None] = print) -> tuple[bool, str]:
    """Cherry-pick `sha` into `wt`, then regenerate and amend. The real land and the resume check both
    build the pushed tree here, so the tree the resume check expects cannot drift from the tree pushed.
    A pick that stops empty without a conflict is skipped, not failed: it returns `(True, land.SKIP_EMPTY)`."""
    cp = bounded_run.completed(
        ["git", "-C", str(wt), "-c", "merge.conflictStyle=diff3", "cherry-pick", sha], capture_output=True, text=True
    )
    regenerated: list[str] = []
    if cp.returncode != 0 and _skip_empty_pick(wt, sha, cp, echo):
        return True, land.SKIP_EMPTY
    if cp.returncode != 0:
        conflicted = bounded_run.completed(
            ["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True
        ).stdout.splitlines()

        def resolve(name: str) -> str | None:
            # A binary add/add leaves no markers, and a rename or directory/file conflict can leave no file:
            # neither is resolvable, so both take the abort path rather than escaping mid-cherry-pick.
            try:
                return land.resolve_add_add_conflicts((wt / name).read_text())
            except (OSError, UnicodeDecodeError):
                return None

        generated_only = land.generated_conflict_only(conflicted)
        # A generated-only conflict is regenerated or it stops the land: it never reaches the add/add resolver.
        failure = _regenerate_generated_conflicts(wt, conflicted) if generated_only else None
        resolved = {} if generated_only else {name: resolve(name) for name in conflicted}
        unresolvable = not generated_only and (not conflicted or any(text is None for text in resolved.values()))
        if failure is not None or unresolvable:
            bounded_run.completed(["git", "-C", str(wt), "cherry-pick", "--abort"], capture_output=True, text=True)
            # The conflicts line leads, so the land prints it as `cherry_pick: ... conflicts in: ...` (what
            # `partial_land.conflicting_paths` reads); git's own "could not apply" line follows for `land_refusal`.
            named = f"cherry-pick of {sha[:8]} conflicts in: {', '.join(sorted(conflicted))}" if conflicted else ""
            return False, "\n".join(filter(None, [named, failure, cp.stderr.strip() or cp.stdout.strip()]))
        for name, text in resolved.items():
            (wt / name).write_text(text)
        staged = sorted(conflicted) if generated_only else sorted(resolved)
        bounded_run.completed(["git", "-C", str(wt), "add", *staged], capture_output=True, text=True)
        cont = bounded_run.completed(
            ["git", "-C", str(wt), "-c", "core.editor=true", "cherry-pick", "--continue"],
            capture_output=True, text=True,
        )
        if cont.returncode != 0:
            bounded_run.completed(["git", "-C", str(wt), "cherry-pick", "--abort"], capture_output=True, text=True)
            return False, cont.stderr.strip() or cont.stdout.strip()
        if generated_only:
            regenerated = staged
        else:
            echo(f"resolved add/add: {', '.join(staged)}")
    ok, detail = _generate_and_amend(wt, umbrella, echo)
    # Named once the land has got past the step that could still stop it, so the line is never printed for a failed land.
    if ok:
        for name in regenerated:
            echo(f"cherry_pick: regenerated {name} after a conflict in generated files only")
    return ok, detail


def _regenerate_generated_conflicts(wt: Path, conflicted: Sequence[str]) -> str | None:
    """Edge. Resolve each of `conflicted`, all declared in `land.GENERATED_FILES`, to ours inside its generated
    span, then rewrite the file with its generator run in `wt` so it sees the cherry-picked code.

    None when every file was regenerated; otherwise why it was not, for the caller to abort the cherry-pick
    with: a conflict outside the generated span, an unreadable file, or the generator's exit code and output."""
    for name in conflicted:
        spec = land.GENERATED_FILES[name]
        try:
            text = land.resolve_block_conflicts((wt / name).read_text(encoding="utf-8"), spec.begin, spec.end)
        except (OSError, UnicodeDecodeError):
            return f"{name} cannot be read as text"
        if text is None:
            return f"{name} conflicts outside its generated block"
        (wt / name).write_text(text, encoding="utf-8")
        # Run from the worktree's own tree: `python -m` puts the cwd first on sys.path, ahead of any install.
        run = bounded_run.completed([sys.executable, *spec.command, str(wt / name)], bounded_run.STEP_TIMEOUT_SECONDS, cwd=wt / spec.cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if run.returncode != 0:
            return f"generator {' '.join(spec.command)} for {name} exited {run.returncode}: {run.stdout.strip()}"
    return None


def _generated_tree(repo: Path, branch: str, base: str, sha: str, umbrella: str | None) -> tuple[str | None, str]:
    """The tree `_cherry_pick_generated` of `sha` onto `base` yields, or `(None, why)` when it cannot be built.

    Built at `branch`'s own land worktree path, so `COX_WORKTREE` matches the real land's. The generator runs
    again on a rerun, as it does on any land that is not resumed: it must be deterministic and idempotent,
    and its writes outside the worktree (the umbrella) repeat."""
    scratch = _land_worktree(repo, branch)
    _remove_land_worktree(repo, branch)
    try:
        add = bounded_run.completed(["git", "-C", str(repo), "worktree", "add", "--detach", str(scratch), base], capture_output=True, text=True)
        if add.returncode != 0:
            return None, add.stderr.strip() or add.stdout.strip()
        _link_venv(repo, scratch)
        ok, detail = _cherry_pick_generated(scratch, sha, umbrella, echo=lambda _: None)
        if ok and detail == land.SKIP_EMPTY:
            return None, f"cherry-pick of {sha[:8]} is empty on {base}"
        return (_git_out(scratch, "rev-parse", "HEAD^{tree}"), "") if ok else (None, detail)
    finally:
        _remove_land_worktree(repo, branch)


def _remove_land_worktree(repo: Path, branch: str) -> None:
    """Drop `branch`'s land worktree and its registration; the branch itself stays."""
    wt = _land_worktree(repo, branch)
    bounded_run.completed(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
    # A leftover directory git no longer knows (a crashed land, another repo) would block `worktree add`.
    shutil.rmtree(wt, ignore_errors=True)
    bounded_run.completed(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)


def _scratch_merge_branch(branch: str) -> str:
    """A throwaway local branch name for merging `from` into a scratch copy of `branch`; never `branch`
    itself, so the real phase branch is never checked out into a second worktree."""
    return f"{branch}--land-scratch"


def _remove_scratch_worktree(repo: Path, scratch_branch: str) -> None:
    """Like `_remove_land_worktree`, but the scratch branch is throwaway, so it goes too."""
    _remove_land_worktree(repo, scratch_branch)
    bounded_run.completed(["git", "-C", str(repo), "branch", "-D", scratch_branch], capture_output=True, text=True)


def _resolve_landed_conflicts(repo: Path, scratch_wt: Path, from_ref: str, branch_tip: str,
                               conflicted: list[str]) -> list[str]:
    """Resolve each of `conflicted`'s paths to `scratch_wt`'s side (the phase branch) when `from_ref`'s
    content there matches that path's content at some commit already reachable from `branch_tip` -
    already-landed content the branch is reintroducing, not a real conflict. Return the paths left
    unresolved."""
    unresolved = []
    for path in conflicted:
        main_show = bounded_run.completed(["git", "-C", str(repo), "show", f"{from_ref}:{path}"], capture_output=True, text=True)
        log = bounded_run.completed(["git", "-C", str(repo), "log", "--format=%H", branch_tip, "--", path], capture_output=True, text=True)
        shas = [s for s in log.stdout.split() if s]
        landed = main_show.returncode == 0 and any(
            bounded_run.completed(["git", "-C", str(repo), "show", f"{sha}:{path}"], capture_output=True, text=True).stdout == main_show.stdout
            for sha in shas)
        if landed:
            bounded_run.completed(["git", "-C", str(scratch_wt), "checkout", "--ours", "--", path], capture_output=True, text=True)
            bounded_run.completed(["git", "-C", str(scratch_wt), "add", path], capture_output=True, text=True)
        else:
            unresolved.append(path)
    return unresolved


def _appended_python(name: str, text: str) -> str | None:
    """`text` resolved to ours-then-theirs, only for a `.py` file that held a real conflict hunk and still
    parses with no top-level name defined twice; None otherwise (no markers is a modify/delete, not an append)."""
    resolved = land.resolve_add_add_conflicts(text) if name.endswith(".py") else None
    if resolved is None or resolved == text:
        return None
    try:
        tops = [n.name for n in ast.parse(resolved).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    except (SyntaxError, ValueError):
        return None
    return resolved if len(tops) == len(set(tops)) else None


def _resolve_append_only(wt: Path, paths: list[str]) -> bool:
    """Edge. Resolve each conflicted `paths` file in `wt` to ours-then-theirs and stage it; all or nothing."""
    if not land.append_only_paths(paths):
        return False

    def resolve(name: str) -> str | None:
        try:
            return _appended_python(name, (wt / name).read_text())
        except (OSError, UnicodeDecodeError):
            return None

    resolved = {name: resolve(name) for name in paths}
    if any(text is None for text in resolved.values()):
        return False
    for name, text in resolved.items():
        (wt / name).write_text(text)
    staged = bounded_run.completed(["git", "-C", str(wt), "add", *sorted(resolved)], capture_output=True, text=True)
    if staged.returncode != 0:
        return False
    print(f"resolved: append-only in {', '.join(sorted(resolved))}")
    return True


def _gather_worktree(repo: Path, step: dict) -> tuple[Path | None, str]:
    """The pr branch's land worktree for a gather step: made from `from` by the first step, reused by the rest."""
    wt = _land_worktree(repo, step["onto"])
    if wt.exists():
        return wt, ""
    co = bounded_run.completed(["git", "-C", str(repo), "worktree", "add", step.get("worktree_flag", "-b"), step["onto"], str(wt), step["from"]],
                        capture_output=True, text=True)
    if co.returncode != 0:
        return None, co.stderr.strip() or co.stdout.strip()
    _link_venv(repo, wt)
    return wt, ""


def _commit_count_refusal(step: dict, shas: list[str]) -> str | None:
    """Why a `cherry_pick` step's `shas` are refused, or None. A step with no `commits` key accepts any positive count."""
    ahead = f"ahead of {step['from']} on {step['branch']}, found {len(shas)}"
    if "commits" in step and step["commits"] != len(shas):
        return f"expected {step['commits']} commits {ahead}"
    return None if shas else f"expected at least one commit {ahead}"


def _gather_failed(wt: Path, what: str, abort: list[str], result: subprocess.CompletedProcess) -> tuple[bool, str]:
    """Name the unmerged files, run `abort` in `wt`, and report `what conflicts in: ...` like a squash does."""
    conflicted = bounded_run.completed(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                                capture_output=True, text=True).stdout.split()
    bounded_run.completed(["git", "-C", str(wt), *abort], capture_output=True, text=True)
    return False, f"{what} conflicts in: {', '.join(conflicted)}" if conflicted else result.stderr.strip() or result.stdout.strip()


def _execute_gather_step(repo: Path, step: dict) -> tuple[bool, str]:
    """Edge. A gather `cherry_pick` or `patch_apply` step, run on the pr branch's worktree."""
    wt, why = _gather_worktree(repo, step)
    if wt is None:
        return False, why
    if step["kind"] == "cherry_pick":
        rev = bounded_run.completed(["git", "-C", str(repo), "rev-list", "--reverse", "--no-merges", "--cherry-pick", "--right-only",
                              f"{step['from']}...{step['branch']}"], capture_output=True, text=True)
        shas = rev.stdout.split()
        dropped = _patch_id_skipped(repo, step) if rev.returncode == 0 and not shas else None
        if dropped:
            return True, dropped
        refusal = _commit_count_refusal(step, shas)
        if rev.returncode != 0 or refusal:
            return False, refusal or rev.stderr.strip()
        skipped: list[str] = []
        for sha in shas:
            cp = bounded_run.completed(["git", "-C", str(wt), "cherry-pick", sha], capture_output=True, text=True)
            if cp.returncode == 0:
                continue
            if _skip_empty_pick(wt, sha, cp):
                skipped.append(sha)
                continue
            _, detail = _gather_failed(wt, f"cherry-pick of {sha[:8]}", ["cherry-pick", "--abort"], cp)
            return False, f"{detail}\nstopped at {sha[:8]} on {step['branch']}"
        return True, _range_detail(step, shas, skipped)
    applied = bounded_run.completed(["git", "-C", str(wt), "apply", "--3way", "--index", "-"], input=step["patch"],
                             capture_output=True, text=True)
    if applied.returncode != 0:
        return _gather_failed(wt, f"patch of {step['task']}", ["reset", "--hard"], applied)
    commit = bounded_run.completed(["git", "-C", str(wt), "commit", "-qm", step["task"]], capture_output=True, text=True)
    if commit.returncode != 0:
        bounded_run.completed(["git", "-C", str(wt), "reset", "--hard"], capture_output=True, text=True)
        return False, commit.stderr.strip() or commit.stdout.strip()
    return True, f"applied patch of {step['task']} onto {step['onto']}"


def _execute_land_step(repo: Path, step: dict, forge_module=forge_github) -> tuple[bool, str]:
    kind = step["kind"]
    if kind == "pick_branch":
        return True, f"{step['branch']} ({step['commit_subject']})"
    if kind == "fetch":
        # The one fetch path: `cox runs fetch <run>`, read from the profile and runs dir the land enriched in.
        fetch = argparse.Namespace(run_id=step["run"], all=False, runs_dir=step.get("runs_dir"), profile=step.get("profile"))
        return (True, f"fetched {step['run']}") if _runs_fetch(fetch) == 0 else (False, f"fetch of {step['run']} failed")
    if kind == "patch_apply" or (kind == "cherry_pick" and "task" in step):
        return _execute_gather_step(repo, step)
    if kind == "cherry_pick":
        # The pr branch is built in its own worktree, so `repo`'s HEAD never moves.
        wt = _land_worktree(repo, step["onto"])
        _remove_land_worktree(repo, step["onto"])
        co = bounded_run.completed(["git", "-C", str(repo), "worktree", "add", "-b", step["onto"], str(wt), step["from"]], capture_output=True, text=True)
        if co.returncode != 0:
            return False, co.stderr.strip() or co.stdout.strip()
        _link_venv(repo, wt)
        # The branch is `commits` ahead of `from`, counted as `_land_branches` counts it: merges and commits
        # whose patch `from` already has (a stacked parent that merged as a squash) are left out. The range
        # names them without matching on subject text, and `--reverse` picks them oldest first. A step from
        # an older plan has no `commits` key and accepts any positive count.
        rev = bounded_run.completed(["git", "-C", str(repo), "rev-list", "--reverse", "--no-merges", "--cherry-pick", "--right-only",
                              f"{step['from']}...{step['branch']}"], capture_output=True, text=True)
        shas = [s for s in rev.stdout.split() if s]
        dropped = _patch_id_skipped(repo, step) if rev.returncode == 0 and not shas else None
        if dropped:
            _drop_pr_branch(repo, step["onto"])
            return True, dropped
        refusal = _commit_count_refusal(step, shas)
        if rev.returncode != 0 or refusal:
            return False, refusal or rev.stderr.strip()
        skipped: list[str] = []
        for sha in shas:
            ok, detail = _cherry_pick_generated(wt, sha, step.get("umbrella"))
            if not ok:
                break
            if detail == land.SKIP_EMPTY:
                skipped.append(sha)
        if not ok and detail.startswith(_GENERATE_FAILED):
            # Keep the worktree, detached, for inspection; free the branch so a rerun starts fresh
            # instead of finding a pr branch that holds the un-generated tree and refusing on it.
            bounded_run.completed(["git", "-C", str(wt), "checkout", "-q", "--detach"], capture_output=True, text=True)
            bounded_run.completed(["git", "-C", str(repo), "branch", "-D", step["onto"]], capture_output=True, text=True)
        # The sha and branch follow the detail: `land_conflict_reason` reads a detail that leads with `cherry-pick of`.
        if not ok:
            return False, f"{detail}\nstopped at {sha[:8]} on {step['branch']}"
        picked = _range_detail(step, shas, skipped)
        if land.is_skipped_all(picked):
            _drop_pr_branch(repo, step["onto"])
        return True, picked
    if kind == "squash_phase":
        # Built in its own worktree from `from`, exactly like `cherry_pick`, so `repo`'s HEAD never moves.
        wt = _land_worktree(repo, step["onto"])
        _remove_land_worktree(repo, step["onto"])
        # `-B` (set by a plan that found a stale local pr branch) resets that branch instead of refusing on it.
        co = bounded_run.completed(["git", "-C", str(repo), "worktree", "add", step.get("worktree_flag", "-b"), step["onto"], str(wt), step["from"]],
                            capture_output=True, text=True)
        if co.returncode != 0:
            return False, co.stderr.strip() or co.stdout.strip()
        _link_venv(repo, wt)
        def drop() -> None:
            # A refused squash leaves neither worktree nor pr branch, so a rerun starts fresh.
            bounded_run.completed(["git", "-C", str(wt), "reset", "--hard"], capture_output=True, text=True)
            bounded_run.completed(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True, text=True)
            bounded_run.completed(["git", "-C", str(repo), "branch", "-D", step["onto"]], capture_output=True, text=True)
        # When a parent phase already squash-landed, `from` (main) and `branch` both carry the same new
        # content and a plain squash conflicts on it for no reason. Unless `from` is already an ancestor
        # of `branch`, merge `from` into a scratch copy of `branch` first, auto-resolving paths where
        # `from`'s content matches content already reachable from `branch`'s tip, and squash that merge
        # commit instead - `branch` itself is never moved.
        squash_source = step["branch"]
        appended: set[str] = set()
        ancestor = bounded_run.completed(["git", "-C", str(repo), "merge-base", "--is-ancestor", step["from"], step["branch"]],
                                  capture_output=True, text=True)
        if ancestor.returncode != 0:
            scratch_branch = _scratch_merge_branch(step["branch"])
            _remove_scratch_worktree(repo, scratch_branch)
            scratch_wt = _land_worktree(repo, scratch_branch)
            branch_tip = _git_out(repo, "rev-parse", step["branch"])
            sco = bounded_run.completed(["git", "-C", str(repo), "worktree", "add", "-b", scratch_branch, str(scratch_wt), step["branch"]],
                                 capture_output=True, text=True)
            if sco.returncode != 0:
                drop()
                return False, sco.stderr.strip() or sco.stdout.strip()
            smerge = bounded_run.completed(["git", "-C", str(scratch_wt), "-c", "merge.conflictStyle=diff3", "merge", "--no-edit", step["from"]],
                                    capture_output=True, text=True)
            if smerge.returncode != 0:
                conflicted = bounded_run.completed(["git", "-C", str(scratch_wt), "diff", "--name-only", "--diff-filter=U"],
                                            capture_output=True, text=True).stdout.split()
                unresolved = _resolve_landed_conflicts(repo, scratch_wt, step["from"], branch_tip, conflicted)
                if unresolved and _resolve_append_only(scratch_wt, unresolved):
                    appended |= set(unresolved)
                    unresolved = []
                if unresolved:
                    bounded_run.completed(["git", "-C", str(scratch_wt), "merge", "--abort"], capture_output=True, text=True)
                    _remove_scratch_worktree(repo, scratch_branch)
                    drop()
                    return False, f"squash of {step['branch']} conflicts in: {', '.join(unresolved)}"
                commit = bounded_run.completed(["git", "-C", str(scratch_wt), "commit", "-qm",
                                         f"merge {step['from']} into {step['branch']} (land scratch)"], capture_output=True, text=True)
                if commit.returncode != 0:
                    _remove_scratch_worktree(repo, scratch_branch)
                    drop()
                    return False, commit.stderr.strip() or commit.stdout.strip()
            squash_source = scratch_branch
        merge = bounded_run.completed(["git", "-C", str(wt), "-c", "merge.conflictStyle=diff3", "merge", "--squash", squash_source],
                               capture_output=True, text=True)
        if squash_source != step["branch"]:
            _remove_scratch_worktree(repo, squash_source)
        if merge.returncode != 0:
            conflicted = bounded_run.completed(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                                        capture_output=True, text=True).stdout.split()
            if _resolve_append_only(wt, conflicted):
                appended |= set(conflicted)
            else:
                drop()
                return False, f"squash of {step['branch']} conflicts in: {', '.join(conflicted)}"
        if not _git_out(wt, "diff", "--cached", "--name-only"):
            drop()
            return False, f"{step['branch']} adds nothing over {step['from']}"
        commit = bounded_run.completed(["git", "-C", str(wt), "commit", "-qm", step["subject"]], capture_output=True, text=True)
        if commit.returncode != 0:
            return False, commit.stderr.strip() or commit.stdout.strip()
        squashed_files = (_git_out(wt, "diff", "--name-only", step["from"], "HEAD") or "").splitlines()
        refusal = land.squash_missing_files(step.get("approved_files", []), squashed_files)
        if refusal is not None:
            # Leave the worktree, branch and squash commit local for inspection: nothing is
            # pushed and no task is marked done until the missing files show up in a rerun.
            return False, refusal
        ok, detail = _generate_and_amend(wt, step.get("umbrella"), print)
        if not ok and detail.startswith(_GENERATE_FAILED):
            # Keep the worktree, detached, for inspection; free the branch so a rerun starts fresh
            # instead of finding a pr branch that holds the un-generated tree and refusing on it.
            bounded_run.completed(["git", "-C", str(wt), "checkout", "-q", "--detach"], capture_output=True, text=True)
            bounded_run.completed(["git", "-C", str(repo), "branch", "-D", step["onto"]], capture_output=True, text=True)
        suffix = f"; resolved: append-only in {', '.join(sorted(appended))}" if appended else ""
        return (True, f"squashed {step['branch']} onto {step['onto']}{suffix}") if ok else (False, detail)
    if kind == "reuse_branch":
        wt = _land_worktree(repo, step["branch"])
        _remove_land_worktree(repo, step["branch"])
        args = ["worktree", "add", str(wt), step["branch"]] if step["local"] else ["worktree", "add", "-b", step["branch"], str(wt), f"origin/{step['branch']}"]
        co = bounded_run.completed(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if co.returncode == 0:
            _link_venv(repo, wt)
        return co.returncode == 0, (f"reusing {step['branch']}" if co.returncode == 0 else co.stderr.strip() or co.stdout.strip())
    if kind == "checks":
        cap = (step.get("cores", 1), step.get("capacity"))  # set by `_with_check_cap` on an applied land
        if step.get("gate") == "ticket":
            # The ticket gate shares the repo lock with level check runs; task checks below never take it.
            from harness import check_gate
            with check_gate.repo_gate_lock(repo):
                return _run_checks(step["checks"], _land_worktree(repo, step["worktree_of"]), *cap)
        if "skipped" in step:
            return True, f"checks: skipped ({step['skipped']})"
        if "kept" in step:
            print(step["kept"])
        if "branch" in step:
            # Phase mode: never check out the phase branch in the working
            # repo (that races anything else using it) — build it fresh in
            # its own worktree instead, and remove it whatever happens. The
            # directory is allocated here, at execution, not in the plan
            # (`land.py`/`_land_enrich`), so a dry run never touches disk.
            wt = Path(tempfile.mkdtemp(prefix="land-checks-"))
            add = bounded_run.completed(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), step["branch"]], capture_output=True, text=True)
            if add.returncode != 0:
                return False, add.stderr.strip() or add.stdout.strip()
            _link_venv(repo, wt)
            try:
                return _run_checks(step["checks"], wt, *cap)
            finally:
                bounded_run.completed(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        if "worktree_of" in step:
            return _run_checks(step["checks"], _land_worktree(repo, step["worktree_of"]), *cap)
        return _run_checks(step["checks"], repo, *cap)
    if kind == "push":
        return _land_push(repo, step["branch"], forge_module)
    if kind == "pr_branch_cut":
        return _cut_pr_branch(repo, step["branch"], step["from"])
    if kind == "merge_phase":
        return _merge_phase(repo, step["branch"], step["onto"], step["message"])
    if kind == "pr_create":
        draft = {"draft": True} if step.get("draft") else {}
        return forge_module.open_pr(repo, step["title"], step["body"], head=step.get("head"), base=step.get("base"), **draft)
    if kind == "pr_update":
        return forge_module.update_pr(repo, step["title"], step["body"], head=step["head"])
    if kind == "pr_ready":
        return forge_module.mark_ready(repo, head=step["head"])
    if kind == "mark_merged":
        return _mark_merged(repo, step)
    if kind == "phase_done":
        return _phase_done_record(step)
    if kind == "wait_checks":
        return forge_module.wait_checks(repo, float(step.get("timeout_s", 180)), ref=step.get("branch", "HEAD"))
    if kind == "batch_squash":
        return land_batch_squash.squash_onto_batch(repo, step)
    if kind == "merge":
        return forge_module.merge(repo, step)
    if kind == "clean":
        wt = Path(step["worktree_root"]).expanduser() / step["run"] / step["task"]
        if wt.exists():
            bounded_run.completed(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        bounded_run.completed(["git", "-C", str(repo), "branch", "-D", step["branch"]], capture_output=True)
        return True, f"deleted branch {step['branch']}"
    if kind == "clean_phase":
        doomed = [b for b in cleanup.git_branches(repo) if b == step["phase_branch"] or b.startswith(step["phase_branch"] + "--")]
        doomed += [f"agents/{step['run']}/{t}" for t in step["tasks"]]
        if step.get("pr_branch"):
            # git refuses `branch -D` on a branch a worktree holds, and squash_phase's worktree sits under the
            # temp dir, not `worktree_root`, so drop it here rather than trust an earlier step to have done so.
            _remove_land_worktree(repo, step["pr_branch"])
            doomed.append(step["pr_branch"])
        for t in step["tasks"]:
            wt = Path(step["worktree_root"]).expanduser() / step["run"] / t
            if wt.exists():
                bounded_run.completed(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        for b in doomed:
            bounded_run.completed(["git", "-C", str(repo), "branch", "-D", b], capture_output=True)
        # The phase branch is named too, so one that lives only on origin is still deleted there.
        parts = step["phase_branch"].split("/")
        prefix = "/".join(parts[:2]) + "/" if parts[0] == "epic" and len(parts) >= 3 else ""
        gone, failed = _delete_remote_branches(repo, _remote_doomed([step["phase_branch"], *doomed], prefix))
        # A failed remote delete is reported, never a failed land: the merge has already happened.
        remote = (f"; deleted on origin: {', '.join(gone)}" if gone else "") \
            + (f"; remote delete failed: {'; '.join(failed)}" if failed else "")
        return True, "deleted " + ", ".join(doomed) + remote
    if kind == "mark_done" and step.get("recorded") is False:
        return _mark_done_unrecorded(step)
    if kind == "mark_done":
        record_path = Path(step["path"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record_path.write_text(json.dumps({**record, "landed": True}, indent=2), encoding="utf-8")
        line = _mirror_landed(step)
        if line:
            print(line)
        stop = _close_approved_item(step.get("item"), runs_dir=record_path.resolve().parents[3], by=step.get("by", _UNLABELED),
                                    merged=True, mode=step.get("mode", "files"))
        if stop is not None:
            return False, stop
        return True, f"{step['task']} marked landed at {record_path}"
    if kind == "route_sync":
        # A failed sync is reported, never a failed land: after the merge nothing is undone,
        # before the PR it opens without `Closes`.
        when = "before the PR" if step.get("before") else "after the merge"
        tail = "the PR opens without Closes" if step.get("before") else "land not undone"
        sync = argparse.Namespace(item=step["item"], workspace=step["workspace"], dry_run=False, project=None,
                                  profile=step.get("profile"), runs_dir=step.get("runs_dir"))
        if _sync_skipped(sync):
            return True, f"sync of {step['item']} skipped {when}: tracker is none"
        try:
            rc = _route_sync(sync)
        except Exception as exc:
            return True, f"sync of {step['item']} failed {when} ({type(exc).__name__}: {exc}); {tail}"
        return True, f"synced {step['item']}" if rc == 0 else f"sync of {step['item']} failed {when} (exit {rc}); {tail}"
    return False, f"unknown step {kind!r}"


def _git_run(repo: Path | str, *args: str) -> tuple[bool, str]:
    done = bounded_run.completed(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.returncode == 0, (done.stderr.strip() or done.stdout.strip())


def _remote_doomed(branches: list[str], prefix: str) -> list[str]:
    """Names under `prefix` (`epic/<initiative>/`), first occurrence kept, in order. `agents/` scratch branches were never pushed."""
    return [b for b in dict.fromkeys(branches) if prefix and b.startswith(prefix)]


def _delete_remote_branches(repo: Path, names: list[str]) -> tuple[list[str], list[str]]:
    """Edge. Delete `names` on origin; returns (deleted, failed as 'name: reason'). No origin, or a branch already gone,
    is neither."""
    if not names or not _git_run(repo, "remote", "get-url", "origin")[0]:
        return [], []
    results = [(n, *_git_run(repo, "push", "origin", f":refs/heads/{n}")) for n in names]
    gone = [n for n, ok, _ in results if ok]
    return gone, [f"{n}: {why}" for n, ok, why in results if not ok and "remote ref does not exist" not in why]


def _cut_pr_branch(repo: Path, branch: str, base: str) -> tuple[bool, str]:
    """Edge. Cut `branch` from `base` in its land worktree when no local branch of that name exists. An existing branch
    is never reset or moved: a later phase's tip is the work already merged into it."""
    if _git_run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")[0]:
        return True, f"reusing {branch}"
    wt = _land_worktree(repo, branch)
    _remove_land_worktree(repo, branch)
    ok, detail = _git_run(repo, "worktree", "add", "-b", branch, str(wt), base)
    if ok:
        _link_venv(repo, wt)
    return ok, f"cut {branch} from {base}" if ok else detail


def _merge_phase(repo: Path, phase_branch: str, onto: str, message: str) -> tuple[bool, str]:
    """Edge. Merge `phase_branch` into `onto` with a merge commit, in `onto`'s land worktree, which stays for the checks
    that follow. A conflict is aborted and the worktree dropped, so `onto` keeps its tip and no half merge is left."""
    wt = _land_worktree(repo, onto)
    _remove_land_worktree(repo, onto)
    ok, detail = _git_run(repo, "worktree", "add", str(wt), onto)
    if not ok:
        return False, detail
    _link_venv(repo, wt)
    ok, detail = _git_run(wt, "-c", "merge.conflictStyle=diff3", "merge", "--no-ff", "-m", message, phase_branch)
    if ok:
        return True, f"merged {phase_branch} into {onto}"
    _git_run(wt, "merge", "--abort")
    _remove_land_worktree(repo, onto)
    return False, detail


def _mark_merged(repo: Path, step: dict) -> tuple[bool, str]:
    """Edge. The task's work is on the pr branch: `merged` and the branch tip are set on the task record, the work item
    and the store row, and the state stays `approved`. The store takes it first under `store` mode, as `mark_done` does."""
    task, mode = step["task"], step.get("mode", "files")
    ok, tip = _git_run(repo, "rev-parse", "--verify", step["pr_branch"])
    if not ok:
        return False, f"{task}: no pr branch {step['pr_branch']}: {tip}"
    item = step.get("item")
    if item and step.get("runs_dir"):
        result = store_cli.mark_merged(Path(step["runs_dir"]), Path(item).absolute().parents[1].name, task, tip, step.get("by", _UNLABELED))
        problem = None if isinstance(result, store_cli.MergeRecorded) else f"store did not record {task} as merged: {result}"
        if problem and mode == "store":
            return False, problem
        if problem:
            print(f"warning: {problem}")
    path = step.get("path")
    if path and Path(path).exists():
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        Path(path).write_text(json.dumps({**record, "merged": True, "merged_commit": tip}, indent=2), encoding="utf-8")
    if item and Path(item).exists():
        from core import workstore
        found = workstore.read_item(item)
        if found["state"] == "approved":
            workstore.write_item({**found, "merged": True, "merged_commit": tip}, item)
    return True, f"{task} marked merged at {tip[:7]}, state approved"


def _phase_done_record(step: dict) -> tuple[bool, str]:
    """Edge. The phase record is written done and merged into the pr branch, never landed: the manifest file
    `<runs_dir>/<run_id>.json` when one exists, and the store's phase row. A phase recorded in neither fails the step."""
    run_id, branch = step.get("phase_record_id"), step["pr_branch"]
    if not run_id or not step.get("runs_dir"):
        return False, f"phase {step['phase']} not recorded as done: no phase record id or runs dir"
    runs_dir, wrote = Path(step["runs_dir"]), []
    path = runs_dir / f"{run_id}.json"
    if path.exists():
        record = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "landed"}
        path.write_text(json.dumps({**record, "status": "done", "merged_into": branch}, indent=2), encoding="utf-8")
        wrote.append("file")
    run, _, phase = run_id.partition(":")
    stored = store_cli.mark_phase_done(runs_dir, run, phase, branch)
    if isinstance(stored, store_cli.MergeRecorded):
        wrote.append("store")
    elif not wrote:
        return False, f"phase {run_id} not recorded as done: no manifest file and the store gave {stored}"
    return True, f"phase {phase} done, merged into {branch} ({' and '.join(wrote)})"


def _mark_done_unrecorded(step: dict) -> tuple[bool, str]:
    """Edge. A `mark_done` with no task record here: the store row moves approved to done carrying the land's `pr`, with
    no record file and no landed mirror. The failure line is the route edit's own."""
    item, task = step.get("item"), step["task"]
    if not item or not step.get("runs_dir"):
        return False, f"{task} has no work item or runs dir to mark done"
    runs_dir, initiative = Path(step["runs_dir"]), Path(item).absolute().parents[1].name
    row = next((r for r in run_store.read_queue(runs_dir, initiative, kind="task") if r.get("task_id") == task), None)
    if row is None:
        return False, f"no store row for {task}"
    result = route_edit.mark_done_with_pr(runs_dir, row, step.get("pr", ""), step.get("at"))
    if result is None:
        return True, f"{task} marked done with {step.get('pr') or 'no PR'}"
    return result.code == route_edit.DONE, result.text


def _mark_remaining_done(steps: list[dict], pr: str, at: str, mark: Callable[[dict], tuple[bool, str]]) -> list[tuple[str, str]]:
    """Edge. Mark done with `pr` every task of the `mark_done` steps in `steps`; returns `(task, reason)` for each it could
    not. A task whose record file is missing, or that never had one, goes by its store item as `_mark_done_unrecorded` does.
    A record `mark` fails on (False or an exception) falls back to the store item. Never raises."""
    unmarked: list[tuple[str, str]] = []
    for step in {s["task"]: s for s in steps if s["kind"] == "mark_done"}.values():
        path = step.get("path")
        has_record = step.get("recorded") is not False and path is not None and Path(path).is_file()
        reason = ""
        if has_record:
            try:
                ok, detail = mark(_mark_done_facts(step, pr, at))
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            if ok:
                continue
            reason = detail
        try:
            runs_dir = step.get("runs_dir") or (str(Path(path).resolve().parents[3]) if path else None)
            ok, detail = _mark_done_unrecorded({**step, "runs_dir": runs_dir, "pr": pr, "at": at})
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            unmarked.append((step["task"], f"{reason}; then {detail}" if reason else detail))
    return unmarked


def _mark_done_facts(step: dict, pr: str, at: str) -> dict:
    """`step` with the store facts `mark_done` mirrors: run and phase from the
    `runs/<run>/tasks/<phase>/<task>.json` path, the PR url, and the caller's ISO time."""
    path = Path(step["path"])
    return {**step, "run": path.parents[2].name, "phase": path.parent.name, "pr": pr, "at": at}


def _mirror_landed(step: dict) -> str | None:
    """The line to print for the store mirror of a landed record, None when it took.
    A step with no `at` was not given the store facts and is not mirrored. Never raises."""
    if "at" not in step:
        return None
    try:
        # The record path is <runs_dir>/<run>/tasks/<phase>/<task>.json.
        result = store_cli.mark_landed(Path(step["path"]).resolve().parents[3], step["run"], step["phase"], step["task"], step["pr"], step["at"])
    except Exception as exc:
        return f"warning: store mirror of {step['task']} failed ({type(exc).__name__}: {exc}); land not undone"
    if isinstance(result, store_cli.Landed):
        return None
    if isinstance(result, store_cli.NotInStore):
        return f"store mirror of {step['task']} skipped: the record predates the mirror"
    if isinstance(result, store_cli.Failed):
        return f"warning: store mirror of {step['task']} failed (exit {result.code}: {result.detail}); land not undone"
    return f"warning: store mirror of {step['task']} skipped: harness not available; land not undone"


def _land_item_facts(item_path: str | None) -> tuple[str | None, str | None]:
    """`(id, issue)` from the work item's frontmatter, each `None` when the
    item is absent or does not carry it. `issue` is whatever the sync wrote
    back, normally a bare number."""
    text = _read_text_or_none(Path(item_path)) if item_path else None
    fields = route.parse_frontmatter(text)[0] if text is not None else {}
    item_id, issue = fields.get("id"), fields.get("issue")
    return (str(item_id) if item_id else None), (str(issue) if issue else None)


def _land_reference(profile: dict, workspace: str, record: dict, item_id: str | None,
                    issue: str | None, repo: Path | None = None,
                    sync: argparse.Namespace | None = None) -> tuple[str, str | None]:
    """Edge. `(scope, reference)` for a task land: the resolved `tracker_scope` and the PR's issue line under it.
    `all` has no reference; the store is read only for a narrowed scope, and unreadable means no initiative issue.
    A one-task fix with a target release says `Part of` its `<version> fixes` issue; `sync`, when given, creates
    that issue first if it is not on file yet."""
    scope, threshold, error = _tracker_scope(profile, repo)
    if error:
        print(f"land: {error}; landing under tracker_scope {scope}", file=sys.stderr)
        return scope, land.pr_reference(issue, None, False)
    if scope == "all":
        return scope, None
    initiative = record.get("initiative")
    try:
        items = route_sync_gh.items_from_store(workspace)
    except (OSError, ValueError) as exc:
        print(f"land: work store unreadable ({type(exc).__name__}: {exc}); the PR carries no issue reference",
              file=sys.stderr)
        items = []
    rollup = route_sync.rollup_for(items, scope, threshold, _initiative_item_id(initiative) or "")
    if rollup is not None:
        number = route_sync_gh.rollup_issue_number(bounded_run.completed, rollup)
        if number is None and sync is not None:
            _route_sync(argparse.Namespace(**{**vars(sync), "item": item_id or record.get("task", "")}))
            number = route_sync_gh.rollup_issue_number(bounded_run.completed, rollup)
        return scope, land.pr_reference(issue, number, False)
    parent_issue, last = route_sync.initiative_facts(items, scope, threshold,
                                                     _initiative_item_id(initiative), item_id or record.get("task", ""))
    return scope, land.pr_reference(issue, parent_issue, last)


def _initiative_item_id(initiative: str | None) -> str | None:
    return f"initiative:{initiative}" if initiative else None


def _pr_url(detail: str) -> str | None:
    """The `pr_create` detail as the PR, only when it is a URL; the local forge opens none."""
    url = detail.strip()
    return url if url.startswith(("http://", "https://")) else None


def _landed_pr(kind: str, ok: bool, detail: str, prior: str, step_pr: object = None) -> str:
    """The land's `pr` after a step: `""`, never None, is "no PR", because the store's `--pr` argv takes a str.
    `pr_update` and `pr_ready` act on a PR a phase-two land never opened: they carry its url when the forge answers one,
    else the number the plan pinned (`#<n>`), else the PR already carried."""
    if not ok:
        return prior
    if kind == "pr_create":
        return _pr_url(detail) or ""
    if kind in ("pr_update", "pr_ready"):
        return _pr_url(detail) or prior or (f"#{step_pr}" if step_pr is not None else "")
    return prior


def _open_pr_ref(repo: Path, steps: Sequence[dict], forge_module) -> str:
    """Edge. `#<n>` of the open PR for the pr branch, for a plan that starts after the PR was opened and so has no
    `pr_create`; `""` when the plan opens one, names no pr branch, or the forge finds none."""
    head = next((s["pr_branch"] for s in steps if s["kind"] == "phase_done"), None)
    if head is None or any(s["kind"] == "pr_create" for s in steps) or not hasattr(forge_module, "find_open_prs"):
        return ""
    try:
        found = forge_module.find_open_prs(repo, head)
    except Exception:
        return ""
    return f"#{found[0]}" if isinstance(found, list) and found else ""


# A conflict, a missing review or a draft can never merge; UNKNOWN is GitHub still computing, so it is re-read, and
# merges if it stays UNKNOWN (gh's own merge then refuses what cannot merge). UNSTABLE (only optional checks red) and
# HAS_HOOKS merge, as every land did before this gate (2026-10-06: #1426 and #1430 stopped on UNKNOWN with green checks).
_MERGE_STOPS = frozenset({"DIRTY", "BLOCKED", "DRAFT"})
_UNKNOWN_READS = 6
_UNKNOWN_WAIT_S = 5.0


_LOCAL_MERGE_SHA = re.compile(r"^merged \S+ into \S+ at ([0-9a-f]{7,40})$")


def _landing_ref(detail: str, pr: str, number: int | None, default_branch: str) -> str:
    """What a review PR's close comment names as the landing, never "": the sha the local forge's merge detail reports
    (its exact `merged <branch> into <default> at <sha>` first line), else the merged PR's url, else the PR number the
    land pinned (an adopted or resumed land opens no PR of its own), else the merge into `default_branch`.
    `gh pr merge` reports no sha, so a github land is named by its PR."""
    first = detail.splitlines()[0] if detail else ""
    found = _LOCAL_MERGE_SHA.match(first)
    if found:
        return found.group(1)
    if pr:
        return pr
    return f"PR #{number}" if number is not None else f"the merge into {default_branch}"


def review_close_targets(initiative: str, task: str, prs: Sequence[Mapping]) -> tuple[list[tuple[int, str]], list[str]]:
    """`(number, head)` of each of `prs` whose head is exactly the task's review branch, and one line per matching
    row too malformed to close (no integer `number`); pure, never raises on a forge row."""
    rows = [p for p in prs if isinstance(p, Mapping)]
    targets, bad = [], []
    for p in review_pr_close.review_prs_for_task(initiative, task, rows):
        try:
            targets.append((int(p["number"]), str(p["head"])))
        except (KeyError, TypeError, ValueError):
            bad.append(f"warning: review PR row {dict(p)!r} of {task} unreadable; not closed")
    return targets, bad


def _close_review_prs(repo: Path, initiative: str, task: str, ref: str, forge_module) -> list[str]:
    """Close the landed task's open review PRs and delete their branches; the lines to print. Best effort: a forge
    failure or a malformed forge row becomes a line, never an exception, and never stops the next PR. A PR the forge
    reports already closed counts as closed, so a rerun is quiet."""
    prefix = review_pr_close.review_branch(initiative, task)
    try:
        listed = forge.list_open_prs_by_prefix(forge_module, repo, prefix)
    except Exception as exc:
        listed = f"{type(exc).__name__}: {exc}"
    if isinstance(listed, str):
        return [f"warning: review PRs of {task} not closed: {listed}; land not undone"]
    try:
        targets, lines = review_close_targets(initiative, task, listed)
    except Exception as exc:
        return [f"warning: review PRs of {task} not closed ({type(exc).__name__}: {exc}); land not undone"]
    for number, head in targets:
        try:
            ok, detail = forge.close_pr(forge_module, repo, number, review_pr_close.close_comment(ref, task))
            if not ok:
                lines.append(f"warning: review PR #{number} of {task} not closed: {detail}; land not undone")
                continue
            ok, detail = forge.delete_branch(forge_module, repo, head)
            lines.append(f"closed review PR #{number} and deleted {head}" if ok else
                         f"warning: closed review PR #{number}, branch {head} not deleted: {detail}")
        except Exception as exc:
            lines.append(f"warning: review PR #{number} of {task} failed ({type(exc).__name__}: {exc}); land not undone")
    return lines


def _step_initiative(step: dict, record: dict | None) -> str | None:
    """The initiative a mark_done step's task belongs to: its record file's, else the land's own record's.
    A step with no record file has its work item's directory."""
    if step.get("recorded") is False and step.get("item"):
        return Path(step["item"]).absolute().parents[1].name
    try:
        found = json.loads(Path(step["path"]).read_text(encoding="utf-8")).get("initiative")
    except (KeyError, TypeError, OSError, ValueError, AttributeError):
        found = None
    return found or (record or {}).get("initiative")


def merge_gate(state: str) -> tuple[str, str]:
    """`(decision, state)` from a PR's mergeStateStatus: BEHIND is updated first, DIRTY, BLOCKED and DRAFT stop, and
    anything else merges."""
    if state == "BEHIND":
        return "update_then_merge", ""
    if state in _MERGE_STOPS:
        return "stop", state
    return "merge", ""


def _settled_merge_state(read: Callable[[], str], sleep: Callable[[float], None] | None = None) -> str:
    """The first state other than UNKNOWN in up to `_UNKNOWN_READS` reads, `_UNKNOWN_WAIT_S` apart; UNKNOWN if it never settles."""
    state = read()
    for _ in range(_UNKNOWN_READS - 1):
        if state != "UNKNOWN":
            return state
        (sleep or time.sleep)(_UNKNOWN_WAIT_S)
        state = read()
    return state


def _pr_number(steps: Sequence[dict], i: int, pr_url: str) -> int | None:
    """The PR a `merge` at `steps[i]` lands: the `pr` of the last earlier `wait_checks`, `pr_update` or `pr_ready` that has one, else the number ending the carried PR url."""
    pinned = [s["pr"] for s in steps[:i] if s["kind"] in ("wait_checks", "pr_update", "pr_ready") and s.get("pr") is not None]
    found = re.search(r"(\d+)/?$", str(pinned[-1] if pinned else pr_url))
    return int(found.group(1)) if found else None


def _fetch_branch(repo: Path, branch: str) -> str | None:
    """Edge. Fetch `origin/<branch>`; the failure text, or None when it landed."""
    r = bounded_run.completed(["git", "-C", str(repo), "fetch", "origin", branch], capture_output=True, text=True)
    return None if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip() or f"git fetch origin {branch} failed")


def _merge_updating(repo: Path, steps: Sequence[dict], i: int, pr_url: str, forge_module,
                    guard: Callable[[str], str | None] | None = None,
                    attempts: int | None = None,
                    lock: Callable[[], contextlib.AbstractContextManager] | None = None) -> tuple[bool, str, str | None]:
    """Edge. Run the `merge` at `steps[i]`, updating the PR branch first when it is BEHIND and again after each merge refused as behind.

    Each update is one pass: the repo lease is renewed through `guard`, the branch updated, its checks awaited again, the merge tried.
    At most `attempts` passes run, `land.MERGE_UPDATE_ATTEMPTS` when None (read at call time: `land` loads lazily). Returns `(ok, detail, halt)`: `halt` is the reason to stop at the merge, else the merge's own result.
    No PR number, or a forge with no PRs, merges as before.
    `lock`, when given, makes a `land_merge_lock.hold` for the repo. It is held for the first state read and each merge try only,
    and released before every update and wait, which take minutes. A lock not taken in time stops the land naming its holder."""
    step = steps[i]
    attempts = land.MERGE_UPDATE_ATTEMPTS if attempts is None else attempts
    number = _pr_number(steps, i, pr_url)
    # Imported here: `agent_tools.cli` must not import a command module before one is dispatched.
    from agent_tools.forge import ForgeError, ForgeNotSupported

    wait = next((s for s in reversed(steps[:i]) if s["kind"] == "wait_checks"), {"kind": "wait_checks"})
    behind = False
    reading = number is not None  # the state is read once, on the first lock; a later merge refused as behind says the base moved
    refusal = ""
    used = 0
    while True:
        if behind:
            if used >= attempts:
                return False, "", land.behind_stop(attempts, refusal)
            used += 1
            lost = guard("update_branch") if guard is not None else None
            if lost is not None:
                return False, "", lost
            try:
                forge_module.update_branch(number, repo=repo)
            except ForgeError as exc:
                if land.is_conflict_error(str(exc)):
                    return False, "", land.conflict_stop(number, str(exc))
                return False, "", f"merge: cannot update pull request #{number}: {exc}"
            print(f"land: pull request #{number} was BEHIND; updated its branch (update {used} of {attempts}), waiting for checks again")
            failed = _fetch_branch(repo, step["branch"])
            if failed is not None:
                return False, "", f"merge: cannot fetch the updated {step['branch']}: {failed}"
            ok, detail = _execute_land_step(repo, {**wait, "branch": f"origin/{step['branch']}"}, forge_module)
            if not ok:
                return False, "", f"wait_checks: {detail}"
            behind = False
        with (lock() if lock is not None else contextlib.nullcontext()) as held:
            if isinstance(held, land_merge_lock.NotHeld):
                return False, "", land.lock_stop(held.holder, held.reason)
            if reading:
                reading = False
                try:
                    decision, state = merge_gate(_settled_merge_state(lambda: forge_module.merge_state(number, repo=repo)))
                except ForgeNotSupported:
                    return (*_execute_land_step(repo, step, forge_module), None)
                except ForgeError as exc:
                    return False, "", f"merge: cannot read the state of pull request #{number}: {exc}"
                if decision == "stop":
                    return False, "", f"merge: pull request #{number} is {state}; not merging"
                if decision == "update_then_merge":
                    behind = True
                    continue  # leaves the `with`, releasing the lock before the update
            ok, refusal = _execute_land_step(repo, step, forge_module)
            if ok or not land.is_behind_refusal(refusal) or number is None:
                return ok, refusal, None
            behind = True


def _close_approved_item(item_path: str | None, *, runs_dir: Path, by: str, merged: bool = False, mode: str = "files") -> str | None:
    """Moves the item to `done` and mirrors any item left `done`, rerun or not, to the store; `merged` only after a merge step succeeded.

    Under `store` the store moves first, as `set-state done --expect approved`, whatever the local file says or even
    when it is missing; a refusal comes back as the reason, with the item untouched. Else None."""
    if item_path is None:
        return None
    item = Path(item_path)
    if mode == "store":
        stop = land.set_state_stop(store_cli.set_state(runs_dir, item.absolute().parents[1].name, item.stem, "done", by, expected="approved"))
        if stop is not None:
            return stop
    if not item.exists():
        return None
    text = item.read_text(encoding="utf-8")
    new_text, message = land.approve_to_done(text, merged=merged)
    if new_text is not None:
        item.write_text(new_text, encoding="utf-8")
        queue_export.record_written_paths(runs_dir.parent, [item.absolute()])
    if message:
        print(message)
    if mode == "store" or (new_text is None and message is not None):
        return None
    task = str(route.parse_frontmatter(new_text or text)[0].get("id") or item.stem)
    line = store_cli.mirror_state(runs_dir, item.absolute().parents[1].name, task, "done", by)
    if line is not None:
        print(line)
    return None


_await_checks = _LazyHandler("agent_tools.land:await_checks")
_wait_checks = _LazyHandler("agent_tools.forge_github:wait_checks")


def _repo_is_dirty(repo: Path) -> bool:
    status = bounded_run.completed(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return bool(status.stdout.strip())


def sync_decision(has_origin: bool, behind: int, ahead: int) -> str:
    """Pure. What to do with the local default branch: skip, current, fast_forward or refuse."""
    if not has_origin:
        return "skip"
    if behind > 0 and ahead > 0:
        return "refuse"
    if behind > 0:
        return "fast_forward"
    return "current"


def _sync_default_branch(repo: Path, default_branch: str) -> tuple[str, str]:
    """Edge. Fast-forward the local default branch to origin's; HEAD and the tree stay put."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return bounded_run.completed(["git", "-C", str(repo), *args], capture_output=True, text=True)

    remotes = git("remote").stdout.split()
    if "origin" not in remotes:
        return sync_decision(False, 0, 0), ""
    fetched = git("fetch", "origin", default_branch)
    if fetched.returncode != 0:
        return "refuse", fetched.stderr.strip()
    remote_ref = f"origin/{default_branch}"
    counted = git("rev-list", "--left-right", "--count", f"{default_branch}...{remote_ref}")
    if counted.returncode != 0:
        return "refuse", counted.stderr.strip()
    ahead, behind = (int(n) for n in counted.stdout.split())
    decision = sync_decision(True, behind, ahead)
    if decision == "refuse":
        return decision, f"{default_branch} has diverged from {remote_ref} ({ahead} ahead, {behind} behind); reconcile it first"
    if decision != "fast_forward":
        return decision, ""
    if git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() == default_branch:
        moved = git("merge", "--ff-only", remote_ref)
    else:
        old = git("rev-parse", f"refs/heads/{default_branch}").stdout.strip()
        moved = git("update-ref", f"refs/heads/{default_branch}", remote_ref, old)
    if moved.returncode != 0:
        return "refuse", (moved.stderr or moved.stdout).strip()
    return decision, f"{default_branch} fast-forwarded to {remote_ref} ({behind} commits)"


_RUN_FILE_SUFFIXES = re.compile(
    r"\.(?:log|pid|json|jsonl|result\.json|remote\.json|launched\.json|fetched\.json|usage\.json|progress\.json"
    r"|ceiling\.json|calls\.jsonl|jsonl\.zst|parquet|provider-profile\.yaml)$"
)


def _run_id_of(name: str) -> str:
    """`<id>.log`, `<id>.remote.json`, `<id>:<task>.json` and `<id>-trace` all belong to `<id>`.

    Wrong belief: "the id ends at the first dot". A phase run id is `<initiative>.<phase>-<N>`, so only a known
    run-file suffix is cut, from the right; a name with none keeps its dots up to the first colon."""
    return _RUN_FILE_SUFFIXES.sub("", name).split(":", 1)[0].removesuffix("-trace")


def _taken_run_names(runs_dir: Path) -> list[str]:
    """Edge. Names in `runs_dir`, the bare run id each belongs to, and every run id in the store.

    A remote lane leaves only `<id>.remote.json`, which `next_run_id` does not read as an id; the bare ids let
    both it and an exact `--run-id` check see that run as taken."""
    names = [p.name for p in runs_dir.iterdir()] if runs_dir.is_dir() else []
    return [*names, *sorted({_run_id_of(n) for n in names}), *sorted(run_store.run_ids(runs_dir))]
