from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# `land_at` answers one question: which level opens a pull request on origin. Everything below it completes locally.
# `initiative` is the default; `ticket` is its permanent alias, so no cartridge or `initiative.md` needs editing.
# `level` (one pull request per phase) is retired: it is still read, with a warning, and treated as `initiative`.
INITIATIVE = "initiative"
TASK = "task"
LAND_AT_VALUES = (INITIATIVE, TASK)
LAND_AT_ALIASES = {"ticket": INITIATIVE, "level": INITIATIVE}
RETIRED_LAND_AT = ("level",)
ACCEPTED_LAND_AT = (*LAND_AT_VALUES, *LAND_AT_ALIASES)
DEFAULT_LAND_AT = INITIATIVE


def canonical_land_at(value: Any) -> str | None:
    """`initiative` or `task` for any accepted spelling (`ticket` and the retired `level` read as `initiative`); None for anything else."""
    if not isinstance(value, str):
        return None
    if value in LAND_AT_VALUES:
        return value
    return LAND_AT_ALIASES.get(value)


def lands_at_initiative(value: Any) -> bool:
    """True for `initiative`, its alias `ticket`, the retired `level`, and an absent value (the default)."""
    return value is None or canonical_land_at(value) == INITIATIVE


def retired_land_at_warning(value: Any, where: str) -> str | None:
    """The warning a retired `land_at` value earns, None for every other value."""
    if value in RETIRED_LAND_AT:
        return (f"{where} land_at {value!r} is retired: per-phase pull requests no longer exist; "
                f"it is treated as {INITIATIVE!r} (one pull request per initiative)")
    return None


def _checked(value: Any, where: str) -> str:
    canonical = canonical_land_at(value)
    if canonical is not None:
        return canonical
    allowed = ", ".join(repr(v) for v in ACCEPTED_LAND_AT)
    raise ValueError(f"{where} land_at must be one of {allowed}, got {value!r}")


def resolve_land_at(cartridge: Mapping[str, Any], initiative_land_at: str | None) -> str:
    """The initiative value, else the cartridge or overlay value, else `initiative`; every value given is validated,
    and every accepted spelling comes back canonical (`initiative` or `task`)."""
    landing = cartridge.get("landing_areas")
    configured = landing.get("land_at") if isinstance(landing, Mapping) else None
    # Both places are checked even when the initiative overrides, so a bad cartridge never hides.
    checked_config = None if configured is None else _checked(configured, "cartridge")
    checked_initiative = None if initiative_land_at is None else _checked(initiative_land_at, "initiative")
    return checked_initiative or checked_config or DEFAULT_LAND_AT


def land_at_warnings(cartridge: Mapping[str, Any], initiative_land_at: str | None) -> list[str]:
    """One warning per retired `land_at` value given, the cartridge's first."""
    landing = cartridge.get("landing_areas")
    configured = landing.get("land_at") if isinstance(landing, Mapping) else None
    found = (retired_land_at_warning(configured, "cartridge"), retired_land_at_warning(initiative_land_at, "initiative"))
    return [w for w in found if w is not None]


_BRANCH_FORBIDDEN = frozenset("~^:?*[\\")


def valid_branch(value: Any, where: str) -> str:
    """A bare branch name under git's ref-name rules; a non-string or any broken rule raises."""
    ok = (
        isinstance(value, str)
        and value != ""
        and not any(c.isspace() or c < " " or c == "\x7f" or c in _BRANCH_FORBIDDEN for c in value)
        and ".." not in value
        and "@{" not in value
        and not value.startswith(("-", "/"))
        and not value.endswith(("/", ".", ".lock"))
    )
    if ok:
        return value
    raise ValueError(f"{where} target_branch must be a valid branch name, got {value!r}")


def resolve_target_branch(
    cartridge: Mapping[str, Any], epic_branch: str | None, initiative_branch: str | None
) -> str | None:
    """The initiative value, else the epic, else `landing_areas.target_branch`, else None; every value given is validated."""
    landing = cartridge.get("landing_areas")
    configured = landing.get("target_branch") if isinstance(landing, Mapping) else None
    # Validate all three even when a higher one wins, so a bad lower value never hides.
    checked = [
        None if value is None else valid_branch(value, where)
        for value, where in ((initiative_branch, "initiative"), (epic_branch, "epic"), (configured, "cartridge"))
    ]
    return next((value for value in checked if value is not None), None)


def land_gate_level(land_at: str) -> str:
    """The granularity at which the human approval gate applies: the `land_at` value itself."""
    return _checked(land_at, "gate")


