"""Set planner: one setting change as new file text, a unified diff, and the operator_only flag.

Pure: takes scope, key, the raw value string and the current text of the target file,
returns a `Plan` or a `PlanError`. Nothing is read, written or raised.

The new text is checked with the loaders the chair uses. The profile goes through
`route.parse_profile`, and a `ProfileError` message follows the `scope:key` of the setting
in the error reason. The cartridge goes through `chair_cap`, whose loader swallows parse errors and returns
None for a value it will not use, so it has no message; the reason names the rule instead.
`validate`, when given, is an extra cartridge schema check, `text -> list of error strings`.

A cartridge and a profile both keep the old text: only the target key's lines are replaced,
or added at the end of their block or of the file. Comments, blank lines, key order and
flow mappings such as `notify: {ntfy: ...}` stay byte for byte. A comment on the changed
key's own line stays. A cartridge value is written as one flow-style line. A key inside a
flow mapping, such as a crew seat, is edited in place: only the bytes of that value change,
or one `key: value` is added before the closing brace. A block value that spans lines and
holds a `#` is refused instead of dropping the comment. A value that is already set leaves
the text untouched, and an edit that would repeat a key or land elsewhere is refused. The profile
replacement lines use the
flat format `parse_profile` reads: one-line JSON for `sources` and `repo_map`, `[a, b]` lists,
and indented `lane_hosts` items. The registry names spend keys flat because `parse_profile`
flattens them; the file keeps them under `spend:`. A profile refusal begins `profile:<key>:`.
"""

from __future__ import annotations

import difflib
import functools
import json
import textwrap
from collections.abc import Callable
from dataclasses import dataclass

import yaml

from agent_tools import chair_cap, route

# Private names, imported the way settings_model.py imports them.
from agent_tools.editor_model import _MISSING, _get
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


def _key_of(line: str) -> str:
    """The key a `key: value` line holds, without the quotes a file may put around it."""
    return line.partition(":")[0].strip().strip("\"'")


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


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _flow(value: object) -> str:
    """`value` on one line, quoted so it is safe inside a flow mapping; non-ASCII stays as written."""
    dumped = yaml.safe_dump([value], default_flow_style=True, sort_keys=False, allow_unicode=True, width=10**9)
    return dumped.strip()[1:-1]


def _trailing_comment(rest: str) -> str:
    """The spaces and `# ...` ending `rest`, the text after a key's colon; "" when there is none."""
    rest = rest.rstrip("\n")
    for i, ch in enumerate(rest):
        if ch != "#" or i == 0 or rest[i - 1] not in " \t":
            continue
        try:
            yaml.safe_load(rest[:i])
        except yaml.YAMLError:
            continue
        return rest[len(rest[:i].rstrip()):]
    return ""


def _rewrite(lines: list[str], at: int, end: int, value: object) -> str | PlanError:
    """`lines` with the entry at `lines[at:end]` as one line; a `#` inside several lines is refused."""
    key = lines[at].partition(":")[0].strip()
    if end - at > 1 and any("#" in line for line in lines[at:end]):
        return PlanError(f"{_key_of(lines[at])} spans lines and holds a comment, so it cannot be rewritten without losing it")
    comment = _trailing_comment(lines[at].partition(":")[2]) if end - at == 1 else ""
    return _join(lines, at, end, [f"{_indent(lines[at])}{key}: {_flow(value)}{comment}\n"])


def _find_child(lines: list[str], lo: int, hi: int, key: str) -> int | None:
    """Index of the last direct child `key` in `lines[lo:hi]`, the lines under one parent."""
    rows = [i for i in range(lo, hi) if _is_content(lines[i])]
    width = len(_indent(lines[rows[0]])) if rows else 0
    hits = [i for i in rows if len(_indent(lines[i])) == width and _key_of(lines[i]) == key]
    return hits[-1] if hits else None


def _block_end(lines: list[str], start: int, hi: int) -> int:
    """Like `_entry_end` for an indented entry: its lines run until content no deeper than it."""
    base = len(_indent(lines[start]))
    stop = next((
        i for i in range(start + 1, hi)
        if _is_content(lines[i]) and len(_indent(lines[i])) <= base and not lines[i].lstrip().startswith("-")
    ), hi)
    return max([start, *(i for i in range(start + 1, stop) if _is_content(lines[i]))]) + 1


def _child(lines: list[str], lo: int, hi: int, key: str, root: bool) -> int | None:
    at = _find(lines, lo, hi, key, True) if root else None
    return at if at is not None else _find_child(lines, lo, hi, key)


def _end(lines: list[str], at: int, hi: int, root: bool) -> int:
    return _entry_end(lines, at) if root else _block_end(lines, at, hi)


