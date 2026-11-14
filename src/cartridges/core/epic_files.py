"""Epic definition files: `epics/<epic-id>.md` in the work store root, beside `work/`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.epics import valid_epic_id
from core.landing import valid_branch
from core.workstore import WorkStoreError, _split_frontmatter


def _title(raw: Any) -> str | None:
    if raw is None or isinstance(raw, str):
        return raw
    raise ValueError(f"epic title must be a string, got {raw!r}")


def parse_epic(text: str, name: str) -> dict[str, Any]:
    """{id, title, target_branch} from one epic file's frontmatter; absent keys are None. Every failure is a ValueError.

    `issue` (the epic's tracker issue, which an initiative's pull request names as `Part of #n`) and `epic_stack`
    (its initiatives land as a stack on the epic branch) are added only when the file sets them."""
    stem = Path(name).stem
    try:
        meta, _ = _split_frontmatter(text, Path(name))
    except WorkStoreError as exc:
        raise ValueError(str(exc)) from exc
    epic_id = valid_epic_id(meta.get("id", stem))
    if epic_id != stem:
        raise ValueError(f"epic id {epic_id!r} must equal the file name {stem!r}")
    branch = meta.get("target_branch")
    return {
        "id": epic_id,
        "title": _title(meta.get("title")),
        "target_branch": None if branch is None else valid_branch(branch, "epic"),
        **({"issue": meta["issue"]} if meta.get("issue") is not None else {}),
        **({"epic_stack": meta["epic_stack"]} if isinstance(meta.get("epic_stack"), bool) else {}),
    }


def _read_one(path: Path) -> dict[str, Any]:
    try:
        return parse_epic(path.read_text(encoding="utf-8"), path.name)
    except ValueError as exc:
        raise WorkStoreError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise WorkStoreError(f"{path}: cannot be read: {exc}") from exc


def read_epic_definitions(store_root: Path | str) -> dict[str, dict[str, Any]]:
    """Epic id to definition for every `*.md` under `<store_root>/epics/`; the `definitions` argument of `rollup`."""
    directory = Path(store_root) / "epics"
    if not directory.is_dir():
        return {}
    definitions = (_read_one(path) for path in sorted(directory.glob("*.md")))
    return {definition["id"]: definition for definition in definitions}
