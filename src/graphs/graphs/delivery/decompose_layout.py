"""The layout check over a proposed decomposition, before it is approved.

Pure: it reads the tasks it is given and nothing else. Blocking findings mean a build seat
would be handed a ticket whose body and surfaces disagree. The unsupported-edge finding is
advisory: an edge may be real with no data flow written down, so it is flagged for review.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from harness.lanes import max_width

__all__ = ["LayoutFinding", "LayoutReport", "check_layout"]

_NEW = " (new)"
_TOKEN = re.compile(r"[\w./\-]+")
_ARTIFACT_NAME = re.compile(r"^(?:test_.+\.py|.+_test\.py|.+\.(?:test|spec)\.\w+|\d{3,}_.+\.(?:sql|py)|.+\.sh)$")
_ARTIFACT_DIRS = frozenset({"migrations", "scripts"})
_PATH_LIKE = re.compile(r"/|\w\.[A-Za-z]\w{0,4}$")


@dataclass(frozen=True)
class LayoutFinding:
    task: str
    rule: str
    detail: str
    fix: str
    advisory: bool = False


@dataclass(frozen=True)
class LayoutReport:
    findings: tuple[LayoutFinding, ...]
    width: int
    layers: tuple[tuple[str, ...], ...]

    @property
    def blocking(self) -> tuple[LayoutFinding, ...]:
        return tuple(f for f in self.findings if not f.advisory)

    def as_outcome(self) -> dict[str, Any]:
        return {
            "findings": [
                {"task": f.task, "rule": f.rule, "detail": f.detail, "advisory": f.advisory} for f in self.findings
            ],
            "width": self.width,
            "layers": [list(layer) for layer in self.layers],
        }


def _bare(surface: str) -> str:
    return surface[: -len(_NEW)] if surface.endswith(_NEW) else surface


def _is_artifact(token: str) -> bool:
    parts = token.split("/")
    return bool(_ARTIFACT_NAME.match(parts[-1])) or any(part in _ARTIFACT_DIRS for part in parts[:-1])


def _covers(surface: str, token: str) -> bool:
    """A surface covers a named file when one path is a whole-segment suffix of the other."""
    return surface == token or surface.endswith("/" + token) or token.endswith("/" + surface)


def _names_a_path(body: str) -> bool:
    return any(_PATH_LIKE.search(token.rstrip(".")) for token in _TOKEN.findall(body))


def _named(surface: str, body: str, tokens: Sequence[str]) -> bool:
    """The body names a surface in full, by a whole-segment trailing part, or by a path under a directory surface."""
    if surface in body:
        return True
    if surface.endswith("/"):
        return any(t.startswith(surface) or surface.rstrip("/").endswith("/" + t.rstrip("/")) for t in tokens)
    return any(surface.endswith("/" + t) for t in tokens)


def _surface_findings(task: Mapping[str, Any]) -> list[LayoutFinding]:
    """A body that names no path at all was never written against its surfaces: flagged, not blocking."""
    body = str(task.get("body") or "")
    tokens = [t.rstrip(".") for t in _TOKEN.findall(body)]
    unassessed = not _names_a_path(body)
    return [
        LayoutFinding(
            str(task["id"]),
            "surface_not_named",
            f"surface {_bare(str(s))} is not named in the body",
            "name every surface path in the body, or drop it from surfaces",
            advisory=unassessed,
        )
        for s in task.get("surfaces") or []
        if not _named(_bare(str(s)), body, tokens)
    ]


def _artifact_findings(task: Mapping[str, Any]) -> list[LayoutFinding]:
    surfaces = [_bare(str(s)) for s in task.get("surfaces") or []]
    named = dict.fromkeys(t.rstrip(".") for t in _TOKEN.findall(str(task.get("body") or "")))
    return [
        LayoutFinding(
            str(task["id"]),
            "artifact_not_surface",
            f"body names {token}, which is not among the surfaces",
            "add the file to surfaces with the exact name the body uses, or correct the body",
        )
        for token in named
        if not token.startswith("/") and _is_artifact(token) and not any(_covers(s, token) for s in surfaces)
    ]


def _edge_findings(tasks: Sequence[Mapping[str, Any]]) -> list[LayoutFinding]:
    by_id = {str(t["id"]): t for t in tasks}
    return [
        LayoutFinding(
            str(task["id"]),
            "edge_unsupported",
            f"needs {need}, but the body names no id, title or surface of {need}",
            "state what the task takes from the one it needs, or drop the edge",
            advisory=True,
        )
        for task in tasks
        for need in dict.fromkeys(str(n) for n in task.get("needs") or [])
        if need in by_id and not _flows(str(task.get("body") or ""), by_id[need])
    ]


def _flows(body: str, upstream: Mapping[str, Any]) -> bool:
    names = [
        str(upstream["id"]),
        str(upstream.get("title") or ""),
        *(_bare(str(s)) for s in upstream.get("surfaces") or []),
    ]
    return any(name and name in body for name in names)


def check_layout(tasks: Sequence[Mapping[str, Any]]) -> LayoutReport:
    """Findings per task, then the largest set of tasks that can run at once. A cycle is a finding, with width 0."""
    findings = [f for t in tasks for f in (*_surface_findings(t), *_artifact_findings(t))] + _edge_findings(tasks)
    try:
        width, layers = max_width(tasks)
    except ValueError as err:
        return LayoutReport(
            (*findings, LayoutFinding("", "cycle", str(err), "remove the cycle")),
            0,
            (),
        )
    return LayoutReport(tuple(findings), width, tuple(tuple(layer) for layer in layers))
