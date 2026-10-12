"""The notes body a release draft starts from: grouped merged PRs, the two blanks, components and upgrade facts."""

from devtools.release_notes_blanks import GAPS_BLANK, OPENER_BLANK

UMBRELLA = "coxswain"

_PAST = {
    "add": "Added",
    "allow": "Allowed",
    "change": "Changed",
    "drop": "Dropped",
    "fix": "Fixed",
    "make": "Made",
    "remove": "Removed",
    "stop": "Stopped",
    "support": "Supported",
    "update": "Updated",
}


def _past_tense(title: str) -> str:
    """Title with a known leading verb in past tense and trailing full stops dropped; other titles are kept."""
    text = title.strip().rstrip(".")
    verb, _, rest = text.partition(" ")
    past = _PAST.get(verb.lower())
    return text if past is None else f"{past} {rest}".rstrip()


def _bullet(pr: dict) -> str:
    return f"- {_past_tense(pr['title'])} ({pr['component']} #{pr['number']})"


def _section(heading: str, prs: list[dict]) -> list[str]:
    return [heading, "", *(_bullet(pr) for pr in sorted(prs, key=lambda pr: pr["number"])), ""]


def _upgrading(facts: dict) -> str:
    schema = f"The store schema version is {facts['schema_version']}."
    migrations = "This release includes migrations." if facts["has_migrations"] else "This release has no migrations."
    return f"{schema} {migrations}"


def render_notes(
    version: str, prs_by_component: dict[str, list[dict]], components: list[tuple[str, str]], facts: dict
) -> str:
    """Notes text for docs/releases/<version>.md. The Components list is prose, not bullets, because release-check
    requires every bullet to cite a landed PR. PRs grouped under the umbrella name follow the component sections."""
    sections = [
        _section(f"## coxswain-{name}", prs_by_component[name]) for name, _ in components if prs_by_component.get(name)
    ]
    umbrella = (
        [_section("## coxswain (umbrella)", prs_by_component[UMBRELLA])] if prs_by_component.get(UMBRELLA) else []
    )
    listed = ", ".join(f"coxswain-{name} {number}" for name, number in components)
    lines = [
        f"# {version}",
        "",
        OPENER_BLANK,
        "",
        *(line for section in [*sections, *umbrella] for line in section),
        "## Known gaps",
        "",
        GAPS_BLANK,
        "",
        "## Components",
        "",
        f"{listed}.",
        "",
        "## Upgrading",
        "",
        _upgrading(facts),
    ]
    return "\n".join(lines) + "\n"
