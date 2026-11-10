import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agent_tools import chair_exec, chair_report, cli
from agent_tools.chair_run import RunDeps, tick
from agent_tools.lane_hosts import LaneHost
from agent_tools.notify_core import NotifyConfig
from agent_tools.notify_dispatch import dispatch

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
LEASE = {"holder": "me", "host": "box", "epoch": 3, "mine": True, "released": False, "stale": False}
FACTS = {
    "lease": LEASE,
    "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
    "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
    "approved": [], "initiatives": [], "quarantines": [], "intake": [],
    "work_store_ready": False, "sources_configured": False,
    "last_housekeeping_at": NOW.isoformat(), "housekeeping_hours": 24,
}


def dirty(repo: str = "/srv/repo", host: str = "box-a") -> dict:
    return {
        "kind": "needs_chair", "host": host, "cause": "lane_checkout_dirty",
        "reason": f"lane host {host}, checkout {repo}: uncommitted changes",
    }


DIRTY = dirty()


class Rig:
    """Every dep as a stub but `perform` and the notifier: `recorded` is what reached the action log, `posts` what the
    real `dispatch` handed to its post function, `answers` the post results in order (True once they run out)."""

    def __init__(self, tmp_path, sync, answers=()):
        self.recorded, self.posts, self.syncs, self.sync = [], [], 0, sync
        self.tmp_path, self.answers = tmp_path, list(answers)

    def _sync(self):
        self.syncs += 1
        return self.sync()

    def _post(self, url, *rendered):
        self.posts.append(rendered)
        return self.answers.pop(0) if self.answers else True

    def deps(self) -> RunDeps:
        exec_deps = chair_exec.Deps(
            run=lambda argv: (0, ""),
            delete_branches=lambda repo, pattern: ([], ""),
            acquire_lease=lambda holder, host: "",
            record=self.recorded.append,
            run_id=lambda action: "run-1",
            repo_for=lambda action: "/srv/repo",
        )
        return RunDeps(
            facts_deps=object(),
            exec_deps=exec_deps,
            report_deps=chair_report.Deps(echo=lambda line: None, notify=None),
            beat=lambda: None,
            current_epoch=lambda: 3,
            holds=lambda: True,
            release=lambda: None,
            sleep=lambda seconds: None,
            now=lambda: NOW,
            gather=lambda _deps, _now: FACTS,
            plan=lambda facts, now: [],
            notify=lambda events, config, path, stamp: dispatch(events, config, path, stamp, post=self._post),
            notify_config=NotifyConfig("https://ntfy.sh/t"),
            workspace=self.tmp_path,
            lane_checkout_actions=self._sync,
        )

    def lane_records(self):
        return [a for a in self.recorded if a.get("kind") == "needs_chair"]

    def lane_posts(self):
        return [p for p in self.posts if "box-a" in str(p)]  # the idle-queue push is the facts' own, not the lane's


def test_a_clean_lane_checkout_produces_no_action_and_no_push(tmp_path):
    rig = Rig(tmp_path, lambda: [])
    tick(rig.deps(), False, NOW)
    assert rig.syncs == 1 and rig.lane_records() == [] and rig.lane_posts() == []


def test_a_dirty_lane_checkout_is_recorded_once_naming_host_and_checkout_and_pushed(tmp_path):
    rig = Rig(tmp_path, lambda: [DIRTY])
    tick(rig.deps(), False, NOW)
    [record] = rig.lane_records()
    assert record["status"] == "recorded" and "box-a" in record["reason"] and "/srv/repo" in record["reason"]
    [push] = rig.lane_posts()
    assert "box-a" in str(push) and "/srv/repo" in str(push)


def test_the_same_dirty_checkout_on_a_second_tick_is_not_recorded_or_pushed_again(tmp_path):
    rig = Rig(tmp_path, lambda: [DIRTY])
    deps = rig.deps()
    tick(deps, False, NOW)
    tick(deps, False, NOW)
    assert rig.syncs == 2 and len(rig.lane_records()) == 1 and len(rig.lane_posts()) == 1


def test_a_dirty_checkout_that_cleared_and_came_back_is_recorded_again(tmp_path):
    answers = iter([[DIRTY], [], [DIRTY]])
    rig = Rig(tmp_path, lambda: next(answers))
    deps = rig.deps()
    for _ in range(3):
        tick(deps, False, NOW)
    assert len(rig.lane_records()) == 2


def test_two_dirty_checkouts_on_one_host_are_each_recorded_and_pushed(tmp_path):
    one, two, three = dirty("/srv/repo-1"), dirty("/srv/repo-2"), dirty("/srv/repo-3")
    answers = iter([[one, two], [one, two], [one, two, three]])
    rig = Rig(tmp_path, lambda: next(answers))
    deps = rig.deps()
    tick(deps, False, NOW)
    assert [("/srv/repo-1" in a["reason"], "/srv/repo-2" in a["reason"]) for a in rig.lane_records()] == [
        (True, False), (False, True),
    ]
    assert len(rig.lane_posts()) == 2
    tick(deps, False, NOW)
    assert len(rig.lane_records()) == 2 and len(rig.lane_posts()) == 2
    tick(deps, False, NOW)
    assert len(rig.lane_records()) == 3 and "/srv/repo-3" in rig.lane_records()[2]["reason"]
    assert len(rig.lane_posts()) == 3


