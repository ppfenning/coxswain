"""Which repo a land is recorded against: the umbrella's working clone, never its install checkout."""

import os

REFUSAL_PREFIX = "refused: "


def _normal(path: str) -> str:
    # expanduser reads HOME for a leading `~`; it is the one lookup the setting's own contract asks for.
    return os.path.expanduser(path).rstrip("/") or "/"


def working_clone(profile: dict) -> str | None:
    """The expanded `umbrella_clone_dir`, or None when absent or empty."""
    value = profile.get("umbrella_clone_dir")
    return _normal(value) if value else None


def filing_repo(repo: str, profile: dict) -> str:
    """The repo to record, or a string starting with REFUSAL_PREFIX. Only the install checkout is redirected."""
    umbrella = profile.get("umbrella_dir")
    clone = working_clone(profile)
    if not umbrella or _normal(repo) != _normal(umbrella):
        return repo
    if clone:
        return clone
    return f"{REFUSAL_PREFIX}{repo} is the umbrella install checkout; set umbrella_clone_dir to the working clone"
