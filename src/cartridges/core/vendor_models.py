"""Edge fetchers for the vendor model list. Fetch and parse only; comparing to the catalog is not here.

Breadcrumbs, 2026-10-07:
- The Anthropic key lives in the env var named by `auth_env` in providers/anthropic-default.yaml
  (ANTHROPIC_API_KEY). These functions take the key as a parameter and never read the environment.
- The repo has no HTTP client and pyproject.toml lists only pyyaml. `get` is injected, so the caller picks one.
- `get` returns a body with no status code, so an API error arrives as a body, not an exception. The parser
  treats a `"type": "error"` body, a missing `data` list, or `has_more` without `last_id` as a FetchError.
  An empty list is success only when the body says so: `"data": []` and `"has_more": false`.
- `claude --help`, run by the builder in its first pass on this ticket, lists the subcommands agents, attach,
  auth, auto-mode, doctor, gateway, import, install, logs, mcp, plugin, purge, respawn, rm, setup-token,
  stop, ultrareview and update. None of them lists models. So the runner is asked via `-p`, with tools off.
- unknown: the stdout of a real runner. The parser is strict: every non-blank line must be one id, else
  FetchError. The ids are model output, not a registry. The caller must confirm them against the Anthropic list.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
PAGE_LIMIT = 1000
MAX_PAGES = 50
CLAUDE_MODELS_ARGV = (
    "claude",
    "-p",
    "--tools",
    "",
    "--no-session-persistence",
    "List the exact model ids available to this session, one per line, nothing else.",
)

# A Claude model id: the `claude-` prefix, lowercase tokens joined by hyphens, and at least one digit.
_CLAUDE_ID = re.compile(r"(?=.*\d)claude(?:-[a-z0-9.]+)+")


@dataclass(frozen=True)
class FetchError:
    reason: str


def parse_anthropic_page(body: object) -> tuple[list[str], str | None] | FetchError:
    """Ids on one /v1/models page and the `after_id` cursor for the next, None on the last page."""
    if not isinstance(body, dict):
        return FetchError("anthropic models body is not an object")
    if body.get("type") == "error":
        err = body.get("error")
        return FetchError(f"anthropic error body: {err.get('type') if isinstance(err, dict) else 'unknown'}")
    data, has_more, last_id = body.get("data"), body.get("has_more"), body.get("last_id")
    ids = (
        [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]
        if isinstance(data, list)
        else []
    )
    if not isinstance(data, list) or len(ids) != len(data):
        return FetchError("anthropic models page has no data list, or an entry without a string id")
    if not isinstance(has_more, bool):
        return FetchError("anthropic models page has no boolean has_more")
    if has_more and not (isinstance(last_id, str) and last_id):
        return FetchError("anthropic models page says has_more but gives no last_id; the list would be truncated")
    return ids, last_id if has_more else None


def parse_claude_models(output: str) -> list[str] | FetchError:
    """Ids from runner output, one per non-blank line, bullets and backticks stripped, first-seen order."""
    lines = [ln.strip().lstrip("-*• ").strip("`").strip() for ln in output.splitlines()]
    entries = [ln for ln in lines if ln]
    stray = next((ln for ln in entries if not _CLAUDE_ID.fullmatch(ln)), None)
    return (
        FetchError(f"claude output line is not a model id: {stray[:60]!r}") if stray else list(dict.fromkeys(entries))
    )


def list_anthropic_models(api_key: str, get: Callable[[str, dict, dict], object]) -> list[str] | FetchError:
    """GET /v1/models, following `after_id` until `has_more` is false. `get(url, headers, params)` returns the JSON body."""
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    result = _fetch_pages(get, headers, None, MAX_PAGES)
    return result if isinstance(result, FetchError) else list(dict.fromkeys(result))


def _fetch_pages(
    get: Callable[[str, dict, dict], object], headers: dict, cursor: str | None, left: int
) -> list[str] | FetchError:
    if left == 0:
        return FetchError(f"anthropic models pagination exceeded {MAX_PAGES} pages")
    params = {"limit": PAGE_LIMIT, **({"after_id": cursor} if cursor else {})}
    try:
        body = get(ANTHROPIC_MODELS_URL, headers, params)
    except Exception as exc:  # the edge turns any client failure into a value
        return FetchError(f"anthropic models request failed: {type(exc).__name__}")
    page = parse_anthropic_page(body)
    if isinstance(page, FetchError):
        return page
    ids, nxt = page
    if nxt is None:
        return ids
    if nxt == cursor:
        return FetchError(f"anthropic models cursor did not advance past {nxt!r}")
    rest = _fetch_pages(get, headers, nxt, left - 1)
    return rest if isinstance(rest, FetchError) else [*ids, *rest]


def list_claude_models(
    run: Callable[[list[str]], tuple[int, str]], argv: tuple[str, ...] = CLAUDE_MODELS_ARGV
) -> list[str] | FetchError:
    """Ask a Claude Code runner for its models. `run(argv)` returns (exit code, stdout)."""
    try:
        code, out = run(list(argv))
    except Exception as exc:  # a missing binary or a timeout is a value, not a raise
        return FetchError(f"claude command failed: {type(exc).__name__}")
    return parse_claude_models(out) if code == 0 else FetchError(f"claude command exited {code}")


__all__ = [
    "FetchError",
    "list_anthropic_models",
    "list_claude_models",
    "parse_anthropic_page",
    "parse_claude_models",
]
