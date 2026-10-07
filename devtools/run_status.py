"""Classify `gh run list --branch <b> --json conclusion,name,url` output. Pure: no gh call, clock or environment."""

from dataclasses import dataclass
from typing import Literal

Kind = Literal["startup_failure", "no_workflow", "unknown"]


@dataclass(frozen=True)
class Verdict:
    kind: Kind
    name: str = ""
    url: str = ""


def classify_runs(runs: list[dict[str, str]]) -> Verdict:
    """The first startup_failure run in list order wins, wherever it sits; a missing conclusion is not one."""
    failed = next((r for r in runs if r.get("conclusion") == "startup_failure"), None)
    if failed is not None:
        return Verdict("startup_failure", failed["name"], failed["url"])
    if not runs:
        return Verdict("no_workflow")
    return Verdict("unknown")


def render_verdict(verdict: Verdict, branch: str) -> str:
    """One message line; the branch is a parameter because the verdict does not carry it."""
    if verdict.kind == "startup_failure":
        return f"workflow {verdict.name} hit startup_failure: {verdict.url}"
    if verdict.kind == "no_workflow":
        return f"no workflow ran for branch {branch}"
    return f"no startup_failure among the runs for branch {branch}"
