"""The pre-build ticket check: names, then claims, over lookups read from one commit.

`check_ticket` is pure. `ticket_lookups` is the edge: it builds the two callables the pure checks take.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from functools import cache

from harness.ticket_claims import Hit, check_claims
from harness.ticket_names import check_names

__all__ = ["LookupUnavailable", "check_ticket", "ticket_lookups"]

GitStdout = Callable[..., str | None]

_TIMEOUT_SECONDS = 30
_BUDGET_SECONDS = 60.0
_KEYWORDS = "def|class|fn|func|function|struct|enum|trait|interface|type|const|let|var|static|impl|macro_rules!"
_METHOD_PREFIX = "async|static|public|private|protected|get|set"
# Extensions whose definition syntax the patterns cover, and data files that never define anything.
_COVERED = (".py", ".rs", ".go", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_DATA = (".md", ".rst", ".txt", ".yaml", ".yml", ".toml", ".json", ".lock", ".snap")


class LookupUnavailable(RuntimeError):
    """The tree could not be read, so "no such name" cannot be told apart from "could not look"."""


def check_ticket(
    task: Mapping[str, object],
    paths: frozenset[str],
    defined: Callable[[str], Sequence[str]],
    search: Callable[[str], Sequence[Hit]],
) -> tuple[dict[str, object], tuple[str, ...]]:
    """The ticket healed by check_names, its lint extended by each correction prefixed `correction:`, and all corrections."""
    named = check_names(task, paths, defined)
    corrections = (*named.corrections, *check_claims(named.task, search))
    if not corrections:
        return dict(named.task), ()
    lint = named.task.get("lint")
    prior = list(lint) if isinstance(lint, (list, tuple)) else []
    noted = [f"correction: {c}" for c in corrections]
    return {**named.task, "lint": list(dict.fromkeys([*prior, *noted]))}, corrections


def _unprefixed(line: str, commit: str) -> str | None:
    return line[len(commit) + 1 :] if line.startswith(f"{commit}:") else None


def _hit(line: str, commit: str) -> Hit | None:
    rest = _unprefixed(line, commit)
    if rest is None:
        return None
    path, _, tail = rest.partition(":")
    number, _, text = tail.partition(":")
    return (path, int(number), text) if path and number.isdigit() else None


def _definition_patterns(name: str) -> tuple[str, ...]:
    """Extended regexes for a line that defines `name`: after a keyword, bound by `=`, or as a braced method."""
    n = re.escape(name)
    edge = "(^|[^[:alnum:]_])"
    tail = "([^[:alnum:]_]|$)"
    return (
        rf"{edge}({_KEYWORDS})[[:space:]]+([^;{{=]*[^[:alnum:]_])?{n}{tail}",
        rf"{edge}{n}[[:space:]]*:?=([^=]|$)",
        rf"^[[:space:]]*(({_METHOD_PREFIX})[[:space:]]+)*{n}[[:space:]]*\([^)]*\)[^;]*\{{[[:space:]]*$",
    )


def ticket_lookups(
    repo: str,
    commit: str,
    git_stdout: GitStdout,
    clock: Callable[[], float] = time.monotonic,
    budget_seconds: float = _BUDGET_SECONDS,
) -> tuple[Callable[[str], tuple[str, ...]], Callable[[str], tuple[Hit, ...]]]:
    """Edge. `defined` and `search` over the file contents at `commit`, read with `git grep` through `git_stdout`.

    `git_stdout(*args, timeout=, ok_codes=)` returns stdout or None. Each call is cut off after 30 seconds, all calls
    together after `budget_seconds` of git time, and a repeated query is answered from memory.
    A read that fails gives no hits from `search`, so no claim is called false. `defined` raises LookupUnavailable
    instead of returning empty, because empty would read as "no such name". It also reports a name that only
    appears in a source language the patterns do not cover, so a definition it cannot recognise is never denied.
    """
    spent = [0.0]

    @cache
    def grep(args: tuple[str, ...]) -> str | None:
        if not commit or spent[0] > budget_seconds:
            return None
        started = clock()
        out = git_stdout("-C", repo, "grep", "--no-color", *args, commit, timeout=_TIMEOUT_SECONDS, ok_codes=(0, 1))
        spent[0] += clock() - started
        return out

    def listed(args: tuple[str, ...]) -> tuple[str, ...]:
        out = grep(args)
        if out is None:
            raise LookupUnavailable(f"could not read the tree at {commit or 'no commit'}")
        return tuple(sorted({p for p in (_unprefixed(line, commit) for line in out.splitlines()) if p}))

    def defined(name: str) -> tuple[str, ...]:
        patterns = tuple(arg for p in _definition_patterns(name) for arg in ("-e", p))
        found = listed(("-l", "-E", *patterns))
        if found:
            return found
        mentioned = listed(("-l", "-w", "-F", "-e", name))
        return tuple(p for p in mentioned if not p.endswith(_COVERED + _DATA))

    def search(word: str) -> tuple[Hit, ...]:
        out = grep(("-n", "-w", "-F", "-e", word))
        if out is None:
            return ()
        return tuple(h for h in (_hit(line, commit) for line in out.splitlines()) if h)

    return defined, search
