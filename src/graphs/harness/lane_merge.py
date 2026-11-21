"""The phase lane's local merge agent (L5): two parallel tasks whose changes collide on the roll-up are merged on the
worker, and the result has to pass both tasks' checks before it lands.

A task's change is applied onto the phase tip; when it does not apply cleanly, another task of the phase landed on the
same lines since this one was cut. The lane then, still under the phase's local lock and inside its claim fence:

1. finds the tasks it collides with: every task landed since this task's base whose commit touched a file this task
   changes (`landed`, `colliding`), and the files they share (`shared_files`); the last of them is the one the
   `Cox-Merged-With` trailer names;
2. opens a scratch worktree at the phase tip, which holds their changes, and applies this task's change onto it three
   ways (`git apply --3way`), so the files that conflict hold both sides between conflict markers;
3. asks the `merge_tasks` seat for each conflicted file's whole merged text, keeping both sides' intent (`brief`,
   `SCHEMA`). An answer that leaves a file out or a conflict marker in is not a merge (`resolution`), and neither is
   one that drops a line either side added to a conflicted file (`lost_lines`);
4. writes the merged files, commits them in the scratch, and runs on that tree, for **every** colliding task and for
   this one, the task's cheap checks (narrowed to what changed since its parent, or since the tip for this task) and
   its own `verify` commands;
5. `outcome` decides: every step good is `commit`, the merged change (the scratch against the tip) rolls up as this
   task's one commit with a `Cox-Merged-With` trailer; anything else is `rebuild`, and the lane builds this task
   again on the new phase tip. A side whose checks are none at all is not checked (`side_ok`), so it rebuilds too:
   a merge lands only on evidence from both sides.

Either way the lane writes one `collision` step row (`collision_row`): the tasks and the files. It is the record a
decompose reads back as a miss: two tasks the DAG said were independent edited the same code.

`outcome`, `side_ok`, `collision_row`, `landed`, `colliding`, `shared_files`, `lost_lines`, `brief`, `resolution` and
`has_markers` are pure; `resolve` is the edge, given the seat and the checks as callables by the lane.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from harness.epic_branches import IDENTITY
from harness.store_dialect import Json, JsonObject

__all__ = [
    "LANDED_ARGS", "ROLE", "SCHEMA", "Merge", "Outcome", "Results", "brief", "colliding", "collision_row",
    "has_markers", "landed", "lost_lines", "outcome", "resolution", "resolve", "shared_files", "side_ok",
]

Outcome = Literal["commit", "rebuild"]
Results = Sequence[Mapping[str, object]]  # check or verify results, as `checks.run_checks` returns them
Commit = tuple[str, str, tuple[str, ...]]  # (sha, task, paths), as `landed` reads them
ROLE = "merge_tasks"
_TIMEOUT_S = 120  # a local git command
_FILE_CHARS = 60_000  # the most of one conflicted file the brief carries
# Conflict marker lines, the diff3 style's `|||||||` (the common ancestor) included.
_MARKERS = ("<<<<<<<", "|||||||", ">>>>>>>")
# `git log` of the commits landed since a task's base, oldest first: each record the commit, its `Cox-Task` trailer,
# then the paths it touched.
LANDED_ARGS = ("--reverse", "--no-renames", "--name-only",
               "--format=%x1e%H%x1f%(trailers:key=Cox-Task,valueonly,separator=)%x1f")

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["resolved", "files", "summary"],
    "properties": {
        "resolved": {"type": "boolean"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
        },
        "summary": {"type": "string"},
    },
}


# -- pure ------------------------------------------------------------------------------------------------------------

def outcome(merge_ok: bool, checks_a_ok: bool, checks_b_ok: bool) -> Outcome:
    """Pure. `commit` the merged change only when the merge resolved and both sides' checks pass on it (`side_ok`);
    any other case `rebuild`s the later task on the new phase tip."""
    return "commit" if merge_ok and checks_a_ok and checks_b_ok else "rebuild"


def side_ok(checks: Results, verify: Results) -> bool:
    """Pure. Whether one side of a merge is checked and passes: at least one cheap check ran, and every check and
    every `verify` command passed. No checks at all is no evidence, so it is not a pass."""
    return bool(checks) and all(r.get("passed") is True for r in (*checks, *verify))


def collision_row(task: str, other: str, files: Sequence[str]) -> JsonObject:
    """Pure. The `collision` step row's result: the task that collided, the task it collided with, and the files they
    share, sorted and once each."""
    shared: list[Json] = [*sorted(set(files))]
    return {"task": task, "other": other, "files": shared}


def landed(log_text: str) -> list[Commit]:
    """Pure. `git log` in `LANDED_ARGS` form as (commit, task, paths) per commit, oldest first; a commit with no
    `Cox-Task` trailer has task ""."""
    out: list[Commit] = []
    for record in log_text.split("\x1e"):
        parts = record.split("\x1f")
        if len(parts) < 3 or not parts[0].strip():
            continue
        paths = tuple(p.strip() for p in parts[2].splitlines() if p.strip())
        out.append((parts[0].strip(), parts[1].strip(), paths))
    return out


def shared_files(files: Sequence[str], commits: Sequence[Commit]) -> list[str]:
    """Pure. The files of `files` that a commit of `commits` also touched, sorted."""
    touched = {p for _, _, paths in commits for p in paths}
    return sorted(set(files) & touched)


def colliding(files: Sequence[str], commits: Sequence[Commit]) -> list[tuple[str, str]]:
    """Pure. The tasks a change collides with: each task commit in `commits` (oldest first) that touched one of
    `files`, as (task, commit), oldest first. Every one is checked on the merge, since the merged text of a shared
    file holds each of their edits; the last is the one the roll-up's `Cox-Merged-With` names."""
    wanted = set(files)
    return [(task, sha) for sha, task, paths in commits if task and wanted & set(paths)]


