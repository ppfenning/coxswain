"""Set planner: one setting change as new file text, a unified diff, and the operator_only flag.

Pure: takes scope, key, the raw value string and the current text of the target file,
returns a `Plan` or a `PlanError`. Nothing is read, written or raised.

The new text is checked with the loaders the chair uses. The profile goes through
`route.parse_profile`, and a `ProfileError` message follows the `scope:key` of the setting
in the error reason. The cartridge goes through `chair_cap`, whose loader swallows parse errors and returns
None for a value it will not use, so it has no message; the reason names the rule instead.
`validate`, when given, is an extra cartridge schema check, `text -> list of error strings`.

A cartridge is rewritten with `yaml.safe_dump`, so its comments and blank lines do not
survive and the diff shows them. A profile keeps the old text: only the target key's lines
are replaced, or added at the end of their block or of the file. Comments, blank lines and
flow mappings such as `notify: {ntfy: ...}` stay byte for byte. The replacement lines use the
flat format `parse_profile` reads: one-line JSON for `sources` and `repo_map`, `[a, b]` lists,
and indented `lane_hosts` items. The registry names spend keys flat because `parse_profile`
flattens them; the file keeps them under `spend:`. A profile refusal begins `profile:<key>:`.
"""

from __future__ import annotations

import difflib
import json
import textwrap
from collections.abc import Callable
from dataclasses import dataclass

import yaml

from agent_tools import chair_cap, route

# Private names, imported the way settings_model.py imports them.
from agent_tools.editor_model import _MISSING, _get, _set_nested
from agent_tools.settings_model import STORE_MODES, Setting, setting_for

# Cartridge keys the chair reads, with the reader it calls and the rule it enforces.
_CHAIR_READS = {
    "policy.dispatch.max_in_flight": (chair_cap.cap_from_cartridge, "a positive integer"),
    "policy.dispatch.local_lanes": (chair_cap.local_lanes_from_cartridge, f"one of {', '.join(chair_cap.LOCAL_LANES)}"),
}


@dataclass(frozen=True)
class Plan:
    new_text: str
    diff: str
    operator_only: bool


@dataclass(frozen=True)
class PlanError:
    reason: str


def _scalar(raw: str) -> object | PlanError:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return PlanError(f"not a valid value: {raw!r}")


def _coerce(current: object, raw: str) -> object | PlanError:
    """`raw` in the type of `current`; with no current value it is parsed as a YAML scalar."""
    kind = type(current)
    if current is _MISSING or current is None:
        return _scalar(raw)
    if kind in (bool, list, dict):
        parsed = _scalar(raw)
        if isinstance(parsed, (PlanError, kind)):
            return parsed
        return PlanError(f"expected {kind.__name__}, got {raw!r}")
    try:
        return kind(raw)
    except (TypeError, ValueError):
        return PlanError(f"expected {kind.__name__}, got {raw!r}")


def _load(text: str) -> dict | PlanError:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return PlanError(f"current file does not parse: {exc}")
    if doc is None:
        return {}
    return doc if isinstance(doc, dict) else PlanError("current file is not a mapping")


def _parts(scope: str, doc: dict, path: str) -> list[str]:
    """Path segments; `crew` addresses `cast` when the document uses that older name, and a
    profile spend key lives under `spend:` in the file."""
    head, *rest = path.split(".")
    if scope == "profile" and path in _PROFILE_SPEND_KEYS:
        return ["spend", path]
    return ["cast" if head == "crew" and "crew" not in doc and "cast" in doc else head, *rest]


# Private names of route, the profile parser's own key sets.
_PROFILE_SPEND_KEYS = route._SPEND_KEYS | route._SPEND_TEXT_KEYS
_PROFILE_BLOCKS = ("spend", "chair", "store")


def _profile_scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _profile_entry(key: str, value: object) -> str:
    """One top-level entry in the flat format `route.parse_profile` reads."""
    if key in route._JSON_KEYS:
        return f"{key}: {json.dumps(value)}\n"
    if key in _PROFILE_BLOCKS and isinstance(value, dict):
        return f"{key}:\n" + "".join(f"  {k}: {_profile_scalar(v)}\n" for k, v in value.items())
    if key == "lane_hosts" and isinstance(value, list):
        return f"{key}:\n" + textwrap.indent(yaml.safe_dump(value, sort_keys=False), "  ")
    if isinstance(value, list):
        return f"{key}: [{', '.join(_profile_scalar(v) for v in value)}]\n"
    return f"{key}: {_profile_scalar(value)}".rstrip() + "\n"


