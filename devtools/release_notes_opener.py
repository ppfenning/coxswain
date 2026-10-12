"""Draft a release-notes opener from the merged bullets, and reject any draft that breaks the prose rules."""

from __future__ import annotations

import re
from collections.abc import Callable

BANNED_WORDS: tuple[str, ...] = (
    "genuinely",
    "exactly",
    "precisely",
    "robust",
    "comprehensive",
    "thorough",
)
BANNED_MARKS: tuple[str, ...] = ("—", "(", "->", "→")

MIN_SENTENCES = 3
MAX_SENTENCES = 5

_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")


def opener_prompt(version: str, bullets: list[str]) -> str:
    lines = "\n".join(f"- {bullet}" for bullet in bullets)
    words = ", ".join(BANNED_WORDS)
    marks = " ".join(BANNED_MARKS)
    return (
        f"Write the opening paragraph of the release notes for version {version}.\n"
        f"Write {MIN_SENTENCES} to {MAX_SENTENCES} sentences in the past tense, one claim per sentence.\n"
        "State nothing that the bullets below do not state.\n"
        f"Do not use these words: {words}.\n"
        f"Do not use these marks: {marks}\n"
        "Bullets:\n"
        f"{lines}\n"
    )


def _sentence_count(text: str) -> int:
    return len([piece for piece in _SENTENCE_END.split(text) if piece.strip()])


def check_opener(text: str) -> tuple[str, ...]:
    count = _sentence_count(text)
    length = (
        (f"sentence count {count} is outside {MIN_SENTENCES} to {MAX_SENTENCES}",)
        if not MIN_SENTENCES <= count <= MAX_SENTENCES
        else ()
    )
    words = tuple(
        f"banned word: {word}"
        for word in BANNED_WORDS
        if re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE)
    )
    marks = tuple(f"banned mark: {mark}" for mark in BANNED_MARKS if mark in text)
    return length + words + marks


def draft_opener(
    version: str,
    bullets: list[str],
    complete: Callable[[str], str],
    retries: int = 1,
) -> str | None:
    prompt = opener_prompt(version, bullets)
    text = complete(prompt)
    violations = check_opener(text)
    for _ in range(retries):
        if not violations:
            break
        fix = "\n".join(f"- {violation}" for violation in violations)
        text = complete(f"{prompt}\nThe previous draft broke these rules. Fix them:\n{fix}\n")
        violations = check_opener(text)
    return None if violations else text
