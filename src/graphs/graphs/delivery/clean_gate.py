"""Run the repository's clean check on a built patch, before review.

The clean script is `.github/check_clean.py`, run with `--require-names` in `.agent-checks`.
A hit on it is a build fault the builder can fix; an unset names pattern is a gate that did not run.
`lifecycle_propose` runs it on every build's patch before the handoff and review, and `harness.checks.check_feedback`
gives the same refusal when the lane gate's own clean line fails after approval.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from uuid import uuid4

from core.bounded_proc import GIT_TIMEOUT_SECONDS, completed

_CLEAN_SCRIPT = ".github/check_clean.py"
_REQUIRE_NAMES = "--require-names"
# `path:line: rule: 'text'`; a build-artifact hit has no line number.
_HIT_LINE = re.compile(r"^\S+?:(?:\d+:)? (?P<rule>[\w-]+): '.*'$")
# Per rule of the clean script: the advice, and the literal placeholders it suggests; test_clean_gate runs each
# placeholder through the script itself.
_PLACEHOLDERS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "names": ("a placeholder machine name", ("lane-host", "lander-host")),
    "home-path": ("a path built from pytest's `tmp_path`, or the neutral home", ("/home/user",)),
    "lan-host": ("a host with no `.lan` suffix", ("lane-host",)),
    "address": ("a documentation address", ("192.0.2.10",)),
    "vendor": ("a neutral word", ("tracker",)),
    "build-artifact": ("nothing: remove the file under `build/`", ()),
}
# The most hits a refusal quotes; the rest are counted, so a tree full of hits cannot swamp a builder's prompt.
_MAX_HITS = 20
_RETRY_RULE = (
    "Replace each matched text with its placeholder, and use it nowhere else: not in code, a comment, a fixture or "
    "another spelling. The names rule reads a pattern you cannot see, so any other real name fails the same way."
)
_UNPARSED_RULE = (
    "Its output named no file and line. Rerun `python .github/check_clean.py . --require-names` in the worktree to "
    "see each hit, then replace what it quotes with a neutral stand-in: `lane-host` or `lander-host` for a machine, "
    "a path from `tmp_path` for a home folder."
)
# `ran`: the clean entry ran; `skipped`: it exited 2, names pattern not exported; `none`: nothing to gate.
GateStatus = Literal["ran", "skipped", "none"]


def clean_checks(checks: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """The entries that run the clean script with its require-names flag."""
    return tuple(
        dict(check)
        for check in checks
        if _CLEAN_SCRIPT in str(check.get("cmd") or "") and _REQUIRE_NAMES in str(check.get("cmd") or "")
    )


def _advice(rule: str) -> str:
    lead, literals = _PLACEHOLDERS.get(rule, ("a neutral stand-in", ()))
    return f"{lead} {' or '.join(f'`{p}`' for p in literals)}" if literals else lead


def clean_refusal(output: str) -> str | None:
    """The refusal a builder reads for clean-script output: the first `_MAX_HITS` hits as `file:line: rule: 'matched'`
    and a count of the rest, the placeholder each rule present accepts, and the rule for a retry; None with no hit line."""
    hits = tuple(
        (line.strip(), match.group("rule"))
        for line in (output or "").splitlines()
        if (match := _HIT_LINE.match(line.strip()))
    )
    if not hits:
        return None
    shown = "\n".join(f"  {hit}" for hit, _rule in hits[:_MAX_HITS])
    more = f"\n  +{len(hits) - _MAX_HITS} more" if len(hits) > _MAX_HITS else ""
    rules = "\n".join(f"  {rule}: use {_advice(rule)}" for rule in dict.fromkeys(rule for _hit, rule in hits))
    return (
        f"The clean check ({_CLEAN_SCRIPT} {_REQUIRE_NAMES}) refused a personal reference:\n{shown}{more}\n"
        f"Instead:\n{rules}\n{_RETRY_RULE}"
    )


def clean_finding(exit_code: int | None, output: str) -> str | None:
    """A finding for a clean-script hit (exit 1); exit 2 means the names pattern was not exported, so the gate did not run."""
    if exit_code != 1:
        return None
    return clean_refusal(output) or (
        f"The clean check failed:\n  {(output or '').strip()[-_MAX_HITS * 80:]}\n{_UNPARSED_RULE}"
    )


def clean_gate_outcome(
    patch: str, checks: Sequence[Mapping[str, Any]], repo_dir: str | None
) -> tuple[str | None, GateStatus]:
    """The clean check's finding for `patch` and whether the gate ran, run in a worktree of `repo_dir` at `HEAD`.

    No clean entry, no repository, a worktree that cannot be made, or a patch that
    does not apply gives `(None, "none")`: there is nothing to gate. An entry that
    exits 2 and finds nothing gives `(None, "skipped")`. The child inherits this
    process's environment, which is how it sees `COXSWAIN_CLEAN_DENY`.

    Imported here, not at module level: `harness` imports `graphs.delivery`
    (via `harness.epic`), so a top-level `harness` import is circular.
    """
    entries = clean_checks(checks)
    if not entries or not repo_dir:
        return None, "none"
    from harness.checks import run_checks
    from harness.worktree import apply_patch, create_worktree, link_venv, remove_worktree

    repo = Path(repo_dir)
    branch = f"clean-gate-{uuid4().hex[:8]}"
    with TemporaryDirectory() as scratch:
        worktree = Path(scratch) / "worktree"
        made, _detail = create_worktree(repo, worktree, branch=branch, base="HEAD")
        if not made:
            return None, "none"
        try:
            link_venv(repo, worktree)
            applied, _detail = apply_patch(patch or "", worktree)
            if not applied:
                return None, "none"
            results = run_checks(worktree, entries)
            findings = tuple(
                found
                for found in (clean_finding(r.get("exit_code"), r.get("output_tail") or "") for r in results)
                if found
            )
            if findings:
                return findings[0], "ran"
            return None, "skipped" if any(r.get("exit_code") == 2 for r in results) else "ran"
        finally:
            remove_worktree(repo, worktree)
            completed(["git", "-C", str(repo), "branch", "-D", branch], timeout=GIT_TIMEOUT_SECONDS, capture_output=True, text=True)