def _is_content(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


def _is_top(line: str) -> bool:
    """A top-level entry line: content that is neither indented nor a `- ` item."""
    return _is_content(line) and line[0] not in " \t-"


def _find(lines: list[str], lo: int, hi: int, key: str, top: bool) -> int | None:
    """Index of the last line in `lines[lo:hi]` that holds `key`; None when there is none."""
    hits = [
        i for i in range(lo, hi)
        if (_is_top(lines[i]) if top else _is_content(lines[i])) and lines[i].partition(":")[0].strip() == key
    ]
    return hits[-1] if hits else None


def _entry_end(lines: list[str], start: int) -> int:
    """Index just past the entry at `start`: its own line and the indented lines after it.
    Trailing blank and comment lines belong to whatever follows."""
    stop = next((i for i in range(start + 1, len(lines)) if _is_top(lines[i])), len(lines))
    return max([start, *(i for i in range(start + 1, stop) if _is_content(lines[i]))]) + 1


def _join(lines: list[str], lo: int, hi: int, new: list[str]) -> str:
    """`lines` with `lines[lo:hi]` replaced by `new`; a last line lacking its newline gets one."""
    before = [line if line.endswith("\n") else line + "\n" for line in lines[:lo]]
    return "".join([*before, *new, *lines[hi:]])


def _splice_profile(text: str, parts: list[str], value: object) -> str | PlanError:
    """`text` with only the lines of the key at `parts` replaced, or added when it is absent."""
    lines = text.splitlines(keepends=True)
    head = parts[0]
    start = _find(lines, 0, len(lines), head, True)
    if len(parts) == 1:
        entry = _profile_entry(head, value).splitlines(keepends=True)
        if start is None:
            return _join(lines, len(lines), len(lines), entry)
        return _join(lines, start, _entry_end(lines, start), entry)
    if len(parts) > 2:
        return PlanError(f"{'.'.join(parts)} is nested deeper than one block")
    leaf = parts[1]
    if start is None:
        return _join(lines, len(lines), len(lines), [f"{head}:\n", f"  {leaf}: {_profile_scalar(value)}\n"])
    inline = lines[start].partition(":")[2].strip()
    if inline and not inline.startswith("#"):
        return PlanError(f"{head} is written inline, so {leaf} has no line of its own")
    end = _entry_end(lines, start)
    at = _find(lines, start + 1, end, leaf, False)
    if at is not None:
        indent = lines[at][: len(lines[at]) - len(lines[at].lstrip())]
        return _join(lines, at, at + 1, [f"{indent}{leaf}: {_profile_scalar(value)}\n"])
    last = lines[end - 1]
    indent = last[: len(last) - len(last.lstrip())] if end > start + 1 else "  "
    return _join(lines, end, end, [f"{indent}{leaf}: {_profile_scalar(value)}\n"])


def _blocked(doc: dict, parts: list[str]) -> bool:
    """True when a parent of the target exists and is not a mapping."""
    parents = (_get(doc, ".".join(parts[:n])) for n in range(1, len(parts)))
    return any(p is not _MISSING and not isinstance(p, dict) for p in parents)


def _diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}",
    ))


def _refusal(target: str) -> Callable[[str], PlanError]:
    """A builder of refusals that lead with `target`; with no target the reason stands alone."""
    return lambda reason: PlanError(f"{target}: {reason}" if target else reason)


def _chair_error(setting: Setting, new_text: str) -> str | None:
    """Why the chair's own loader rejects `new_text`; None when it accepts it."""
    if setting.scope == "profile":
        try:
            route.parse_profile(new_text)
        except route.ProfileError as exc:
            return str(exc)
        return None
    rule = _CHAIR_READS.get(setting.path)
    if rule is None or rule[0](new_text) is not None:
        return None
    return f"{setting.path}: the chair ignores this value, expected {rule[1]}"


def plan_set(
    scope: str, key: str, raw: str, text: str, validate: Callable[[str], list[str]] | None = None,
) -> Plan | PlanError:
    setting = setting_for(scope, key)
    if setting is None:
        return PlanError(f"unknown setting {scope}:{key}")
    is_profile = setting.scope == "profile"
    refuse = _refusal(f"{scope}:{key}" if is_profile else "")
    doc = _load(text)
    if isinstance(doc, PlanError):
        return refuse(doc.reason)
    parts = _parts(setting.scope, doc, setting.path)
    if _blocked(doc, parts):
        return refuse("a parent key is not a mapping" if is_profile else f"{setting.path}: a parent key is not a mapping")
    value = _coerce(_get(doc, ".".join(parts)), raw)
    if isinstance(value, PlanError):
        return PlanError(f"{scope}:{key}: {value.reason}")
    if is_profile and setting.path == "store.mode" and value not in STORE_MODES:
        return refuse(f"store.mode must be one of {', '.join(STORE_MODES)}, got '{value}'")
    if is_profile:
        new_text = _splice_profile(text, parts, value)
        if isinstance(new_text, PlanError):
            return refuse(new_text.reason)
    else:
        new_text = yaml.safe_dump(_set_nested(doc, parts, value), sort_keys=False)
    chair_error = _chair_error(setting, new_text)
    if chair_error is not None:
        return refuse(chair_error)
    errors = validate(new_text) if validate is not None and setting.scope == "cartridge" else []
    if errors:
        return PlanError(f"{setting.source_file} violates the schema: {'; '.join(errors)}")
    return Plan(new_text, _diff(text, new_text, setting.source_file), setting.operator_only)
