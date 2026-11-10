"""Find empty or corrupt loose ref files under a repository's `.git/refs`.

The scan only reports. It never deletes or repairs a ref, and `.git/packed-refs` is not read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
SYMBOLIC_REF = re.compile(r"ref:[ \t]*\S[^\n]*")


def is_valid_ref(text: str) -> bool:
    """A 40 or 64 character hex id, or a one-line `ref: <target>`, with surrounding whitespace ignored."""
    body = text.strip()
    return OBJECT_ID.fullmatch(body) is not None or SYMBOLIC_REF.fullmatch(body) is not None


def corrupt_refs(refs: Mapping[str, str]) -> list[str]:
    """The sorted paths in `refs` whose text is not a valid ref."""
    return sorted(path for path, text in refs.items() if not is_valid_ref(text))


def read_text_or_empty(path: Path) -> str:
    """The file's text, or "" when it cannot be read, so the core reports it."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return ""


def read_refs(repo: Path, folder: Path) -> dict[str, str]:
    """Each regular file under `folder`, keyed by its path from `repo`; a directory that cannot be listed is skipped."""
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return {}
    files = {e.relative_to(repo).as_posix(): read_text_or_empty(e) for e in entries if e.is_file()}
    subdirs = [e for e in entries if e.is_dir() and not e.is_symlink()]
    return files | {path: text for d in subdirs for path, text in read_refs(repo, d).items()}


def scan_repo(repo: Path) -> list[str]:
    """Corrupt ref files, as paths relative to `repo` that rm accepts from its root; a directory is never reported."""
    top = repo / ".git" / "refs"
    return corrupt_refs(read_refs(repo, top) if top.is_dir() else {})
