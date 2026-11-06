"""`route epic new` core: write one epic definition file, `epics/<id>.md` in the work store root, beside `work/`.

`render_epic`, `invalid_reason` and `unreadable_reason` are pure. `create_epic` is the edge: it validates, refuses an id
that already has a file, checks that the rendered text reads back as given, and only then writes. The read-back check runs
before the write and on a dry run, so a title the quoting helper cannot protect (`123`, `true`, `null`, a newline) is
refused with nothing on disk. An epic definition lives only as a file, so nothing here touches the store, and an existing
file is never overwritten.
"""

from __future__ import annotations

from pathlib import Path

from agent_tools import route, route_guard

DONE = route_guard.DONE
REFUSED = route_guard.REFUSED
FAILED = 1  # the file could not be written, as in route_edit


def render_epic(epic_id: str, title: str | None, target_branch: str | None) -> str:
    """The file text: frontmatter with id, then title and target_branch when given, a closing fence and an empty body line."""
    given = (("title", title), ("target_branch", target_branch))
    lines = [f"id: {epic_id}", *(f"{key}: {route._yaml_scalar(value)}" for key, value in given if value is not None)]
    return "\n".join(["---", *lines, "---", "", ""])


def invalid_reason(epic_id: str, target_branch: str | None) -> str | None:
    """Why the id or the branch cannot be written, else None. The branch is checked only when given."""
    from core.epics import valid_epic_id  # lazy: core sits beside agent_tools in one venv
    from core.landing import valid_branch  # lazy: core sits beside agent_tools in one venv

    try:
        valid_epic_id(epic_id)
        if target_branch is not None:
            valid_branch(target_branch, "epic")
    except ValueError as exc:
        return str(exc)
    return None


def unreadable_reason(text: str, epic_id: str, title: str | None, target_branch: str | None) -> str | None:
    """Why `text` would not read back through `parse_epic` as exactly the given id, title and branch, else None.

    The quoting helper leaves a bare `123`, `true`, `null` or `2026-01-01` as written, and YAML reads those as a number,
    a bool, None or a date. A newline in a title breaks the frontmatter. Only the parsed text can say which.
    """
    from core.epic_files import parse_epic  # lazy: core sits beside agent_tools in one venv

    expected = {"id": epic_id, "title": title, "target_branch": target_branch}
    try:
        got = parse_epic(text, f"{epic_id}.md")
    except ValueError as exc:
        return f"the epic file would not read back: {exc}"
    return None if got == expected else f"the epic file would read back as {got!r}, not {expected!r}"


def create_epic(ws: Path, epic_id: str, title: str | None, target_branch: str | None, dry_run: bool) -> tuple[int, str]:
    """Write `<ws>/epics/<id>.md` and return (code, message). REFUSED when the id, branch or title is unusable or the file exists.

    With `dry_run` the message is the text that would be written and nothing is created. Never overwrites.
    """
    reason = invalid_reason(epic_id, target_branch)
    if reason is not None:
        return REFUSED, reason
    path = ws / "epics" / f"{epic_id}.md"
    if path.exists():
        return REFUSED, f"{path} already exists; not overwriting"
    text = render_epic(epic_id, title, target_branch)
    reason = unreadable_reason(text, epic_id, title, target_branch)
    if reason is not None:
        return REFUSED, reason
    if dry_run:
        return DONE, text
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:  # mode x: a file that appeared meanwhile is never overwritten
            handle.write(text)
    except FileExistsError:
        return REFUSED, f"{path} already exists; not overwriting"
    except OSError as exc:
        return FAILED, f"{path} cannot be written: {exc}"
    return DONE, f"wrote {path}"
