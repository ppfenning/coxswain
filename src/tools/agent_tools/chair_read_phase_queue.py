"""The tick's read of the phase queue: the stored `phase_queue` rows and the lane-prerequisite gate."""

from collections.abc import Mapping
from pathlib import Path

from agent_tools import chair_capacity, run_store
from agent_tools.chair_types import PhaseQueueFacts


def read_phase_queue(runs_dir: Path, profile: Mapping[str, object]) -> PhaseQueueFacts | None:
    """Edge. None when the store cannot be read, so the tick plans no queue write on a guess."""
    from harness import store_api

    rows = store_api.phase_queue_rows(run_store.store_url_for(runs_dir))
    if isinstance(rows, store_api.Failed | store_api.NotAvailable):
        return None
    return {"rows": rows, "lane_prereqs": chair_capacity.chair_lane_prereqs(profile)}