def test_a_raising_sync_is_one_lane_checkout_error_action_and_the_tick_goes_on(tmp_path):
    def boom():
        raise OSError("ssh gone")

    rig = Rig(tmp_path, boom)
    line = tick(rig.deps(), False, NOW)
    [record] = rig.lane_records()
    assert record["cause"] == "lane_checkout_error" and "ssh gone" in record["reason"]
    assert line


def test_a_sync_that_raises_between_two_dirty_ticks_does_not_clear_the_dirty_checkout(tmp_path):
    answers = iter([[DIRTY], OSError("ssh gone"), [DIRTY]])

    def sync():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    rig = Rig(tmp_path, sync)
    deps = rig.deps()
    for _ in range(3):
        tick(deps, False, NOW)
    assert [a["cause"] for a in rig.lane_records()] == ["lane_checkout_dirty: /srv/repo", "lane_checkout_error"]


def test_a_dry_run_never_calls_the_sync(tmp_path):
    rig = Rig(tmp_path, lambda: [DIRTY])
    tick(rig.deps(), True, NOW)
    assert rig.syncs == 0 and rig.recorded == [] and rig.lane_posts() == []


def test_no_sync_wired_leaves_the_tick_as_it_was(tmp_path):
    rig = Rig(tmp_path, lambda: [DIRTY])
    tick(replace(rig.deps(), lane_checkout_actions=None), False, NOW)
    assert rig.syncs == 0 and rig.lane_records() == [] and rig.lane_posts() == []


def test_an_action_lost_to_a_failing_perform_is_raised_again_on_the_next_tick(tmp_path):
    def perform_fails(actions, exec_deps, current_epoch, dry_run):
        raise RuntimeError("store gone")

    rig = Rig(tmp_path, lambda: [DIRTY])
    deps = rig.deps()
    with pytest.raises(RuntimeError):
        tick(replace(deps, perform=perform_fails), False, NOW)
    assert rig.lane_records() == []
    tick(deps, False, NOW)
    assert len(rig.lane_records()) == 1


def test_an_action_that_perform_refused_is_raised_again_on_the_next_tick(tmp_path):
    rig = Rig(tmp_path, lambda: [DIRTY])
    deps = rig.deps()
    tick(replace(deps, perform=lambda actions, exec_deps, current_epoch, dry_run: []), False, NOW)
    tick(deps, False, NOW)
    assert len(rig.lane_records()) == 1


def test_a_push_that_failed_is_tried_again_on_the_next_tick_without_recording_again(tmp_path):
    rig = Rig(tmp_path, lambda: [DIRTY], answers=[False])
    deps = rig.deps()
    tick(deps, False, NOW)
    tick(deps, False, NOW)
    tick(deps, False, NOW)
    assert len(rig.lane_posts()) == 2 and len(rig.lane_records()) == 1


def _cli_deps(tmp_path, profile_text, dry_run=False):
    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    profile = tmp_path / "p.yaml"
    if profile_text is not None:
        profile.write_text(profile_text)
    return cli._chair_run_deps(ws / "runs", {}, "chair", 1, "h", dry_run, print, profile, "files"), ws


HOSTS = "lane_hosts:\n  - name: box-a\n    ssh: me@box-a\n    workspace_dir: /srv/op\n"


@pytest.mark.parametrize("profile_text", [None, "x: 1\n", "lane_hosts: not-a-list\n", "lane_hosts: []\n"])
def test_a_profile_without_usable_lane_hosts_wires_no_sync(tmp_path, profile_text):
    deps, _ws = _cli_deps(tmp_path, profile_text)
    assert deps.lane_checkout_actions is None


def test_a_dry_run_wires_no_sync_even_with_lane_hosts(tmp_path):
    deps, _ws = _cli_deps(tmp_path, HOSTS, dry_run=True)
    assert deps.lane_checkout_actions is None


def test_lane_hosts_wire_a_sync_over_the_hosts_the_existing_repos_the_workspace_and_the_bounded_runner(monkeypatch, tmp_path):
    here = tmp_path / "repo-a"
    here.mkdir()
    seen = []
    monkeypatch.setattr(cli.chair_read_quarantined, "initiative_files", lambda ws, files: {})
    monkeypatch.setattr(cli, "_initiative_repos", lambda files, home: {"one": str(here), "two": str(tmp_path / "gone")})
    monkeypatch.setattr(cli.lane_checkout_sync, "sync_lane_checkouts", lambda *args: seen.append(args) or [DIRTY])
    deps, ws = _cli_deps(tmp_path, HOSTS)
    assert deps.lane_checkout_actions() == [DIRTY]
    [(hosts, repos, workspace, run)] = seen
    assert (tuple(hosts), list(repos), workspace, run) == (
        (LaneHost("box-a", "me@box-a", "/srv/op"),), [str(here)], str(ws), cli._lane_sync_ssh,
    )


def test_the_lane_sync_ssh_edge_never_prompts_bounds_each_call_and_reads_a_timeout_as_a_nonzero_code(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    code, out = cli._lane_sync_ssh(["ssh", "me@box-a", "true"])
    assert calls == [(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "me@box-a", "true"], 60)]
    assert code == 124 and "60s" in out
