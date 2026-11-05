"""The decompose's reach check, run when an intake is filed so a body it would refuse is refused at the door."""
from collections.abc import Mapping, Sequence
from typing import Any


def reach_refusal(
    body: str, frontmatter: Mapping[str, Any], tree: Sequence[Mapping[str, Any]], repo_name: str,
) -> str | None:
    """The shared idea-reach refusal for `body`, with `reach_allow` read as the decompose reads it: a list of strings, else empty."""
    from graphs.delivery.ticket_lint import idea_reach_refusal  # lazy: graphs sits beside agent_tools in one venv

    value = frontmatter.get("reach_allow")
    allow = list(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else []
    return idea_reach_refusal(body, tree, repo_name, allow)


def launch_reach_refusal(body: str, frontmatter: Mapping[str, Any]) -> str | None:
    """The refusal a launched decompose would raise for `body`; a launch passes no --tree and no --target-repo."""
    return reach_refusal(body, frontmatter, [], "")


def launch_text_refusal(text: str) -> str | None:
    """The refusal a launched decompose would raise for an intake file's whole text."""
    # The decompose's own splitter, so the two read frontmatter alike; `route.parse_frontmatter` does not (it keeps quotes).
    from graphs.delivery.initiative_decompose import _split_frontmatter  # lazy: same venv

    frontmatter, body = _split_frontmatter(text)
    return launch_reach_refusal(body, frontmatter)
