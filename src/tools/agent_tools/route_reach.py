"""The decompose's reach heal, run when an intake is filed so filing reports what the decompose will rewrite and refuses what it refuses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graphs.delivery.ticket_lint import IdeaHeal


def _reach_allow(frontmatter: Mapping[str, Any]) -> list[str]:
    """`reach_allow` as the decompose reads it: a list of strings, else empty."""
    value = frontmatter.get("reach_allow")
    return list(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def reach_heal(
    body: str,
    frontmatter: Mapping[str, Any],
    tree: Sequence[Mapping[str, Any]],
    repo_name: str,
) -> IdeaHeal:
    """What the decompose does with `body`: the healed text, one note per rewritten path, and a refusal only when no text is left."""
    from graphs.delivery.ticket_lint import heal_idea_reach  # lazy: graphs sits beside agent_tools in one venv

    return heal_idea_reach(body, tree, repo_name, _reach_allow(frontmatter))


def launch_reach_heal(body: str, frontmatter: Mapping[str, Any]) -> IdeaHeal:
    """The heal a launched decompose runs on `body`; a launch passes no --tree and no --target-repo."""
    return reach_heal(body, frontmatter, [], "")


def launch_text_heal(text: str) -> IdeaHeal:
    """The heal a launched decompose runs on an intake file's whole text."""
    from graphs.delivery.initiative_decompose import _split_frontmatter  # lazy: same venv

    frontmatter, body = _split_frontmatter(text)
    return launch_reach_heal(body, frontmatter)


def reach_refusal(
    body: str,
    frontmatter: Mapping[str, Any],
    tree: Sequence[Mapping[str, Any]],
    repo_name: str,
) -> str | None:
    """The strict idea-reach refusal for `body`, for `route edit`, which still refuses any outside path."""
    from graphs.delivery.ticket_lint import idea_reach_refusal  # lazy: same venv

    return idea_reach_refusal(body, tree, repo_name, _reach_allow(frontmatter))


def launch_reach_refusal(body: str, frontmatter: Mapping[str, Any]) -> str | None:
    """The refusal a launched decompose would raise for `body`; a launch passes no --tree and no --target-repo."""
    return reach_refusal(body, frontmatter, [], "")


def launch_text_refusal(text: str) -> str | None:
    """The refusal a launched decompose would raise for an intake file's whole text."""
    # The decompose's own splitter, so the two read frontmatter alike; `route.parse_frontmatter` does not (it keeps quotes).
    from graphs.delivery.initiative_decompose import _split_frontmatter  # lazy: same venv

    frontmatter, body = _split_frontmatter(text)
    return launch_reach_refusal(body, frontmatter)
