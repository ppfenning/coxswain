"""The merged-PR window a release's notes are drafted from: per component,
every PR merged to main since that component's previous tag, plus the two
facts the Upgrading section needs.

`previous_tag`, `in_window` and `upgrade_facts` are pure. Only
`gather_component_prs` touches the world, and only through the injected
`run`, which takes an argv and returns stdout."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def _sort_key(tag: str) -> tuple | None:
    """Sort key for `X.Y.Z` or `X.Y.Z-pre`, with or without a leading `v`.
    A pre-release sorts below its own release; `None` when `tag` is not semver."""
    m = _SEMVER_RE.match(tag)
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    pre_key = (
        (1, ())
        if pre is None
        else (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    )
    return (int(major), int(minor), int(patch), pre_key)


def previous_tag(tags: list[str], version: str) -> str | None:
    """The highest semver tag strictly below `version`, or None. Non-semver tags are ignored."""
    ceiling = _sort_key(version)
    keyed = [(k, t) for t in tags if (k := _sort_key(t)) is not None]
    below = [(k, t) for k, t in keyed if ceiling is not None and k < ceiling]
    return max(below)[1] if below else None


def _instant(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def in_window(prs: list[dict], after: str | None) -> list[dict]:
    """PRs based on main and merged strictly after the ISO timestamp `after`
    (every main PR when `after` is None), as a new list sorted by number."""
    cutoff = None if after is None else _instant(after)
    kept = [
        pr for pr in prs
        if pr["base"] == "main" and (cutoff is None or _instant(pr["merged_at"]) > cutoff)
    ]
    return sorted(kept, key=lambda pr: pr["number"])


def upgrade_facts(manifest: dict, migration_files: list[str]) -> dict:
    """`schema_version` is `manifest["coxswain"]["version"]`, the one version
    devtools/release.py reads from the manifest; the manifest has no separate
    schema key. None when the manifest names no version."""
    return {
        "schema_version": manifest.get("coxswain", {}).get("version"),
        "has_migrations": len(migration_files) > 0,
    }


def gather_component_prs(
    run: Callable[[list[str]], str], component: str, repo: str, directory: str, version: str
) -> tuple[str | None, list[dict]]:
    """The previous tag of `component` below `version`, and the PRs merged to
    main since that tag's commit time. `repo` is the `org/name` slug for gh;
    `directory` is the local checkout git reads tags from."""
    tags = run(["git", "-C", directory, "tag", "--list"]).split()
    previous = previous_tag(tags, version)
    after = (
        None
        if previous is None
        else _instant(run(["git", "-C", directory, "log", "-1", "--format=%cI", previous]).strip())
        .astimezone(UTC)
        .isoformat()
    )
    raw = json.loads(
        run([
            "gh", "pr", "list", "--repo", repo, "--state", "merged", "--base", "main",
            "--json", "number,title,mergedAt,baseRefName", "--limit", "1000",
        ])
    )
    prs = [
        {"component": component, "number": r["number"], "title": r["title"],
         "merged_at": r["mergedAt"], "base": r["baseRefName"]}
        for r in raw
    ]
    return previous, in_window(prs, after)