def phase_land_deferred(land_at: str, phase: str, phases: Sequence[str]) -> bool:
    """True only at `initiative` for a phase that is not the last of `phases` (workstore.phases order)."""
    return _checked(land_at, "phase") == INITIATIVE and bool(phases) and phase != phases[-1]


def phase_counts_complete(land_at: str, states: Sequence[tuple[str, bool]]) -> bool:
    """Every (state, merged) is done or dropped, or approved and merged unless `land_at` is `task`; empty is True."""
    merged_counts = _checked(land_at, "phase") != TASK
    return all(s in ("done", "dropped") or (merged_counts and s == "approved" and merged) for s, merged in states)


def need_met(land_at: str, state: str, merged: bool) -> bool:
    """An unmerged approved task is met only above `land_at` task."""
    level = _checked(land_at, "need")
    return state in ("done", "dropped") or (state == "approved" and (merged or level != TASK))


def pr_branch_name(initiative: str) -> str:
    """The pre-template name of an initiative branch. Branches cut before the landing record existed carry it, and
    adopt keeps them under it; a new initiative's branch comes from `initiative_branch_name`."""
    return f"pr/{initiative}"


def phase_merge_ready(land_at: str, task_states: Sequence[str], quarantined: Sequence[bool]) -> bool:
    """`quarantined` holds one flag per task; ready only at `initiative`, never empty, never with a quarantined task."""
    return (
        _checked(land_at, "phase") == INITIATIVE
        and bool(task_states)
        and not any(quarantined)
        and all(s in ("approved", "done", "dropped") for s in task_states)
    )


def pr_stage(phase: str, phases: Sequence[str]) -> str:
    """`ready` for the last of `phases` (so a one-phase initiative), `draft` for the first, else `update`."""
    if phases and phase == phases[-1]:
        return "ready"
    if phases and phase == phases[0]:
        return "draft"
    return "update"


# -- The initiative's one branch and one pull request -------------------------------------------------------------
# One block, `landing_areas.initiative_pr`, sets the branch name, the pull request title and the issue line of the
# body together, all from the same fields of the initiative:
#
#     landing_areas:
#       initiative_pr:
#         branch: "{kind}/{issue}-{name}"        # or a mapping of target branch to template, with `default`
#         kinds: {develop: feature, main: fix}   # `{kind}` per target branch; an initiative's `branch_kind` wins
#         title: "{title}"
#         reference: "Closes #{issue}"
#
# Fields: `id` and `name` (the initiative's slug), `title`, `issue` (the initiative's issue or ticket key), `epic`,
# `epic_issue`, `target` (the resolved parent branch) and `kind`. A template that names a field the initiative does
# not carry is refused by name: a branch that breaks the team's convention is never invented.

DEFAULT_BRANCH_TEMPLATE = "init/{id}"
DEFAULT_TITLE_TEMPLATE = "{title}"
DEFAULT_BRANCH_KIND = "feature"
TEMPLATE_FIELDS = ("id", "name", "title", "issue", "epic", "epic_issue", "target", "kind")
ON_BLOCKED_VALUES = ("hold", "split")
PUSH_INITIATIVE_VALUES = ("at_pr", "early")
DEFAULT_ON_BLOCKED = "hold"
DEFAULT_PUSH_INITIATIVE = "at_pr"
DEFAULT_PHASE_CAP = 2
DEFAULT_LAND_CAP = 1

_FIELD = re.compile(r"\{([a-z_]+)\}")
_SLUG_DROP = re.compile(r"[^a-z0-9]+")


class TemplateError(ValueError):
    """A landing template is malformed, or names a field the initiative does not carry."""


def _landing(cartridge: Mapping[str, Any]) -> Mapping[str, Any]:
    landing = cartridge.get("landing_areas")
    return landing if isinstance(landing, Mapping) else {}


def _initiative_pr(cartridge: Mapping[str, Any]) -> Mapping[str, Any]:
    block = _landing(cartridge).get("initiative_pr")
    return block if isinstance(block, Mapping) else {}


def branch_slug(text: str) -> str:
    """Lowercase letters, digits and single hyphens: what a title or id contributes to a branch name."""
    return _SLUG_DROP.sub("-", str(text).lower()).strip("-")


def template_fields(template: str) -> tuple[str, ...]:
    """The `{field}` names a template uses, in order, each once."""
    return tuple(dict.fromkeys(_FIELD.findall(template)))


def template_problems(template: Any, where: str) -> list[str]:
    """Why `template` cannot be used: not a non-empty string, or it names a field outside `TEMPLATE_FIELDS`."""
    if not isinstance(template, str) or not template.strip():
        return [f"{where} must be a non-empty string, got {template!r}"]
    unknown = [f for f in template_fields(template) if f not in TEMPLATE_FIELDS]
    if unknown:
        return [f"{where} names unknown field(s) {', '.join(unknown)}; the fields are {', '.join(TEMPLATE_FIELDS)}"]
    return []


