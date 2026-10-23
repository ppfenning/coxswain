"""Pure migration plan for the lane hosts, per docs/design/coxswain-home.md questions 1 and 4.

A host is judged only by what its own beat reports. A beat host is migrated when it reports at least one
checkout and every checkout path lies in the one monorepo checkout, `<...>/installs/coxswain`. No host path is
ever compared with the chair's, because hosts on different operating systems have different home roots.
"""

from __future__ import annotations

import itertools
import json
import posixpath
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from agent_tools.home import Locations


@dataclass(frozen=True)
class ObservedHost:
    name: str
    ssh: str
    workspace_dir: str
    checkouts: Mapping[str, str]
    profile_workspace_dir: str | None
    source: Literal["beat", "fallback"]


@dataclass(frozen=True)
class Step:
    op: Literal["remote_migrate", "rewrite_host_workspace", "skip", "print"]
    host: str
    why: str
    argv: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


def in_checkout(path: str) -> bool:
    """True when `path` is the monorepo checkout `<...>/installs/coxswain` or a folder inside it. A component
    checkout of the old layout, `installs/coxswain-tools`, is not."""
    parts = posixpath.normpath(path).split("/")
    return any(a == "installs" and b == "coxswain" for a, b in itertools.pairwise(parts))


def _home_of(path: str) -> str | None:
    """`<home>` for a path in `<home>/installs/coxswain`, else None."""
    parts = posixpath.normpath(path).split("/")
    found = next((i for i, (a, b) in enumerate(itertools.pairwise(parts)) if a == "installs" and b == "coxswain"), None)
    return "/".join(parts[:found]) if found is not None else None


def is_migrated(host: ObservedHost) -> bool:
    """Reports a checkout, and every reported path lies in the monorepo checkout (the checkout itself, or a folder
    inside it: an older beat reports `src/cartridges/providers` beside it) or is that home's own `providers`
    folder, where `setup migrate` copies a team provider profile. Every migrated host reports that folder, so
    without it no host ever read as migrated (2026-10-23)."""
    paths = list(host.checkouts.values())
    homes = {home for home in map(_home_of, paths) if home is not None}
    providers = {posixpath.join(home, "providers") for home in homes}
    return bool(paths) and all(in_checkout(path) or posixpath.normpath(path) in providers for path in paths)


def _host_steps(host: ObservedHost) -> list[Step]:
    if host.source == "fallback":
        return [Step("print", host.name, f"{host.name} has no beat row, so its paths cannot be judged until it beats.")]
    if not is_migrated(host):
        return [
            Step(
                "remote_migrate",
                host.name,
                f"{host.name} reports checkouts outside the monorepo checkout, so run setup migrate on it.",
                argv=["ssh", host.ssh, "cox", "setup", "migrate", "--apply"],
            )
        ]
    if host.profile_workspace_dir != host.workspace_dir:
        return [
            Step(
                "rewrite_host_workspace",
                host.name,
                f"{host.name} beats the monorepo checkout but the lane_hosts entry is stale, so re-point it.",
                payload={"workspace_dir": host.workspace_dir, "checkouts": dict(host.checkouts)},
            )
        ]
    return [Step("skip", host.name, f"{host.name} beats the monorepo checkout and the lane_hosts entry already agrees.")]


def host_migrate_plan(hosts: Sequence[ObservedHost], locations: Locations) -> list[list[Step]]:
    """One list of steps per host, in input order. `locations` is accepted and unused: no rule compares with the chair."""
    return [_host_steps(host) for host in hosts]


def _render_step(step: Step) -> str:
    detail = [
        part
        for part in (shlex.join(step.argv), json.dumps(step.payload, sort_keys=True) if step.payload else "", step.why)
        if part
    ]
    return f"{step.host}: {step.op}: " + " | ".join(detail)


def render_hosts_plan(plan: Sequence[Sequence[Step]]) -> list[str]:
    """Dry-run lines, one per step, grouped by host. argv goes through shlex.join so odd aliases stay one word."""
    return [_render_step(step) for group in plan for step in group]
