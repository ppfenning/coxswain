"""Report empty or corrupt ref files under a repository's .git/refs. Report only: nothing is deleted or repaired.

The core takes repo-relative keys such as `.git/refs/heads/epic/x/p1`. The edge returns `str(repo / key)`, with `repo`
as the caller gave it, so each result can be passed to `rm` as printed. Packed refs are not read.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


def is_valid_ref(text: str) -> bool:
    """A 40 or 64 digit hex object id, or a `ref:` symbolic ref, ignoring surrounding whitespace."""
    body = text.strip()
    return bool(_OBJECT_ID.fullmatch(body)) or body.startswith("ref:")


def corrupt_refs(refs: Mapping[str, str]) -> list[str]:
    """The sorted paths whose text is not a valid ref."""
    return sorted(path for path, text in refs.items() if not is_valid_ref(text))


def scan_repo_refs(repo: Path) -> list[str]:
    """Corrupt ref files under `repo/.git/refs`, as `repo/<relative path>` strings; an unreadable file counts as corrupt."""
    refs_dir = repo / ".git" / "refs"
    if not refs_dir.is_dir():
        return []
    readable: dict[str, str] = {}
    unreadable: list[str] = []
    for root, _dirs, names in os.walk(refs_dir):
        for name in names:
            file = Path(root) / name
            if file.is_symlink() or not file.is_file():
                continue
            key = file.relative_to(repo).as_posix()
            try:
                readable[key] = file.read_text(encoding="utf-8")
            except OSError, UnicodeDecodeError:
                unreadable.append(key)
    return [str(repo / key) for key in sorted([*corrupt_refs(readable), *unreadable])]