def render_template(template: str, fields: Mapping[str, Any], where: str) -> str:
    """`template` with each `{field}` replaced. A field that is absent or empty raises `TemplateError` naming it."""
    problems = template_problems(template, where)
    if problems:
        raise TemplateError(problems[0])
    missing = [f for f in template_fields(template) if fields.get(f) in (None, "")]
    if missing:
        raise TemplateError(f"{where} {template!r} needs {', '.join(missing)}, which the initiative does not carry")
    return _FIELD.sub(lambda m: str(fields[m.group(1)]), template)


def _branch_template(cartridge: Mapping[str, Any], target: str | None) -> str:
    configured = _initiative_pr(cartridge).get("branch")
    if isinstance(configured, Mapping):
        chosen = configured.get(target) if target is not None else None
        return chosen if chosen is not None else configured.get("default", DEFAULT_BRANCH_TEMPLATE)
    return DEFAULT_BRANCH_TEMPLATE if configured is None else configured


def branch_kind(cartridge: Mapping[str, Any], target: str | None, initiative_kind: str | None = None) -> str:
    """The `{kind}` of a branch: the initiative's own `branch_kind`, else `initiative_pr.kinds[<target>]`, else `feature`."""
    if initiative_kind:
        return str(initiative_kind)
    kinds = _initiative_pr(cartridge).get("kinds")
    chosen = kinds.get(target) if isinstance(kinds, Mapping) and target is not None else None
    return str(chosen) if chosen else DEFAULT_BRANCH_KIND


def template_values(initiative: Mapping[str, Any], cartridge: Mapping[str, Any], target: str | None,
                    epic_issue: Any = None) -> dict[str, Any]:
    """The fields a template may use, read from an initiative (`id`, `title`, `issue` or `ticket`, `epic`, `branch_kind`)."""
    ident = str(initiative.get("id") or "")
    issue = initiative.get("issue")
    if issue in (None, ""):
        issue = initiative.get("ticket")
    return {
        "id": ident,
        "name": branch_slug(ident),
        "title": str(initiative.get("title") or ident),
        "issue": None if issue in (None, "") else str(issue).lstrip("#"),
        "epic": initiative.get("epic"),
        "epic_issue": None if epic_issue in (None, "") else str(epic_issue).lstrip("#"),
        "target": target,
        "kind": branch_kind(cartridge, target, initiative.get("branch_kind")),
    }


def initiative_branch_name(cartridge: Mapping[str, Any], initiative: Mapping[str, Any], target: str | None,
                           epic_issue: Any = None) -> str:
    """The initiative branch: the initiative's own `branch` when it sets one, else the cartridge template rendered
    (default `init/<id>`). The result is checked as a branch name; a missing field or a bad name raises `TemplateError`."""
    own = initiative.get("branch")
    if own:
        name = str(own)
    else:
        name = render_template(_branch_template(cartridge, target), template_values(initiative, cartridge, target, epic_issue),
                               "landing_areas.initiative_pr.branch")
    try:
        return valid_branch(name, "initiative")
    except ValueError as exc:
        raise TemplateError(f"initiative branch {name!r} is not a valid branch name") from exc


def initiative_pr_title(cartridge: Mapping[str, Any], initiative: Mapping[str, Any], target: str | None,
                        epic_issue: Any = None) -> str:
    """The pull request title: `initiative_pr.title` rendered, default the initiative's own title."""
    template = _initiative_pr(cartridge).get("title", DEFAULT_TITLE_TEMPLATE)
    return render_template(template, template_values(initiative, cartridge, target, epic_issue), "landing_areas.initiative_pr.title")


def initiative_pr_reference(cartridge: Mapping[str, Any], initiative: Mapping[str, Any], target: str | None,
                            epic_issue: Any = None) -> str | None:
    """The issue line of the pull request body. A configured template (`initiative_pr.reference`, or the flat
    `landing_areas.pr_reference`) is rendered, and a field it lacks raises. With no template the existing issue rule
    applies: `Closes #n` for the initiative's own issue, else `Part of #n` for its epic's, else no line."""
    template = _initiative_pr(cartridge).get("reference", _landing(cartridge).get("pr_reference"))
    values = template_values(initiative, cartridge, target, epic_issue)
    if template is not None:
        return render_template(template, values, "landing_areas.initiative_pr.reference")
    if values["issue"] and str(values["issue"]).isdigit():
        return f"Closes #{values['issue']}"
    if values["epic_issue"] and str(values["epic_issue"]).isdigit():
        return f"Part of #{values['epic_issue']}"
    return None


