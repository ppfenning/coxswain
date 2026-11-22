"""lifecycle-propose's reading of a patch, and its scratch hygiene.

Pure counts and parses of a unified diff (files, hunks, source against test lines), which files fall outside a
task's surfaces, and which of those are a builder's scratch files to drop from the patch rather than objections.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from core.bounded_proc import GIT_TIMEOUT_SECONDS, completed
from graphs.delivery.snapshot_surfaces import drop_pending_snapshots, is_surface_snapshot
from runner.tier_resolution import Hints


def is_test_path(path: str) -> bool:
    """A file whose changes are evidence for a change, not the change itself."""
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


_DIFF_PREFIXES = ("a/", "b/", "c/", "i/", "w/", "o/")


_DIFF_GIT_RE = re.compile(r"^diff --git \S+ (\S+)$", re.MULTILINE)


def _diff_path(token: str) -> str:
    return token[2:] if token[:2] in _DIFF_PREFIXES else token


def _file_chunks(patch: str) -> list[tuple[str, str]]:
    """One `(path, chunk)` per `diff --git` header, path taken from its `b/` (or `i/`) side."""
    headers = list(_DIFF_GIT_RE.finditer(patch))
    ends = [h.start() for h in headers[1:]] + [len(patch)]
    return [(_diff_path(header.group(1)), patch[header.end():end]) for header, end in zip(headers, ends)]


def _changed_in(chunk: str) -> int:
    return sum(
        1
        for line in chunk.splitlines()
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---"))
    )


def _lines_by_kind(patch: str) -> tuple[int, int]:
    """Added-plus-removed lines per file in the patch, split source from test."""
    chunks = _file_chunks(patch) or [("", patch)]
    source_lines = sum(_changed_in(chunk) for path, chunk in chunks if not is_test_path(path))
    test_lines = sum(_changed_in(chunk) for path, chunk in chunks if is_test_path(path))
    return source_lines, test_lines


_HUNK_RE = re.compile(r"^@@ -\d+,(\d+) \+\d+,(\d+) @@")


def patch_parses(patch: str) -> str | None:
    """None when every hunk header's counts match its body and the text ends with a newline."""
    if not patch.strip():
        return None
    name, hunk_index = "the patch", 0
    for path, chunk in _file_chunks(patch) or [("", patch)]:
        name = path or "the patch"
        lines = chunk.splitlines()
        hunk_index = 0
        for i, line in enumerate(lines):
            match = _HUNK_RE.match(line)
            if not match:
                continue
            hunk_index += 1
            old_count, new_count = int(match.group(1)), int(match.group(2))
            body: list[str] = []
            for later in lines[i + 1 :]:
                if later.startswith("@@"):
                    break
                body.append(later)
            seen_old = sum(1 for l in body if not l.startswith("+"))
            seen_new = sum(1 for l in body if not l.startswith("-"))
            if seen_old != old_count or seen_new != new_count:
                return f"the patch was cut off at {name} hunk {hunk_index}; emit the complete patch"
    if not patch.endswith("\n"):
        return f"the patch was cut off at {name} hunk {hunk_index}; emit the complete patch"
    return None


def _hints(
    patch: str,
    *,
    attempt: int | None = None,
    judgment: Literal["low", "normal", "high"] | None = None,
) -> Hints:
    """Call hints: files and added-plus-removed lines counted from `patch`, not asked of a model."""
    chunks = _file_chunks(patch)
    return Hints(
        judgment=judgment,
        files_changed=len(chunks) or None,
        lines_changed=sum(_changed_in(chunk) for _, chunk in chunks or [("", patch)]) if patch.strip() else None,
        attempt=attempt,
    )


