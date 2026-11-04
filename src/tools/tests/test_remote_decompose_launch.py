from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import ssh_argv
from agent_tools.remote_launch import launch_plan

HOST = LaneHost("box", "me@box", "/srv/ws")
RSYNC_BOUND = ["rsync", "-a", "--timeout=30", "-e", "ssh -o ConnectTimeout=30"]


def test_a_decompose_plan_copies_the_intake_file_then_starts_the_decompose_launch():
    plan = launch_plan(HOST, "init-x", "run-1", "lbl", graph="decompose", intake="work/intake/init-x.md")
    assert plan == [
        [*RSYNC_BOUND, "--relative", "work/intake/init-x.md", "me@box:/srv/ws/"],
        ssh_argv(
            "me@box",
            [
                "cox", "route", "launch", "decompose", "--local", "--detach",
                "--idea", "/srv/ws/work/intake/init-x.md",
                "--initiative-id", "init-x",
                "--run-id", "run-1",
                "--label", "lbl",
                "--no-claim",
            ],
        ),
    ]


def test_the_default_plan_is_still_the_epic_mirror_then_the_epic_launch():
    plan = launch_plan(HOST, "init-x", "run-1", "lbl")
    assert plan == [
        [*RSYNC_BOUND, "--delete", "work/init-x/", "me@box:/srv/ws/work/init-x/"],
        ssh_argv(
            "me@box",
            [
                "cox", "route", "launch", "epic", "--local",
                "--initiative", "/srv/ws/work/init-x",
                "--run-id", "run-1",
                "--label", "lbl",
                "--no-claim",
            ],
        ),
    ]
