"""Readers for the chair's `intake` and `sources_configured` facts.

The core is pure. The edge lists intake with `route.intake_entries` and reads the profile with `route.parse_profile`.
"""
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_tools import intake_queue, release_gate, route


def queued_oldest_first(
    groups: Mapping[str, Sequence[Mapping]], mtimes: Mapping[str, float], current: str | None = None
) -> list[str]:
    """Paths of the `queued` group of `route.intake_groups`: priority descending, then oldest mtime, then path.

    A held entry (a draft) is skipped. An entry with any non-empty `after` is skipped too: files and mtimes cannot say
    whether its target has landed, so it waits for the row path, which can. This is the conservative choice.
    With `current` (the release in progress), an entry whose `target_release` is later is skipped as well."""
    free = [
        row
        for row in groups["queued"]
        if not row.get("held")
        and not row.get("after")
        and not (current is not None and release_gate.is_deferred(row.get("target_release") or "", current))
    ]
    return [path for _, _, path in sorted((-row.get("priority", 0), mtimes[row["path"]], row["path"]) for row in free)]


def has_sources(sources: Sequence[str]) -> bool:
    return len(sources) > 0


def intake_from_rows(rows: Sequence[Mapping], current: str | None = None) -> list[Mapping]:
    """Runnable intake rows naming no initiative: `intake_queue.runnable_intake` over every row, then the initiative filter.

    Held rows and rows with an unmet `after` are dropped; order is priority descending, then row order. All rows go in
    because landing is judged from the task rows. `route.intake_groups` never queues one that names an initiative, done or not.
    With `current` (the umbrella version), a row whose `target_release` is above it is dropped too.

    unknown: an `initiative.md` whose `intake:` field names the entry also unqueues it; rows carry no initiative.md, so that is not checked.
    """
    deferred = release_gate.deferred_intakes(rows, current) if current is not None else {}
    return [
        r
        for r in intake_queue.runnable_intake(list(rows))
        if r["extra"].get("initiative") is None and str(r["task_id"]) not in deferred
    ]


def read_intake(ws: Path, initiative_texts: Mapping[Path, str] | None = None, current: str | None = None) -> list[str]:
    """Edge. Queued intake paths under `ws/intake`, oldest first; none when the directory is absent.

    `initiative_texts` (path to text of each `work/*/initiative.md`) stands in for reading them when a tick already holds them.
    `current` is the release in progress; see `queued_oldest_first`."""
    root = ws / "intake"
    paths = sorted(root.glob("*.md")) + sorted((root / "done").glob("*.md"))
    files = {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in paths}
    groups = route.intake_groups(route.intake_entries(files), _initiatives(ws, initiative_texts))
    return queued_oldest_first(groups, {row["path"]: (ws / row["path"]).stat().st_mtime for row in groups["queued"]}, current)


def _initiatives(ws: Path, texts: Mapping[Path, str] | None = None) -> list[dict]:
    """Initiative rows as `route.intake_groups` reads them. `done` is False: it never decides membership of `queued`."""
    held = texts if texts is not None else {p: p.read_text(encoding="utf-8") for p in sorted((ws / "work").glob("*/initiative.md"))}
    return [{"id": p.parent.name, "done": False, "text": text} for p, text in held.items()]


def keep_last_good(previous: bool | None, outcome: bool | route.ProfileError) -> tuple[bool | route.ProfileError, bool]:
    """The value to use and whether it is stale. A failed re-read keeps `previous`; with none (the first load) it stays the error."""
    if not isinstance(outcome, route.ProfileError):
        return outcome, False
    return (outcome, False) if previous is None else (previous, True)


def read_sources_configured(profile_path: Path) -> bool | route.ProfileError:
    """Edge. True when the profile's `sources` block names any source; an unreadable profile configures none.

    A profile that fails to parse comes back as the `ProfileError`, not raised, so the caller decides what it costs."""
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        return has_sources(list(route.parse_profile(text).get("sources", {})))
    except route.ProfileError as exc:
        return exc


def sources_configured_keeping_last_good(profile_path: Path) -> tuple[Callable[[], bool], Callable[[], bool]]:
    """Edge. A `sources_configured` reader and a `profile_stale` reader sharing one cell of the last good read.

    The first read that fails raises the `ProfileError`, as before. A later failure returns the last good value and
    marks it stale until a read succeeds again. `profile_stale` is meant to be called after `sources_configured`."""
    good: list[bool] = []  # edge state: empty until the first good read, then the one last good value
    stale: list[bool] = [False]  # edge state: whether the latest read failed and the held value was used

    def sources_configured() -> bool:
        value, is_stale = keep_last_good(good[0] if good else None, read_sources_configured(profile_path))
        if isinstance(value, route.ProfileError):
            raise value
        good[:] = [value]
        stale[0] = is_stale
        return value

    return sources_configured, lambda: stale[0]
