"""The build scratch a Claude Code node edits: making and dropping it, the environment a node runs in, and the diff.

The diff git computes in the scratch is the patch; when the scratch is clean, the model's own reported patch is
applied there and recomputed, never trusted as text. `verify:` commands run in the scratch, and it is put back after.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from runner.claude_token import TOKEN_ENV, read_token, token_path
from runner.protocol import RunnerError

_VERIFY_TIMEOUT_S = 120
_VERIFY_TOTAL_S = 300
_VERIFY_TAIL_LINES = 40
_DIFF_CMD = "git add -A && git diff --cached --binary --full-index"


def child_env(environ: Mapping[str, str], bash: str | None, token: str | None = None) -> dict[str, str]:
    """Claude Code's Bash tool runs in $SHELL, and zsh fails an unquoted glob such as `--include=*.py`.

    A given `token` replaces whatever `environ` holds under TOKEN_ENV.
    """
    with_shell = {**environ, "SHELL": bash} if bash is not None else dict(environ)
    return {**with_shell, TOKEN_ENV: token} if token is not None else with_shell


def _capture_diff(scratch: Path) -> str:
    """The scratch's half-written change, or "" — never a raise.

    A `BudgetStop` on a threaded role leaves the scratch behind for the next
    phase to resume into; this is what lets the stopped node hand over what it
    had already built rather than just an apology.
    """
    try:
        proc = subprocess.run(_DIFF_CMD, shell=True, capture_output=True, text=True, cwd=scratch)
    except OSError:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def reconcile_patch(reported: str, computed: str) -> tuple[str, str | None]:
    """The scratch's own diff over the model's account of it, or a named failure.

    A reported patch beside an empty scratch means the edits landed somewhere else.
    """
    if computed:
        return computed, None
    return ("", "patch_outside_scratch") if reported.strip() else ("", "patch_empty")


def redirect_paths(text: str, repo_dir: Path, scratch: Path) -> str:
    """`text` with every path under `repo_dir` moved to the same path under `scratch`."""
    pattern = r"(?<![\w.\-])" + re.escape(str(repo_dir)) + r"(?![\w\-]|\.\w)"
    return re.sub(pattern, lambda _: str(scratch), text)


def reported_patch(stdout: str) -> str:
    """The `patch` a finished, non-error result reports, or "" — read without side effects."""
    for line in (*reversed(stdout.splitlines()), stdout):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and ("structured_output" in event or event.get("type") == "result"):
            out = event.get("structured_output")
            if event.get("is_error") or not isinstance(out, dict):
                return ""
            return str(out.get("patch") or "")
    return ""


def apply_reported_patch(scratch: Path, patch: str) -> bool:
    """Apply `patch` in `scratch` if `git apply --check` accepts it whole; never raises."""
    text = patch if patch.endswith("\n") else patch + "\n"
    try:
        for args in (["--check"], []):
            proc = subprocess.run(["git", "apply", *args], input=text, capture_output=True, text=True, cwd=scratch)
            if proc.returncode != 0:
                return False
    except OSError:
        return False
    return True


def _run_verify(scratch: Path, commands: Sequence[str]) -> list[dict[str, str]]:
    """Run each `verify:` command in the scratch: one row per command, never a raise."""
    rows: list[dict[str, str]] = []
    deadline = time.monotonic() + _VERIFY_TOTAL_S
    for cmd in commands:
        timeout = min(_VERIFY_TIMEOUT_S, deadline - time.monotonic())
        if timeout <= 0:
            output = f"skipped: the {_VERIFY_TOTAL_S}s verify budget is spent"
            rows.append({"command": cmd, "output": output, "source": "harness_verify"})
            continue
        try:
            proc = subprocess.run(cmd, shell=True, cwd=scratch, capture_output=True, text=True, timeout=timeout)
            tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-_VERIFY_TAIL_LINES:])
            output = f"{tail}\n(exit {proc.returncode})"
        except subprocess.TimeoutExpired:
            output = f"timed out after {timeout:g}s"
        except OSError as exc:
            output = f"could not start: {exc}"
        rows.append({"command": cmd, "output": output, "source": "harness_verify"})
    return rows


def _snapshot(scratch: Path) -> str | None:
    """The scratch's index as a tree id, or None: what `_capture_diff` just staged, before a verify command can touch it."""
    try:
        proc = subprocess.run("git write-tree", shell=True, capture_output=True, text=True, cwd=scratch)
    except OSError:
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _restore(scratch: Path, tree: str | None) -> bool:
    """Put the scratch back to `tree`: index, tracked files and untracked files. False when it could not."""
    if not tree:
        return False
    cmd = f"git read-tree {tree} && git checkout-index -a -f && git clean -fdq"
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=scratch).returncode == 0
    except OSError:
        return False


