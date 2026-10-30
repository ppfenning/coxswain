import itertools
import threading

import pytest

from agent_tools import cli, store_cli

REPO = "/r/repo"
TTL = 1200
STEPS = [{"kind": "push", "branch": "pr/x"}, {"kind": "pr_create"}, {"kind": "wait_checks"}]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _walk(monkeypatch, tmp_path, guard):
    ran = []
    monkeypatch.setattr(cli, "_execute_land_step", lambda repo, step, forge=None: (ran.append(step["kind"]), (True, "ok"))[1])
    rc, reached, _ = cli._land_walk(tmp_path, STEPS, STEPS, None, None, "merge", False, None, [], guard=guard)
    return rc, reached, ran


def test_a_clock_past_the_ttl_renews_before_every_step_and_the_walk_finishes(monkeypatch, tmp_path):
    clock, renewed, lost = Clock(), [], []

    def renew():
        renewed.append(clock.now)
        clock.now += TTL + 1
        return store_cli.LeaseGranted(7, "me")

    rc, reached, ran = _walk(monkeypatch, tmp_path, cli._repo_lease_guard(REPO, renew, clock, TTL, lost))
    assert (rc, reached, ran, lost) == (0, ["push", "pr_create", "wait_checks"], ["push", "pr_create", "wait_checks"], [])
    assert renewed == [0.0, TTL + 1, 2 * (TTL + 1)]


def test_a_refused_renew_stops_the_walk_at_that_step_and_names_the_repo(monkeypatch, tmp_path, capsys):
    clock, lost, answers = Clock(), [], iter([store_cli.LeaseGranted(7, "me"), store_cli.LeaseRefused(8, "other@h:9")])
    guard = cli._repo_lease_guard(REPO, lambda: next(answers), clock, TTL, lost)
    rc, reached, ran = _walk(monkeypatch, tmp_path, guard)
    out = capsys.readouterr().out
    assert (rc, reached, ran) == (2, ["push"], ["push"])
    assert "land: refusing pr_create, the repo lease for /r/repo was lost: held by other@h:9 at epoch 8" in out
    assert "stopped; remaining: pr_create, wait_checks" in out
    assert len(lost) == 1


@pytest.mark.parametrize("result, why", [
    (store_cli.LeaseError("boom"), "boom"),
    (store_cli.NotAvailable(), "the store is not available"),
    (store_cli.NotAvailable("no harness"), "no harness"),
])
def test_an_unrenewable_lease_stops_the_walk_but_is_not_marked_lost(monkeypatch, tmp_path, capsys, result, why):
    lost = []
    rc, reached, ran = _walk(monkeypatch, tmp_path, cli._repo_lease_guard(REPO, lambda: result, Clock(), TTL, lost))
    assert (rc, reached, ran, lost) == (2, [], [], [])
    assert f"land: refusing push, the repo lease for /r/repo could not be renewed: {why}" in capsys.readouterr().out


def test_the_stop_says_how_long_the_lease_went_unrenewed_when_it_had_expired():
    refused = store_cli.LeaseRefused(8, "other")
    assert "1500s since the last renewal against a 1200s ttl" in cli._repo_lease_stop(REPO, "merge", refused, 0.0, 1500.0, TTL)
    assert "since the last renewal" not in cli._repo_lease_stop(REPO, "merge", refused, 0.0, 10.0, TTL)
    assert cli._repo_lease_stop(REPO, "merge", store_cli.LeaseGranted(7, "me"), 0.0, 9999.0, TTL) is None


def test_the_second_guard_runs_only_when_the_first_lets_the_step_run():
    seen = []

    def then(kind):
        seen.append(kind)

    assert cli._first_stop(lambda kind: None, then)("merge") is None
    assert cli._first_stop(lambda kind: "lost", then)("push") == "lost"
    assert cli._first_stop(lambda kind: None, None)("push") is None
    assert seen == ["merge"]


def test_any_refusal_is_settled_by_an_acquire_because_the_store_does_not_say_why_a_renew_failed():
    assert cli._lease_standing(store_cli.LeaseGranted(7, "me")) == "kept"
    assert cli._lease_standing(store_cli.LeaseRefused(None, None)) == "refused"
    assert cli._lease_standing(store_cli.LeaseRefused(8, "other@h:9")) == "refused"
    assert cli._lease_standing(store_cli.LeaseError("boom")) == "unrenewable"
    assert cli._lease_standing(store_cli.NotAvailable()) == "unrenewable"


def test_the_store_answers_a_refused_renew_with_no_holder_whether_it_lapsed_or_was_taken():
    refused = store_cli.parse_lease(3, '{"ok": false, "epoch": null, "holder": null}')
    assert refused == store_cli.LeaseRefused(None, None)
    assert cli._lease_standing(refused) == "refused"


def test_a_timer_store_error_is_not_remembered_and_the_next_renew_still_runs():
    answers, lost = iter([store_cli.LeaseError("boom"), store_cli.LeaseGranted(7, "me")]), []
    guard = cli._repo_lease_guard(REPO, lambda: next(answers), Clock(), TTL, lost)
    assert guard.beat() is None
    assert guard("merge") is None
    assert lost == []


