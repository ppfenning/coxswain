"""The handoff a finished task leaves for the tasks that need it, and the plan context built from them.

Pure: the patch, branch and commit arrive as arguments. Only the five HANDOFF_KEYS ever cross a lane,
so a sibling's transcript or any other field of its build result cannot reach a plan.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

HANDOFF_KEYS = ("files_changed", "interfaces_added", "decisions", "branch", "commit")

_LIST_KEYS = ("files_changed", "interfaces_added", "decisions")
_PLUS_RE = re.compile(r"^\+\+\+ (?:[a-z]/)?(\S+)", re.MULTILINE)
_MINUS_RE = re.compile(r"--- (?:[a-z]/)?(\S+)")


def files_from_patch(patch: str) -> list[str]:
    """Repository paths in first-seen order. A deletion is named by its `---` line."""

    def path_of(mark: re.Match[str]) -> str:
        if mark.group(1) != "/dev/null":
            return mark.group(1)
        old = _MINUS_RE.match(patch[: mark.start()].rstrip("\n").rsplit("\n", 1)[-1])
        return old.group(1) if old else mark.group(1)

    paths = (path_of(m) for m in _PLUS_RE.finditer(patch))
    return list(dict.fromkeys(p for p in paths if p != "/dev/null"))


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


# The build result is the dict lifecycle_propose's build node returns: `patch` is its unified diff,
# and `interfaces_added` and `decisions` are optional list fields on it, absent on most builds.
def build_handoff(build_result: Mapping[str, Any], branch: str, commit: str) -> dict[str, Any]:
    return {
        "files_changed": files_from_patch(str(build_result.get("patch") or "")),
        "interfaces_added": _strings(build_result.get("interfaces_added")),
        "decisions": _strings(build_result.get("decisions")),
        "branch": branch,
        "commit": commit,
    }


def _clean(record: Mapping[str, Any]) -> dict[str, Any]:
    """The record cut down to the five keys, with absent ones defaulted."""
    return {k: _strings(record.get(k)) if k in _LIST_KEYS else str(record.get(k) or "") for k in HANDOFF_KEYS}


def serialise_handoff(record: Mapping[str, Any]) -> str:
    """The JSON stored in the handoff column."""
    return json.dumps(_clean(record), sort_keys=True)


def parse_handoff(text: str) -> dict[str, Any]:
    loaded = json.loads(text)
    return _clean(loaded if isinstance(loaded, dict) else {})


def render_handoff(task: str, record: Mapping[str, Any]) -> str:
    clean = _clean(record)
    lines = [f"## Handoff from {task}", f"branch: {clean['branch']}", f"commit: {clean['commit']}"]
    for key in _LIST_KEYS:
        lines += [f"{key}:", *(f"- {v}" for v in clean[key])]
    return "\n".join(lines)


def plan_context(item: Mapping[str, Any], handoffs_by_task: Mapping[str, Mapping[str, Any]]) -> str:
    """The item's own body, then one section per task in its `needs`, in `needs` order."""
    needed = [t for t in item.get("needs") or [] if t in handoffs_by_task]
    return "\n\n".join([str(item.get("body") or ""), *(render_handoff(t, handoffs_by_task[t]) for t in needed)])