def recover_diff(scratch: Path, computed: str, stdout: str) -> tuple[str, str]:
    """The scratch diff and its source: a clean scratch falls back to an applicable reported patch."""
    if computed:
        return computed, "computed"
    reported = reported_patch(stdout)
    if reported.strip() and apply_reported_patch(scratch, reported):
        recomputed = _capture_diff(scratch)
        if recomputed:
            return recomputed, "reported"
    return computed, "computed"


def _unprefixed(token: str) -> str:
    """Strip the one-letter path prefix git puts on a `+++`/`---` token — `a/`, `b/`, or (under `diff.mnemonicPrefix`) `c/`, `i/`, `w/`, `o/`."""
    _, sep, rest = token.partition("/")
    return rest if sep else token


def files_touched_from_patch(patch: str) -> list[str]:
    """The paths a patch changes, read from its own '+++'/'---' headers — never asked.

    `+++ b/<path>` names every add or modify; a deletion has `+++ /dev/null` and
    is named by `--- a/<path>` instead. Pure: no git invoked, no filesystem touched.
    """
    seen: list[str] = []
    minus_token = ""
    for line in patch.splitlines():
        if line.startswith("--- "):
            minus_token = line[4:]
        elif line.startswith("+++ "):
            plus_token = line[4:]
            path = _unprefixed(minus_token if plus_token == "/dev/null" else plus_token)
            if path and path not in seen:
                seen.append(path)
    return seen


def agent_env(scratch: Path | None) -> dict[str, str]:
    """Edge. `child_env`, under a scratch's linked shared venv with uv never syncing it (`shared_venv_env`)."""
    from harness.worktree import linked_venv_bin, shared_venv_env  # here: harness imports this module

    env = child_env(os.environ, shutil.which("bash"), read_token(token_path(str(Path.home()))))
    return shared_venv_env(env, linked_venv_bin(scratch) if scratch is not None else None, os.path.realpath)


def make_scratch(repo_dir: Path, role: str) -> tuple[Path, Path]:
    """Edge. A detached worktree of `repo_dir` at HEAD for `role`, its venv linked: (the temp parent, the tree)."""
    parent = Path(tempfile.mkdtemp(prefix="agent-graphs-build-"))
    scratch = parent / "tree"
    from harness.worktree import link_venv, registry_lock  # here, not at the top: harness imports this module

    with registry_lock(repo_dir):
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(scratch), "HEAD"],
            capture_output=True, text=True,
        )
    if proc.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        raise RunnerError(f"could not create a scratch worktree for '{role}': {(proc.stderr or '').strip()[:300]}")

    link_venv(Path(repo_dir), scratch)
    return parent, scratch


def drop_scratch(repo_dir: Path, parent: Path, scratch: Path) -> None:
    """Edge. Remove the scratch worktree from `repo_dir`'s registry and delete its temp parent."""
    from harness.worktree import registry_lock  # here, not at the top: harness imports this module

    with registry_lock(repo_dir):
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(scratch)],
            capture_output=True, text=True,
        )
    shutil.rmtree(parent, ignore_errors=True)
