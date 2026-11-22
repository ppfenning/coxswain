"""Shared by the tests that prove a cartridge writer keeps the file's comments."""

from __future__ import annotations

import difflib


def _edits(before: str, after: str) -> list[tuple[str, list[str], list[str]]]:
    """The non-equal line ranges between two texts, as (tag, lines before, lines after)."""
    a, b = before.splitlines(), after.splitlines()
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    return [(tag, a[i1:i2], b[j1:j2]) for tag, i1, i2, j1, j2 in ops if tag != "equal"]


def _comments(text: str) -> list[str]:
    return [line for line in text.splitlines() if "#" in line]
