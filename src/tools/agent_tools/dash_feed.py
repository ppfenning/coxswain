"""Pure assembly of the versioned dash feed snapshot shape, plus the edge that
gathers a live one from the fleet's existing readers: `console_screen.gather`
for chair/spend/machines/runs, `run_store.read_queue` for the initiative
queue, and `courier.inbox` for the unacknowledged bus.
"""

from __future__ import annotations

from typing import Any

from agent_tools.dash_feed_build import EMPTY_CACHE as EMPTY_CACHE
from agent_tools.dash_feed_build import FeedCache as FeedCache
from agent_tools.dash_feed_build import _action_rows as _action_rows
from agent_tools.dash_feed_build import gather_feed as gather_feed
from agent_tools.dash_feed_build import gather_feed_cached as gather_feed_cached
from agent_tools.dash_feed_build import iso_ts_row as iso_ts_row
from agent_tools.dash_feed_build import open_needs_chair_ids as open_needs_chair_ids
from agent_tools.dash_feed_sections import _open_decisions as _open_decisions
from agent_tools.dash_feed_sections import _parse_now as _parse_now
from agent_tools.dash_feed_sections import _profile_ceilings as _profile_ceilings
from agent_tools.dash_feed_sections import _profile_path as _profile_path
from agent_tools.dash_feed_sections import _spend as _spend
from agent_tools.dash_feed_sections import _spend_points as _spend_points
from agent_tools.dash_feed_sections import _spend_v1 as _spend_v1
from agent_tools.dash_feed_sections import courier_blob as courier_blob
from agent_tools.dash_feed_sections import snapshot as snapshot
from core import module_facade

__all__ = ["EMPTY_CACHE", "FeedCache", "gather_feed", "gather_feed_cached", "snapshot"]


# H9 (docs/hardening.md): the feed lives in the part modules above.
# This module answers for every name they define, and a write (a test's monkeypatch) reaches every part that
# holds the name (core.module_facade). Callers' names are re-exported here, typed; the rest resolve on read.
def __getattr__(name: str) -> Any:
    return module_facade.attribute(__name__, name)


module_facade.install(
    __name__,
    (
        "agent_tools.dash_feed_sections",
        "agent_tools.dash_feed_build",
    ),
    eager=True,
)
