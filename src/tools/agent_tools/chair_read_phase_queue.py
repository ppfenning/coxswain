"""The tick's read of the phase queue: the stored `phase_queue` rows, the lane-prerequisite gate, and which
initiatives are on the phase model or closed, from the work-item rows the tick already read (no second read)."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools import chair_capacity, queue_rows, run_store
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
    rollups = store_api.phase_rollups(url, phase)
    unread = (store_api.Failed, store_api.NotAvailable)
    return {
        "rows": rows if not isinstance(rows, unread) else [],
        "readable": not isinstance(rows, unread) and not isinstance(rollups, unread),
        "lane_prereqs": chair_capacity.chair_lane_prereqs(profile),
        "phase_initiatives": phase, "closed": closed, "tasks": phase_task_rows(work_rows, phase),
        "rollups": rollups if not isinstance(rollups, unread) else [],
    }


def phase_task_rows(heads: Sequence[Mapping[str, object]], initiatives: Sequence[str]) -> list[dict[str, object]]:
    """Pure. The task rows of the phase initiatives, as the chair's approvals read them."""
    keep = set(initiatives)
    return [{"initiative": r.get("initiative"), "task_id": r.get("task_id"), "phase": r.get("phase"), "state": r.get("state")}
            for r in heads if r.get("kind") == "task" and r.get("initiative") in keep]
