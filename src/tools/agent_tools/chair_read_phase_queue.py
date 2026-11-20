"""The tick's read of the phase queue: the stored `phase_queue` rows, the lane-prerequisite gate, and which
initiatives are on the phase model or closed, from their own store rows."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools import chair_capacity, queue_rows, run_store
from agent_tools.chair_types import PhaseQueueFacts


def initiative_sets(heads: Sequence[Mapping[str, object]]) -> tuple[list[str], list[str]]:
    """Pure. From the initiative rows: the ids stamped `lane_model: phase`, and those done or dropped."""
    phase = sorted(str(r["initiative"]) for r in heads if queue_rows.lane_model_of_row(r) == "phase")
    closed = sorted(str(r["initiative"]) for r in heads if r.get("state") in ("done", "dropped"))
    return phase, closed


def read_phase_queue(runs_dir: Path, profile: Mapping[str, object]) -> PhaseQueueFacts | None:
    """Edge. None when the store cannot be read, so the tick plans no queue write on a guess."""
    from harness import store_api

    url = run_store.store_url_for(runs_dir)
    rows = store_api.phase_queue_rows(url)
    heads = store_api.queue_rows(url, None, "initiative")
    if isinstance(rows, store_api.Failed | store_api.NotAvailable) or isinstance(heads, store_api.Failed | store_api.NotAvailable):
        return None
    phase, closed = initiative_sets([dict(h) for h in heads])
    return {"rows": rows, "lane_prereqs": chair_capacity.chair_lane_prereqs(profile), "phase_initiatives": phase, "closed": closed}