def _splice_flow(region: str, rest: list[str], value: object) -> str | PlanError:
    """`region`, a `key: {...}` entry, with the value at `rest` replaced or added inside the braces."""
    try:
        root = yaml.compose(region, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        return PlanError(f"cannot read the entry: {exc}")
    node = root.value[0][1] if isinstance(root, yaml.MappingNode) and len(root.value) == 1 else None
    done = 0
    for name in rest:
        pairs = node.value if isinstance(node, yaml.MappingNode) else []
        found = next((v for k, v in pairs if isinstance(k, yaml.ScalarNode) and k.value == name), None)
        if found is None:
            break
        node, done = found, done + 1
    if done == len(rest):
        return region[: node.start_mark.index] + _flow(value) + region[node.end_mark.index :]
    if not isinstance(node, yaml.MappingNode) or not node.flow_style:
        return PlanError(f"{rest[done]} cannot be added: its parent is not a flow mapping")
    entry = f"{rest[done]}: {_flow(functools.reduce(lambda acc, name: {name: acc}, reversed(rest[done + 1 :]), value))}"
    if not node.value:
        return region[: node.end_mark.index - 1] + entry + region[node.end_mark.index - 1 :]
    last = node.value[-1][1].end_mark.index
    return region[:last] + ", " + entry + region[last:]


def _splice_path(text: str, parts: list[str], value: object) -> str | PlanError:
    """`text` with only the key at `parts` replaced, or the missing keys added at the end of the deepest section."""
    lines = text.splitlines(keepends=True)
    lo, hi, found, start = 0, len(lines), 0, None
    for seg in parts[:-1]:
        root = start is None
        at = _child(lines, lo, hi, seg, root)
        if at is None:
            break
        end = _end(lines, at, hi, root)
        inline = lines[at].partition(":")[2].strip()
        if inline and not inline.startswith("#"):
            edited = _splice_flow("".join(lines[at:end]), parts[found + 1 :], value)
            return edited if isinstance(edited, PlanError) else _join(lines, at, end, [edited])
        lo, hi, start, found = at + 1, end, at, found + 1
    missing = parts[found:]
    leaf = _child(lines, lo, hi, parts[-1], start is None) if len(missing) == 1 else None
    if leaf is not None:
        return _rewrite(lines, leaf, _end(lines, leaf, hi, start is None), value)
    first = next((i for i in range(lo, hi) if _is_content(lines[i])), None)
    indent = _indent(lines[first]) if first is not None else "" if start is None else _indent(lines[start]) + "  "
    new = [f"{indent}{'  ' * k}{seg}:\n" for k, seg in enumerate(missing[:-1])]
    return _join(lines, hi, hi, [*new, f"{indent}{'  ' * (len(missing) - 1)}{missing[-1]}: {_flow(value)}\n"])


def _has_duplicate_keys(text: str) -> bool:
    """True when a mapping in `text` repeats a key, which `safe_load` accepts and resolves to the last."""
    def repeats(node: yaml.Node | None) -> bool:
        if isinstance(node, yaml.MappingNode):
            keys = [k.value for k, _ in node.value if isinstance(k, yaml.ScalarNode)]
            return len(keys) != len(set(keys)) or any(repeats(v) for _, v in node.value)
        return isinstance(node, yaml.SequenceNode) and any(repeats(n) for n in node.value)

    try:
        return repeats(yaml.compose(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return False


def _misplaced(old: str, new: str, parts: list[str], value: object) -> str | None:
    """Why `new` does not hold `value` at `parts` as a single key; None when it does."""
    path = ".".join(parts)
    doc = _load(new)
    if isinstance(doc, PlanError) or _get(doc, path) != value:
        return f"{path}: the edit did not land where the file keeps this key"
    if _has_duplicate_keys(new) and not _has_duplicate_keys(old):
        return f"{path}: the edit would repeat a key already in the file"
    return None


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
        current = _get(doc, ".".join(parts))
        new_text = text if type(value) is type(current) and value == current else _splice_path(text, parts, value)
        if isinstance(new_text, PlanError):
            return refuse(new_text.reason)
        placed = None if new_text == text else _misplaced(text, new_text, parts, value)
        if placed is not None:
            return refuse(placed)
    chair_error = _chair_error(setting, new_text)
    if chair_error is not None:
        return refuse(chair_error)
    errors = validate(new_text) if validate is not None and setting.scope == "cartridge" else []
    if errors:
        return PlanError(f"{setting.source_file} violates the schema: {'; '.join(errors)}")
    return Plan(new_text, _diff(text, new_text, setting.source_file), setting.operator_only)
