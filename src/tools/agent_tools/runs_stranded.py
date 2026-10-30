"""Approved task records dropped without a quarantine (`docs/design/triage.md` §3)."""

from __future__ import annotations

from collections.abc import Callable

from agent_tools.base_branch import DEFAULT_BASE
from agent_tools.land import _approved as land_approved


def _scoped_item(record: dict, candidates: list[dict]) -> dict | None:
    """The one item this record's task names. A task id repeats across
    initiatives and across phases of the same initiative, so a
    `candidates` list with more than one entry resolves only when the
    record's own `initiative` and `phase` narrow it to one. A task moved to
    another phase after the record was written matches no phase, and
    resolves when its initiative alone leaves one item. Otherwise this comes
    back `None` rather than guessing which item is meant."""
    initiative = record.get("initiative")
    phase = record.get("phase")
    in_initiative = [c for c in candidates if initiative is None or c.get("initiative") == initiative]
    scoped = [c for c in in_initiative if phase is None or c.get("phase") == phase]
    if len(scoped) == 1:
        return scoped[0]
    return in_initiative[0] if not scoped and len(in_initiative) == 1 else None


# A record quarantined after its approval for one of these causes holds work that was judged wrong (its full
# checks failed, or its ticket forbade what the change needed); the chair re-grounds the ticket and relaunches
# it, so the old approval is not stranded work. A harness cause keeps its approval: rescue lands that patch.
SUPERSEDING_CAUSES = frozenset({"code", "ticket"})


def _approved_and_unlanded(record: dict) -> bool:
    """The land's own approval rule, so a unanimously approved record (no arbiter ran) counts too, less a
    record whose approval a later code or ticket quarantine superseded."""
    return (
        land_approved(record) is None
        and not record.get("landed")
        and record.get("cause") not in SUPERSEDING_CAUSES
    )


def _remedy(record: dict, item: dict | None, base_of: Callable[[str], str] | None = None) -> str | None:
    """The exact `cox runs land` line, or `None` when no repo path can be
    resolved — a line that looks runnable and is not is worse than none.
    `base_of` names the repo's integration branch; one other than `main` is pinned with `--base`."""
    repo = record.get("repo") or (item.get("repo") if item else None)
    if not repo:
        return None
    base = base_of(repo) if base_of else DEFAULT_BASE
    pin = "" if base == DEFAULT_BASE else f" --base {base}"
    return f"cox runs land {record.get('run')} --task {record.get('task')} --repo {repo}{pin}"


def _row(record: dict, item: dict | None, base_of: Callable[[str], str] | None = None) -> dict:
    # A land that conflicted left its reason on the item: the task needs a rebuild on current main, not a land.
    # Only while the item is still `ready`: a rebuilt task that was approved again has a new run to land.
    conflict = (item or {}).get("land_conflict") if (item or {}).get("state") == "ready" else None
    return {
        "run": record.get("run"), "task": record.get("task"), "phase": record.get("phase"),
        "branch": record.get("branch"), "remedy": f"rebuild: {conflict}" if conflict else _remedy(record, item, base_of),
    }


_LIVE_STATES = frozenset({"ready", "approved"})


def _live_pairs(records: list[dict], items: list[dict]) -> list[tuple[dict, dict | None]]:
    """Approved, unlanded records paired with their resolved item. A record
    whose task names no item is left out; one whose item is ambiguous is
    kept with `None`; one whose item is not in a live state is left out."""
    by_id: dict[str, list[dict]] = {}
    for item in items:
        by_id.setdefault(item.get("id"), []).append(item)
    pairs = []
    for record in records:
        if not _approved_and_unlanded(record):
            continue
        candidates = by_id.get(record.get("task"), [])
        if not candidates:
            continue
        item = _scoped_item(record, candidates)
        if item is not None and item.get("state") not in _LIVE_STATES:
            continue
        pairs.append((record, item))
    return pairs


def _missing(record: dict, repo_exists: Callable[[str], bool]) -> bool:
    """True when the record's own non-empty `repo` fails `repo_exists`."""
    repo = record.get("repo")
    return bool(repo) and not repo_exists(repo)


def _carried(record: dict, item: dict | None, branch_exists: Callable[[str, str], bool]) -> bool:
    """True when the record's `branch` is a bare phase branch `epic/<init>/<phase>`
    that `branch_exists` reports present in the resolved repo. `epic/...--<task>`
    is task-scoped, as in `cleanup.plan_cleanup`, so it is never carried."""
    branch = record.get("branch")
    repo = record.get("repo") or (item.get("repo") if item else None)
    return (bool(branch) and bool(repo) and branch.startswith("epic/") and "--" not in branch
            and branch_exists(repo, branch))


def stranded(records: list[dict], items: list[dict],
             repo_exists: Callable[[str], bool] | None = None,
             branch_exists: Callable[[str, str], bool] | None = None,
             base_of: Callable[[str], str] | None = None) -> list[dict]:
    """Rows for approved, unlanded records whose matching item is `ready`
    or `approved`. `items` are expected to carry a resolved `repo` (the edge
    reads it from the initiative's own frontmatter, since a work item's
    `id`/`initiative` name a ticket, not a filesystem path). A record
    whose task id does not resolve to one item under its own initiative
    and phase is still reported, with `remedy` left `None` rather than
    matched against a foreign item's state or repo. `repo_exists` is
    injected; `None` treats every repo as present, and a record with no
    `repo` is never skipped for a missing one. `branch_exists` is injected
    too; when given, a record on a bare phase branch it reports as still
    present is skipped as not stranded but waiting on its phase's carried
    branch under the one-merge-per-phase relaunch rule, while `None`, any
    task-scoped branch, and a record with no resolvable repo behave exactly
    as before. `base_of` is injected too: it names a repo's integration branch for the remedy's `--base`."""
    return [_row(record, item, base_of) for record, item in _live_pairs(records, items)
            if (repo_exists is None or not _missing(record, repo_exists))
            and (branch_exists is None or not _carried(record, item, branch_exists))]


def missing_repos(records: list[dict], items: list[dict],
                  repo_exists: Callable[[str], bool]) -> list[str]:
    """Sorted, de-duplicated repo paths of records `stranded` skips because
    the repo is missing. A record with a non-live item is not counted."""
    return sorted({record["repo"] for record, _ in _live_pairs(records, items)
                   if _missing(record, repo_exists)})
