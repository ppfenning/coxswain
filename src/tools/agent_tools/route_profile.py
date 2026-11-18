"""The routing core, profiles and names: parsing the profile, run ids, initiative and intake files, pulls."""

from __future__ import annotations

import json
import re
import sys
import zoneinfo
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from agent_tools import card_keys, intake_fields, repo_path, upgrade_edge
from agent_tools.intake_pull_map import PullMap, map_fields
from agent_tools.route_status import parse_frontmatter

_PROFILE_FIELDS = (
    "team",
    "cartridges_dir",
    "skills_roots",
    "provider_profile",
    "harness_dir",
    "workspace_dir",
    "assume",
)

_KNOWN_KEYS = {
    "team",
    "cartridges_dir",
    "skills_roots",
    "provider_profile",
    "harness_dir",
    "workspace_dir",
    "assume",
    "router",
    "sources",
    "repo_map",
    "forge",
    "tracker",
    "tracker_scope",  # `all` (default), `epics` or `initiatives`: which items `route sync` mirrors; a cartridge's own `tracker_scope` wins
    "tracker_backfill",  # `true` makes `route sync` create issues for settled items under a narrowed `tracker_scope`; absent or `false` starts the board with open items only
    "umbrella_dir",
    "umbrella_clone_dir",  # the working clone the umbrella's lands run in; `umbrella_dir` stays where the installed components live
    "log_retention_days",  # run logs and traces kept locally, in days; run_logs.DEFAULT_RETENTION_DAYS when absent
    "ids",  # `slug` (default) or `sequence`; `intake_file`'s own id source, spec's `route file --intake`
    "operator",  # the human the courier and decisions address; settings_model.operator_name, "operator" when absent
    "serial_files",  # `[path, ...]` the steer rule never lets two initiatives share; an entry ending in `/` covers a directory
    "intake_stage",  # `draft` or `ready`: the stage a session files an intake at when the caller names none
    "upgrade_channel",  # `stable` (default: release tags) or `edge` (the newest green commit on main); cox upgrade reads it
    "intake_map",  # JSON: the `PullMap` label names a pull maps onto an intake's stage, priority, epic and target release
}

INTAKE_STAGES = ("draft", "ready")

_JSON_KEYS = {"sources", "repo_map", "intake_map"}

TRACKER_SCOPES = ("all", "epics", "initiatives")

_SPEND_KEYS = {"window_ceiling_usd", "weekly_ceiling_usd", "node_cap_usd", "monthly_budget_usd"}
_SPEND_TEXT_KEYS = {"weekly_reset", "budget_timezone"}  # `Sun 04:00 America/New_York`, parsed by usage_window.parse_weekly_reset; an IANA zone name

_CHAIR_KEYS = {"housekeeping_hours", "stale_days", "idle_stall_minutes", "launch_stagger_s", "max_launches_per_hour", "max_shared_file_initiatives", "approval_hold_ticks", "quarantine_hold_after", "no_progress_after"}  # every key `cli._chair_run_deps` reads off `profile["chair"]`

_CHAIR_BOOL_WORDS = {"true": True, "yes": True, "false": False, "no": False}  # the spellings a boolean `chair:` key accepts, any case
_CHAIR_STEWARD_SCOPES = {"all", "epics"}  # the values `chair: steward_scope:` accepts, stored lower case
_CHAIR_SESSION_TAKES = {"never", "when_free", "always"}  # the values `chair: session_takes:` accepts, stored lower case

_STORE_KEYS = {"mode"}  # the one key of the `store:` block, kept as text in `profile["store"]["mode"]`


class ProfileError(Exception):
    """A profile file line is nested, malformed, or has a bad value. An unknown top-level key is not an error."""


