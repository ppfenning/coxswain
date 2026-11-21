"""The lane's fence against a real store (sqlite): it holds while the claim does, with the margin the claim's own lease
gives it, and once the claim is lost it keeps stopping the lane's children until the lane is done, so a check started
after the loss is stopped too."""

import threading
import time

from harness import store_phases
from harness.phase_lane import Fence, LaneClaim
from harness.store_migrate import open_store

AT = "2026-11-20T00:00:00Z"


def test_a_lost_fence_keeps_stopping_the_lane_s_children_until_the_lane_is_done(tmp_path):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    owner = open_store(url, AT)
    try:
        store_phases.apply_changes(owner, [{"op": "insert", "initiative": "ship", "phase": "p1", "requires": []}], "chair")
        claim = store_phases.claim_next(owner, "w1", [], 1, lease_s=32)
        stops: list[float] = []
        fence = Fence(lambda: open_store(url, AT), LaneClaim("ship", "p1", "w1", claim.epoch), lambda line: None,
                      lambda: stops.append(time.monotonic()), watch_s=0.05)
        assert fence.check()  # 32 s left, more than the 8 s margin its own lease gives the lane
        fence.start()
        time.sleep(0.3)
        assert stops == [] and not fence.lost.is_set()
        owner.execute("UPDATE phase_queue SET lease_until = '2000-01-01T00:00:00Z' WHERE phase = 'p1'")
        deadline = time.monotonic() + 10
        while len(stops) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert fence.lost.is_set() and len(stops) >= 3, stops  # the loss, then again while the lane runs on
        fence.stop()
        settled = len(stops)
        time.sleep(0.3)
        assert len(stops) == settled  # done: nothing more is stopped
        assert not [t for t in threading.enumerate() if t.name == "lane-fence"]
    finally:
        owner.close()
