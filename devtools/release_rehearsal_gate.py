"""Decide from a listing of rehearsal runs whether a release may be cut (docs/release-rehearsal.md).

Every function here takes plain data and returns plain data. The edge in cli.py runs `git` and `gh`."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

from devtools import release_migrations

COMPONENTS = ("cartridges", "graphs", "tools", "crew", "dash")
SCENARIOS = ("fresh", "upgrade", "migrate")
WINDOW = 200
_OVERRIDE_LINE = re.compile(r"^Cut with `--allow-no-rehearsal`: (.*)\.$")


def run_title(shas: Mapping[str, str], previous_tag: str, migrations: Sequence[str]) -> str:
    """The run name the workflow gives itself: the five SHAs in manifest order, then the previous tag and migrations."""
    parts = [f"{name}={shas[name]}" for name in COMPONENTS]
    return " ".join(["rehearsal", *parts, f"previous={previous_tag}", f"migrations={','.join(migrations) or 'none'}"])


def newest_match(runs: Sequence[Mapping], title: str) -> Mapping | None:
    """Among runs whose displayTitle equals `title` exactly, the one with the greatest ISO `createdAt`."""
    matches = [run for run in runs if run.get("displayTitle") == title]
    return max(matches, key=lambda run: run["createdAt"]) if matches else None


def run_passes(run: Mapping, jobs: Sequence[Mapping], declared: Sequence[str]) -> str | None:
    """None when the run passed, else the failing condition as a sentence."""
    if run.get("conclusion") != "success":
        return f"The run concluded {run.get('conclusion') or 'with no conclusion'}, not success."
    by_name = {job.get("name"): job.get("conclusion") for job in jobs}
    missing = [name for name in SCENARIOS if name not in by_name]
    if missing:
        return f"The run has no {', '.join(missing)} job."
    unmet = [name for name in ("fresh", "upgrade") if by_name[name] != "success"]
    if unmet:
        return f"The {unmet[0]} job concluded {by_name[unmet[0]]}, not success."
    result = {"success": "pass", "skipped": "skip"}.get(by_name["migrate"], "fail")
    if not release_migrations.migrate_requirement_met(tuple(declared), result):
        return (f"The migrate job concluded {by_name['migrate']} while the release declares migrations: "
                f"{', '.join(declared)}.")
    return None


def _step(kind: str, detail: str) -> dict:
    return {"kind": kind, "component": "rehearsal", "detail": detail}


def _override(reason: str, notes_text: str) -> list[dict]:
    wanted = reason.strip()
    if not wanted:
        return [_step("refuse", "--allow-no-rehearsal needs a reason, and an empty one does not count.")]
    carried = [m.group(1).strip() for line in notes_text.splitlines() if (m := _OVERRIDE_LINE.match(line.rstrip()))]
    if wanted in carried:
        return [_step("note", f"{wanted} (rehearsal waived, recorded in the release notes)")]
    return [_step("refuse", f"The release notes lack the line: Cut with `--allow-no-rehearsal`: {wanted}.")]


def decide(runs: Sequence[Mapping], jobs_of: Callable[[Mapping], Sequence[Mapping]], shas: Mapping[str, str],
           previous_tag: str, declared: Sequence[str], allow_reason: str | None, notes_text: str) -> list[dict]:
    """Release-plan steps: a refuse blocks the cut, a note records why it may go on. `None` for
    `allow_reason` means no override, so the lookup runs; an empty string is an override with no reason."""
    if allow_reason is not None:
        return _override(allow_reason, notes_text)
    unresolved = [name for name in COMPONENTS if name not in shas]
    if unresolved:
        return [_step("refuse", f"No origin commit resolved for {', '.join(unresolved)}.")]
    title = run_title(shas, previous_tag, declared)
    run = newest_match(runs, title)
    if run is None:
        full = f" The listing returned {WINDOW} runs, so the window may be full." if len(runs) == WINDOW else ""
        return [_step("refuse", f"No rehearsal run matches. Looked for: {title}.{full}")]
    failure = run_passes(run, jobs_of(run), declared)
    if failure is not None:
        return [_step("refuse", f"{failure} Run: gh run view {run['databaseId']}. Looked for: {title}")]
    return [_step("note", f"rehearsal passed: gh run view {run['databaseId']}")]