def _is_iana_zone(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return False
    return True


_NOTIFY_URL = re.compile(r"https?://\S+")


def _notify_mapping(value: str) -> dict | None:
    """`{ntfy: <url>}` as `{"ntfy": url}`; None for any other shape. The URL holds colons, so split on the first only."""
    if not (value.startswith("{") and value.endswith("}")):
        return None
    name, _, url = value[1:-1].partition(":")
    url = url.strip().strip("\"'")
    return {"ntfy": url} if name.strip() == "ntfy" and _NOTIFY_URL.fullmatch(url) and "," not in url else None


def _stripped_content(line: str) -> str:
    # An inline `#` (preceded by whitespace, per spec §1's own sample
    # `assume: a          # gate answer ...`) starts a trailing comment;
    # strip it before splitting key/value so it never lands in a value.
    comment = re.search(r"(?<=\s)#", line)
    return line[: comment.start()].rstrip() if comment else line


def _parse_profile(text: str) -> tuple[dict, list[tuple[str, int]]]:
    """Parse the flat `key: scalar` / `key: [a, b]` YAML subset in spec §1,
    plus two nested blocks: a bare `spend:` line followed by indented
    `window_ceiling_usd:`/`weekly_ceiling_usd:`/`node_cap_usd:`/`monthly_budget_usd:` lines, all optional, parsed as
    floats onto the flat result, and `weekly_reset:`/`budget_timezone:` lines kept as text, quotes stripped.
    `monthly_budget_usd` must be above zero and `budget_timezone` must be an IANA zone name, else ProfileError
    names the line; neither key appears in the result when absent, and the readers apply any default;
    and a bare `chair:` line followed by indented `housekeeping_hours:`/`stale_days:`/`idle_stall_minutes:`/`launch_stagger_s:`/`max_launches_per_hour:`/`max_shared_file_initiatives:`/`approval_hold_ticks:`/`quarantine_hold_after:`/`no_progress_after:` lines,
    all optional, parsed as floats into `result["chair"]` (every key `cli._chair_run_deps`
    reads off `profile["chair"]`; `log_retention_days` stays a flat top-level key, not one of
    these). The same block also takes `steward:`, a boolean (true, false, yes or no, any case)
    kept as a Python bool in `result["chair"]["steward"]`; any other value raises ProfileError.
    It also takes `steward_scope:`, text with quotes stripped, `all` or `epics` in any case, kept lower case as a str in
    `result["chair"]["steward_scope"]`; any other value raises ProfileError. The key is absent when not written and the reader applies the default `all`.
    `intake_stage:` is `draft` or `ready`, quotes and case ignored, kept lower case; any other value raises ProfileError.
    `upgrade_channel:` is `stable` or `edge` under the same rule.
    It also takes `session_takes:`, text with quotes stripped, `never`, `when_free` or `always` in any case, kept lower case as a str in
    `result["chair"]["session_takes"]`; any other value raises ProfileError. The key is absent when not written and the reader applies the default `when_free`.
    A bare `store:` line followed by an indented `mode:` line is kept as text in
    `result["store"]["mode"]`. A `notify: {ntfy: <url>}` line is the one inline mapping, kept as `result["notify"]`.

    A nested key outside a `spend:` or `chair:` block, or an unrecognized key inside one,
    raises ProfileError naming the offending line (number + text). A top-level key
    outside the known set is dropped with any indented block under it, and returned as a
    (key, line number) pair beside the profile. `assume` defaults to 'a' when absent.
    """
    result: dict = {}
    unknown: list[tuple[str, int]] = []
    in_spend = False
    in_chair = False
    in_store = False
    in_aliases = False  # `repo_aliases:` block: indented `<retired repo>: <owner/name>` lines, kept as text
    in_hosts = False  # `lane_hosts:` is a list of mappings that cli reads from the YAML text; its lines are skipped here
    in_unknown = False  # an unknown top-level key's indented block is dropped with the key, not read as a stray nested line
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        if (in_hosts or in_unknown) and line != line.lstrip():
            continue
        if line != line.lstrip():
            if in_aliases:
                key, _, value = _stripped_content(line).partition(":")
                key, value = key.strip().strip("\"'"), value.strip().strip("\"'")
                if not key or not value:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                result.setdefault("repo_aliases", {})[key] = value
                continue
            if in_store:
                content = _stripped_content(line)
                if ":" not in content:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                key, _, value = content.partition(":")
                key, value = key.strip(), value.strip().strip("\"'")
                if key not in _STORE_KEYS or not value:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                result.setdefault("store", {})[key] = value
                continue
            if in_chair:
                content = _stripped_content(line)
                if ":" not in content:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                key, _, value = content.partition(":")
                key, value = key.strip(), value.strip()
                if key in ("steward", "workspace_git"):
                    flag = _CHAIR_BOOL_WORDS.get(value.lower())
                    if flag is None:
                        raise ProfileError(f"line {lineno}: {raw_line}")
                    result.setdefault("chair", {})[key] = flag
                    continue
                if key == "steward_scope":
                    scope = value.strip("\"'").lower()
                    if scope not in _CHAIR_STEWARD_SCOPES:
                        raise ProfileError(f"line {lineno}: {raw_line}")
                    result.setdefault("chair", {})[key] = scope
                    continue
                if key == "session_takes":
                    taken = value.strip("\"'").lower()
                    if taken not in _CHAIR_SESSION_TAKES:
                        raise ProfileError(f"line {lineno}: chair session_takes must be one of never, when_free, always: {raw_line}")
                    result.setdefault("chair", {})[key] = taken
                    continue
                if key not in _CHAIR_KEYS:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                try:
                    result.setdefault("chair", {})[key] = float(value)
                except ValueError:
                    raise ProfileError(f"line {lineno}: {raw_line}") from None
                continue
            if not in_spend:
                raise ProfileError(f"line {lineno}: {raw_line}")
            content = _stripped_content(line)
            if ":" not in content:
                raise ProfileError(f"line {lineno}: {raw_line}")
            key, _, value = content.partition(":")
            key, value = key.strip(), value.strip()
            if key in _SPEND_TEXT_KEYS:
                text_value = value.strip("\"'")
                if not text_value:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                if key == "budget_timezone" and not _is_iana_zone(text_value):
                    raise ProfileError(f"line {lineno}: {raw_line}")
                result[key] = text_value
                continue
            if key not in _SPEND_KEYS:
                raise ProfileError(f"line {lineno}: {raw_line}")
            try:
                amount = float(value)
            except ValueError:
                raise ProfileError(f"line {lineno}: {raw_line}") from None
            if key == "monthly_budget_usd" and not amount > 0:
                raise ProfileError(f"line {lineno}: {raw_line}")
            result[key] = amount
            continue
        in_spend = False
        in_chair = False
        in_store = False
        in_aliases = False
        in_hosts = False
        in_unknown = False
        content = _stripped_content(line)
        if ":" not in content:
            raise ProfileError(f"line {lineno}: {raw_line}")
        key, _, value = content.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "spend":
            if value:
                raise ProfileError(f"line {lineno}: {raw_line}")
            in_spend = True
            continue
        if key == "chair":
            if value:
                raise ProfileError(f"line {lineno}: {raw_line}")
            in_chair = True
            continue
        if key == "store":
            if value:
                raise ProfileError(f"line {lineno}: {raw_line}")
            in_store = True
            continue
        if key == "repo_aliases":
            if value:
                raise ProfileError(f"line {lineno}: {raw_line}")
            in_aliases = True
            continue
        if key == "lane_hosts":
            if value:
                raise ProfileError(f"line {lineno}: {raw_line}")
            in_hosts = True
            continue
        if key == "notify":
            mapping = _notify_mapping(value)
            if mapping is None:
                raise ProfileError(f"line {lineno}: {raw_line}")
            result["notify"] = mapping
            continue
        if key not in _KNOWN_KEYS:
            unknown.append((key, lineno))
            in_unknown = True
            continue
        if key in _JSON_KEYS:
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                raise ProfileError(f"line {lineno}: {raw_line}") from None
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [item.strip() for item in inner.split(",")] if inner else []
            result[key] = items
        elif key == "tracker_scope" and value not in TRACKER_SCOPES:
            raise ProfileError(f"line {lineno}: {raw_line} (tracker_scope is one of {', '.join(TRACKER_SCOPES)})")
        elif key == "tracker_backfill" and value not in ("true", "false"):
            raise ProfileError(f"line {lineno}: {raw_line} (tracker_backfill is true or false)")
        elif key == "intake_stage":
            stage_value = value.strip("\"'").lower()
            if stage_value not in INTAKE_STAGES:
                raise ProfileError(f"line {lineno}: {raw_line} (intake_stage is one of {', '.join(INTAKE_STAGES)})")
            result[key] = stage_value
        elif key == "upgrade_channel":
            channel_value = value.strip("\"'").lower()
            if channel_value not in upgrade_edge.CHANNELS:
                raise ProfileError(f"line {lineno}: {raw_line} (upgrade_channel is one of {', '.join(upgrade_edge.CHANNELS)})")
            result[key] = channel_value
        else:
            result[key] = value
    # An `assume:` with no value is treated the same as an absent key, not
    # as a literal empty string, so it never poisons the harness argv.
    if result.get("assume") == "":
        result.pop("assume")
    result.setdefault("assume", "a")
    return result, unknown


def unknown_keys(text: str) -> list[tuple[str, int]]:
    """Top-level keys `parse_profile` drops, as (key, 1-based line number) in file order."""
    # Breadcrumb: an unknown key no longer raises ProfileError, so a caller that
    # treated ProfileError as "profile has an unknown key" (doctor.py, settings_plan)
    # now sees a clean parse. Such a caller must read this function instead.
    return _parse_profile(text)[1]


# Keyed by key name only: the same key at another line or in another file stays silent.
_WARNED_KEYS: set[str] = set()  # only `parse_profile` touches this


def parse_profile(text: str) -> dict:
    """The profile dict from `_parse_profile`; warns on stderr once per process per unknown key."""
    profile, unknown = _parse_profile(text)
    for key, lineno in unknown:
        if key not in _WARNED_KEYS:
            _WARNED_KEYS.add(key)
            print(f"profile: ignoring unknown key {key} at line {lineno}", file=sys.stderr)
    return profile


_SLUG_STOPWORDS = {"the", "a", "an", "cox", "fix", "loop", "initiative"}


def slugify(title: str) -> str:
    """Lower-case, non-alphanumerics collapsed to '-'. Longer than 48 chars,
    leading stopwords (`the`, `a`, `an`, `cox`, `fix`, `loop`, `initiative`)
    are dropped and the result is cut at the last word boundary under the
    cap, never mid-word; otherwise returned as-is.

    A title with no alphanumeric characters (all punctuation/symbols)
    slugifies to the empty string; a caller that uses the result as a path
    segment (spec §3, `work/<slug>/...`) must guard against that itself —
    this pure core does not refuse or substitute.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if len(slug) <= 48:
        return slug
    words = [w for w in slug.split("-") if w]
    while len(words) > 1 and words[0] in _SLUG_STOPWORDS:
        words = words[1:]
    slug = "-".join(words)
    if len(slug) <= 48:
        return slug
    cut = slug[:48]
    boundary = cut.rfind("-")
    return (cut[:boundary] if boundary > 0 else cut).strip("-")


def phase_run_prefix(initiative_id: str, phase: str) -> str:
    """The run-id prefix of one phase's run: `<prefix>-<N>` via `next_run_id`, so its lease is `runs:<prefix>`, not `runs:<initiative_id>`."""
    return f"{initiative_id}.{phase}"


def next_run_id(existing_names, prefix: str) -> str:
    """Return `<prefix>-<n>` for one past the highest n used by a name in
    existing_names with that prefix, or `<prefix>-1` when there is none.

    It never fills a gap: a gap may be a run whose files were archived while
    its store rows remain, and reusing that id would collide with the rows.

    Spec §4/§5 fill `existing_names` from a real `runs_dir` listing, whose
    entries are `<run-id>.log` and `<run-id>.pid`, not bare ids — so the
    pattern must tolerate an optional trailing extension. Matching bare ids
    only would let `next_run_id` hand back an id already in use, appending
    to a live run's log and clobbering its pidfile.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:\.\w+)?$")
    taken = set()
    for name in existing_names:
        match = pattern.match(name)
        if match:
            taken.add(int(match.group(1)))
    return f"{prefix}-{max(taken, default=0) + 1}"


class _Raw(str):
    """Marks a frontmatter value this module wrote itself as a literal YAML
    fragment (`needs: []`, `surfaces: []`) that must be emitted verbatim.

    Never wrap a caller-supplied string in this: a value's *shape* is not
    evidence of its meaning. An earlier version of `yaml_scalar` treated
    any bracketed value as a raw list, which meant a title like
    `[draft] ship it` was emitted bare as `title: [draft] ship it` — a real
    YAML reader parses that as a flow sequence, not the string it looks
    like. Only the caller who wrote the literal can know it is one; that
    is what wrapping it in `_Raw` at the call site records.
    """


def yaml_scalar(value: str) -> str:
    """Quote a frontmatter value that would otherwise read as something
    other than the plain string it is.

    Wrong belief this guards against: that emitting `key: {value}` verbatim
    is safe because a title or repo is "just text". A title with a colon
    (this very task's own title, `route.py: initiative_files`) or a `#`
    turns `title: route.py: initiative_files` into a line a real YAML
    reader parses as a nested mapping or a truncated comment, not the
    string it looks like. `_Raw` values pass through unquoted; every other
    value is a scalar regardless of what it looks like.
    """
    if isinstance(value, _Raw):
        return str(value)
    starts_like_yaml_flow = value.startswith("[") or value.startswith("{")
    # `'` is quoted for the same reason as `"`: a value that already looks
    # quoted must not be handed to a YAML reader bare. This half has no
    # round-trip test below. `parse_frontmatter` has no single-quoted value
    # form, so a title starting with `'` parses as the bare string whether
    # or not this line quotes it, and a test that failed on its removal
    # would have to assert the writer's output shape, which this task
    # rules out.
    starts_like_a_quote = value.startswith('"') or value.startswith("'")
    if (
        value == ""
        or ":" in value
        or "#" in value
        or value != value.strip()
        or starts_like_yaml_flow
        or starts_like_a_quote
    ):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _frontmatter(fields, body: str) -> str:
    """Render `---`-delimited frontmatter followed by a body, spec §3.

    `fields` is an ordered iterable of (key, value) pairs; each value is
    quoted by `yaml_scalar` when left bare it would change meaning (a
    colon, a `#`, surrounding whitespace, or emptiness), never otherwise.
    """
    # A list value (`reach_allow`) is a literal this module writes itself, so it goes out as `_Raw`, not as a quoted string.
    header = "\n".join(
        f"{key}: {yaml_scalar(_Raw(_header_value(value)) if isinstance(value, list) else value)}" for key, value in fields
    )
    return f"---\n{header}\n---\n\n{body}\n"


def _slug_or_raise(title: str) -> str:
    """`slugify`'s own docstring hands the empty-slug case to any caller
    that uses the result as a path segment (spec §3, `work/<slug>/...`).
    A title of pure punctuation would otherwise slugify to `""`, and
    `work/{slug}/...` would collapse to `work/initiative.md` — a write to
    the top of the work store, past spec §3's existing-path refusal, since
    that path is genuinely new. This is the guard slugify asked for.
    """
    slug = slugify(title)
    if not slug:
        raise ValueError(f"title {title!r} has no alphanumeric characters to slugify")
    return slug


# A one-task initiative's task id is its initiative's id plus this, so the two store rows never share a key.
ONE_TASK_SUFFIX = "-task"


def initiative_files(
    title: str,
    body: str,
    repo: str,
    phase: str = "build",
    surfaces: list[str] | None = None,
    budget_usd: float | None = None,
    slug: str | None = None,
) -> dict:
    """Content for a one-task initiative (spec §3, `route file` without
    `--intake`): `work/<slug>/initiative.md` and
    `work/<slug>/<phase>/<slug>-task.md`, keyed by path relative to the work
    store. The task's id is `<slug>-task`, never the initiative's own id: the
    store keys rows by (initiative, task id), so a task sharing its
    initiative's id overwrote the initiative row, launch then found no
    initiative, and `route edit` could not address the task (2026-10-23). `body` falls back to `title` when empty. No filesystem writes
    happen here — the caller applies the mapping.

    `slug`, given, is normalized through the same guard as `title` and used
    for the id and both paths in its place — an intake's own `slug:` field,
    for instance, is never trusted raw as a path segment. `surfaces`
    defaults to empty (unscoped); each entry is quoted the same way any
    other frontmatter scalar is, so a path carrying `:` or `#` round-trips.
    `budget_usd`, given, also adds `attempts: []` and `lint: []`; omitted,
    none of the three appear.
    """
    slug = _slug_or_raise(slug or title)
    text = body if body else title
    initiative_text = _frontmatter(
        [("id", slug), ("title", title), ("repo", repo)], text
    )
    surfaces_raw = _Raw("[" + ", ".join(yaml_scalar(s) for s in surfaces) + "]") if surfaces else _Raw("[]")
    task_id = f"{slug}{ONE_TASK_SUFFIX}"
    task_fields = [
        ("id", task_id),
        ("phase", phase),
        ("state", "ready"),
        ("needs", _Raw("[]")),
        ("surfaces", surfaces_raw),
        ("title", title),
    ]
    if budget_usd is not None:
        task_fields += [
            ("budget_usd", str(budget_usd)),
            ("attempts", _Raw("[]")),
            ("lint", _Raw("[]")),
        ]
    task_text = _frontmatter(task_fields, text)
    return {
        f"work/{slug}/initiative.md": initiative_text,
        f"work/{slug}/{phase}/{task_id}.md": task_text,
    }


def initiative_text(id: str, title: str, repo: str, intake: str, body: str) -> str:
    """`work/<id>/initiative.md` for `route launch decompose` (spec §3),
    written from data the launcher already has rather than proposed by a
    seat: the idea's `title`/`repo` and its path relative to the workspace
    as `intake`, with the idea's own body carried through unchanged.
    """
    return _frontmatter([("id", id), ("title", title), ("repo", repo), ("intake", intake)], body)


class RepoRefused(NamedTuple):
    """`resolve_repo`'s refusal: the `--repo` value and the path it tried, which the message names."""

    value: str
    tried: str

    @property
    def message(self) -> str:
        return f"routing: --repo {self.value!r} is not a git checkout (tried {self.tried})"


def resolve_repo(
    value: str, repos_root: Path, is_checkout: Callable[[Path], bool], home: str | None = None
) -> str | RepoRefused:
    """Pure. The `repo:` a `route file --repo <value>` writes.

    An absolute path is kept as is. A tilde value (`~` or `~/...`) is expanded against `home` first and then treated as
    the absolute path it names; without `home` it is refused. A bare name (one path component) is
    `<repos_root>/<name>`, written as that absolute path when `is_checkout` says it is a git checkout; repos sit beside
    the workspace, so `repos_root` is the workspace's parent. Anything else, or a bare name with no checkout there, is
    refused naming the path it tried."""
    expanded = repo_path.expand(value, home) if home is not None and value.startswith("~") else value
    path = Path(expanded)
    if path.is_absolute():
        return expanded
    if value in ("", ".", "..") or len(path.parts) != 1 or value.startswith("~"):
        return RepoRefused(value, value)
    candidate = repos_root / value
    return str(candidate) if is_checkout(candidate) else RepoRefused(value, str(candidate))


def repo_field(path: str, home: str) -> str:
    """Pure. The `repo:` text to write for a resolved absolute `path`: the tilde form when it is `home` or under it.

    The prefix is checked against `home` here, so a path under another user's home stays absolute."""
    h = home.rstrip("/")
    inside = path.rstrip("/") == h or path.startswith(h + "/")
    return repo_path.contract(path, home) if inside else path


def is_git_checkout(path: Path) -> bool:
    """Edge. A directory with a `.git` entry: a directory in a clone, a file in a worktree."""
    return path.is_dir() and (path / ".git").exists()


def link_fields(link: str) -> list[tuple[str, str]]:
    """`link`, then `issue` and `issue_repo` when the url is an issue url; empty for no link."""
    if not link:
        return []
    found = _ISSUE_URL.match(link)
    issue = [("issue", found.group(2)), ("issue_repo", found.group(1))] if found else []
    return [("link", link), *issue]


def intake_file(
    title: str,
    body: str,
    repo: str,
    date: str,
    *,
    source: str = "",
    link: str = "",
    ids: str = "slug",
    allocate: Callable[[], str] | None = None,
    after: Sequence[str] = (),
    stage: str | None = None,  # None writes no line, which reads as ready; a pulled or programmatic caller is unchanged
    epic: str = "",
    priority: int | str = "",
    target_release: str = "",
) -> dict:
    """Content for `route file --intake` (spec §3):
    `intake/<date>-<slug>.md`, same frontmatter shape as the initiative
    file. `date` arrives as a string (e.g. `2026-09-03`) so this stays
    pure — no clock reads here. A pulled file also carries `source` and `link`.

    Under `ids="sequence"` the file is `intake/<id>.md` with the id from `allocate`; a failed allocation raises, never slugifies.
    A non-empty `after` is written last as an `after: [a, b]` list; empty leaves the file as it was.
    A `stage` ("draft" or "ready") is written as `stage: <value>` after `repo`; None writes no line.
    A non-empty `epic`, `priority` (a bare integer) and `target_release` follow `stage` in that order; empty writes no line.
    """
    text = body if body else title
    staged = [
        *([("stage", stage)] if stage is not None else []),
        *([("epic", epic)] if epic else []),
        *([("priority", _Raw(str(int(priority))))] if priority not in ("", None) else []),
        *([("target_release", target_release)] if target_release else []),
    ]
    # A pulled file keeps its old form. Sync reads a frontmatter `issue` as the item's own mirror issue, so a pull
    # must not write the source issue number there; `route_sync._issue_steps` would skip `issue_create` for it.
    origin = [("source", source), ("link", link)] if source and link else link_fields(link)
    waits = [("after", _Raw("[" + ", ".join(yaml_scalar(a) for a in after) + "]"))] if after else []
    if ids == "sequence":
        id_ = (allocate or _no_allocator)()
        return {f"intake/{id_}.md": _frontmatter([("id", id_), ("title", title), *staged, *origin, *waits], text)}
    slug = _slug_or_raise(title)
    file_text = _frontmatter([("id", slug), ("title", title), ("repo", repo), *staged, *origin, *waits], text)
    return {f"intake/{date}-{slug}.md": file_text}


def _no_allocator() -> str:
    """`ids: sequence` with no allocator passed. The harness subprocess that stood here asked `store_ids allocate`
    with no store and read a key it never printed, so it always failed; this says so instead of pretending to try."""
    raise ValueError("id allocation failed: ids: sequence needs an allocator, and this caller passes none")


def _issue_number(link: str) -> str:
    return link.rstrip("/").rsplit("/", 1)[-1]


_ISSUE_URL = re.compile(r"^https?://[^/]+/([^/]+/[^/]+)/issues/(\d+)/?$")
_PLACEHOLDER_HOME = "/home/user"


def _same_form(path: str) -> str:
    # Contract against a fixed home so a tilde path and any machine's absolute spelling of it compare equal.
    return repo_path.contract(path, _PLACEHOLDER_HOME)


def row_issue_key(row: Mapping, repo_map: Mapping[str, str]) -> tuple[str, str] | None:
    """(owner/name, issue number) of a store row; None when the number or the repository cannot be told.

    The repository is read from an issue-URL `link`, else the row's `repo` path inverted through `repo_map`.
    """
    number = card_keys.row_keys(row)[0]
    link = row.get("link")
    from_link = _ISSUE_URL.match(link) if isinstance(link, str) else None
    path = row.get("repo")
    by_path = {_same_form(p): name for name, p in repo_map.items()}
    repo = from_link.group(1) if from_link else by_path.get(_same_form(path)) if isinstance(path, str) and path else None
    return (repo, str(number)) if number is not None and repo else None


def held_candidates(candidates: Sequence, known_issues: Mapping[tuple[str, str], str]) -> list[tuple]:
    """(candidate, row label) for each issue candidate whose (repo, number) the store already holds."""
    keyed = [(c, (c.repo, _issue_number(c.link))) for c in candidates if c.kind != "pr"]
    return [(c, known_issues[key]) for c, key in keyed if key in known_issues]


def pull_plan(
    candidates: Sequence,
    taken_links: frozenset[str],
    profile_repos: Mapping[str, str],
    *,
    date: str,
    source: str,
    ids: str = "slug",
    known_issues: Mapping[tuple[str, str], str] = MappingProxyType({}),
    pull_map: PullMap | None = None,
    known_epics: Collection[str] = frozenset(),
    default_stage: str = "ready",
) -> tuple[list[dict], list[str], list[str]]:
    """Intake files, PR links to review, and refusals; an unmapped repo is refused, never guessed, and a second candidate on one path is refused, never dropped.

    An issue is held only by repository and number together: a candidate whose (repo, number) is in `known_issues` is skipped like a taken link, without a refusal.
    With a `pull_map`, each file carries the stage, priority, epic and target release it reads off the candidate's labels
    and milestone: the draft label files a draft, the ready label files it ready, and with neither it takes
    `default_stage` (ready, or the profile's `intake_stage`). An epic not in `known_epics` is refused with the valid choices, and
    nothing is written for it. With none (a source that carries no labels), the file has no stage line and reads as ready.
    """
    reviews = [c.link for c in candidates if c.kind == "pr"]
    held = [c for c, _ in held_candidates(candidates, known_issues)]
    fresh = [c for c in candidates if c.kind != "pr" and c.link not in taken_links and c not in held]
    valid_repos = f"valid repos: {', '.join(sorted(profile_repos)) or 'none mapped'}"
    unmapped = [
        _pull_refusal(c, f"repo {c.repo} has no mapping in the profile; {valid_repos}")
        for c in fresh
        if c.repo not in profile_repos
    ]
    mapped = [c for c in fresh if c.repo in profile_repos]
    unnamed = [_pull_refusal(c, "title has no alphanumeric characters to slugify") for c in mapped if not slugify(c.title)]
    fielded = [(c, _pulled_fields(c, pull_map, default_stage)) for c in mapped if slugify(c.title)]
    # The repo was checked above against the profile's own names; only the label-borne fields are judged here.
    judged = [
        (c, f, intake_fields.validate(c.repo, f["epic"], f["priority"], f["target_release"], {c.repo}, known_epics))
        for c, f in fielded
    ]
    invalid = [_pull_refusal(c, "; ".join(problems)) for c, _, problems in judged if problems]
    planned = [
        (
            c,
            intake_file(
                c.title, c.body, profile_repos[c.repo], date, source=source, link=c.link, ids=ids,
                stage=f["stage"], epic=f["epic"], priority="" if f["priority"] is None else f["priority"],
                target_release=f["target_release"],
            ),
        )
        for c, f, problems in judged
        if not problems
    ]
    paths = [next(iter(mapping)) for _, mapping in planned]
    first = [paths.index(path) == i for i, path in enumerate(paths)]
    clashes = [
        _pull_refusal(c, f"{path} is already planned for another candidate")
        for (c, _), path, is_first in zip(planned, paths, first)
        if not is_first
    ]
    files = [mapping for (_, mapping), is_first in zip(planned, first) if is_first]
    return files, reviews, unmapped + unnamed + invalid + clashes


_UNMAPPED_FIELDS = MappingProxyType({"stage": None, "priority": None, "epic": "", "target_release": ""})


def _pulled_fields(candidate, pull_map: PullMap | None, default_stage: str) -> Mapping:
    """The intake fields a candidate's labels and milestone give under `pull_map`; none of them without one."""
    if pull_map is None:
        return _UNMAPPED_FIELDS
    return map_fields(candidate.labels, candidate.milestone, pull_map, default_stage)[0]


def pull_notes(candidates: Sequence, pull_map: PullMap) -> list[str]:
    """The mapping's notes (a milestone that is not a version) for each issue candidate, each naming its candidate."""
    return [
        f"routing: {c.title!r} ({c.link}): {note}"
        for c in candidates
        if c.kind != "pr"
        for note in map_fields(c.labels, c.milestone, pull_map)[1]
    ]


def review_argv(link: str, profile: str) -> list[str]:
    return ["cox", "runs", "review", "--pr", link, "--profile", profile]


def _pull_refusal(candidate, reason: str) -> str:
    return f"routing: refusing {candidate.title!r} ({candidate.link}): {reason}"


def link_intake(initiative_text: str, intake_text: str, intake_path: str, initiative_id: str) -> tuple[str, str]:
    """`initiative_text`'s `intake` field becomes `intake_path`; `intake_text`'s `initiative` field becomes `initiative_id`; a rerun updates the value in place rather than duplicating the key."""
    initiative_fields, initiative_body = parse_frontmatter(initiative_text)
    intake_fields, intake_body = parse_frontmatter(intake_text)
    new_initiative = _frontmatter({**initiative_fields, "intake": intake_path}.items(), initiative_body)
    new_intake = _frontmatter({**intake_fields, "initiative": initiative_id}.items(), intake_body)
    return new_initiative, new_intake


_BACKTICKED = re.compile(r"`([^`\s]+)`")


def surface_candidates(body: str, repo: str) -> list[str]:
    """Backticked tokens in `body` that look like a path under `repo` — a
    `/` or a recognizable file suffix, no whitespace, no `scheme://` — in
    first-seen order, de-duplicated. Pure: this never touches a filesystem,
    so a token that merely looks like a path but names nothing real is
    still a candidate; the caller checks that.
    """
    found = []
    for token in _BACKTICKED.findall(body):
        if "://" in token or token in found:
            continue
        if "/" in token or re.search(r"\.[A-Za-z0-9]{1,8}$", token):
            found.append(token)
    return found


def harness_argv(
    profile: dict, graph: str, run_id: str, *, python: str | None = None, result_out: str | None = None, **needs,
) -> list:
    """Build the harness command line, spec §4, from a parsed `profile`
    and the graph-specific `needs` (`initiative`/`repo` for `epic`, plus
    `task` for `rescue`, and an optional `phase` for `epic`; `idea`/`initiative_id`, plus an optional `task_ids` and `repo`, for `decompose`). Pure: no Popen, no env reads.
    `python` is the interpreter `install_root.harness_python` chose; without it, the harness checkout's own venv.
    `result_out`, for `decompose` only, adds `--result-out <path>` so the graph result lands as JSON.
    """
    harness_dir = profile["harness_dir"]
    workspace_dir = profile["workspace_dir"]
    argv = [
        python or f"{harness_dir}/.venv/bin/python",
        f"{harness_dir}/shell.py",
        graph,
        "--team",
        profile["team"],
        "--cartridges-dir",
        profile["cartridges_dir"],
    ]
    for root in profile.get("skills_roots", []):
        argv += ["--skills-root", root]
    argv += [
        "--provider-profile",
        profile["provider_profile"],
        "--runs-dir",
        f"{workspace_dir}/runs",
        "--assume",
        profile["assume"],
        "--run-id",
        run_id,
    ]
    if graph in ("epic", "rescue"):
        argv += ["--initiative", needs["initiative"], "--repo", needs["repo"]]
        if graph == "epic" and needs.get("phase"):
            argv += ["--phase", needs["phase"]]
        if graph == "rescue":
            argv += ["--task", needs["task"]]
        if needs.get("fix_attempts") is not None:
            argv += ["--fix-attempts", str(needs["fix_attempts"])]
        if needs.get("base"):
            argv += ["--base", needs["base"]]
    elif graph == "decompose":
        argv += ["--idea", needs["idea"], "--initiative-id", needs["initiative_id"]]
        if needs.get("task_ids"):
            argv += ["--task-ids", needs["task_ids"]]
        # The intake's repository: its seats get it as a readable directory and a digest of it.
        if needs.get("repo"):
            argv += ["--repo", needs["repo"]]
        if result_out:
            argv += ["--result-out", str(result_out)]
    elif graph == "cos":
        # `_KNOWN_KEYS` carries no `max_parallel` field, so a profile can
        # never supply one; the bound is the literal default until the
        # profile schema grows a key for it.
        argv += ["--max-parallel", "3"]
    argv += ["--workdir", workspace_dir]
    if "node_cap_usd" in profile:
        argv += ["--node-cap-usd", str(profile["node_cap_usd"])]
    return argv


def build_sweep_argv(idea: str, initiative_id: str, label: str | None = None) -> list:
    """`cox route launch sweep --idea <file> --initiative-id <id>`, work-shape.md §1."""
    argv = ["sweep", "--idea", idea, "--initiative-id", initiative_id]
    if label is not None:
        argv += ["--label", label]
    return argv


def _header_value(value) -> str:
    """A frontmatter value as `_frontmatter` writes it: a list as a flat list literal, anything else as a scalar."""
    if isinstance(value, list):
        return "[" + ", ".join(yaml_scalar(str(v)) for v in value) + "]"
    return yaml_scalar(str(value))