def has_markers(text: str) -> bool:
    """Pure. Whether `text` still holds a conflict marker line: `<<<<<<<`, `|||||||` (diff3) or `>>>>>>>` at a line's
    start, alone or before a space."""
    return any(line.rstrip("\r\n") == m or line.startswith(f"{m} ") for line in text.splitlines(keepends=True)
               for m in _MARKERS)


def _added(base: str, side: str) -> Counter[str]:
    """The non-blank lines `side` holds more often than `base`, with how many more: what that side added."""
    def count(text: str) -> Counter[str]:
        return Counter(line.strip() for line in text.splitlines() if line.strip())
    return count(side) - count(base)


def lost_lines(base: str, ours: str, theirs: str, merged: str) -> list[str]:
    """Pure. The lines either side added to a file (against their common `base`) that the `merged` text does not hold
    as often, sorted, compared without their indentation (a merge may nest an edit): a merge that drops one side's
    edit to make the conflict go away loses lines here."""
    have = Counter(line.strip() for line in merged.splitlines() if line.strip())
    wanted = _added(base, ours) | _added(base, theirs)
    return sorted((wanted - have).elements())


def brief(task: str, title: str, other: str, other_title: str, conflicted: Mapping[str, str], change: str) -> str:
    """Pure. The `merge_tasks` seat's prompt: the two tasks, this task's change, and each conflicted file's text as the
    three-way apply left it (both sides between conflict markers), the file cut at `_FILE_CHARS`."""
    files = "\n\n".join(f'<file path="{path}">\n{text[:_FILE_CHARS]}\n</file>' for path, text in sorted(conflicted.items()))
    return (
        f"Two tasks of one phase were built in parallel and edit the same code. Task `{other}` ({other_title}) is "
        f"already on the phase branch. Task `{task}` ({title}) was built on the tip before it, and its change does not "
        "apply cleanly on top.\n\n"
        "Merge them: for each file below, return its whole merged text, keeping what BOTH tasks meant. The text "
        f"between `<<<<<<<` and `=======` is the phase branch's (holding `{other}`); between `=======` and `>>>>>>>` "
        f"is `{task}`'s. Leave no conflict marker, and keep every line either side added. Change nothing outside what "
        "the two tasks changed. If the two cannot both hold (they contradict each other), answer `resolved: false` "
        f"and say why in `summary`: the harness then builds `{task}` again on the new tip.\n\n"
        f"Task `{task}`'s whole change, against the tip it was built on:\n<change>\n{change[:_FILE_CHARS]}\n</change>\n\n"
        f"The conflicted files:\n\n{files}\n"
    )


def resolution(answer: Mapping[str, object], conflicted: Sequence[str]) -> dict[str, str] | str:
    """Pure. The merged text of each conflicted file from the seat's answer, or why the answer is not a merge: it was
    not resolved, it left out a conflicted file, named one that is not, or left a conflict marker in."""
    if answer.get("resolved") is not True:
        return f"the merge agent did not resolve it: {' '.join(str(answer.get('summary') or '').split())[:300]}"
    raw = answer.get("files")
    entries = [e for e in raw if isinstance(e, Mapping)] if isinstance(raw, list) else []
    files = {str(e.get("path") or ""): str(e.get("content") or "") for e in entries}
    wanted = set(conflicted)
    if missing := sorted(wanted - set(files)):
        return f"the merge left out {', '.join(missing)}"
    if extra := sorted(set(files) - wanted):
        return f"the merge named files that do not conflict: {', '.join(extra)}"
    if marked := sorted(p for p, text in files.items() if has_markers(text)):
        return f"the merge left conflict markers in {', '.join(marked)}"
    return files


# -- the edge --------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Merge:
    """What a collision came to: the `outcome`, the task the trailer names and its commit, every colliding task, the
    shared files, the merged change against the tip (only on `commit`), why not (on `rebuild`), and each side's check
    and verify results, by task."""

    outcome: Outcome
    other: str
    other_sha: str
    files: tuple[str, ...]
    others: tuple[str, ...] = ()
    change: str = ""
    why: str = ""
    checks: Mapping[str, Results] = field(default_factory=dict[str, Results])


