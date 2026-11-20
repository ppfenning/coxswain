"""The lander's refusal of a change that carries nothing (2026-11-20).

An initiative pull request was opened with "+0 -0, 0 files": its phase branch held only merge commits, and its task
had been marked approved with no commit of its own. Before the lander opens or updates an initiative's pull request,
and before it merges a phase, it asks whether merging the branch would change its target's tree at all (`carries_nothing`).
If it would not, and the branch has approved tasks, it refuses, and the chair is told once (`empty_news`) which approved
tasks have no commit on the remote (`uncommitted`).

The decisions are pure. The two edges read git (`tree_facts`, `on_remote`), and `refuse_empty` is the one place the
refusal is remembered and told: the memory is the store's `lander_notices`, so a pass that finds the same refusal again
changes nothing and tells nobody, and a pass that finds the branch carries a change again forgets it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness import landing_git as lg
from harness import landing_store as ls
from harness.store_dialect import Connection

EMPTY_KEY = "empty:"  # the lander_notices key prefix of an initiative (or one of its phases) refused as empty

Notify = Callable[[str, Mapping[str, object]], None]


@dataclass(frozen=True)
class TreeFacts:
    """What git says of merging `source` into `target`. `target_tree`: the target's tree. `merged_tree`: the tree a
    clean merge of the two would have, None when it conflicts or cannot be computed. `held`: the target already holds
    the source tip, so there is nothing to merge and nothing to judge."""

    target_tree: str | None
    merged_tree: str | None
    held: bool


@dataclass(frozen=True)
class Target:
    """One branch a change is judged against: the ref git reads, the name the refusal gives, and whether a source
    it already holds counts as empty (`carries_nothing`)."""

    ref: str
    name: str
    held_is_empty: bool = False


# -- the pure decisions -------------------------------------------------------------------------------------------

def approved_tasks(tasks: Sequence[Mapping[str, object]], phase: str | None = None) -> list[str]:
    """Pure. The ids of the approved tasks, of `phase` when one is named, else of the whole initiative."""
    return [str(t["id"]) for t in tasks if t.get("state") == "approved" and (phase is None or str(t.get("phase")) == phase)]


def carries_nothing(facts: TreeFacts, approved: Sequence[str], held_is_empty: bool = False) -> bool:
    """Pure. Whether the branch is to be refused as empty: merging it would leave the target's tree as it is, and it
    has approved tasks that claim to have changed something. A conflict, an unknown tree, or a branch with no approved
    task (all dropped or done) is never refused here. A branch the target already holds is refused only when
    `held_is_empty`: held by main it has nothing of its own, while held by its initiative branch it is a merge that
    was pushed and not yet recorded, which is recorded as it is."""
    if not approved or facts.target_tree is None or facts.merged_tree is None:
        return False
    if facts.held:
        return held_is_empty
    return facts.merged_tree == facts.target_tree


def empty_against(facts: Sequence[tuple[Target, TreeFacts]], approved: Sequence[str]) -> str | None:
    """Pure. The name of the first target the branch is refused against (`carries_nothing`), or None. A phase is
    judged against its parent (main) and its initiative branch: holding nothing beyond main, or adding nothing to the
    initiative branch, each means its approved tasks have no commits."""
    return next((t.name for t, found in facts if carries_nothing(found, approved, t.held_is_empty)), None)


def uncommitted(tasks: Sequence[Mapping[str, object]], remote: Mapping[str, bool], phase: str | None = None) -> list[str]:
    """Pure. The approved tasks with no commit on the remote: none recorded, or one that `remote` (sha -> on the
    remote) says the remote does not hold."""
    rows = [t for t in tasks if str(t["id"]) in set(approved_tasks(tasks, phase))]
    return [str(t["id"]) for t in rows if not t.get("merged_commit") or not remote.get(str(t["merged_commit"]), False)]


def empty_reason(initiative: str, against: str, phase: str | None = None) -> str:
    """Pure. Why the lander refuses: the initiative (and phase), what it was compared with, and what that means."""
    what = f"{initiative}: phase {phase}" if phase else initiative
    return f"{what}: no changes against {against}; the approved tasks have no commits"


def empty_alarm(reason: str, missing: Sequence[str]) -> str:
    """Pure. What the chair is told: the refusal, and the approved tasks with no commit on the remote."""
    listed = ", ".join(missing) if missing else "none; their commits change nothing"
    return f"{reason} (approved with no commit on the remote: {listed})"


def empty_news(told: str | None, alarm: str | None) -> Literal["tell", "forget", "quiet"]:
    """Pure. What one check does with the chair. `alarm` is what it found (None: the branch carries a change), `told`
    what was told before. A new or changed alarm is told; the same one is not told twice; a branch that carries a
    change again forgets the old one, so an empty branch later is told as new."""
    if alarm is None:
        return "forget" if told is not None else "quiet"
    return "quiet" if told == alarm else "tell"


def empty_key(initiative: str, phase: str | None = None) -> str:
    """Pure. The `lander_notices` key of one initiative's (or phase's) empty refusal."""
    return f"{EMPTY_KEY}{initiative}" + (f":{phase}" if phase else "")


# -- the edges ----------------------------------------------------------------------------------------------------

def _tree(repo: Path, ref: str) -> str | None:
    ok, out = lg.git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}")
    return out if ok and out else None


def tree_facts(repo: Path, target: str, source: str) -> TreeFacts:
    """Edge. Read what merging `source` into `target` would do, without touching either: `git merge-tree
    --write-tree` writes only objects. A ref git cannot read gives unknown trees, which are never refused."""
    target_tree = _tree(repo, target)
    if target_tree is None or lg.rev(repo, source) is None:
        return TreeFacts(target_tree, None, False)
    if lg.is_ancestor(repo, source, target):
        return TreeFacts(target_tree, target_tree, True)
    ok, out = lg.git(repo, "merge-tree", "--write-tree", target, source)
    merged = out.splitlines()[0].strip() if ok and out else None
    return TreeFacts(target_tree, merged or None, False)


def on_remote(repo: Path, shas: Sequence[str]) -> dict[str, bool]:
    """Edge. For each sha, whether a branch on origin holds it; with no origin, whether a local branch does (this
    clone is all there is). An unknown sha is not held."""
    scope = "refs/remotes/origin" if lg.git(repo, "remote", "get-url", "origin")[0] else "refs/heads"
    found: dict[str, bool] = {}
    for sha in dict.fromkeys(s for s in shas if s):
        ok, out = lg.git(repo, "for-each-ref", "--count=1", "--format=%(refname)", "--contains", sha, scope)
        found[sha] = ok and bool(out)
    return found


def refuse_empty(conn: Connection, notify: Notify, now: str, repo: Path, *, initiative: str, source: str,
                 targets: Sequence[Target], tasks: Sequence[Mapping[str, object]], phase: str | None = None) -> str | None:
    """Edge. The refusal of a branch that carries nothing against any of `targets`, in order, or None
    when it carries a change. An empty one raises needs_chair once per alarm, remembered in `lander_notices` after the
    note is handed over; a branch that carries a change forgets a refusal told before. Running it again with nothing
    changed writes nothing and tells nobody."""
    key = empty_key(initiative, phase)
    told = ls.told(conn, key).get(key)
    against = empty_against([(t, tree_facts(repo, t.ref, source)) for t in targets], approved_tasks(tasks, phase))
    reason = empty_reason(initiative, against, phase) if against is not None else None
    alarm = None
    if reason is not None:
        shas = [str(t.get("merged_commit") or "") for t in tasks]
        alarm = empty_alarm(reason, uncommitted(tasks, on_remote(repo, shas), phase))
    step = empty_news(told, alarm)
    if step == "forget":
        ls.forget(conn, key)
    elif step == "tell" and alarm is not None:
        notify("needs_chair", {"initiative": initiative, **({"phase": phase} if phase else {}), "reason": alarm})
        ls.remember(conn, key, alarm, now)
    return reason