def _change_facts(build: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic facts about the change, for the reviewer and the gate.

    Counted from the patch rather than asked of the model: a node reporting its
    own diff size is reporting a recollection, and the review tier keys off
    these numbers.
    """
    patch = build.get("patch") or ""
    lines = patch.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    files = list(build.get("files_touched") or [])
    source_lines, test_lines = _lines_by_kind(patch)
    return {
        "files_touched": files,
        "module_count": len({f.rsplit("/", 1)[0] for f in files}),
        "added_lines": added,
        "removed_lines": removed,
        "changed_lines": added + removed,
        "source_lines": source_lines,
        "test_lines": test_lines,
        "patch_ok": patch_parses(patch) is None,
    }


# Builders emit `a/`+`b/` or, under diff.mnemonicPrefix, `c/`+`i/`/`w/`;
# any one-letter prefix is stripped so the key is the repository path.
_PLUS_PATH_RE = re.compile(r"^\+\+\+ (?:[a-z]/)?(\S+)", re.MULTILINE)


_GIT_HEADER_PATH_RE = re.compile(r"^diff --git \S+ (?:[a-z]/)?(\S+)$", re.MULTILINE)


def _patch_files(patch: str) -> list[tuple[str, str]]:
    """One `(repository path, diff text)` per file, in patch order.

    A `diff --git` header opens each file's block, so an empty new file, which
    has no `+++` line, is still found; its text is its header block. Within a
    block the `+++` path wins when present. A deletion's `+++` side is
    /dev/null, and its header names the removed file. A block with no `+++`
    line is a file only when it is `new file mode`; a pure rename or a mode
    change names no new content and is skipped. A patch with no
    `diff --git` headers is sliced between its `+++` lines instead.
    """
    headers = list(_GIT_HEADER_PATH_RE.finditer(patch))
    if headers:
        ends = [h.start() for h in headers[1:]] + [len(patch)]
        blocks = [(h.group(1), patch[h.start() : end]) for h, end in zip(headers, ends)]
        return [((m.group(1) if m and m.group(1) != "/dev/null" else path), text)
                for path, text in blocks for m in [_PLUS_PATH_RE.search(text)]
                if m or re.search(r"^new file mode", text, re.MULTILINE)]
    marks = list(_PLUS_PATH_RE.finditer(patch))

    def path_of(mark: re.Match[str]) -> str:
        if mark.group(1) != "/dev/null":
            return mark.group(1)
        old = re.match(r"--- (?:[a-z]/)?(\S+)", patch[: mark.start()].rstrip("\n").rsplit("\n", 1)[-1])
        return old.group(1) if old else mark.group(1)

    return [(path_of(m), patch[m.end() : n.start() if n else len(patch)]) for m, n in zip(marks, marks[1:] + [None])]


def _patch_sections(patch: str) -> dict[str, str]:
    """Each touched file's own diff text, keyed by its repository path."""
    return dict(_patch_files(patch))


# A ticket's own contract commands, always backtick-fenced in its prose.
_CONTRACT_COMMAND_RE = re.compile(r"`(pytest -q[^`\n]*|ruff check \.)`")


def _build_output_valid(build: Mapping[str, Any], ticket_text: str) -> str | None:
    """None when the patch's files match `files_touched` and its contract commands ran with output."""
    files, touched = set(_patch_sections(build.get("patch") or "")), set(build.get("files_touched") or [])
    if files != touched:
        return f"files_touched {sorted(touched)} does not match the patch's files {sorted(files)}"
    run = [(str(e.get("command") or "").strip(), str(e.get("output") or "").strip())
           for e in build.get("commands_run") or [] if isinstance(e, Mapping)]
    missing = [c for c in _CONTRACT_COMMAND_RE.findall(ticket_text)
               if not any(_contract_command_matches(c, cmd) and out for cmd, out in run)]
    return f"commands_run has no output for {missing}" if missing else None


def _contract_command_matches(contract: str, ran: str) -> bool:
    """A contract line names a shape, not a byte string: `<your test file>`
    placeholders stand for one token, and the builder may chain or wrap the
    command (`… && ruff check .`, `… 2>&1 | tail -20`), so the contract must
    appear inside what ran, whitespace-normalised."""
    # A quoted command that trails off — `pytest -q <file> ...` in a chair
    # note — names its head, not a byte string: the tail is open.
    contract = re.sub(r"\s*(\.\.\.|\u2026)\s*$", "", " ".join(contract.split()))
    ran = " ".join(ran.split())
    parts = [re.escape(p) for p in re.split(r"<[^>]*>", contract)]
    if re.search(r"\S+".join(parts), ran) is not None:
        return True
    # A pytest line names files to prove, not an argv to reproduce: a run
    # that covers every named file — or a bare `pytest -q` over the whole
    # suite — is the same evidence, however the builder spelled it.
    want, got = _pytest_files(contract), _pytest_files(ran)
    if want is None or got is None:
        return False
    return not got or want <= got


def _pytest_files(command: str) -> set[str] | None:
    """The test paths a `pytest` invocation names (empty set = whole suite); None if it is not one."""
    head = command.split("|")[0].split("&&")[0].split(";")[0]
    if not re.match(r"^\s*(python\S*\s+-m\s+)?pytest\b", head):
        return None
    return {t for t in head.split()[1:] if not t.startswith("-") and (t.endswith(".py") or "/" in t)}


def _touched_paths(patch: str) -> list[str]:
    """Every path a unified diff touches, in patch order; an empty new file by its `diff --git` header, a deletion by its old path."""
    return [path for path, _ in _patch_files(patch or "")]


def _is_test_path(path: str) -> bool:
    """Whether `path` is a test or a test fixture: under a `tests/` or `test/` directory, or named `test_*.py` or
    `*_test.py`. Insta snapshots are judged by `is_surface_snapshot`, not here."""
    parts = path.split("/")
    name = parts[-1]
    return (
        any(part in ("tests", "test") for part in parts[:-1])
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


def _files_outside_surfaces(touched: Sequence[str], surfaces: Sequence[str]) -> list[str]:
    """`touched` paths not named in `surfaces`; empty whenever `surfaces` is empty.

    A surface written `path (new)` names the path; the marker is the
    decompose's note that the file does not exist yet, not part of it. A
    surface ending in `/` covers everything under it. Tests go with any
    change, so a test path is never outside; review still judges it. An insta
    `.snap` is inside when its module is a surface; a pending `.snap.new` is
    never a touched file of its own.
    """
    if not surfaces:
        return []
    declared = {re.sub(r"\s*\([^)]*\)\s*$", "", str(s_)) for s_ in surfaces}
    dirs = tuple(d for d in declared if d.endswith("/"))
    return [
        path
        for path in drop_pending_snapshots(touched)
        if path not in declared
        and not path.startswith(dirs)
        and not _is_test_path(path)
        and not is_surface_snapshot(path, declared)
    ]


_SCRATCH_SUFFIXES = (".diff", ".patch", ".out", ".log", ".orig", ".rej")


_SCRATCH_SUBSTRINGS = ("scratch", "probe", "debug")


def _is_scratch_name(path: str) -> bool:
    """Whether `path`'s own basename marks it as build scratch, not part of the change.

    A leading underscore, one of the diff/patch/log-family suffixes, or the
    literal substring `scratch`, `probe`, or `debug` anywhere in the name —
    matched case-as-written, with no folding beyond that.
    """
    name = path.rsplit("/", 1)[-1]
    return name.startswith("_") or name.endswith(_SCRATCH_SUFFIXES) or any(token in name for token in _SCRATCH_SUBSTRINGS)


def _is_tracked(repo_dir: str | None, path: str) -> bool:
    """Whether `path` is tracked in `repo_dir`'s index; `True` when that cannot be checked.

    A path this graph cannot verify against a real worktree is left as
    tracked, today's behavior, so the scratch exemption below only ever
    fires where a repository can prove the file is the build's own.
    """
    if not repo_dir:
        return True
    result = completed(
        ["git", "-C", repo_dir, "ls-files", "--error-unmatch", path],
        timeout=GIT_TIMEOUT_SECONDS, capture_output=True, text=True,
    )
    return result.returncode == 0


def _scratch_ignored(outside: Sequence[str], repo_dir: str | None) -> list[str]:
    """`outside` files that are the build's own scratch: scratch-named and untracked.

    A tracked file keeps counting toward `files_outside_surfaces` regardless
    of its name: `debug.py` already in the repository is not a probe the
    build dropped, it is a file the build touched.
    """
    return [path for path in outside if _is_scratch_name(path) and not _is_tracked(repo_dir, path)]


_SCRATCH_SECTION_RE = re.compile(
    r"(?:^diff --git \S+ \S+\n(?:(?!^---).*\n)*)?^--- \S.*\n^\+\+\+ (\S+).*\n",
    re.MULTILINE,
)


def _drop_scratch(patch: str, names: Sequence[str]) -> str:
    """`patch` with every named file's diff section removed, other sections untouched.

    A scratch file's hunk never reaches `_checkpoint_checks_pass`: a probe or
    a stray `.log` diff is not part of the ticket's own checks, whatever the
    worktree it was cut from.
    """
    if not names or not patch:
        return patch
    drop = set(names)
    sections = [(match.start(), _diff_path(match.group(1))) for match in _SCRATCH_SECTION_RE.finditer(patch)]
    if not sections:
        return patch
    ends = [start for start, _ in sections[1:]] + [len(patch)]
    prefix = patch[: sections[0][0]]
    return prefix + "".join(patch[start:end] for (start, path), end in zip(sections, ends) if path not in drop)
