"""The tick's read of the phase queue: the stored `phase_queue` rows, the lane-prerequisite gate, and which
initiatives are on the phase model or closed, and each one's `target_release` and intake `kind` (the queue's order,
`chair_phase_queue.order`), from the work-item rows the tick already read (no second read)."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools import chair_capacity, queue_rows, release_gate, run_store
from agent_tools.chair_types import PhaseQueueFacts


def initiative_sets(heads: Sequence[Mapping[str, object]]) -> tuple[list[str], list[str]]:
    """Pure. From the work-item rows: the initiatives stamped `lane_model: phase`, and those done or dropped."""
    own = [r for r in heads if r.get("kind") == "initiative"]
    phase = sorted(str(r["initiative"]) for r in own if queue_rows.lane_model_of_row(r) == "phase")
    closed = sorted(str(r["initiative"]) for r in own if r.get("state") in ("done", "dropped"))
    return phase, closed


def read_phase_queue(runs_dir: Path, profile: Mapping[str, object], work_rows: Sequence[Mapping[str, object]]) -> PhaseQueueFacts:
    """Edge. `work_rows` is the tick's own cached read of `work_items`. When `phase_queue` cannot be read the fact says
    so (`readable` False): the phase model is still known from `work_rows`, so the old-model gate holds, and the
    planner writes nothing to a queue it could not read."""
    from harness import store_api

    phase, closed = initiative_sets(work_rows)
    url = run_store.store_url_for(runs_dir)
    rows = store_api.phase_queue_rows(url)
    review = store_api.phase_review_facts(url, phase)
    unread = (store_api.Failed, store_api.NotAvailable)
    rollups, tasks, rounds = review if not isinstance(review, unread) else ([], [], [])
    return {
        "rows": rows if not isinstance(rows, unread) else [],
        "readable": not isinstance(rows, unread) and not isinstance(review, unread),
        "lane_prereqs": chair_capacity.chair_lane_prereqs(profile),
        "phase_initiatives": phase, "closed": closed, "tasks": tasks, "rollups": rollups, "rounds": rounds,
        "releases": release_gate.initiative_values(work_rows, "target_release"),
        "kinds": release_gate.initiative_values(work_rows, "kind"),
    }
