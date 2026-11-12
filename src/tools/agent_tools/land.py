"""Land an approved run: pick the branch it lives on, cherry-pick it onto a
fresh PR branch, open the PR, wait for it to go green, merge, and clean up.

`land_plan` and `pr_body` are pure: given the task record (the json at
`runs/<run>/tasks/<phase>/<task>.json`) and, for each candidate branch, the
commit subjects the edge found ahead of the default branch, they return the
ordered steps or a single `refuse` step naming why. `branches` already
excludes merge commits — the edge gathers it with `git log --no-merges`,
because a merge commit is a fact git holds (parent count), not something a
commit subject reliably spells out. The record is expected to carry `run`,
`task`, `phase`, and `initiative` (the edge fills the first three in from the
file's own path when the record itself is silent on them) plus `proposals`,
`review`, `arbitration`, `change_facts`, and `build`; a record silent on
`initiative` still lands cleanly off the scratch branch, and only fails to
resolve a phase branch, which becomes the ordinary "no branch is exactly one
commit ahead" refuse rather than a crash. `cli.py` is the edge: it gathers
the record and the branches with `git log`, then walks the plan through
`subprocess` and `gh`.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, NamedTuple

from agent_tools import readme_commands
from agent_tools.card_commits import card_commit_steps
from agent_tools.card_pr import card_pr_steps
from agent_tools.chair_plan_land import fetch_action
from core.landing import (
    ACCEPTED_LAND_AT,
    DEFAULT_LAND_AT,
    canonical_land_at,
    is_initiative,
    phase_merge_ready,
    pr_branch_name,
    pr_stage,
)

if TYPE_CHECKING:
    from agent_tools import store_cli

__all__ = [
    "GENERATED_FILES",
    "LAUNCH_ERROR",
    "MERGE_UPDATE_ATTEMPTS",
    "GeneratedFile",
    "LeaseRenewal",
    "approve_to_done",
    "approved_state",
    "arbitration_verdict",
    "await_checks",
    "behind_stop",
    "check_exit",
    "check_poll_result",
    "checks_argv",
    "checks_detail",
    "close_to_done",
    "gate_steps",
    "gate_stop",
    "generated_conflict_only",
    "is_behind_refusal",
    "is_pending",
    "issue_closes",
    "land_log_row",
    "land_plan",
    "last_command",
    "mark_done",
    "merge_pages",
    "parent_land_wait",
    "phase_landable",
    "phase_landed_tasks",
    "phase_pr_body",
    "phase_pr_title",
    "poll_backoff_s",
    "pr_body",
    "recover_plan",
    "recover_record",
    "renew_due",
    "renewal_stop",
    "resolve_block_conflicts",
    "rest_checks_argvs",
    "set_state_stop",
    "skip_line",
    "task_gather_status",
    "unreadable_poll",
    "wait_decision",
]

LAUNCH_ERROR = "refuse checks: "  # a check that cannot launch, marked so the edge refuses (exit 2) not just stops (exit 1)


def _proposal(record: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((p for p in record.get("proposals", []) if p.get("kind") == kind), None)


def _verdict(record: dict[str, Any], section: str) -> str | None:
    return (record.get(section) or {}).get("verdict")


_ARBITER_SKIPPED = "arbiter: skipped (both approved)"


def arbitration_verdict(record: dict[str, Any]) -> str | None:
    """A dict gives its "verdict"; the arbiter-skip string means both approved; null or else is None."""
    arbitration = record.get("arbitration")
    if isinstance(arbitration, dict):
        return arbitration.get("verdict")
    return "approve" if arbitration == _ARBITER_SKIPPED else None


def _approved(record: dict[str, Any]) -> str | None:
    """None when the record's decision is approve, else the reason it is not.
    An arbiter only runs on disagreement, so unanimous approval leaves no
    arbitration verdict at all — the best outcome, not a missing one."""
    arbitration = arbitration_verdict(record)
    if arbitration == "approve":
        return None
    if arbitration is not None:
        return f"arbitration verdict is {arbitration!r}, not 'approve'"
    reviews = [v for v in (_verdict(record, "review"), _verdict(record, "adversary")) if v is not None]
    if reviews and all(v == "approve" for v in reviews):
        return None
    return f"no arbitration, and the reviewers were {reviews or 'silent'}"


# The ticket names the first three; `setup.py`, `tox.ini` and `requirements.txt` mark the same Python repos, which
# would otherwise drop from "pytest ran" to "nothing ran".
PYTHON_PROJECT_FILES = ("pyproject.toml", "setup.cfg", "pytest.ini", "setup.py", "tox.ini", "requirements.txt")


def resolved_checks(cartridge_checks: Sequence[dict[str, Any]], repo_checks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The cartridge's checks, then the repo's `.agent-checks` entries whose `cmd` the cartridge does not already run.
    The chair's rule (`_Ctx.checks` in harness/epic.py), so the land runs what the build was held to."""
    known = {c.get("cmd") for c in cartridge_checks}
    return [*cartridge_checks, *(c for c in repo_checks if c.get("cmd") not in known)]


def checks_argv(repo_facts: dict[str, Any]) -> list[tuple[str, str]]:
    """One `(name, shell line)` pair per `repo_facts["checks"]` entry (`{name, cmd}`,
    already resolved by the cartridge, §4), in the given order. The line is a
    shell line exactly as written, as `.agent-checks` lines are (`cd x && y`),
    never split into an argv. With no `checks`
    fact the single launch named `"tests"` stays: its own venv first, `uv run`
    next when the repo pins its dependencies with a lockfile, a bare `pytest -q`
    only when neither holds. A repo that says `python_project` is false (a key
    absent reads as true) has no pytest to run, so it gets no checks at all,
    whatever stray `.venv` it holds."""
    checks = repo_facts.get("checks")
    if checks:
        return [(c["name"], c["cmd"]) for c in checks]
    if not repo_facts.get("python_project", True):
        return []
    if repo_facts.get("venv_python"):
        line = ".venv/bin/python -m pytest -q"
    elif repo_facts.get("uv_lock"):
        line = "uv run pytest -q"
    else:
        line = "pytest -q"
    return [("tests", line)]


def checks_gate(repo_facts: dict[str, Any]) -> tuple[bool, str | None]:
    """`(skip, reason)` for the checks step. `land_checks` ci skips only when `required_checks` is a non-empty tuple; an empty or unread one keeps the checks and says why. Absent or local gives `(False, None)`."""
    if repo_facts.get("land_checks", "local") != "ci":
        return False, None
    required = repo_facts.get("required_checks")
    if required is None:
        return False, "checks kept locally: the required status checks on main could not be read"
    if not required:
        return False, "checks kept locally: main has no required status check"
    return True, f"CI runs the required status checks on main: {', '.join(required)}"


def checks_step(repo_facts: dict[str, Any], worktree_of: str) -> dict[str, Any]:
    """The `checks` step; `skipped` or `kept` carries `checks_gate`'s reason, and a default land adds neither."""
    skip, reason = checks_gate(repo_facts)
    gate = {} if reason is None else {"skipped": reason} if skip else {"kept": reason}
    return {"kind": "checks", "checks": checks_argv(repo_facts), "worktree_of": worktree_of, **gate}


def ticket_checks_steps(repo_facts: dict[str, Any], worktree_of: str) -> list[dict[str, Any]]:
    """The ticket gate's one `checks` step, as `(name, shell line)` pairs from `repo_facts["ticket_checks"]`; no step at all when that list is empty."""
    checks = [(c["name"], c["cmd"]) for c in repo_facts.get("ticket_checks") or []]
    return [{"kind": "checks", "gate": "ticket", "checks": checks, "worktree_of": worktree_of}] if checks else []


PYTEST_NO_TESTS = 5  # pytest's exit code when it collected no tests


def _is_pytest(argv: Sequence[str]) -> bool:
    """Only `pytest ...`, `python* -m pytest ...` or `uv run pytest ...`; `coverage run pytest` is not pytest."""
    head = PurePath(argv[0]).name if argv else ""
    return (head == "pytest" or (head.startswith("python") and list(argv[1:3]) == ["-m", "pytest"])
            or list(argv[:3]) == ["uv", "run", "pytest"])


_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")


