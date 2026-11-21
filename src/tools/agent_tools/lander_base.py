"""The lander's shared ground: the ports every lander module is given, what one item came to (`Outcome`), and the
edges that read origin's copy of a branch and publish one (`fetch_origin`, `publish_initiative`).

It is kept apart from `lander` so the modules `lander` itself calls (`lander_pr`) can take these without importing
`lander`: no import cycle. `lander` re-exports each name, so `lander.Ports` and `lander.Outcome` read as before.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from agent_tools import lander_hold
from core import landing_record as lr
from harness import landing_git as lg
from harness.phase_remote import push_branch
from harness.store_dialect import Connection

__all__ = ["ConflictAsk", "ForgePort", "LandMeta", "Outcome", "Ports", "RecordText", "Row", "fetch_origin",
           "origin_holds", "ports_repo", "publish_initiative", "record_text"]

Row = Mapping[str, object]  # a task row (id, phase, state, needs, title) or a queue item
RecordText = Literal["initiative", "repo", "branch", "parent"]  # the text keys a landing record always carries


def record_text(record: lr.LandingRecord, key: RecordText) -> str:
    """Pure. The str `key` that a landing record always carries; KeyError, as `record[key]` raised, when it is absent."""
    value = record.get(key)
    if value is None:
        raise KeyError(key)
    return value


class LandMeta(TypedDict, total=False):
    """What `Ports.meta` reads for one initiative: its own fields, the resolved cartridge, and its epic."""

    initiative: Mapping[str, object]
    cartridge: Mapping[str, object]
    cartridge_error: str | None
    epic_issue: object
    gate: str  # "full": the lander merges the pull request, not a person
    epic_stack: bool
    epic_branch: object
    epic_title: object
    epic_parent: object  # where the epic branch itself lands
    target_branch: object


class ForgePort(Protocol):
    """The forge as the lander uses it: the forge module the profile names, plus what the lander adds to it."""

    def push(self, repo: Path, branch: str) -> tuple[bool, str]: ...

    def find_open_prs(self, repo: Path, branch: str) -> Sequence[object] | str: ...

    def open_pr(self, repo: Path, title: str, body: str, *, head: str | None = None,
                base: str | None = None) -> tuple[bool, str]: ...

    def merge(self, repo: Path, step: Mapping[str, object]) -> tuple[bool, str]: ...

    def pr_merged(self, repo: Path, branch: str) -> tuple[bool, str]: ...

    def delete_branch(self, repo: Path, branch: str) -> tuple[bool, str]: ...

    def poll_checks(self, repo: Path, *, ref: str, memo: Mapping[str, object] | None = None) -> tuple[str, str, dict[str, object]]: ...

    def has_ci(self, repo: Path, ref: str) -> bool: ...

    def retarget(self, repo: Path, branch: str, base: str) -> tuple[bool, str]: ...

    def pr_state(self, url: str) -> Mapping[str, object]: ...

    def mark_draft(self, repo: Path, branch: str) -> tuple[bool, str]: ...

    def mark_ready(self, repo: Path, branch: str) -> tuple[bool, str]: ...

    def update_pr(self, repo: Path, title: str, body: str, *, head: str) -> tuple[bool, str]: ...


def _no_rectify(initiative: str, phase: str, title: str, body: str) -> str | None:
    return None


def _no_split(initiative: str, phases: Sequence[str]) -> str | None:
    return None


def _no_notify(kind: str, fields: Mapping[str, object]) -> None:
    return None


def _no_fetch(repo: Path, branch: str) -> str | None:
    return None


@dataclass(frozen=True)
class ConflictAsk:
    """One new-model phase merge that conflicted, as the merge agent is asked to settle it: the merge of `source` (the
    phase, as origin holds it) into the initiative `branch` is in progress in `scratch`, and `paths` hold its markers."""

    initiative: str
    phase: str
    branch: str
    source: str
    scratch: Path
    paths: tuple[str, ...]


def _no_merge_agent(ask: ConflictAsk) -> lg.Resolved | str:
    return lander_hold.NO_MERGE_AGENT


@dataclass
class Ports:
    """Everything the lander touches outside its own logic."""

    conn: Connection  # the store connection
    worker: str
    now: Callable[[], str]
    tasks: Callable[[str], Sequence[Row]]  # an initiative's task rows: id, phase, state, needs, title
    meta: Callable[[str], LandMeta]  # {"initiative": {...}, "cartridge": {...}, "epic_issue": ..., "gate": "full"}
    forge: ForgePort
    checks: Callable[[Path, Path, str], tuple[bool, str]]  # (repo, tree, "level" | PHASE_GATE | "ticket") -> passed, output
    mark_merged: Callable[[str, str, str], None]  # initiative, task, the initiative tip that holds it
    mark_done: Callable[[str, str, str], str | None]  # initiative, task, pr url -> why it was refused, or None
    file_rectify: Callable[[str, str, str, str], str | None] = _no_rectify  # initiative, phase, title, body -> task id
    split_blocked: Callable[[str, Sequence[str]], str | None] = _no_split  # initiative, phases -> follow-up id
    notify: Callable[[str, Mapping[str, object]], None] = _no_notify
    # The initiative's checkout on this machine. The record's own `repo` is the path on the host that cut the branch.
    repo: Callable[[lr.LandingRecord], Path] | None = None
    # Bring one branch up to origin's copy, forward only: why it must not be moved here (it diverged), else None.
    fetch: Callable[[Path, str], str | None] = _no_fetch
    log: Callable[[str], None] = print
    # The new model's merge agent, run through the local harness: each conflicted file's merged text, or why not.
    resolve_conflict: Callable[[ConflictAsk], lg.Resolved | str] = _no_merge_agent


@dataclass(frozen=True)
class Outcome:
    """What one item came to. `status`: done, waiting (left queued), failed, skipped."""

    status: str
    item: str
    detail: str = ""
    events: tuple[str, ...] = field(default_factory=tuple)



def ports_repo(ports: Ports, record: lr.LandingRecord) -> Path:
    found = ports.repo(record) if ports.repo is not None else Path(str(record.get("repo") or "")).expanduser()
    if not str(found).strip(".") or not found.is_absolute():
        # Never the current directory: an empty path would run git in whatever checkout the process happens to be in.
        raise LookupError(f"{record.get('initiative')}: its repository is not known on this machine")
    return found



def origin_holds(local_tip: str | None, origin_tip: str | None) -> bool:
    """Pure. Whether origin's copy of a branch is the local tip, so a push is done. A branch with no local tip has
    nothing to push."""
    return local_tip is None or local_tip == origin_tip


def publish_initiative(ports: Ports, repo: Path, branch: str) -> str | None:
    """Edge. Push `branch` through the forge, then make sure origin holds its tip: a forge that keeps an initiative
    branch local until it merges (the local forge) is followed by a plain fast-forward push. None once origin holds
    it (or there is no origin), else why not."""
    pushed, why = ports.forge.push(repo, branch)
    if not pushed:
        return f"push of {branch} to origin failed: {why.splitlines()[0] if why else 'no detail from the forge'}"
    if not lg.git(repo, "remote", "get-url", "origin")[0]:
        return None
    local = lg.rev(repo, f"refs/heads/{branch}")
    ok, out = lg.git(repo, "ls-remote", "origin", f"refs/heads/{branch}")
    if ok and origin_holds(local, out.split()[0] if out.split() else None):
        return None
    pushed, why = push_branch(repo, branch)
    return None if pushed else f"push of {branch} to origin failed: {why.splitlines()[0] if why else 'no detail from git'}"



def fetch_origin(repo: Path, branch: str) -> tuple[str | None, str]:
    """Fetch origin's `branch` into its remote-tracking ref. (the ref, '') when fetched; (None, '') when origin has
    no such branch; (None, why) when origin could not be read."""
    ref = f"refs/remotes/origin/{branch}"
    if not lg.git(repo, "remote", "get-url", "origin")[0]:
        return None, ""  # a repository with no origin: this clone is all there is
    ok, out = lg.git(repo, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{ref}")
    if ok:
        return ref, ""
    return None, "" if "couldn't find remote ref" in out.lower() else out.splitlines()[0] if out else "fetch failed"
