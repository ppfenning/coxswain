"""With the chair's cartridges published to the store, a lane host writes them itself and nothing is copied for them."""

from __future__ import annotations

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import cartridges_fetch_argv, rsync_check_argv, rsync_push_argv, ssh_argv, sync_argv
from agent_tools.remote_launch import LaunchError, launch_on_host, launch_plan

HOST = LaneHost("lane-a", "me@lane-a", "/ws")
SHA = "c" * 64
FETCH = ssh_argv("me@lane-a", cartridges_fetch_argv(SHA, "/ws/cartridges"))
UNGUARDED = {"chair_head": lambda path: None, "chair_base": lambda path: None}


def test_the_plan_swaps_the_two_copies_for_one_fetch_after_the_harness_sync() -> None:
    plan = launch_plan(HOST, "init-x", "init-x-1", "l", repo="/r", harness_dir="/h", cartridges="cartridges", cartridge_sha=SHA)
    assert rsync_push_argv("cartridges", "me@lane-a:/ws/cartridges", mirror=True) not in plan
    assert rsync_check_argv("cartridges", "me@lane-a:/ws/cartridges") not in plan
    assert plan[1] == ssh_argv("me@lane-a", sync_argv("/h")), "the harness first, so the host's cox knows the command"
    assert plan[2] == FETCH and plan[3] == ssh_argv("me@lane-a", sync_argv("/r"))
    assert FETCH[-1].endswith(f"cox route cartridges fetch --sha {SHA} --into /ws/cartridges --replace")


def test_without_a_sha_the_plan_is_the_copy_it_always_was() -> None:
    plan = launch_plan(HOST, "init-x", "init-x-1", "l", cartridges="cartridges")
    assert plan[1:3] == [
        rsync_push_argv("cartridges", "me@lane-a:/ws/cartridges", mirror=True),
        rsync_check_argv("cartridges", "me@lane-a:/ws/cartridges"),
    ]


def test_a_launch_runs_the_fetch_and_starts_the_lane() -> None:
    ran: list = []
    probed: list = []

    def probe(argv):
        probed.append(argv)
        return 0, "cartridges: /ws/cartridges holds cccccccccccc (2 written, 0 removed)\n"

    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", lambda argv: ran.append(argv) or 0, probe=probe, cartridges="cartridges",
        cartridge_sha=SHA, **UNGUARDED,
    )
    assert not isinstance(result, LaunchError)
    assert probed == [FETCH] and len(ran) == 2, "the initiative copy and the launch; no copy of the cartridges"


def test_a_host_that_cannot_write_the_snapshot_stops_the_launch_and_says_why() -> None:
    ran: list = []
    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", lambda argv: ran.append(argv) or 0,
        probe=lambda argv: (1, "cartridges: LookupError: the store holds no cartridge snapshot cccc\n"), cartridges="cartridges",
        cartridge_sha=SHA, **UNGUARDED,
    )
    assert isinstance(result, LaunchError) and result.step == "cartridges"
    assert "lane-a" in result.message and "holds no cartridge snapshot" in result.message
    assert "a cartridge other than the chair's" in result.message
    assert len(ran) == 1, "the lane was never started"
