"""Pure mapping from a candidate's labels and milestone onto intake fields."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, fields

_VERSION = re.compile(r"\d+(\.\d+)+")
_INTEGER = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class PullMap:
    """Label names that carry intake fields; the profile may change them."""

    ready_label: str = "intake:ready"
    priority_prefix: str = "priority:"
    epic_prefix: str = "epic:"
    milestone_is_release: bool = True

    @classmethod
    def from_profile(cls, raw: dict) -> PullMap | str:
        """Build from a plain dict; a refusal string names an unknown key or a wrong-typed value."""
        known = {f.name: f.type for f in fields(cls)}
        unknown = sorted(set(raw) - set(known))
        if unknown:
            return f"unknown pull-map key: {unknown[0]}"
        wrong = [k for k, v in sorted(raw.items()) if not isinstance(v, bool if known[k] == "bool" else str)]
        if wrong:
            want = "a boolean" if known[wrong[0]] == "bool" else "a string"
            return f"pull-map key {wrong[0]} must be {want}"
        return cls(**raw)


def _remainders(sorted_labels: list[str], prefix: str) -> list[str]:
    return [label[len(prefix) :] for label in sorted_labels if prefix and label.startswith(prefix)]


def map_fields(labels: Iterable[str], milestone: str | None, pull_map: PullMap) -> tuple[dict, list[str]]:
    """Return the intake fields and notes; priority is None when no label gives an integer."""
    ordered = sorted(labels)
    priorities = [int(r) for r in _remainders(ordered, pull_map.priority_prefix) if _INTEGER.fullmatch(r)]
    epics = _remainders(ordered, pull_map.epic_prefix)
    is_version = bool(milestone) and _VERSION.fullmatch(milestone) is not None
    release = milestone if pull_map.milestone_is_release and is_version else ""
    notes = (
        [f"milestone {milestone!r} is not a version; target_release left empty"]
        if pull_map.milestone_is_release and milestone and not is_version
        else []
    )
    fields_out = {
        "stage": "ready" if pull_map.ready_label in ordered else "draft",
        "priority": priorities[0] if priorities else None,
        "epic": epics[0] if epics else "",
        "target_release": release,
    }
    return fields_out, notes
