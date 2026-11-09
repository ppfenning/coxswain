"""Pure validation of intake fields. Callers pass the known sets in; nothing is read here."""

import re
from collections.abc import Collection

from agent_tools.queue_rows import coerce_priority

_VERSION = re.compile(r"v?\d+(\.\d+)+")


def _text(value: object) -> str:
    """Surrounding whitespace never counts, in validate or in normalise."""
    return "" if value is None else str(value).strip()


def _priority(value: object) -> int | None:
    """The queue's own coercion, so intake and queue agree. A bool is not a priority."""
    coerced = coerce_priority(value)
    if isinstance(coerced, int) and not isinstance(coerced, bool):
        return coerced
    return None


def _priority_given(value: object) -> bool:
    return value is not None and _text(value) != ""


def _repo_refusals(repo: str, known_repos: Collection[str]) -> list[str]:
    valid = ", ".join(sorted(known_repos))
    return [] if repo in known_repos else [f"repo {repo!r} is not known; valid repos: {valid}"]


def _epic_refusals(epic: str, known_epics: Collection[str]) -> list[str]:
    choices = "no epic exists yet" if not known_epics else f"valid epics: {', '.join(sorted(known_epics))}"
    return [] if epic == "" or epic in known_epics else [f"epic {epic!r} is not known; {choices}"]


def _priority_refusals(priority: object) -> list[str]:
    ok = not _priority_given(priority) or _priority(priority) is not None
    return [] if ok else [f"priority {priority!r} must be a whole number; higher runs first"]


def _release_refusals(release: str) -> list[str]:
    shape = "a dotted numeric version such as 2026.11.6 or v2026.11.6"
    ok = release == "" or _VERSION.fullmatch(release) is not None
    return [] if ok else [f"target_release {release!r} must be {shape}"]


def validate(
    repo: str,
    epic: str | None,
    priority: object,
    target_release: str | None,
    known_repos: Collection[str],
    known_epics: Collection[str],
) -> list[str]:
    """One-line refusals, each carrying the valid choices. Empty when every field is valid."""
    return [
        *_repo_refusals(_text(repo), known_repos),
        *_epic_refusals(_text(epic), known_epics),
        *_priority_refusals(priority),
        *_release_refusals(_text(target_release)),
    ]


def normalise(
    repo: str,
    epic: str | None,
    priority: object,
    target_release: str | None,
) -> dict[str, object]:
    """Accepted values as frontmatter fields: int priority, release without its v, empties dropped."""
    candidates = {
        "repo": _text(repo),
        "epic": _text(epic),
        "priority": _priority(priority) if _priority_given(priority) else None,
        "target_release": _text(target_release).removeprefix("v"),
    }
    return {key: value for key, value in candidates.items() if value is not None and value != ""}