def test_a_refusal_on_the_timer_is_remembered_and_appended_to_lost_once():
    renews, lost = [], []

    def renew():
        renews.append(1)
        return store_cli.LeaseRefused(None, None)

    guard = cli._repo_lease_guard(REPO, renew, Clock(), TTL, lost, lambda: store_cli.LeaseRefused(8, "other@h:9"))
    assert guard.beat() is not None
    assert "held by other@h:9 at epoch 8" in guard("merge")
    assert guard.beat() is not None
    assert (len(renews), len(lost)) == (1, 1)


def test_the_renew_loop_beats_once_per_interval_until_stopped_and_survives_a_failed_beat():
    clock, beats = Clock(), []

    class Stop:
        calls = 0

        def wait(self, interval):
            clock.now += interval
            self.calls += 1
            return self.calls > 4

    def beat():
        beats.append(clock.now)
        raise RuntimeError("one bad renew")

    cli._renew_loop(beat, 400, Stop())
    assert beats == [400.0, 800.0, 1200.0, 1600.0]


class FakeStop(threading.Event):
    """A stop event whose wait parks the renewer thread until the test fires a beat, so the fake clock decides when it runs."""

    def __init__(self) -> None:
        super().__init__()
        self.parked, self.go = threading.Semaphore(0), threading.Semaphore(0)

    def wait(self, timeout=None):
        self.parked.release()
        assert self.go.acquire(timeout=5)
        return self.is_set()

    def set(self):
        super().set()
        self.go.release()

    def fire(self):
        assert self.parked.acquire(timeout=5)
        self.go.release()

    def settle(self):
        assert self.parked.acquire(timeout=5)
        self.parked.release()


class Lease:
    def __init__(self) -> None:
        self.clock, self.stop, self.epoch, self.mode, self.calls, self.lost = Clock(), FakeStop(), 7, "mine", [], []

    def renew(self):
        self.calls.append((self.clock.now, self.epoch))
        # The store refuses a renew with no holder whether the lease lapsed or was taken; only the acquire tells them apart.
        return store_cli.LeaseGranted(self.epoch, "me") if self.mode == "mine" else store_cli.LeaseRefused(None, None)

    def reacquire(self):
        if self.mode == "taken":
            return store_cli.LeaseRefused(8, "other@h:9")
        self.mode, self.epoch = "mine", 9
        return store_cli.LeaseGranted(9, "me")

    def guard(self):
        return cli._LeaseGuard(f"repo lease for {REPO}", self.renew, self.reacquire, self.clock, TTL,
                               lambda kind, result, renewed, now: cli._repo_lease_stop(REPO, kind, result, renewed, now, TTL),
                               self.lost, new_stop=lambda: self.stop)

    def wait_checks(self, after):
        """Four beats a third of the ttl apart, so the wait outlasts the ttl; `after` maps a beat number to the mode the lease takes next."""
        for n in range(4):
            self.clock.now += TTL // 3
            self.stop.fire()
            self.stop.settle()
            self.mode = dict(after).get(n, self.mode)


def _land(monkeypatch, tmp_path, lease, after=()):
    ran, steps = [], [{"kind": "wait_checks"}, {"kind": "merge", "branch": "pr/x"}]

    def step(repo, step, forge=None):
        ran.append(step["kind"])
        if step["kind"] == "wait_checks":
            lease.wait_checks(after)
        return True, "ok"

    monkeypatch.setattr(cli, "_execute_land_step", step)
    guard = lease.guard()
    rc, _reached, _ = cli._land_execute(tmp_path, steps, steps, None, None, "merge", False, None, guard=guard, keep=[guard])
    return rc, ran


def test_a_wait_longer_than_the_ttl_still_merges_and_the_lease_renews_every_third_of_it(monkeypatch, tmp_path):
    lease = Lease()
    assert lease.guard().interval == TTL // 3
    rc, ran = _land(monkeypatch, tmp_path, lease)
    times = [at for at, _ in lease.calls]
    assert (rc, ran, lease.lost) == (0, ["wait_checks", "merge"], [])
    assert times == [0.0, 400.0, 800.0, 1200.0, 1600.0, 1600.0]
    assert max(b - a for a, b in itertools.pairwise(times)) <= TTL // 3


def test_a_lease_taken_by_another_holder_mid_wait_stops_the_land_before_merge(monkeypatch, tmp_path, capsys):
    lease = Lease()
    rc, ran = _land(monkeypatch, tmp_path, lease, after=[(1, "taken")])
    assert (rc, ran) == (2, ["wait_checks"])
    assert "land: refusing merge, the repo lease for /r/repo was lost: held by other@h:9 at epoch 8" in capsys.readouterr().out
    assert len(lease.lost) == 1


def test_a_lease_that_expired_with_no_holder_is_reacquired_and_the_land_continues(monkeypatch, tmp_path, capsys):
    lease = Lease()
    rc, ran = _land(monkeypatch, tmp_path, lease, after=[(0, "expired")])
    assert (rc, ran, lease.lost) == (0, ["wait_checks", "merge"], [])
    assert "land: the repo lease for /r/repo expired with no holder; re-acquired it at epoch 9, continuing" in capsys.readouterr().out
    assert [epoch for _, epoch in lease.calls] == [7, 7, 7, 9, 9, 9]
