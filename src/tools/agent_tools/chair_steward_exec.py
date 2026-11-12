"""The steward executor: merge a finished level onto its ticket branch, or run the gate checks there."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_tools.chair_read_steward import level_branch
from agent_tools.chair_steward_level import GitRun, LevelState, level_state

Action = Mapping[str, Any]
Outcome = dict[str, Any]

# kind -> (success event, failure event); the pairs chair_exec accepts back from this module.
EVENTS = {"merge_level": ("level_merged", "merge_failed"), "run_gate": ("gate_passed", "gate_failed")}

# The merge commit needs an author even where the host has no git identity configured.
_IDENTITY = ["-c", "user.name=coxswain steward", "-c", "user.email=steward@coxswain.invalid"]


@dataclass(frozen=True)
class StewardPorts:
    """Plain callables; the caller supplies the real ones, tests supply literals."""

    git: GitRun
    repo_of: Callable[[str], str]
    ticket_branch_of: Callable[[str], str]
    phase_of: Callable[[str, int], str]  # (epic, level index into its phases in natural order) -> phase name
    run_gate_checks: Callable[[str, str, Sequence[str]], tuple[bool, str]]
    target_branch_of: Callable[[str], str] | None = None  # epic -> the repository's target branch name


def outcome(event: str, action: Action, reason: str) -> Outcome:
    """The mapping Deps.steward_exec documents. `fields` is the action's own."""
    return {"event": event, "fields": dict(action.get("fields", {})), "reason": reason}


def _merge_failed(action: Action, step: str, output: str) -> Outcome:
    return outcome(EVENTS["merge_level"][1], action, f"{step}: {output.strip()}")


def _git(repo: str, *args: str) -> list[str]:
    """`repo` is already expanded: the repo_of port owns that."""
    return ["-C", repo, *args]


def _merge(action: Action, repo: str, ticket: str, level: str, run: GitRun) -> Outcome:
    """Plumbing only: merge-tree for the tree, commit-tree for the commit, update-ref guarded by the old tip."""
    ticket_code, ticket_out = run(_git(repo, "rev-parse", f"{ticket}^{{commit}}"))
    if ticket_code != 0:
        return _merge_failed(action, "rev-parse ticket", ticket_out)
    level_code, level_out = run(_git(repo, "rev-parse", f"{level}^{{commit}}"))
    if level_code != 0:
        return _merge_failed(action, "rev-parse level", level_out)
    tree_code, tree_out = run(_git(repo, "merge-tree", "--write-tree", ticket, level))
    if tree_code != 0 or not tree_out.strip():
        return _merge_failed(action, "merge-tree", tree_out or "no tree written")
    old, tip = ticket_out.strip(), level_out.strip()
    commit_code, commit_out = run(
        _git(
            repo,
            *_IDENTITY,
            "commit-tree",
            tree_out.splitlines()[0].strip(),
            "-p",
            old,
            "-p",
            tip,
            "-m",
            f"Merge {level} into {ticket}",
        )
    )
    if commit_code != 0 or not commit_out.strip():
        return _merge_failed(action, "commit-tree", commit_out or "no commit written")
    new = commit_out.strip()
    ref_code, ref_out = run(_git(repo, "update-ref", "-m", f"steward: merge {level}", f"refs/heads/{ticket}", new, old))
    if ref_code != 0:
        return _merge_failed(action, "update-ref", ref_out)
    return outcome(EVENTS["merge_level"][0], action, f"merged {level} into {ticket} as {new}")


def _land_at(action: Action) -> str:
    from core.landing import DEFAULT_LAND_AT, canonical_land_at  # lazy: core sits beside agent_tools in one venv

    return canonical_land_at(action.get("land_at")) or DEFAULT_LAND_AT


def _ensure_ticket_branch(action: Action, repo: str, ticket: str, ports: StewardPorts) -> Outcome | None:
    """Create the ticket branch at the target tip when absent. None means it exists; else a merge_failed outcome."""
    exists_code, _ = ports.git(_git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{ticket}"))
    if exists_code == 0:
        return None
    target = ports.target_branch_of(action["epic"]) if ports.target_branch_of else ""
    if not target:
        return _merge_failed(action, "target branch", f"{ticket} is missing and no target branch is known")
    tip_code, tip_out = ports.git(_git(repo, "rev-parse", "--verify", f"{target}^{{commit}}"))
    if tip_code != 0 or not tip_out.strip():
        return _merge_failed(action, "rev-parse target", tip_out or f"{target} does not resolve")
    # The all-zeros old value makes update-ref refuse when the branch appeared since the check above.
    create_code, create_out = ports.git(
        _git(repo, "update-ref", "-m", f"steward: create {ticket}", f"refs/heads/{ticket}", tip_out.strip(), "0" * 40)
    )
    if create_code != 0:
        return _merge_failed(action, "create ticket branch", create_out)
    return None


def _merge_level(action: Action, ports: StewardPorts) -> Outcome:
    land_at = _land_at(action)
    if land_at != "initiative":
        reason = f"land_at {land_at}: the land is run by the chair, not the steward executor"
        return outcome(EVENTS["merge_level"][1], action, reason)
    epic = action["epic"]
    repo = ports.repo_of(epic)
    ticket = ports.ticket_branch_of(epic)
    failed = _ensure_ticket_branch(action, repo, ticket, ports)
    if failed is not None:
        return failed
    level = level_branch(epic, ports.phase_of(epic, action["level"]))
    if level_state(repo, ticket, level, ports.git) is LevelState.MERGED:
        return outcome(EVENTS["merge_level"][0], action, "already merged")
    return _merge(action, repo, ticket, level, ports.git)


def _run_gate(action: Action, checks: Sequence[str], ports: StewardPorts) -> Outcome:
    epic = action["epic"]
    passed, detail = ports.run_gate_checks(ports.repo_of(epic), ports.ticket_branch_of(epic), checks)
    return outcome(EVENTS["run_gate"][0 if passed else 1], action, detail)


def chair_steward_exec(action: Action, checks: Sequence[str], ports: StewardPorts) -> Outcome:
    """Run a merge_level or run_gate action. Any other kind raises ValueError.

    A failure of any port is reported as the kind's failure event; nothing else escapes into the tick.
    """
    kind = action.get("kind", "")
    if kind not in EVENTS:
        raise ValueError(f"chair_steward_exec accepts merge_level or run_gate, not {kind!r}")
    try:
        return _merge_level(action, ports) if kind == "merge_level" else _run_gate(action, checks, ports)
    except Exception as exc:  # the edge: a port may raise anything, and the tick must not see it
        return outcome(EVENTS[kind][1], action, f"{type(exc).__name__}: {exc}")