def last_command(line: str) -> list[str]:
    """The words of a shell line's last simple command, past any `VAR=value` prefix: `cd x && A=1 pytest -q` is
    `['pytest', '-q']`. `[]` for a line that will not tokenise (an open quote)."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return []
    start = max((i + 1 for i, w in enumerate(words) if w in _SEPARATORS), default=0)
    command = words[start:]
    while command and _ASSIGNMENT.match(command[0]):
        command = command[1:]
    return command


def check_exit(line: str, returncode: int) -> str:
    """`pass`, `skip` or `fail`. Only a check whose last command is pytest, exiting 5 (no tests collected), is a skip."""
    if returncode == 0:
        return "pass"
    return "skip" if returncode == PYTEST_NO_TESTS and _is_pytest(last_command(line)) else "fail"


def skip_line(name: str) -> str:
    return f"checks: {name} skipped (pytest exit 5, no tests ran)"


def reused_line(name: str) -> str:
    return f"checks: reused: {name} (same tree and command passed earlier)"


def checks_detail(head: str, skipped: Sequence[str], flaky: Sequence[str] = (), reused: Sequence[str] = ()) -> str:
    """`head` first, then one skip line each, one line per reused check, then one line per flaky note. The land
    prints the detail as `checks: <detail>`, so `head` gets that prefix and each later line carries its own; a skip
    line placed first would print as `checks: checks: ...`."""
    return "\n".join([head, *(skip_line(name) for name in skipped), *(reused_line(name) for name in reused),
                      *(f"checks: {note}" for note in flaky)])


def phase_landable(items: list[dict[str, Any]], records: dict[str, dict[str, Any]], quarantine: bool = False) -> str | None:
    """None when every item is landable, else the first reason it is not. A `done` or `dropped` item is
    satisfied without a record; an `approved` item needs one, and any record present must be approved.
    With `quarantine`, an item or record in state `quarantined` is refused by name."""
    for item in items:
        status = item.get("status")
        if quarantine and "quarantined" in (status, (records.get(item.get("id")) or {}).get("status")):
            return f"{item.get('id')} is quarantined; the ticket pull request takes only approved, done or dropped tasks"
        if status not in ("approved", "done", "dropped"):
            return f"{item.get('id')} is {status!r}, not approved, done or dropped"
        if status == "dropped":
            continue
        record = records.get(item.get("id"))
        if record is None:
            if status == "done":
                continue
            return f"{item.get('id')} has no task record"
        refusal = _approved(record)
        if refusal is not None:
            return f"{item.get('id')}: {refusal}"
    return None


def _phase_order(phase: str) -> list[object]:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", phase)]


def parent_land_wait(tasks: Collection[str], phase_of: Mapping[str, str], needs: Mapping[str, Collection[str]],
                     landed: Collection[str], carried: Collection[str] = ()) -> str | None:
    """`waiting on parent land: <phase>` for the first parent phase, in natural order, holding a need that is neither landed nor carried."""
    unmet = {phase_of[n] for t in tasks for n in needs.get(t, ())
             if n in phase_of and phase_of[n] != phase_of.get(t) and n not in landed and n not in carried}
    return f"waiting on parent land: {min(unmet, key=_phase_order)}" if unmet else None


def squash_missing_files(approved_files: Sequence[dict[str, Any]], squashed_files: Sequence[str]) -> str | None:
    """None when every entry in `approved_files` (`{"task": id, "files_touched":
    [...]}`, one per approved task whose record names any) has at least one of
    its files among `squashed_files`, else the first entry's refusal naming the
    task and its files, comma-joined in the order stored on the task record."""
    for entry in approved_files:
        files = entry["files_touched"]
        if not any(f in squashed_files for f in files):
            return f"{entry['task']}: none of {', '.join(files)} is in the squash"
    return None


def append_only_paths(paths: Sequence[str]) -> bool:
    """True when `paths` is non-empty and every path is under `tests/`."""
    return bool(paths) and all(p.startswith("tests/") for p in paths)


def phase_pr_body(phase_record: dict[str, Any], task_records: list[dict[str, Any]]) -> str:
    """The phase PR body (§2): the phase's own verdict reasoning, then one
    block per landed ticket, dropped tickets listed last with their reason."""
    verdict = (phase_record.get("phase_verdict") or {}).get("reasoning", "")
    lines = [f"Phase: {phase_record.get('phase')}", verdict]
    dropped = [r for r in task_records if r.get("status") == "dropped"]
    for r in (r for r in task_records if r.get("status") != "dropped"):
        facts = r.get("change_facts") or {}
        lines += [
            "",
            f"- {r.get('task')}: {r.get('title', r.get('task'))}",
            f"  Review: {_verdict(r, 'review')}",
            f"  Adversary: {_verdict(r, 'adversary')}",
            f"  Arbitration: {arbitration_verdict(r) or 'unanimous'}",
            f"  Fix-loop attempts: {facts.get('fix_loop_attempts')}",
            f"  Files touched: {', '.join(facts.get('files_touched', []))}",
        ]
    if dropped:
        lines += ["", "Dropped:"]
        lines += [f"- {r.get('task')}: {r.get('reason', '')}" for r in dropped]
    lines += ["", pr_footer(phase_record.get("run"))]
    return "\n".join(lines)


def phase_pr_title(initiative_id: str, phase: str, initiative_title: str) -> str:
    """`epic <id>: <phase> - <title>`, `<id>` being whatever `initiative_id` already is -- a short id under
    `ids: sequence`, the old slug key under `ids: slug` -- and `<title>` the initiative's own title. The
    ` - <title>` suffix is dropped when `initiative_title` is empty, since a caller that has not resolved
    one yet (an older record, or a test literal) should still get a readable subject."""
    subject = f"epic {initiative_id}: {phase}"
    return f"{subject} - {initiative_title}" if initiative_title else subject


def _phase_approved_files(items: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One `{"task": id, "files_touched": [...]}` per `approved` item whose
    record's `build.files_touched` is non-empty, in item order; a `done` or
    `dropped` item, or one with no or empty `files_touched`, contributes
    nothing — its files already landed in an earlier squash, or there is
    nothing to check."""
    approved = [item.get("id") for item in items if item.get("status") == "approved"]
    files_by_task = [(task, (records.get(task) or {}).get("build", {}).get("files_touched")) for task in approved]
    return [{"task": task, "files_touched": files} for task, files in files_by_task if files]


def phase_resume(facts: dict[str, Any] | None, pr_branch: str) -> dict[str, Any]:
    """What a phase land does about an existing `pr_branch`. `facts` carries
    `local_tip`, `remote_tip`, `pr` and `pr_head`, each None when absent.
    `wait` resumes at the open PR, `recreate` rebuilds a stale local branch
    nothing else holds, `refuse` names a divergence, else `fresh`. A no-PR leftover local branch is recreated, unless
    optional `unique_commits` is positive, which refuses; optional `is_ancestor_of_main` feeds `leftover_gate`."""
    f = facts or {}
    local, remote, pr, head = f.get("local_tip"), f.get("remote_tip"), f.get("pr"), f.get("pr_head")
    if pr is not None:
        if head is None or head != remote:
            return {"kind": "refuse", "reason": f"open pull request #{pr} head {head} differs from origin/{pr_branch} at {remote}"}
        if local is not None and local != head:
            return {"kind": "refuse", "reason": f"local branch {pr_branch} at {local} differs from open pull request #{pr} head {head}"}
        return {"kind": "wait", "pr": pr}
    if local is not None:
        gated = leftover_gate(pr_branch, local_tip=local, is_ancestor_of_main=f.get("is_ancestor_of_main"),
                              unique_commits=f.get("unique_commits"), remote_exists=remote is not None,
                              open_pr_number=None)
        return gated if gated is not None and gated["kind"] == "refuse" else {"kind": "recreate"}
    return {"kind": "fresh"}


def _gather_one(record: dict[str, Any], branches: Mapping[str, list[str]], run_hosts: Mapping[str, str],
                fetched_missing: Collection[str], *, repo: str, initiative: str, pr_branch: str,
                default_branch: str) -> list[dict[str, Any]]:
    """The steps that bring one approved task's work onto `pr_branch`. A host is a property of the run, not of
    the task record, so `run_hosts` maps run to host; the fetch step is `chair_plan_land.fetch_action`'s shape
    plus `host`. `repo` is the land's own repo for every step; the record's `repo` field is never read."""
    run, task = record.get("run"), record.get("task")
    branch = f"agents/{run}/{task}"
    build = record.get("build") or {}
    where = {"onto": pr_branch, "from": default_branch, "repo": repo, "task": task,
             "files_touched": list(build.get("files_touched") or [])}
    host = run_hosts.get(run)
    if branch in branches:
        subjects = branches[branch]
        return [{"kind": "cherry_pick", "branch": branch, "commit_subject": subjects[0] if subjects else task, **where}]
    if host and task not in fetched_missing:
        return [{**fetch_action(run, repo, initiative), "host": host},
                {"kind": "cherry_pick", "branch": branch, "commit_subject": task, **where}]
    patch = build.get("patch")
    if isinstance(patch, str) and patch.strip():
        return [{"kind": "patch_apply", "branch": branch, "patch": patch, **where}]
    return [{"kind": "refuse", "reason": f"{task}: {branch} is gone and the record holds no build.patch to apply"}]


