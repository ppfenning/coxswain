import re

_VERSION = re.compile(r"v?[0-9]+(\.[0-9]+)+")
_WHOLE = re.compile(r"-?[0-9]+")
_VERSION_SHAPE = "2026.11.6 or v2026.11.6"


def _priority_int(value: object) -> int | None:
    """Return the whole number a priority names; None for a bool, float or other text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _WHOLE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _given(value: object) -> bool:
    return value is not None and value != ""


def _repo_refusal(repo: str, known_repos: frozenset[str] | set[str]) -> list[str]:
    if repo in known_repos:
        return []
    if not known_repos:
        return [f"repo {repo!r} is not known; no repo is known yet"]
    return [f"repo {repo!r} is not known; valid repos: {', '.join(sorted(known_repos))}"]


def _epic_refusal(epic: str | None, known_epics: frozenset[str] | set[str]) -> list[str]:
    if not _given(epic) or epic in known_epics:
        return []
    if not known_epics:
        return [f"epic {epic!r} is not known; no epic exists yet"]
    return [f"epic {epic!r} is not known; valid epics: {', '.join(sorted(known_epics))}"]


def _priority_refusal(priority: object) -> list[str]:
    if not _given(priority) or _priority_int(priority) is not None:
        return []
    return [f"priority {priority!r} must be a whole number; higher runs first"]


def _release_refusal(target_release: object) -> list[str]:
    if not _given(target_release):
        return []
    if isinstance(target_release, str) and _VERSION.fullmatch(target_release):
        return []
    return [f"target_release {target_release!r} must be a dotted numeric version such as {_VERSION_SHAPE}"]


def validate(
    repo: str,
    epic: str | None,
    priority: object,
    target_release: object,
    known_repos: frozenset[str] | set[str],
    known_epics: frozenset[str] | set[str],
) -> list[str]:
    """Return one-line refusals naming the valid choices; empty when every field is valid."""
    return [
        *_repo_refusal(repo, known_repos),
        *_epic_refusal(epic, known_epics),
        *_priority_refusal(priority),
        *_release_refusal(target_release),
    ]


def normalise(
    repo: str,
    epic: str | None,
    priority: object,
    target_release: object,
) -> dict[str, object]:
    """Return accepted values for frontmatter: int priority, no leading v, empty values dropped."""
    number = _priority_int(priority) if _given(priority) else None
    release = target_release.removeprefix("v") if isinstance(target_release, str) else target_release
    pairs = {"repo": repo, "epic": epic, "priority": number, "target_release": release}
    return {key: value for key, value in pairs.items() if _given(value)}