def _git(*args: str, cwd: Path, stdin: str | None = None) -> tuple[bool, str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", *args], cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                              timeout=_TIMEOUT_S, stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "", f"git {' '.join(args[:2])} timed out after {_TIMEOUT_S}s"
    return done.returncode == 0, done.stdout, done.stderr.strip()


def _touched(diff: str) -> list[str]:
    """The paths a `git diff --no-renames` change touches, from its `diff --git a/<p> b/<p>` headers."""
    return sorted({line.split(" b/", 1)[1] for line in diff.splitlines() if line.startswith("diff --git a/") and " b/" in line})


def _stage(scratch: Path, n: int, path: str) -> str:
    """An unmerged file's text at index stage `n` (1 the common base, 2 the tip, 3 the task); "" when it has none."""
    ok, text, _ = _git("show", f":{n}:{path}", cwd=scratch)
    return text if ok else ""


def resolve(scratch: Path, base: str, tip: str, change: str, *, task: str, title: str,
            titles: Mapping[str, str], ask: Callable[[str], Mapping[str, object]],
            check: Callable[[Path, str, str], tuple[Results, Results]]) -> Merge:
    """Edge. Merge `task`'s `change` (built on `base`) with what landed on the phase tip `tip` since, in `scratch`, a
    worktree detached at `tip` that this call leaves holding the merge: `ask` is the `merge_tasks` seat, given the
    brief; `check(worktree, since, task)` runs that task's cheap checks on the worktree's HEAD, narrowed to what
    changed since `since`, and its `verify` commands, as (check results, verify results). `titles` names each task."""
    ok, text, err = _git("log", *LANDED_ARGS, f"{base}..{tip}", cwd=scratch)
    commits = landed(text) if ok else []
    files = tuple(shared_files(_touched(change), commits))
    hits = colliding(files, commits)
    other, other_sha = hits[-1] if hits else ("", "")
    others = tuple(dict.fromkeys(t for t, _ in hits))

    def rebuild(why: str, checks: Mapping[str, Results] | None = None) -> Merge:
        return Merge("rebuild", other, other_sha, files, others, why=why, checks=checks or {})

    if not ok:
        return rebuild(f"what landed since {base[:12]} could not be read: {err[:300]}")
    if not other:
        return rebuild(f"no task landed since {base[:12]} touched the files the change does not apply to")
    # A conflict exits non-zero and leaves both sides between markers, recorded as unmerged in the index.
    applied, _, apply_err = _git("apply", "--3way", "--whitespace=nowarn", "-", cwd=scratch, stdin=change)
    found, names, err = _git("diff", "--name-only", "--diff-filter=U", cwd=scratch)
    conflicted = sorted(p for p in names.splitlines() if p.strip()) if found else []
    if not conflicted and not applied:
        return rebuild(f"the change could not be applied three ways: {(apply_err or err)[:300]}")
    merged: dict[str, str] | str = {}
    if conflicted:  # else git merged it three ways on its own, and only the checks are left to decide
        sides = {p: (scratch / p).read_text(encoding="utf-8", errors="replace") for p in conflicted}
        stages = {p: (_stage(scratch, 1, p), _stage(scratch, 2, p), _stage(scratch, 3, p)) for p in conflicted}
        merged = resolution(ask(brief(task, title, other, titles.get(other, other), sides, change)), conflicted)
        if not isinstance(merged, str):
            lost = {p: lost_lines(*stages[p], merged[p]) for p in conflicted}
            if dropped := sorted(p for p, lines in lost.items() if lines):
                merged = f"the merge dropped lines a side added in {', '.join(dropped)}: " + "; ".join(
                    f"{p}: {', '.join(repr(line) for line in lost[p][:3])}" for p in dropped)[:400]
    if isinstance(merged, str):
        return rebuild(merged)
    for path, content in merged.items():
        (scratch / path).write_text(content, encoding="utf-8")
    staged, _, err = _git("add", "-A", cwd=scratch)
    committed = staged and _git(*IDENTITY, "commit", "-q", "--no-verify", "-m", f"merge {task} with {other}",
                                cwd=scratch)[0]
    if not committed:
        return rebuild(f"the merge could not be committed in the scratch: {err[:300]}")
    shas = dict(hits)  # the last commit of each colliding task
    ran = {t: check(scratch, f"{shas[t]}^", t) for t in others} | {task: check(scratch, tip, task)}
    checks = {t: [*c, *v] for t, (c, v) in ran.items()}
    good = {t: side_ok(c, v) for t, (c, v) in ran.items()}
    if outcome(True, all(good[t] for t in others), good[task]) == "rebuild":
        failing = [t for t in (*others, task) if not good[t]]
        return rebuild(f"the merge is not checked and passing for {', '.join(failing)} (a side with no check counts "
                       "as unchecked)", checks)
    ok, diff, err = _git("diff", "--binary", "--no-renames", tip, "HEAD", cwd=scratch)
    if not ok:
        return rebuild(f"the merged change could not be read: {err[:300]}", checks)
    return Merge("commit", other, other_sha, files, others, change=diff, checks=checks)