def task_gather_status(facts: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Task id to `already_present`, `gather_from_branch`, `gather_from_patch` or `missing`. Each fact is
    `{task, reachable, branch_exists, has_patch}`: the task's agent-branch commit is reachable from the phase
    branch, that branch exists, the record carries a `build.patch`. Reachable wins, then the branch, then the patch."""
    def one(f: Mapping[str, Any]) -> str:
        if f.get("reachable"):
            return "already_present"
        if f.get("branch_exists"):
            return "gather_from_branch"
        if f.get("has_patch"):
            return "gather_from_patch"
        return "missing"
    return {f["task"]: one(f) for f in facts}


def phase_landed_tasks(steps: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Task ids whose work the plan puts on the PR branch: a `squash_phase` step's `tasks` plus each gather step's `task`."""
    squashed = [t for s in steps if s.get("kind") == "squash_phase" for t in s.get("tasks", [])]
    gathered = [s["task"] for s in steps if s.get("kind") in ("cherry_pick", "patch_apply") and "task" in s]
    return frozenset([*squashed, *gathered])


def mark_done(tasks: Sequence[str], merged: Collection[str]) -> list[str]:
    """The ids in `tasks` whose work is in `merged`, in `tasks` order; the others are left as they are."""
    return [t for t in tasks if t in merged]


LANDED_VIA = "landed via"  # the chair counts a line starting with this as a landed report, so the prefix is fixed
_SQUASH_SUFFIX = re.compile(r"\(#(\d+)\)\s*$")
_TAIL_KINDS = frozenset({"clean", "clean_phase", "mark_done"})


def landed_via_line(pr: str) -> str:
    """`landed via PR #<number>` for a bare number or `#<number>`; `landed via <pr>` for a URL."""
    return f"{LANDED_VIA} {pr}" if "://" in pr else f"{LANDED_VIA} PR #{pr.lstrip('#')}"


def pr_number_of_subject(subject: str) -> int | None:
    """The trailing `(#<number>)` a squash merge puts on a subject; a number elsewhere in it does not count."""
    found = _SQUASH_SUFFIX.search(subject)
    return int(found.group(1)) if found else None


def carrying_pr(subjects: Sequence[str], parent_pr: int | None) -> int | None:
    """The first PR number in `subjects` (newest first), else `parent_pr`, else None."""
    return next((n for n in map(pr_number_of_subject, subjects) if n is not None), parent_pr)


SKIP_EMPTY = "empty pick"
SKIP_PATCH_ID = "patch-id match on base"
SKIPPED_ALL = "skipped all "
_EMPTY_PICK = re.compile(r"previous cherry-pick is now empty|^nothing to commit\b", re.IGNORECASE | re.MULTILINE)


def is_empty_pick(stderr: str, stdout: str, unmerged: bool) -> bool:
    """A stopped cherry-pick that git calls empty and that leaves no file unmerged; a real conflict is not."""
    return not unmerged and _EMPTY_PICK.search(f"{stderr}\n{stdout}") is not None


def skip_note(sha: str, subject: str, reason: str) -> str:
    return f"land: skipped {sha[:8]} {subject}: {reason}"


def skipped_all_detail(count: int, base: str) -> str:
    return f"{SKIPPED_ALL}{count} commits, already on {base}"


def is_skipped_all(detail: str) -> bool:
    return detail.startswith(SKIPPED_ALL)


def landed_tail(steps: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the closing steps (clean, clean_phase, mark_done) of `steps`, in order."""
    return [s for s in steps if s.get("kind") in _TAIL_KINDS]


LAND_AT = ACCEPTED_LAND_AT  # every spelling a record may carry; `granularity` returns the canonical one
_GATE_FOR = {"level": "phase", "ticket": "epic", "initiative": "epic"}


def granularity(phase_record: Mapping[str, Any]) -> str:
    """The record's `land_at`, canonical: `initiative` (also for the alias `ticket`, the retired `level`, absent or unknown) or `task`."""
    return canonical_land_at(phase_record.get("land_at")) or DEFAULT_LAND_AT


def gate_level(level: str, land_at: str | None) -> str:
    """The earlier-stopping of `level` and the rung of the PR `land_at` opens (level phase, ticket epic); `task`, `full` and an unset `land_at` keep `level`."""
    rank = {"ticket": 0, "phase": 1, "epic": 1}
    mapped = _GATE_FOR.get(land_at or "")
    return level if mapped is None or level not in rank or rank[level] <= rank[mapped] else mapped


def ticket_done(ticket_tasks: Sequence[str], merged: Collection[str],
                unrecorded: Collection[str] = ()) -> list[dict[str, Any]]:
    """mark_done per ticket task whose work is in `merged`; a note names the rest. A task in `unrecorded`
    has no local task record, so its step says `recorded: False`."""
    held = [t for t in ticket_tasks if t not in merged]
    left = [{"kind": "note", "unmerged": held,
             "reason": f"{', '.join(held)}: work not in the merged PR; left in current state"}] if held else []
    return [*left, *[{"kind": "mark_done", "task": t, **({"recorded": False} if t in unrecorded else {})}
                     for t in ticket_tasks if t not in held]]


def carried_done(carried: Sequence[str], unrecorded: Collection[str] = ()) -> list[dict[str, Any]]:
    """One mark_done per carried task, in order; a task in `unrecorded` has no record file, so its step says `recorded: False`."""
    return [{"kind": "mark_done", "task": t, **({"recorded": False} if t in unrecorded else {})} for t in carried]


def carry_refusal(carried: Sequence[str] | None, accounted: Collection[str]) -> list[dict[str, Any]] | None:
    """A one-step refuse when `carried` is None (unknowable) or holds a task outside `accounted`; else None."""
    if carried is None:
        return [{"kind": "refuse", "reason": "the tasks the pull request carries cannot be told; nothing merged"}]
    unaccounted = [str(t) for t in carried if not t or t not in accounted]
    return [{"kind": "refuse", "reason": f"{', '.join(unaccounted)}: no way to tell the pull request carries the task, "
                                          "so it cannot be marked; nothing merged"}] if unaccounted else None


def _card_phase_plan(phase_record: dict[str, Any], items: list[dict[str, Any]], task_records: list[dict[str, Any]],
                     repo_facts: dict[str, Any] | None, repo: str, default_branch: str = "main",
                     ticket_tasks: Sequence[str] = (), ticket_present: Collection[str] = (),
                     ticket_unrecorded: Collection[str] = ()) -> list[dict[str, Any]]:
    """apply_task per approved task onto `card_branch`; with `ticket_tasks` at ticket one PR on the last phase, then merge and a ticket-wide mark_done."""
    branch = phase_record["card_branch"]
    # Without `ticket_tasks` the per-phase shape below stays: the card-branch tests pin it.
    ticket, last = granularity(phase_record) == "initiative" and bool(ticket_tasks), bool(phase_record.get("last_phase"))
    if not repo:
        return [{"kind": "refuse", "reason": f"card-mode land onto {branch} needs the repo name; none was given"}]
    applied = [{**s, "branch": branch} for s in card_commit_steps(phase_record.get("initiative"), repo, task_records)]
    done = {s["task"] for s in applied}
    approved = [r.get("task") for r in task_records if r.get("status") == "approved"]
    if approved and not done:
        return [{"kind": "refuse", "reason": f"{', '.join(approved)}: approved, but none belongs to repo {repo}; nothing applied to {branch}"}]
    if not done and not (ticket and last):
        return [{"kind": "note", "reason": f"phase {phase_record.get('phase')}: no approved task to apply to {branch}"}]
    if ticket:
        # The ticket checks run once, in the land that opens the ticket PR (the last phase), not in every phase land.
        gate = ticket_checks_steps(repo_facts or {}, branch) if last else []
        build = [*applied, checks_step(repo_facts or {}, branch), *gate, {"kind": "push", "branch": branch}]
        marks = ticket_done(ticket_tasks, {*done, *ticket_present, *ticket_unrecorded}, ticket_unrecorded)
        held = {t for s in marks if s["kind"] == "note" for t in s["unmerged"]}
        pr = card_pr_steps(phase_record.get("initiative"), {repo: {"task_ids": [t for t in ticket_tasks if t not in held],
                                                                 "default_branch": default_branch}})
        if not last:
            return build
        if not pr:
            return [*build, *marks]
        return [*build, *pr, {"kind": "wait_checks", "branch": branch},
                {"kind": "merge", "squash": True, "delete_branch": True, "branch": branch,
                 "default_branch": default_branch, "subject": pr[0]["title"]}, *marks]
    skipped = [i.get("id") for i in items if i.get("status") == "approved" and i.get("id") not in done]
    left = [{"kind": "note", "unmerged": skipped,
             "reason": f"{', '.join(skipped)}: approved item not applied to {branch}; left in current state"}] if skipped else []
    # `last_phase` is set by the caller on the initiative's final phase; the card finds the PR by its head branch.
    pr = card_pr_steps(phase_record.get("initiative"), {repo: {"task_ids": [s["task"] for s in applied],
                                                             "default_branch": default_branch}}) if last else []
    return [
        *applied,
        checks_step(repo_facts or {}, branch),
        {"kind": "push", "branch": branch},
        *[{"kind": "mark_done", "task": s["task"]} for s in applied],
        *left,
        *pr,
    ]


def _ticket_branch_plan(phase_record: dict[str, Any], items: list[dict[str, Any]], task_records: list[dict[str, Any]],
                        records: Mapping[str, dict[str, Any]], repo_facts: dict[str, Any] | None, default_branch: str,
                        pr_facts: dict[str, Any] | None, *, phases: Sequence[str], branches: Mapping[str, list[str]] | None,
                        repo: str, run_hosts: Mapping[str, str] | None, fetched_missing: Collection[str],
                        task_facts: Sequence[Mapping[str, Any]] | None, already_merged: bool, ticket_tasks: Sequence[str],
                        ticket_present: Collection[str], ticket_records: Sequence[dict[str, Any]],
                        ticket_unrecorded: Collection[str]) -> list[dict[str, Any]]:
    """A ticket phase land into the one `pr/<initiative>` branch. A first phase whose branch has no tip plans
    `pr_branch_cut` (`if_missing`); a later phase finding no branch refuses. Then `merge_phase` (or a gather of each task, flagged `keep_branch` so the executor
    keeps the earlier phases), checks, push, `pr_create` (first phase, no open PR) or `pr_update`, then
    `mark_merged` per task and `phase_done`. The last phase adds `pr_ready`, `wait_checks`, `merge`, `clean_phase`
    and `ticket_done`'s marks. No per-phase pull request is opened. `pr_facts` with a `pr` is an open PR found
    by its head branch; its number rides on the update and the wait."""
    run, phase, initiative = phase_record.get("run"), phase_record.get("phase"), phase_record.get("initiative")
    phase_branch, pr_branch = f"epic/{initiative}/{phase}", pr_branch_name(initiative)
    last = pr_stage(phase, phases) == "ready"
    approved_ids = [r.get("task") for r in task_records if r.get("status", "approved") == "approved"]
    if not [r for r in task_records if r.get("status") != "dropped"] and all(i.get("status") in ("done", "dropped") for i in items):
        return [{"kind": "note", "reason": f"phase {phase} done: every item is done or dropped, nothing to land"}]
    if last and already_merged:
        return [{"kind": "clean_phase", "run": run, "phase_branch": phase_branch, "pr_branch": pr_branch,
                 "tasks": [r.get("task") for r in task_records if r.get("status") != "dropped"]},
                *ticket_done(ticket_tasks, set(ticket_tasks), ticket_unrecorded)]
    carried = [*dict.fromkeys([*approved_ids, *sorted(ticket_present), *ticket_unrecorded])]
    refusal = carry_refusal(carried, ticket_tasks) if last else None
    if refusal is not None:
        return refusal
    quarantined = [i.get("status") == "quarantined" or (records.get(i.get("id")) or {}).get("status") == "quarantined" for i in items]
    if not phase_merge_ready("ticket", [i.get("status") for i in items], quarantined):
        return [{"kind": "refuse", "reason": f"phase {phase} is not ready to merge into {pr_branch}"}]
    facts = pr_facts or {}
    open_pr = None
    if facts.get("pr") is not None:
        decision = phase_resume(pr_facts, pr_branch)
        if decision["kind"] == "refuse":
            return [{"kind": "refuse", "reason": decision["reason"]}]
        open_pr = decision["pr"]
    known = branches or {}
    statuses = task_gather_status(task_facts or [])
    missing = [t for t, s in statuses.items() if s == "missing"]
    if missing:
        return [{"kind": "refuse", "reason": f"{', '.join(missing)}: no agent branch and no build.patch to gather the work from; nothing merged"}]
    lacking = [records[t] for t, s in statuses.items() if s.startswith("gather_") and t in records]
    from_branch = {f"agents/{records[t].get('run')}/{t}": [] for t, s in statuses.items() if s == "gather_from_branch" and t in records}
    approved = [records[i["id"]] for i in items if i.get("status") == "approved" and i.get("id") in records]
    merge = {"kind": "merge_phase", "branch": phase_branch, "onto": pr_branch, "phase": phase,
             "message": f"epic {initiative}: merge phase {phase}"}

    def gather_for(recs: list[dict[str, Any]], seen: Mapping[str, list[str]]) -> list[dict[str, Any]]:
        steps = [s for r in recs for s in _gather_one(r, seen, run_hosts or {}, fetched_missing, repo=repo, initiative=initiative,
                                                       pr_branch=pr_branch, default_branch=default_branch)]
        return [{**s, "keep_branch": True} if "onto" in s else s for s in steps]

    if lacking:
        build = [*([merge] if "already_present" in statuses.values() else []), *gather_for(lacking, {**known, **from_branch})]
    elif approved and phase_branch in known and not known[phase_branch]:
        build = gather_for(approved, known)
    else:
        build = [merge]
    if any(s["kind"] == "refuse" for s in build):
        return [next(s for s in build if s["kind"] == "refuse")]
    first = phase == phases[0]
    gone = pr_facts is not None and facts.get("local_tip") is None and facts.get("remote_tip") is None
    if gone and not first:
        return [{"kind": "refuse", "reason": f"{pr_branch} does not exist, so the work of the phases before {phase} is not on it; land the first phase again"}]
    # The cut creates the branch only when it is missing: a branch with a tip, or an open PR, is reused as it is.
    cut = first and facts.get("local_tip") is None and facts.get("remote_tip") is None and open_pr is None
    title = phase_pr_title(initiative, phase, phase_record.get("initiative_title", ""))
    body = phase_pr_body(phase_record, [*ticket_records, *task_records])
    pinned = {"pr": open_pr} if open_pr is not None else {}
    pr_steps = ([{"kind": "pr_create", "title": title, "body": body, "head": pr_branch, "base": default_branch, "draft": True}]
                if open_pr is None and first else
                [{"kind": "pr_update", "head": pr_branch, "base": default_branch, "title": title, "body": body, **pinned}])
    tail = [
        {"kind": "wait_checks", "branch": pr_branch, **pinned},
        {"kind": "merge", "squash": True, "delete_branch": True, "branch": pr_branch, "default_branch": default_branch,
         "subject": f"epic {initiative}: {phase}"},
        {"kind": "clean_phase", "run": run, "phase_branch": phase_branch, "pr_branch": pr_branch, "tasks": approved_ids},
        *ticket_done(ticket_tasks, set(carried), ticket_unrecorded),
    ] if last else []
    return [
        *([{"kind": "pr_branch_cut", "branch": pr_branch, "from": default_branch, "phase": phase, "if_missing": True}] if cut else []),
        *build,
        checks_step(repo_facts or {}, pr_branch),
        *(ticket_checks_steps(repo_facts or {}, pr_branch) if last else []),
        {"kind": "push", "branch": pr_branch},
        *pr_steps,
        *([{"kind": "pr_ready", "head": pr_branch, **pinned}] if last else []),
        *[{"kind": "mark_merged", "task": t, "merged": True} for t in approved_ids],
        {"kind": "phase_done", "initiative": initiative, "phase": phase, "pr_branch": pr_branch},
        *tail,
    ]


def _phase_plan(phase_record: dict[str, Any], items: list[dict[str, Any]], task_records: list[dict[str, Any]],
                repo_facts: dict[str, Any] | None, default_branch: str = "main",
                pr_facts: dict[str, Any] | None = None, *, branches: Mapping[str, list[str]] | None = None,
                repo: str = "", run_hosts: Mapping[str, str] | None = None,
                fetched_missing: Collection[str] = (),
                task_facts: Sequence[Mapping[str, Any]] | None = None,
                already_merged: bool = False, ticket_tasks: Sequence[str] = (),
                ticket_present: Collection[str] = (),
                ticket_records: Sequence[dict[str, Any]] = (),
                ticket_unrecorded: Collection[str] = (),
                phases: Sequence[str] = ()) -> list[dict[str, Any]]:
    """§1's phase step list, or a one-step `refuse` from `phase_landable`. The
    phase branch is squashed onto a fresh `default_branch` in its own PR
    branch, the way a task land builds its PR, since a coxswain repo requires
    an up-to-date branch to merge and a phase branch cut from an older main
    cannot land as it is; `checks` and everything after run off that PR
    branch, not the phase branch itself. `pr_facts` (see `phase_resume`) lets
    a rerun pick up an open PR or rebuild a stale local branch. When
    `branches` lists the phase branch with no commits over `default_branch`
    and an approved task is unlanded, the squash is replaced by a gather of
    each task's own commit or `build.patch`; a branch absent from `branches`
    is unknown and squashes. `task_facts` (see `task_gather_status`), one per
    approved task, makes the plan refuse naming every `missing` task, and
    gathers each task the phase branch lacks onto the PR branch after the
    squash of the ones it holds. `already_merged` is a fact the caller
    supplies (the phase PR is merged, or its commit is on `default_branch` by
    patch-id); nothing is read here. When set, the plan is `clean_phase`
    then one `mark_done` per landed task, with no build or merge steps."""
    records = {r.get("task"): r for r in task_records}
    # Merged already, an approved item with no local record is marked in the store, not refused for lacking one.
    unrecorded_items = [i.get("id") for i in items if already_merged and i.get("status") == "approved" and i.get("id") not in records]
    # With `phases`, a ticket land merges each phase into one `pr/<initiative>` branch; without it the squash shape below stays.
    branch_ticket = granularity(phase_record) == "initiative" and bool(ticket_tasks) and bool(phases) and not phase_record.get("card_branch")
    refusal = phase_landable([i for i in items if i.get("id") not in unrecorded_items], records, quarantine=branch_ticket)
    if refusal is not None:
        return [{"kind": "refuse", "reason": refusal}]
    if phase_record.get("card_branch"):
        return _card_phase_plan(phase_record, items, task_records, repo_facts, repo, default_branch, ticket_tasks, ticket_present,
                                ticket_unrecorded)
    if branch_ticket:
        return _ticket_branch_plan(phase_record, items, task_records, records, repo_facts, default_branch, pr_facts, phases=phases,
                                   branches=branches, repo=repo, run_hosts=run_hosts, fetched_missing=fetched_missing,
                                   task_facts=task_facts, already_merged=already_merged, ticket_tasks=ticket_tasks,
                                   ticket_present=ticket_present, ticket_records=ticket_records, ticket_unrecorded=ticket_unrecorded)
    ticket = granularity(phase_record) == "initiative" and bool(ticket_tasks)
    if ticket and not phase_record.get("last_phase"):
        return [{"kind": "note", "reason": f"phase {phase_record.get('phase')}: ticket granularity, the one pull request opens at the last phase"}]
    run, phase, initiative = phase_record.get("run"), phase_record.get("phase"), phase_record.get("initiative")
    phase_branch = f"epic/{initiative}/{phase}"
    pr_branch = f"pr/{initiative}--{phase}"
    landed_tasks = [r.get("task") for r in task_records if r.get("status") != "dropped"]
    # A `note`, not a phase-level `mark_done`: cli `_land_enrich` reads `step["task"]` on every mark_done.
    if not landed_tasks and all(item.get("status") in ("done", "dropped") for item in items):
        return [{"kind": "note", "reason": f"phase {phase} done: every item is done or dropped, nothing to land"}]
    approved_ids = {r.get("task") for r in task_records if r.get("status", "approved") == "approved"}

    def carried_in(merged: Collection[str]) -> set[str]:
        return (set(merged) & approved_ids) | set(ticket_present) | set(ticket_unrecorded)

    def finish(merged: Collection[str]) -> list[dict[str, Any]]:
        return ticket_done(ticket_tasks, carried_in(merged), ticket_unrecorded) if ticket else carried_done(landed_tasks)

    def unaccounted(merged: Collection[str]) -> list[dict[str, Any]] | None:
        """A refuse when a task the PR carries has no mark_done: one outside the ticket's tasks, or a landed task outside `merged`."""
        if ticket:
            return carry_refusal(sorted(carried_in(merged)), ticket_tasks)
        return carry_refusal(landed_tasks, merged)

    if already_merged:
        clean = {"kind": "clean_phase", "run": run, "phase_branch": phase_branch, "pr_branch": pr_branch,
                 "tasks": landed_tasks}
        if ticket:
            return [clean, *ticket_done(ticket_tasks, set(ticket_tasks), ticket_unrecorded)]
        return [clean, *finish(landed_tasks), *carried_done(unrecorded_items, unrecorded_items)]
    squash_step = {"kind": "squash_phase", "branch": phase_branch, "onto": pr_branch, "from": default_branch,
                   "subject": f"epic {initiative}: {phase}"}
    approved_files = _phase_approved_files(items, records)
    if approved_files:
        squash_step = {**squash_step, "approved_files": approved_files}
    decision = phase_resume(pr_facts, pr_branch)
    if decision["kind"] == "refuse":
        return [{"kind": "refuse", "reason": decision["reason"]}]
    if decision["kind"] == "recreate":
        squash_step = {**squash_step, "worktree_flag": "-B"}
    def after_pr(merged: Collection[str]) -> list[dict[str, Any]]:
        return [
            {"kind": "merge", "squash": True, "delete_branch": True, "branch": pr_branch,
             "default_branch": default_branch, "subject": f"epic {initiative}: {phase}"},
            {"kind": "clean_phase", "run": run, "phase_branch": phase_branch, "pr_branch": pr_branch, "tasks": landed_tasks},
            *finish(merged),
        ]
    if decision["kind"] == "wait":
        # No gather is planned on a resume, so no merged set exists: every landed task is marked, as before.
        # `origin/` ref: the local branch may be absent, and its tip is the PR head when present.
        refusal = unaccounted(landed_tasks)
        if refusal is not None:
            return refusal
        return [{"kind": "wait_checks", "branch": f"origin/{pr_branch}", "pr": decision["pr"]}, *after_pr(landed_tasks)]
    known = branches or {}
    approved = [records[i["id"]] for i in items if i.get("status") == "approved" and i.get("id") in records]
    statuses = task_gather_status(task_facts or [])
    missing = [t for t, s in statuses.items() if s == "missing"]
    if missing:
        return [{"kind": "refuse", "reason": f"{', '.join(missing)}: no agent branch and no build.patch to gather the work from; nothing merged"}]
    present = [t for t, s in statuses.items() if s == "already_present"]
    lacking = [records[t] for t, s in statuses.items() if s.startswith("gather_") and t in records]
    from_branch = {f"agents/{records[t].get('run')}/{t}": [] for t, s in statuses.items() if s == "gather_from_branch" and t in records}

    def gather_for(recs: list[dict[str, Any]], seen: Mapping[str, list[str]]) -> list[dict[str, Any]]:
        return [s for r in recs for s in _gather_one(
            r, seen, run_hosts or {}, fetched_missing, repo=repo, initiative=initiative, pr_branch=pr_branch,
            default_branch=default_branch)]

    if task_facts is not None:
        squash_step = {**squash_step, "tasks": present}
    if lacking:
        gather = gather_for(lacking, {**known, **from_branch})
        refused = [s for s in gather if s["kind"] == "refuse"]
        if refused:
            return refused[:1]
        if present:
            held = [f for f in squash_step.get("approved_files", []) if f["task"] in present]
            held_step = {**{k: v for k, v in squash_step.items() if k != "approved_files"}, **({"approved_files": held} if held else {})}
            build_steps = [{"kind": "pick_branch", "branch": phase_branch, "commit_subject": f"phase {phase}"}, held_step, *gather]
        else:
            first = next(i for i, s in enumerate(gather) if s["kind"] != "fetch")
            flagged = {"worktree_flag": "-B"} if decision["kind"] == "recreate" else {}
            build_steps = [{**s, **flagged} if i == first else s for i, s in enumerate(gather)]
    elif approved and phase_branch in known and not known[phase_branch]:
        gather = gather_for(approved, known)
        refused = [s for s in gather if s["kind"] == "refuse"]
        if refused:
            return refused[:1]
        first = next(i for i, s in enumerate(gather) if s["kind"] != "fetch")
        flagged = {"worktree_flag": "-B"} if decision["kind"] == "recreate" else {}
        build_steps = [{**s, **flagged} if i == first else s for i, s in enumerate(gather)]
    else:
        build_steps = [{"kind": "pick_branch", "branch": phase_branch, "commit_subject": f"phase {phase}"}, squash_step]
    # Without `task_facts` there is no merged set, so every landed task is carried. A task with no fact (not
    # approved) was never gathered and is carried too.
    merged = set(landed_tasks) if task_facts is None else phase_landed_tasks(build_steps) | (set(landed_tasks) - set(statuses))
    refusal = unaccounted(merged)
    if refusal is not None:
        return refusal
    return [
        *build_steps,
        checks_step(repo_facts or {}, pr_branch),
        *(ticket_checks_steps(repo_facts or {}, pr_branch) if ticket else []),
        {"kind": "push", "branch": pr_branch},
        {"kind": "pr_create", "title": phase_pr_title(initiative, phase, phase_record.get("initiative_title", "")),
         "body": phase_pr_body(phase_record, [*ticket_records, *task_records] if ticket else task_records), "head": pr_branch, "base": default_branch},
        {"kind": "wait_checks", "branch": pr_branch},
        *after_pr(merged),
    ]


def gate_steps(steps: Sequence[dict[str, Any]], level: str, land_at: str | None = None,
               ticket_tasks: Sequence[str] = ()) -> list[dict[str, Any]]:
    """`phase`/`epic` keep a `merge` that carries a `target` and stop before one that does not; `land_at` picks the rung via `gate_level`; an unknown level is `ticket`, never `full`."""
    steps, level = list(steps), gate_level(level, land_at)
    if level == "full" or any(s["kind"] == "refuse" for s in steps):
        return steps
    if level in ("phase", "epic"):
        idx = next((i for i, s in enumerate(steps) if s["kind"] == "merge" and "target" not in s), None)
        note = [{"kind": "note", "reason": f"gate: {len(ticket_tasks)} ticket tasks are marked done after the pull request merges"}
                ] if is_initiative(land_at) and ticket_tasks else []
        return steps if idx is None else [*steps[:idx], *note]
    if any(s["kind"] == "phase_done" for s in steps):
        # Every `_ticket_branch_plan` plan ends its phase with `phase_done`, whether it merged or gathered. It stops
        # before the final merge, and before the wait on that PR's checks.
        merge = next((i for i, s in enumerate(steps) if s["kind"] == "merge" and "target" not in s), None)
        if merge is None:
            return steps
        stop = merge - 1 if steps[merge - 1]["kind"] == "wait_checks" else merge
        return [*steps[:stop], {"kind": "note", "reason": "gate: ticket — the pull request is ready and the final merge waits for a person"}]
    # A phase land resumed at an open PR has no `pr_create`; its `wait_checks` carries the PR number.
    idx = next((i for i, s in enumerate(steps) if s["kind"] == "pr_create" or (s["kind"] == "wait_checks" and "pr" in s)), None)
    note = {"kind": "note", "reason": "gate: ticket — the pull request is open and waits for a person"}
    return steps if idx is None else steps[: idx + 1] + [note]


def gate_stop(planned: Sequence[dict[str, Any]], gated: Sequence[dict[str, Any]], level: str, pr: str) -> str | None:
    """None when every planned step is kept, else the one line saying where the gate stopped and which PR stays open."""
    kept = [s for s in gated if s["kind"] != "note"]
    if len(kept) == len([s for s in planned if s["kind"] != "note"]) or any(s["kind"] == "refuse" for s in gated):
        return None
    last = kept[-1]["kind"]
    return f"gate: {level} stopped after {last}; pull request {pr or '(none opened)'} left open, unmerged"


def _ahead_choice(scratch_branch: str, phase_branch: str | None, branches: Mapping[str, Sequence[str]],
                  phase_done: bool) -> tuple[str, Sequence[str]] | None:
    """The first candidate with one or more subjects: phase branch first for a done phase, else scratch first."""
    order = (phase_branch, scratch_branch) if phase_done else (scratch_branch, phase_branch)
    return next(((b, branches[b]) for b in order if b and branches.get(b)), None)


def _refusal_reason(default_branch: str, candidates: Sequence[str | None], branches: Mapping[str, Sequence[str]],
                    compared: Mapping[str, Mapping[str, int]] | None) -> str:
    """`no branch is ahead of <base>` plus each candidate's counts; raw ahead and behind only where `compared` has them."""
    def entry(b: str) -> str:
        unique, raw = len(branches.get(b, [])), (compared or {}).get(b)
        return (f"{b}: ahead {raw['ahead']}, behind {raw['behind']}, {unique} not already on {default_branch}"
                if raw else f"{b}: {unique} not already on {default_branch}")
    return f"no branch is ahead of {default_branch}: " + "; ".join(entry(b) for b in candidates if b)


def land_plan(record: dict[str, Any], branches: dict[str, list[str]], default_branch: str,
              repo_facts: dict[str, Any] | None = None, *, items: list[dict[str, Any]] | None = None,
              task_records: list[dict[str, Any]] | None = None, tracker: str | None = None,
              issue: str | None = None, phase_pr: dict[str, Any] | None = None, repo: str = "",
              run_hosts: Mapping[str, str] | None = None,
              fetched_missing: Collection[str] = (),
              task_facts: Sequence[Mapping[str, Any]] | None = None, scope: str = "all",
              reference: str | None = None, already_merged: bool = False,
              ticket_tasks: Sequence[str] = (), ticket_present: Collection[str] = (),
              ticket_records: Sequence[dict[str, Any]] = (), phase_done: bool = False,
              compared: Mapping[str, Mapping[str, int]] | None = None,
              ticket_unrecorded: Collection[str] = (),
              phases: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The ordered steps to land `record`, or a one-step `refuse`. Task mode lands the newest subject of the
    first branch with any subject: the phase branch first when `phase_done`, else the scratch branch first.
    `compared` maps a branch to its raw `ahead`/`behind` rev-list counts, quoted in a refusal. Phase mode only: `already_merged` plans
    just the clean and the mark_done steps. Under a `scope` other than `all` (task mode),
    the PR body carries `reference` alone, nothing syncs before the PR, and the closing sync runs only for a
    task whose `issue` was set by hand. Phase mode carries no issue line under any scope (`phase_pr_body`
    has none), so `Part of` and the last-task `Closes` apply to task-mode lands only. Phase mode
    (`items` given) lands the whole phase off its own branch instead of one
    task's commit; `phase_pr` there is `phase_resume`'s facts, `task_facts`
    is `task_gather_status`'s input, and `repo`,
    `run_hosts` (run to host) and `fetched_missing` (task ids a fetch found
    nothing for) feed the gather. Task mode only: `tracker` other than None or `none` adds a
    closing `route_sync`, and `issue` adds `Closes #n` to the PR body; `none`
    plans neither and says so in a note. At ticket, `ticket_records` are the earlier phases' task records whose
    work is in the PR; their blocks head the one PR body. `ticket_unrecorded` names ticket tasks the store holds
    approved with no task record here; a ticket plan treats them as in the PR and marks each with `recorded: False`.
    `phases` (the initiative's phases in order) switches a ticket phase land to the one `pr/<initiative>` branch."""
    if items is not None:
        return _phase_plan(record, items, task_records or [], repo_facts, default_branch, phase_pr, branches=branches,
                           repo=repo, run_hosts=run_hosts, fetched_missing=fetched_missing, task_facts=task_facts,
                           already_merged=already_merged, ticket_tasks=ticket_tasks, ticket_present=ticket_present,
                           ticket_records=ticket_records, ticket_unrecorded=ticket_unrecorded, phases=phases)
    if _proposal(record, "draft_pr_create") is None:
        return [{"kind": "refuse", "reason": "no draft_pr_create proposal in record"}]
    refusal = _approved(record)
    if refusal is not None:
        return [{"kind": "refuse", "reason": refusal}]

    run, task, phase = record.get("run"), record.get("task"), record.get("phase")
    initiative = record.get("initiative")
    carried = [task] if task else None
    refusal = carry_refusal(carried, {task})
    if refusal is not None:
        return refusal
    scratch_branch = f"agents/{run}/{task}"
    phase_branch = f"epic/{initiative}/{phase}" if initiative else None

    choice = _ahead_choice(scratch_branch, phase_branch, branches, phase_done)
    if choice is None:
        found = {b: len(subs) for b, subs in branches.items()}
        return [{"kind": "refuse", "reason": _refusal_reason(default_branch, (scratch_branch, phase_branch), branches, compared),
                 "found": found}]
    chosen, subjects = choice
    subject, commits = subjects[0], len(subjects)

    pr_branch = f"pr/{task}"
    draft = _proposal(record, "draft_pr_create")
    mirrored = tracker != "none"
    if not mirrored:
        sync = [{"kind": "note", "reason": "route sync skipped: tracker is none"}]
    elif tracker is None:
        sync = []
    elif scope != "all" and issue is None:
        sync = [{"kind": "note", "reason": f"route sync skipped: tracker_scope {scope} keeps tasks in the store"}]
    else:
        sync = [{"kind": "route_sync", "item": task}]
    # No issue yet: sync before the PR opens so its body can say `Closes #n`. A narrowed scope never makes one.
    presync = ([{"kind": "route_sync", "item": task, "before": "pr_create"}]
               if mirrored and tracker is not None and issue is None and scope == "all" else [])
    body = (pr_body(record, None, reference if mirrored else None) if scope != "all"
            else pr_body(record, issue if mirrored else None))
    return [
        {"kind": "pick_branch", "branch": chosen, "commit_subject": subject, "commits": commits},
        {"kind": "cherry_pick", "branch": chosen, "commit_subject": subject, "onto": pr_branch, "from": default_branch,
         "commits": commits},
        checks_step(repo_facts or {}, pr_branch),
        {"kind": "push", "branch": pr_branch},
        *presync,
        {"kind": "pr_create", "title": draft.get("title", subject), "body": body,
         "head": pr_branch, "base": default_branch},
        {"kind": "wait_checks", "branch": pr_branch},
        {"kind": "merge", "squash": True, "delete_branch": True, "branch": pr_branch,
         "default_branch": default_branch, "subject": subject},
        {"kind": "clean", "run": run, "task": task, "branch": scratch_branch},
        *carried_done([task], ticket_unrecorded),
        *sync,
    ]


def with_issue(steps: Sequence[dict[str, Any]], record: dict[str, Any], issue: str | int | None) -> list[dict[str, Any]]:
    """`steps` with the `pr_create` body rebuilt to carry `Closes #n` for `issue`; unchanged for no issue."""
    if not issue:
        return list(steps)
    return [{**s, "body": pr_body(record, issue)} if s["kind"] == "pr_create" else s for s in steps]


def resume_decision(expected_tree: str, local_tree: str | None, remote_tree: str | None,
                    open_prs: Sequence[int]) -> dict[str, Any]:
    """Whether an existing `pr/<task>` branch can be reused. `fresh` when neither side exists, `adopt` of the
    first open PR whatever the trees, `resume` when every existing side carries `expected_tree`, else a `refuse`."""
    sides = {"local": local_tree, "remote": remote_tree}
    existing = {side: tree for side, tree in sides.items() if tree is not None}
    if not existing:
        return {"kind": "fresh"}
    if open_prs:
        return {"kind": "adopt", "pr": open_prs[0]}
    differing = [f"{side} tree {tree} differs from the cherry-picked tree {expected_tree}"
                 for side, tree in existing.items() if tree != expected_tree]
    if differing:
        return {"kind": "refuse", "reason": "; ".join(differing)}
    return {"kind": "resume", "local": local_tree is not None, "remote": remote_tree is not None}


def _backup_refs(branch: str, local_tip: str | None, remote_tip: str | None) -> dict[str, str]:
    """Backup ref to the tip it must keep: `refs/backup/<branch>` for the local tip, `refs/backup/origin/<branch>`
    for the remote tip, each only when that tip exists."""
    return {**({f"refs/backup/{branch}": local_tip} if local_tip else {}),
            **({f"refs/backup/origin/{branch}": remote_tip} if remote_tip else {})}


def leftover_gate(branch: str, *, local_tip: str | None, is_ancestor_of_main: bool | None,
                  unique_commits: int | None, remote_exists: bool, open_pr_number: int | None) -> dict[str, Any] | None:
    """Decision from facts alone, no tree needed: refuse on an open PR (`reason` names `#<n>`), then on commits
    not on main (`reason` names the count and the branch), recreate (`old_tip`, nothing to back up) for a local
    ancestor of main with no remote and no open PR, else None."""
    if open_pr_number is not None:
        return {"kind": "refuse", "reason": f"{branch} has open pull request #{open_pr_number}"}
    if unique_commits:
        return {"kind": "refuse", "reason": f"{branch} has {unique_commits} commits not on main"}
    if local_tip is not None and is_ancestor_of_main and not remote_exists:
        return {"kind": "recreate", "old_tip": local_tip}
    return None


def leftover_branch_decision(branch: str, remote_exists: bool, open_pr: bool, local_tree: str | None,
                             expected_tree: str | None, *, local_commit: str | None, remote_commit: str | None = None,
                             remote_tree: str | None = None, is_ancestor_of_main: bool | None = None,
                             unique_commits: int | None = None, open_pr_number: int | None = None) -> dict[str, Any]:
    """What a land does about a leftover `pr/<initiative>--<phase>`. `leftover_gate` rules come first and need no
    `expected_tree`; its kinds are `refuse` (`reason`) and `recreate` (`old_tip`). Otherwise only an open PR refuses.
    Without one, a local tree that differs, a pushed `remote_tree` that differs (main moved), or a remote-only
    branch yields `back_up_and_rebuild`, carrying both tips and `backup_refs` (ref to tip); land never deletes them."""
    gated = leftover_gate(branch, local_tip=None if open_pr else local_commit, is_ancestor_of_main=is_ancestor_of_main,
                          unique_commits=unique_commits, remote_exists=remote_exists, open_pr_number=open_pr_number)
    if gated is not None:
        return gated
    if local_tree is None and not remote_exists:
        return {"kind": "fresh"}
    remote_stale = expected_tree is not None and remote_tree is not None and remote_tree != expected_tree
    if expected_tree is not None and local_tree == expected_tree and not remote_stale:
        return {"kind": "reuse"}
    if open_pr:
        differing = [f"{side} tree {tree} differs from the cherry-picked tree {expected_tree}"
                     for side, tree in (("local", local_tree), ("remote", remote_tree))
                     if expected_tree is not None and tree is not None and tree != expected_tree]
        return {"kind": "refuse", "reason": "; ".join(differing) or f"{branch} has an open pull request"}
    refs = _backup_refs(branch, local_commit, remote_commit)
    return {"kind": "back_up_and_rebuild", "backup_ref": f"refs/backup/{branch}" if local_commit else None,
            "old_commit": local_commit, "local_tip": local_commit, "remote_tip": remote_commit, "backup_refs": refs}


def push_lease(remote_tip: str | None, prs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How the `push` step publishes a branch, from the remote tip and the `{"number", "state"}` of every PR headed
    there. `lease` (`tip`, `prs`) only when the remote branch exists and every PR on it is `closed`: a rebuild left it
    behind, and only that tip may be replaced. Anything else is `plain`, which git refuses unless it fast-forwards;
    an open, merged or unknown PR, or none at all, is never evidence enough to rewrite a remote branch."""
    if remote_tip and prs and all(p.get("state") == "closed" for p in prs):
        return {"kind": "lease", "tip": remote_tip, "prs": [p["number"] for p in prs]}
    return {"kind": "plain"}


def resume_steps(steps: Sequence[dict[str, Any]], decision: dict[str, Any], pr_branch: str) -> list[dict[str, Any]]:
    """`steps` with the `cherry_pick` replaced by a `reuse_branch` on a `resume`. On an `adopt` the steps that build
    and open the PR are dropped and `wait_checks` is pinned to the open PR; every other step keeps its place."""
    if decision["kind"] == "adopt":
        built = ("cherry_pick", "checks", "push", "pr_create")
        return [{**s, "pr": decision["pr"], "branch": f"origin/{pr_branch}"} if s["kind"] == "wait_checks" else s
                for s in steps if s["kind"] not in built and s.get("before") != "pr_create"]
    if decision["kind"] != "resume":
        return list(steps)
    reuse = {"kind": "reuse_branch", "branch": pr_branch, "local": decision["local"], "remote": decision["remote"]}
    return [reuse if s["kind"] == "cherry_pick" else s for s in steps]


def recover_record(path: str, ticket: str | None, initiative: str) -> dict[str, Any]:
    """The `{run, task, phase, initiative}` record `recover_plan` expects, read
    off the task file's path `<run>/tasks/<phase>/<task>.json`, or a one-step
    `refuse` dict. The record's `run_id` is a truncated composite and is never
    read. A path of another shape, or a `ticket` that is not the filename, refuses."""
    parts = PurePath(path).parts
    if len(parts) < 4 or parts[-3] != "tasks" or not parts[-1].endswith(".json"):
        return {"kind": "refuse", "reason": f"{path}: not a task record path, expected <run>/tasks/<phase>/<task>.json"}
    run, phase, task = parts[-4], parts[-2], parts[-1][: -len(".json")]
    if ticket != task:
        return {"kind": "refuse", "reason": f"{path}: record ticket {ticket!r} disagrees with its filename {task!r}"}
    return {"run": run, "task": task, "phase": phase, "initiative": initiative}


def recover_plan(record: dict[str, Any], branches: dict[str, list[str]]) -> list[dict[str, Any]]:
    """The one-step merge that recovers `record`'s task commit into its phase
    branch, a one-step `already_recovered`, or a one-step `refuse`. Unlike
    `land_plan`, the target is always the phase branch, never a default
    branch: this is the remedy for a merge the harness itself refused
    because the task escalated to `self_modification`, not an ordinary land.
    `branches` maps each candidate branch this function names to the commit
    subjects the edge found ahead of the phase branch — empty means the
    branch's commit is already an ancestor (already recovered); a candidate
    absent from `branches` was not found in the repo at all."""
    run, task, phase = record.get("run"), record.get("task"), record.get("phase")
    initiative = record.get("initiative")
    if not initiative or not phase:
        return [{"kind": "refuse", "reason": f"{task}: record names no initiative/phase, cannot resolve a phase branch"}]
    phase_branch = f"epic/{initiative}/{phase}"
    candidates = [f"agents/{run}/{task}", f"epic/{initiative}/{phase}--{task}"]
    for candidate in candidates:
        if candidate not in branches:
            continue
        subjects = branches[candidate]
        if not subjects:
            return [{"kind": "already_recovered", "branch": phase_branch,
                     "reason": f"{candidate} has no commits ahead of {phase_branch}"}]
        if len(subjects) == 1:
            return [{"kind": "merge", "source": candidate, "target": phase_branch, "commit_subject": subjects[0]}]
        return [{"kind": "refuse",
                 "reason": f"{candidate} is {len(subjects)} commits ahead of {phase_branch}, expected exactly one"}]
    return [{"kind": "refuse", "reason": f"no candidate branch found for {task}: tried {', '.join(candidates)}"}]


_NO_CHECKS = "no checks reported"
# The REST poll's own "still running" marker: a returncode `gh` and `git` never exit with (EX_TEMPFAIL),
# so `gh pr checks` output from the release flow cannot read as pending, and no phrase is matched.
PENDING_RC = 75
POLL_ERROR_LIMIT = 5
_FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"})


def behind_warning(base_sha: str | None, commits_behind: int | None, limit: int, branch: str = "main") -> str | None:
    """A warning line when the task's base is more than `limit` commits behind `branch`; None for no recorded base, no count, or within the limit."""
    if base_sha is None or commits_behind is None or commits_behind <= limit:
        return None
    return f"land: warning, the task was built on {base_sha[:8]}, {commits_behind} commits behind {branch} (limit {limit}); the cherry-pick may conflict"


def is_pending(returncode: int) -> bool:
    return returncode == PENDING_RC


def wait_decision(returncode: int, output: str, elapsed_s: float, timeout_s: float) -> str:
    """`green`, `failed`, `retry` or `timeout`. No check registered yet retries until `timeout_s`; checks registered and still running retry with no bound, as `gh pr checks --watch` waited."""
    if returncode == 0:
        return "green"
    if is_pending(returncode):
        return "retry"
    if _NO_CHECKS in output.lower():
        return "retry" if elapsed_s < timeout_s else "timeout"
    return "failed"


def merge_expects_check(output: str) -> bool:
    """Whether a refused merge's output is GitHub's `Required status check "<name>" is expected`; the check name is never matched."""
    return "Required status check" in output and "is expected" in output


def _suite_of(step: str) -> str | None:
    """The suite of a failed step named `pytest <suite>`; None for any other spelling."""
    words = step.split()
    return words[1] if len(words) == 2 and words[0] == "pytest" else None


def _touches_suite(suite: str, changed_paths: Sequence[str]) -> bool:
    """`root` is every path outside `src/`; any other suite is the folder `src/<suite>/`."""
    if suite == "root":
        return any(not path.startswith("src/") for path in changed_paths)
    return any(path.startswith(f"src/{suite}/") for path in changed_paths)


def rerun_scope(failed_steps: Sequence[str], changed_paths: Sequence[str]) -> bool:
    """Whether a failed check earns the one rerun: no failed step, an unattributable step, or no failed suite the change touches."""
    suites = [_suite_of(step) for step in failed_steps]
    return any(suite is None for suite in suites) or not any(
        suite is not None and _touches_suite(suite, changed_paths) for suite in suites
    )


MERGE_UPDATE_ATTEMPTS = 3  # branch updates a land makes for one merge before it stops
_BEHIND_PHRASE = "not up to date with the base branch"


def is_behind_refusal(output: str) -> bool:
    """Whether a failed merge's output says the head branch is behind the base branch."""
    return _BEHIND_PHRASE in output


def behind_stop(attempts: int, last: str) -> str:
    """Why the land stops at the merge once `attempts` branch updates have not been enough; `last` is the final refusal."""
    return f"still behind the base branch after {attempts} branch updates; last refusal: {last}"


def is_conflict_error(text: str) -> bool:
    """Whether a failed branch update's error says the branch conflicts with its base: gh's "due to conflicts" or GitHub's "merge conflict"."""
    lowered = text.lower()
    return "conflicts" in lowered or "merge conflict" in lowered


def conflict_stop(number: int, detail: str) -> str:
    """Why the land stops when updating pull request `number` fails: a sibling merged first and the branch now conflicts."""
    return f"the base moved under a merged sibling; pull request #{number} now conflicts with it: {detail}"


def lock_stop(holder: str | None, reason: str) -> str:
    """Why the land stops when the merge lock is not taken in time; `holder` is who holds it, `reason` the lock's own answer."""
    if holder:
        return f"the merge lock for this repository is held by {holder}; retrying on the next tick"
    return f"cannot take the merge lock ({reason}); retrying on the next tick"


def rest_checks_argvs(sha: str) -> tuple[list[str], list[str]]:
    """`gh api --paginate` argvs for a commit's check runs and legacy statuses. REST, so off the GraphQL budget; `gh` fills `{owner}/{repo}` from the cwd's remote."""
    base = f"repos/{{owner}}/{{repo}}/commits/{sha}"
    return (["gh", "api", "--paginate", f"{base}/check-runs?per_page=100"],
            ["gh", "api", "--paginate", f"{base}/status?per_page=100"])


def _json_objects(text: str) -> list[dict[str, Any]] | None:
    rest = text.lstrip()
    if not rest:
        return []
    try:
        obj, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        return None
    tail = _json_objects(rest[end:])
    return [obj, *tail] if tail is not None and isinstance(obj, dict) else None


def merge_pages(text: str, key: str) -> dict[str, Any] | None:
    """One body from `gh api --paginate` output, which is the pages' JSON objects back to back: the first page with every page's `key` rows. `None` when unparseable or empty."""
    pages = _json_objects(text)
    if not pages:
        return None
    return {**pages[0], key: [row for page in pages for row in page.get(key) or []]}


_REQUIRED_UNREADABLE = "required checks could not be read; result follows the old rule"


def _noted(returncode: int, output: str, required: tuple[str, ...] | None) -> tuple[int, str]:
    """The poll pair, with the unreadable-required note on its own line when `required` is None."""
    note = [_REQUIRED_UNREADABLE] if required is None else []
    return returncode, "\n".join([*([output] if output else []), *note])


WORKFLOW_ISSUE = "workflow file issue: "
_SUITE_FAILED = frozenset({"startup_failure", "failure"})


def _seen_names(check_runs: dict[str, Any], status: dict[str, Any]) -> set[str]:
    return {r.get("name", "") for r in check_runs.get("check_runs") or []} | {s.get("context", "") for s in status.get("statuses") or []}


def needs_suites(check_runs: dict[str, Any], status: dict[str, Any], required: tuple[str, ...] | None = None) -> bool:
    """Whether the poll shows no check runs or a required context not yet seen, which is when the edge reads the check suites."""
    seen = _seen_names(check_runs, status)
    return not check_runs.get("check_runs") or any(n not in seen for n in required or ())


def _workflow_issue(suites: tuple[dict[str, Any], ...]) -> str | None:
    """The first suite that concluded `startup_failure` or `failure`, as its detail line with the run url; None when none did."""
    bad = [s for s in suites if s.get("conclusion") in _SUITE_FAILED]
    return " ".join(filter(None, [f"{WORKFLOW_ISSUE}{bad[0].get('workflow')}", bad[0].get("url")])) if bad else None


def check_poll_result(check_runs: dict[str, Any], status: dict[str, Any], required: tuple[str, ...] | None = None, *,
                      suites: tuple[dict[str, Any], ...] = (), elapsed_s: float = 0.0, timeout_s: float = 180.0) -> tuple[int, str]:
    """The `(returncode, output)` pair `wait_decision` reads, from the two REST bodies. A failure beats pending, as `--fail-fast` did. Fewer rows than `total_count` is pending, never green. `required` names the base branch's required contexts, each pending until seen; None means the required-checks call was unreadable, so the old rule applies and the output says so. `suites` matter only when `needs_suites`: a failed one fails the poll as a workflow file issue. A required context still unseen at `elapsed_s` >= `timeout_s` fails, named."""
    runs = [(r.get("name", ""), r.get("status"), r.get("conclusion")) for r in check_runs.get("check_runs") or []]
    statuses = [(s.get("context", ""), s.get("state")) for s in status.get("statuses") or []]
    runs_total, statuses_total = check_runs.get("total_count") or 0, status.get("total_count") or 0
    combined_failed = ["combined status"] if status.get("state") in ("failure", "error") else []
    failed = [n for n, _, c in runs if c in _FAILED_CONCLUSIONS] + (
        [n for n, st in statuses if st in ("failure", "error")] or combined_failed)
    unread = ([f"{len(runs)} of {runs_total} check runs read"] if len(runs) < runs_total else []) + (
        [f"{len(statuses)} of {statuses_total} statuses read"] if len(statuses) < statuses_total else [])
    seen = _seen_names(check_runs, status)
    missing = [n for n in required or () if n not in seen]
    pending = [n for n, st, _ in runs if st != "completed"] + [n for n, st in statuses if st == "pending"] + unread + missing
    issue = _workflow_issue(suites) if needs_suites(check_runs, status, required) else None
    if failed:
        return _noted(1, f"failing checks: {', '.join(failed)}", required)
    if issue:
        return _noted(1, issue, required)
    if missing and elapsed_s >= timeout_s:
        return _noted(1, f"required checks never appeared within {timeout_s:.0f}s: {', '.join(missing)}", required)
    if pending:
        return _noted(PENDING_RC, f"checks pending: {', '.join(pending)}", required)
    if not runs and not statuses:
        return _noted(1, _NO_CHECKS, required)
    return _noted(0, "", required)


def poll_backoff_s(errors: int) -> float:
    """Extra seconds to wait after `errors` unreadable polls in a row: 30, 60, 120, 240, capped at 240."""
    return float(min(15 * 2**errors, 240))


def unreadable_poll(errors: int, detail: str) -> tuple[int, str]:
    """A poll whose `gh api` or `git` call failed, `errors` times in a row: pending below `POLL_ERROR_LIMIT`, so one 502 or rate-limit reply does not fail a land; failed at the limit."""
    if errors < POLL_ERROR_LIMIT:
        return PENDING_RC, f"checks unreadable, retrying ({errors}/{POLL_ERROR_LIMIT}): {detail}"
    return 1, f"checks unreadable {errors} polls in a row: {detail}"


@dataclass(frozen=True)
class LeaseRenewal:
    """The repo lease held while checks are awaited: `renew()` renews it, `ttl` is its ttl, `every` overrides the ttl/3 interval."""

    renew: Callable[[], store_cli.LeaseResult]
    ttl: int = 1200
    every: float | None = None


def renew_due(last_renewed: float, now: float, ttl: int, every: float | None = None) -> bool:
    """True once `every` seconds (default ttl/3) have passed since `last_renewed`; both times are on one clock."""
    return now - last_renewed >= (ttl / 3 if every is None else every)


def renewal_stop(result: store_cli.LeaseResult) -> str | None:
    """None when the repo lease renewed, else why the land must stop before the merge; a refusal names the holder."""
    # land.py loads at CLI startup and store_cli pulls in run_store, so import it only when a renewal runs.
    from agent_tools import store_cli

    if isinstance(result, store_cli.LeaseGranted):
        return None
    if isinstance(result, store_cli.LeaseRefused):
        return f"repo lease lost while waiting on checks: held by {result.holder} at epoch {result.epoch}"
    if isinstance(result, store_cli.LeaseError):
        why = result.detail
    elif isinstance(result, store_cli.NotAvailable):
        why = result.reason or "the store is not available"
    else:
        why = "it was released"
    return f"repo lease could not be renewed while waiting on checks: {why}"


def await_checks(poll, timeout_s: float = 180.0, sleep=time.sleep, now=time.monotonic,
                 *, renewal: LeaseRenewal | None = None) -> tuple[bool, str]:
    """`poll() -> (returncode, output)` until green or failed. No check yet means not yet: retry every 15s for `timeout_s`.

    With a `renewal` the repo lease is renewed in this loop, no thread, every ttl/3; a failed renewal ends the wait as failed."""
    started, waiting = now(), False
    renewed = started
    while True:
        rc, output = poll()
        decision = wait_decision(rc, output, now() - started, timeout_s)
        if decision == "retry":
            if not waiting:
                print(f"{output.strip()}; polling every 15s until they finish" if is_pending(rc)
                      else f"no checks reported yet, waiting up to {timeout_s:.0f}s for the first one to appear")
            waiting = True
            if renewal is not None:
                at = now()
                if renew_due(renewed, at, renewal.ttl, renewal.every):
                    stop = renewal_stop(renewal.renew())
                    if stop is not None:
                        return False, stop
                    renewed = at
            sleep(15)
        elif decision == "timeout":
            return False, f"no checks reported within {timeout_s:.0f}s"
        else:
            return decision == "green", "green" if decision == "green" else output.strip()


def issue_closes(issue: str | int | None) -> str | None:
    """`Closes #n` for a bare `n`, `#n`, or a legacy `owner/repo#n` or `owner/n`; the issue lives in the PR's own repo, so an owner is never emitted."""
    m = re.fullmatch(r"(?:[\w.-]+/[\w.-]+#|[\w.-]+/|#)?(\d+)", str(issue or "").strip())
    return f"Closes #{m[1]}" if m else None


def pr_reference(task_issue: str | int | None, initiative_issue: str | int | None, last_task: bool) -> str | None:
    """The issue lines of a PR under a narrowed `tracker_scope`. A task in a synced initiative says `Part of` its
    issue, or `Closes` it when this is the initiative's last task. An operator-linked task issue is also `Closes`.
    None when neither issue exists (a task in an unsynced initiative)."""
    linked = issue_closes(task_issue)
    parent = issue_closes(initiative_issue)
    parent_line = None if parent is None else parent if last_task else parent.replace("Closes", "Part of", 1)
    lines = [line for line in (parent_line, linked) if line]
    return "\n".join(lines) if lines else None


def pr_footer(run: str | None) -> str:
    """The trailing line every PR body ends with; it carries no version, and `run` is appended only when set."""
    footer = "🚣 Built with [coxswain](https://github.com/ppfenning/coxswain)"
    return f"{footer} · run {run}" if run is not None else footer


def pr_body(record: dict[str, Any], issue: str | int | None = None, reference: str | None = None) -> str:
    """The PR description: verdicts, fix-loop attempts, checks, and cost if present, then the issue line:
    `reference` as given when set, else the `Closes` line for `issue`."""
    lines = [f"Run: {record.get('run')}", f"Task: {record.get('task')}"]
    review, arbitration = _verdict(record, "review"), arbitration_verdict(record)
    if review:
        lines.append(f"Review verdict: {review}")
    if arbitration:
        lines.append(f"Arbitration verdict: {arbitration}")
    facts = record.get("change_facts") or {}
    if facts.get("fix_loop_attempts") is not None:
        lines.append(f"Fix-loop attempts: {facts['fix_loop_attempts']}")
    if facts.get("checks"):
        lines.append(f"Checks: {facts['checks']}")
    summary = (record.get("build") or {}).get("summary")
    if summary:
        lines += ["", summary]
    cost = record.get("cost_usd", (record.get("usage") or {}).get("cost_usd"))
    try:
        cost = None if cost is None else float(cost)
    except (TypeError, ValueError):
        cost = None
    if cost is not None:
        lines.append(f"Cost: ${cost:.2f}")
    closes = reference or issue_closes(issue)
    lines = lines + ["", closes] if closes else lines
    return "\n".join(lines + ["", pr_footer(record.get("run"))])


def land_log_row(ts: str, run: str, task: str | None, steps_reached: Sequence[str], exit_code: int, pr: str | None,
                 stop: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One `land.jsonl` line: step kinds in the order they ran, and an empty `pr` is `None`. `stop` adds the keys of a stopped land."""
    return {"ts": ts, "run": run, "task": task, "steps_reached": list(steps_reached), "exit": exit_code, "pr": pr or None,
            **(stop or {})}


_SQUASH_CONFLICT = re.compile(r"squash of .+ conflicts in: (?P<paths>.+)", re.DOTALL)


def stop_fields(kind: str, detail: str, branch_sha: str | None, from_sha: str | None) -> dict[str, Any]:
    """The land log keys for a stopped land. A `squash_phase` conflict names its paths and commits; a `cherry_pick` conflict, its paths."""
    base = {"stopped_step": kind, "stop_detail": detail}
    picked = _CHERRY_CONFLICT.match(detail) if kind == "cherry_pick" else None
    if picked is not None:
        return {**base, "conflict_paths": sorted(picked["paths"].split(", "))}
    conflict = _SQUASH_CONFLICT.fullmatch(detail) if kind == "squash_phase" else None
    if conflict is None:
        return base
    return {**base, "conflict_paths": sorted(conflict["paths"].split(", ")), "branch_sha": branch_sha, "from_sha": from_sha}


LAND_CONFLICT_KEY = "land_conflict"  # frontmatter and store `extra` key; its value is the reason, and it marks a rebuild
_CHERRY_CONFLICT = re.compile(r"cherry-pick of \S+ conflicts in: (?P<paths>[^\n]+)")


def land_conflict_reason(kind: str, detail: str, base: str) -> str | None:
    """`land conflict with <base> in <files>` for a `cherry_pick` stop whose detail leads with the conflicted paths, else None."""
    found = _CHERRY_CONFLICT.match(detail) if kind == "cherry_pick" else None
    return None if found is None else f"land conflict with {base} in {found['paths']}"


def conflict_note(reason: str) -> str:
    """The body paragraph a land conflict leaves for the rebuild: the recorded reason, capitalised."""
    return f"{reason[0].upper()}{reason[1:]}; rebuilding on current main."


def conflict_to_ready(text: str, reason: str) -> str | None:
    """`text` with `state: approved` rewritten to `state: ready`, a `land_conflict: <reason>` line under it and the
    `conflict_note` as a last paragraph. None when the header has no `state: approved` line."""
    if not text.startswith("---\n"):
        return None
    close = text.find("\n---\n", 4)
    header = text[:close] if close != -1 else text
    line = next((ln for ln in header.splitlines(keepends=True) if ln.strip() == "state: approved"), None)
    if line is None:
        return None
    marked = text.replace(line, f"state: ready\n{LAND_CONFLICT_KEY}: {reason}" + ("\n" if line.endswith("\n") else ""), 1)
    return f"{marked.rstrip(chr(10))}\n\n{conflict_note(reason)}\n"


_READY_MERGED_NOTE = "land: work item was still ready (quarantined after approval, or run on another machine); merged, so done"


def approve_to_done(text: str, *, merged: bool = False) -> tuple[str | None, str | None]:
    """The work item's frontmatter `state: approved` line rewritten to
    `state: done`, with every other byte of `text` untouched, paired with
    `None`; or `None` paired with `None` when the state is already `done`
    (nothing to do); or `None` paired with a one-line message naming the
    state when it is anything else — land never moves an unapproved item.
    Only the `---`-delimited header is searched for `state:`, so a body line
    that happens to start with `state:` is never mistaken for the field.
    With `merged` True a `ready` item also moves to `done`, paired with a
    one-line note: it was quarantined after approval, or its run was on another machine, and the merge landed it."""
    if not text.startswith("---\n"):
        return None, "land: work item has no state field"
    close = text.find("\n---\n", 4)
    header = text[:close] if close != -1 else text
    for line in header.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.startswith("state:"):
            continue
        state = stripped[len("state:"):].strip()
        if state == "done":
            return None, None
        if state == "ready" and merged:
            return text.replace(line, line.replace("ready", "done", 1), 1), _READY_MERGED_NOTE
        if state != "approved":
            return None, f"land: work item state is {state!r}, not moving to done"
        return text.replace(line, line.replace("approved", "done", 1), 1), None
    return None, "land: work item has no state field"


def approved_state(mode: str, store_state: str | None, file_state: str | None) -> str | None:
    """The state the approved check reads: the store's when `mode` is `store` and it has a row, else the file's."""
    return store_state if mode == "store" and store_state is not None else file_state


_LANDED_ELSEWHERE = "land: another machine landed this task; the store state is no longer approved"


def set_state_stop(result: Any) -> str | None:
    """None to continue, else the reason to stop. Matches the store_cli result by class name, so land imports no store_cli.
    A refused precondition means another machine landed the task; not-available stops too, since a compare-and-set that could not run proves nothing."""
    kind = type(result).__name__
    if kind == "StateSet":
        return None
    if kind == "StateRefused":
        current = getattr(result, "current", None)
        return f"{_LANDED_ELSEWHERE} (now {current!r})" if current else _LANDED_ELSEWHERE
    if kind == "Failed":
        return f"land: store set-state failed: exit {result.code}: {result.detail}"
    return "land: store not available; cannot compare-and-set the task to done"


def close_to_done(
    text: str, *, mode: str, merged: bool, set_state: Callable[[str], Any]
) -> tuple[str | None, str | None]:
    """`approve_to_done` under `mode`. `files` is `approve_to_done` unchanged and never calls `set_state`.
    `store` calls `set_state(expected)` for `done --expect approved` only when the item would move; a stop comes back as `(None, reason)`, so the file is never rewritten."""
    new_text, message = approve_to_done(text, merged=merged)
    if mode != "store" or new_text is None:
        return new_text, message
    stop = set_state_stop(set_state("approved"))
    return (None, stop) if stop is not None else (new_text, message)


_Hunk = tuple[list[str], list[str], list[str]]


def _parse_conflict_hunks(text: str) -> list[str | _Hunk] | None:
    """`text` split into a list of plain-text segments and `(ours, base, theirs)` line-list hunks, in order.
    None when a `<<<<<<<`/`|||||||`/`=======`/`>>>>>>>` marker is missing or out of order."""
    lines = text.splitlines(keepends=True)
    segments: list[str | _Hunk] = []
    plain: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("||||||| ") or line.startswith(">>>>>>> ") or line.rstrip("\n") == "=======":
            return None
        if not line.startswith("<<<<<<< "):
            plain.append(line)
            i += 1
            continue
        if plain:
            segments.append("".join(plain))
            plain = []
        ours: list[str] = []
        i += 1
        while i < n and not lines[i].startswith("||||||| "):
            if lines[i].startswith("<<<<<<< ") or lines[i].startswith(">>>>>>> ") or lines[i].rstrip("\n") == "=======":
                return None
            ours.append(lines[i])
            i += 1
        if i >= n:
            return None
        base: list[str] = []
        i += 1
        while i < n and lines[i].rstrip("\n") != "=======":
            if lines[i].startswith("<<<<<<< ") or lines[i].startswith("||||||| ") or lines[i].startswith(">>>>>>> "):
                return None
            base.append(lines[i])
            i += 1
        if i >= n:
            return None
        theirs: list[str] = []
        i += 1
        while i < n and not lines[i].startswith(">>>>>>> "):
            if lines[i].startswith("<<<<<<< ") or lines[i].startswith("||||||| ") or lines[i].rstrip("\n") == "=======":
                return None
            theirs.append(lines[i])
            i += 1
        if i >= n:
            return None
        segments.append((ours, base, theirs))
        i += 1
    if plain:
        segments.append("".join(plain))
    return segments


def resolve_add_add_conflicts(text: str) -> str | None:
    """`text`, a worktree file left by `git -c merge.conflictStyle=diff3 cherry-pick` after an add/add conflict,
    resolved to ours-then-theirs (ours alone when ours and theirs match line-for-line). None when any hunk's
    base holds a non-empty line, or the markers do not parse into well-formed hunks."""
    segments = _parse_conflict_hunks(text)
    if segments is None:
        return None
    resolved: list[str] = []
    for segment in segments:
        if isinstance(segment, str):
            resolved.append(segment)
            continue
        ours, base, theirs = segment
        if "".join(base).strip() != "":
            return None
        resolved.append("".join(ours if ours == theirs else ours + theirs))
    return "".join(resolved)


class GeneratedFile(NamedTuple):
    """How to regenerate one file: the directory to run in, the `python` arguments that rewrite the file given
    its path as a last argument, and the markers around the span the generator owns."""

    cwd: str
    command: tuple[str, ...]
    begin: str
    end: str
    sources: tuple[str, ...] = ()


# Files a cherry-pick conflict may be resolved by regenerating, each repo-relative path mapped to its generator.
# Declared, not guessed from names: the README's generator is `python -m agent_tools.readme_commands`, the one
# `test_readme_commands` compares the README against.
GENERATED_FILES: dict[str, GeneratedFile] = {
    "src/tools/README.md": GeneratedFile(
        "src/tools",
        ("-m", "agent_tools.readme_commands", "--readme"),
        readme_commands.BEGIN,
        readme_commands.END,
        ("src/tools/agent_tools/cli.py", "src/tools/agent_tools/readme_commands.py"),
    ),
}


def generated_conflict_only(conflicted: Sequence[str]) -> bool:
    """True when something conflicted and every conflicted path is a declared generated file."""
    return bool(conflicted) and all(name in GENERATED_FILES for name in conflicted)


def resolve_block_conflicts(text: str, begin: str, end: str) -> str | None:
    """`text`, a worktree file left by `git -c merge.conflictStyle=diff3 cherry-pick`, with every conflict hunk
    resolved to ours. None when a hunk lies outside the `begin`..`end` span or holds a marker line, or the markers
    do not parse: a conflict outside the generated span is a real one."""
    segments = _parse_conflict_hunks(text)
    if segments is None:
        return None
    resolved = ""
    for segment in segments:
        if isinstance(segment, str):
            resolved += segment
            continue
        ours, base, theirs = segment
        inside = resolved.rfind(begin) > resolved.rfind(end)
        if not inside or any(begin in ln or end in ln for ln in (*ours, *base, *theirs)):
            return None
        resolved += "".join(ours)
    return resolved
