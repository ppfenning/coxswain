"""Classify a failed CI run whose jobs never got past setup."""

from collections.abc import Mapping

from core.jsontypes import is_list, is_mapping

_PASSING = ("success", "skipped", "neutral")


def _items(value: object) -> list[Mapping[str, object]]:
    """The mapping elements of a JSON list; an absent or empty value is no elements."""
    return [item for item in value if is_mapping(item)] if is_list(value) else []


def _is_failed(job: Mapping[str, object]) -> bool:
    return job.get("conclusion") not in _PASSING


def _never_started(job: Mapping[str, object]) -> bool:
    return not any(step.get("name") != "Set up job" and bool(step.get("conclusion")) for step in _items(job.get("steps")))


def never_started(payload: Mapping[str, object]) -> bool:
    """True when jobs failed and none ran a step past "Set up job"; a later step conclusion is a real failure."""
    failed = [job for job in _items(payload.get("jobs")) if _is_failed(job)]
    return bool(failed) and all(_never_started(job) for job in failed)
