"""Pure guard for where a land may run: never from a detached or release-install checkout. No git, no profile, no clock."""

import os
from collections.abc import Iterable
from typing import NamedTuple


class Verdict(NamedTuple):
    refusal: str | None
    notice: str | None


def _norm(path: str) -> str:
    return os.path.expanduser(path).rstrip("/") or "/"


def is_install_managed(repo: str, install_roots: Iterable[str]) -> bool:
    return _norm(repo) in {_norm(root) for root in install_roots}


def _clone_hint(clone: str | None) -> str:
    if clone is not None:
        return f"Run the land from the working clone {clone}."
    return "Set umbrella_clone_dir to the working clone and run the land from there."


def verdict(is_detached: bool, install_managed: bool, clone: str | None) -> Verdict:
    if is_detached and install_managed:
        return Verdict(f"This is a release install checkout on a detached HEAD. {_clone_hint(clone)}", None)
    if is_detached:
        return Verdict(f"This checkout is on a detached HEAD. {_clone_hint(clone)}", None)
    if install_managed:
        return Verdict(None, "This is an install checkout: the land moves the code the chair runs from.")
    return Verdict(None, None)
