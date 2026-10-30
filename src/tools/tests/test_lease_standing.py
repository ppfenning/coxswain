from agent_tools.lease_standing import standing, status

NOW = "2026-10-30T12:00:00Z"


def test_no_row_is_none():
    assert standing(None, "run-1", NOW) == "none"


def test_other_holder_is_fenced():
    assert standing(("run-2", "2026-10-30T12:05:00Z", "2026-10-30T12:00:00Z"), "run-1", NOW) == "fenced"


def test_sibling_with_same_prefix_is_fenced():
    assert standing(("run-10", "2026-10-30T12:05:00Z", "2026-10-30T12:00:00Z"), "run-1", NOW) == "fenced"
    assert standing(("run-1", "2026-10-30T12:05:00Z", "2026-10-30T12:00:00Z"), "run-10", NOW) == "fenced"


def test_own_unexpired_lease_is_held():
    assert standing(("run-1", "2026-10-30T12:00:01Z", "2026-10-30T11:59:50Z"), "run-1", NOW) == "held"


def test_own_lease_expiring_now_after_heartbeat_is_expired():
    assert standing(("run-1", NOW, "2026-10-30T11:59:00Z"), "run-1", NOW) == "expired"


def test_own_lease_past_expiry_after_heartbeat_is_expired():
    assert standing(("run-1", "2026-10-30T11:59:30Z", "2026-10-30T11:59:00Z"), "run-1", NOW) == "expired"


def test_own_lease_expiring_at_heartbeat_is_released():
    assert standing(("run-1", "2026-10-30T11:59:00Z", "2026-10-30T11:59:00Z"), "run-1", NOW) == "released"


def test_own_lease_expiring_before_heartbeat_is_released():
    assert standing(("run-1", "2026-10-30T11:00:00Z", "2026-10-30T11:59:00Z"), "run-1", NOW) == "released"


def test_dead_process_is_exited_whatever_the_standing():
    for s in ("none", "held", "expired", "released", "fenced"):
        assert status(False, s) == "exited"


def test_live_process_with_fenced_or_expired_standing_is_fenced():
    assert status(True, "fenced") == "fenced"
    assert status(True, "expired") == "fenced"


def test_live_process_with_none_released_or_held_standing_is_running():
    assert status(True, "none") == "running"
    assert status(True, "released") == "running"
    assert status(True, "held") == "running"