def _one_of(value: Any, allowed: Sequence[str], default: str, where: str) -> str:
    if value is None:
        return default
    if isinstance(value, str) and value in allowed:
        return value
    raise ValueError(f"{where} must be one of {', '.join(repr(v) for v in allowed)}, got {value!r}")


def _flag(value: Any, default: bool, where: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{where} must be true or false, got {value!r}")


def _cap(value: Any, default: int, where: str) -> int:
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    raise ValueError(f"{where} must be a whole number of 1 or more, got {value!r}")


def resolve_on_blocked(cartridge: Mapping[str, Any], initiative_value: str | None = None) -> str:
    """`hold` or `split`: the initiative's own value, else `landing_areas.on_blocked`, else `hold`."""
    configured = _one_of(_landing(cartridge).get("on_blocked"), ON_BLOCKED_VALUES, DEFAULT_ON_BLOCKED, "landing_areas.on_blocked")
    return _one_of(initiative_value, ON_BLOCKED_VALUES, configured, "initiative on_blocked")


def resolve_push_initiative(cartridge: Mapping[str, Any]) -> str:
    """`at_pr` (the initiative branch is first pushed when its pull request opens) or `early` (after every phase merge)."""
    return _one_of(_landing(cartridge).get("push_initiative"), PUSH_INITIATIVE_VALUES, DEFAULT_PUSH_INITIATIVE,
                   "landing_areas.push_initiative")


def resolve_epic_stack(cartridge: Mapping[str, Any], epic_value: bool | None = None) -> bool:
    """Whether an epic's initiatives land as a stack on an epic branch: the epic file's `epic_stack`, else the cartridge's, else off."""
    configured = _flag(_landing(cartridge).get("epic_stack"), False, "landing_areas.epic_stack")
    return _flag(epic_value, configured, "epic epic_stack")


def resolve_phase_cap(cartridge: Mapping[str, Any]) -> int:
    """How many phase merges run at once: `landing_areas.phase_cap`, default 2."""
    return _cap(_landing(cartridge).get("phase_cap"), DEFAULT_PHASE_CAP, "landing_areas.phase_cap")


def resolve_land_cap(cartridge: Mapping[str, Any]) -> int:
    """How many initiative lands run at once per parent branch: `landing_areas.land_cap`, default 1."""
    return _cap(_landing(cartridge).get("land_cap"), DEFAULT_LAND_CAP, "landing_areas.land_cap")


def landing_settings(cartridge: Mapping[str, Any], initiative: Mapping[str, Any] | None = None,
                     epic: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Every resolved landing setting of one initiative, in one mapping; each value given is validated."""
    own = initiative or {}
    return {
        "land_at": resolve_land_at(cartridge, own.get("land_at")),
        "on_blocked": resolve_on_blocked(cartridge, own.get("on_blocked")),
        "push_initiative": resolve_push_initiative(cartridge),
        "epic_stack": resolve_epic_stack(cartridge, (epic or {}).get("epic_stack")),
        "phase_cap": resolve_phase_cap(cartridge),
        "land_cap": resolve_land_cap(cartridge),
    }


def landing_config_problems(landing: Mapping[str, Any]) -> list[str]:
    """Every problem with the landing keys of one `landing_areas` block; empty when it can be used."""
    problems: list[str] = []
    for resolve in (resolve_on_blocked, resolve_push_initiative, resolve_epic_stack, resolve_phase_cap, resolve_land_cap):
        try:
            resolve({"landing_areas": landing})
        except ValueError as exc:
            problems.append(str(exc))
    block = landing.get("initiative_pr")
    if block is None:
        return problems
    if not isinstance(block, Mapping):
        return [*problems, f"landing_areas.initiative_pr must be a mapping, got {type(block).__name__}"]
    branch = block.get("branch")
    templates = dict(branch) if isinstance(branch, Mapping) else ({} if branch is None else {"": branch})
    for target, template in templates.items():
        where = f"landing_areas.initiative_pr.branch{'.' + str(target) if target else ''}"
        problems += template_problems(template, where)
    for key in ("title", "reference"):
        if key in block:
            problems += template_problems(block[key], f"landing_areas.initiative_pr.{key}")
    kinds = block.get("kinds")
    if kinds is not None and not (isinstance(kinds, Mapping) and all(isinstance(v, str) and branch_slug(v) == v for v in kinds.values())):
        problems.append("landing_areas.initiative_pr.kinds must map a target branch to a lowercase word")
    if "pr_reference" in landing:
        problems += template_problems(landing["pr_reference"], "landing_areas.pr_reference")
    return problems
