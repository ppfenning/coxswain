"""A runner that returns canned answers, so a whole graph is testable offline.

This is not a mock in the apologetic sense — it is the reason the graphs can be
tested at all. Every node in this system is a model call, and a test suite that
needs a network and a key to run is a test suite nobody runs in CI.

It is also deliberately strict. A scripted runner that invented a plausible
answer for a node the test forgot to script would let a graph change shape
without any test noticing.

Answers that carry `TREE: true` build in a tree, as the Claude Code runner
does: the runner gets a `repo_dir` (the harness points it, or a view of it, at
the tree a build reads), and a scripted build's patch is applied in a scratch
worktree cut at that tree's HEAD. What the scratch then shows is the patch; a
patch that does not apply there fails the node as `patch_outside_scratch`
(`claude_code_runner.reconcile_patch`). So a harness that hands a builder the
wrong tree fails offline the way it fails with a model.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from runner.decision_log import RouterDecision
from runner.protocol import NodeResult, RunnerError
from runner.tier_resolution import Hints

__all__ = ["TREE", "ScriptedRunner"]

TREE = "@tree"  # the answers' key that makes the runner build in its `repo_dir`'s tree
_PATCH_ROLES = frozenset({"build"})  # the Claude Code runner's own: the roles whose patch is the scratch's diff


def built_in_tree(tree: Path, role: str, answer: Mapping[str, Any]) -> dict[str, Any]:
    """Edge. `answer` with its patch applied in a scratch worktree of `tree` at its HEAD and read back from it, as the
    Claude Code runner computes a build's patch; RunnerError, named as that runner names it, when it does not apply."""
    from harness.worktree import registry_lock  # here, not at the top: harness imports the runners
    from runner.claude_code_runner import reconcile_patch

    parent = Path(tempfile.mkdtemp(prefix="scripted-build-"))
    scratch = parent / "tree"
    reported = str(answer.get("patch") or "")
    with registry_lock(tree):
        added = subprocess.run(["git", "-C", str(tree), "worktree", "add", "--detach", str(scratch), "HEAD"],
                               capture_output=True, text=True)
    if added.returncode != 0:
        raise RunnerError(f"could not create a scratch worktree for '{role}': {added.stderr.strip()[:300]}")
    try:
        applied = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=scratch, input=reported,
                                 capture_output=True, text=True)
        computed = ""
        if applied.returncode == 0:
            subprocess.run(["git", "add", "-A"], cwd=scratch, capture_output=True, text=True)
            computed = subprocess.run(["git", "diff", "--cached", "--binary", "--full-index"], cwd=scratch,
                                      capture_output=True, text=True).stdout
    finally:
        with registry_lock(tree):
            subprocess.run(["git", "-C", str(tree), "worktree", "remove", "--force", str(scratch)],
                           capture_output=True, text=True)
        shutil.rmtree(parent, ignore_errors=True)
    patch, reason = reconcile_patch(reported, computed)
    if reason:
        raise RunnerError(f"node '{role}' {reason}: the scripted patch does not apply at {tree}'s HEAD")
    return {**answer, "patch": patch}


class ScriptedRunner:
    """Replays `{role: response}` (or `{role: [response, ...]}`) by role, and `{"<role>@<task>": ...}` for one task.

    Records every call on `.calls` so a test can assert on what the graph asked
    for — the tier it requested, the context it passed — and not merely on what
    came back. With `TREE` set in the answers it has a `repo_dir`, `repo` or None
    until the harness points it, and its builds are applied there (`built_in_tree`);
    without, it has none, and a `repo_dir` a test assigns changes no answer.
    """

    def __init__(self, responses: Mapping[str, Any], repo: Path | str | None = None) -> None:
        self._responses = {
            role: list(value) if isinstance(value, list) else [value]
            for role, value in responses.items() if role != TREE
        }
        self.calls: list[dict[str, Any]] = []
        self._tree = responses.get(TREE) is True
        if self._tree:
            self.repo_dir: Path | None = Path(repo) if repo else None

    def run(
        self,
        *,
        role: str,
        tier: str | None = None,
        hints: Hints | None = None,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
        router_decision: RouterDecision | None = None,
        wait_if_paused: Callable[[], None] | None = None,
    ) -> NodeResult:
        # `task` is not recorded on `.calls` — this double replays graphs whose
        # existing assertions read that dict verbatim, and stamping `task_id`
        # onto a call record is `ClaudeCodeRunner`'s own contract, not this one's.
        record = {"role": role, "tier": tier, "prompt": prompt, "context": list(context), "thread": thread, "budget_usd": budget_usd}
        if wait_if_paused is not None:
            wait_if_paused()
        record["wait_if_paused_given"] = wait_if_paused is not None
        record["wait_if_paused_calls"] = 1 if wait_if_paused is not None else 0
        self.calls.append({**record, "hints": hints} if hints is not None else record)
        # `<role>@<task>` answers that role for that task only, ahead of the role's own answers, so tasks built in
        # parallel each get their own answer whichever reaches the runner first.
        queued = (self._responses.get(f"{role}@{task}") if task else None) or self._responses.get(role)
        if not queued:
            raise RunnerError(
                f"no scripted response for role '{role}'"
                + (f" (call {len(self.calls)})" if role in self._responses else "")
                + "; scripting every node a graph runs is how a test notices the graph changed shape"
            )
        # An entry may be an exception instance rather than a response, so a test
        # can script a role that fails on a given call — a budget stop on a
        # retry, say. Once it is the only entry left it repeats on every further
        # call of that role, same as any other single-item entry repeats.
        answer = queued.pop(0) if len(queued) > 1 else queued[0]
        if isinstance(answer, BaseException):
            raise answer
        tree = getattr(self, "repo_dir", None) if self._tree else None
        if role in _PATCH_ROLES and tree is not None and isinstance(answer, Mapping):
            return NodeResult(built_in_tree(Path(tree), role, answer))
        return NodeResult(answer)
