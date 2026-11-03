"""A land reads the PR's merge state before `merge` and updates a BEHIND branch first."""
import functools
import threading
import time

from agent_tools import cli, land, land_merge_lock, store_cli
from agent_tools.forge import ForgeError


class FakeForge:
    def __init__(self, state, waits=((True, "green"),), merges=None):
        self.state = state
        self.waits = iter(waits)
        self.merges = None if merges is None else iter(merges)
        self.calls = []
        self.repos = []

    def merge_state(self, pr, *, repo=None):
        self.calls.append("merge_state")
        self.repos.append(repo)
        return self.state

    def update_branch(self, pr, *, repo=None):
        self.calls.append("update_branch")
        self.repos.append(repo)

    def wait_checks(self, repo, timeout_s, ref="HEAD"):
        self.calls.append("wait_checks")
        return next(self.waits)

    def merge(self, repo, step):
        self.calls.append("merge")
        return (True, "merged") if self.merges is None else next(self.merges)


STEPS = [{"kind": "wait_checks", "branch": "origin/pr/x", "pr": 7}, {"kind": "merge", "branch": "pr/x"}]


def _walk(tmp_path, monkeypatch, forge):
    monkeypatch.setattr(cli, "_fetch_branch", lambda repo, branch: None)
    return cli._land_walk(tmp_path, STEPS, STEPS, None, None, "full", False, forge, [])


def test_behind_updates_waits_again_then_merges(tmp_path, monkeypatch):
    forge = FakeForge("BEHIND", [(True, "green"), (True, "green")])
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 0
    assert forge.calls == ["wait_checks", "merge_state", "update_branch", "wait_checks", "merge"]
    assert forge.repos == [tmp_path, tmp_path]  # gh runs in the land's repo, never the chair's working directory


def test_behind_with_a_failing_rewait_stops_before_merge(tmp_path, monkeypatch, capsys):
    forge = FakeForge("BEHIND", [(True, "green"), (False, "check lint failed")])
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 1
    assert "merge" not in forge.calls
    assert "wait_checks: check lint failed" in capsys.readouterr().out


def test_clean_merges_with_no_update_and_no_extra_wait(tmp_path, monkeypatch):
    forge = FakeForge("CLEAN")
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 0
    assert forge.calls == ["wait_checks", "merge_state", "merge"]


def test_dirty_stops_naming_the_state(tmp_path, monkeypatch, capsys):
    forge = FakeForge("DIRTY")
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 1
    assert "DIRTY" in capsys.readouterr().out
    assert "update_branch" not in forge.calls
    assert "merge" not in forge.calls


def test_merge_gate_clean_merges():
    assert cli.merge_gate("CLEAN") == ("merge", "")


def test_merge_gate_behind_updates_then_merges():
    assert cli.merge_gate("BEHIND") == ("update_then_merge", "")


def test_merge_gate_stops_only_on_a_state_that_can_never_merge():
    for state in ("DIRTY", "BLOCKED", "DRAFT"):
        assert cli.merge_gate(state) == ("stop", state)
    for state in ("UNSTABLE", "HAS_HOOKS", "UNKNOWN"):
        assert cli.merge_gate(state) == ("merge", "")


def test_settled_merge_state_rereads_unknown_until_it_settles():
    reads, naps = iter(["UNKNOWN", "UNKNOWN", "CLEAN"]), []
    assert cli._settled_merge_state(lambda: next(reads), naps.append) == "CLEAN"
    assert naps == [cli._UNKNOWN_WAIT_S, cli._UNKNOWN_WAIT_S]


def test_settled_merge_state_gives_up_as_unknown_after_its_reads():
    naps = []
    assert cli._settled_merge_state(lambda: "UNKNOWN", naps.append) == "UNKNOWN"
    assert len(naps) == cli._UNKNOWN_READS - 1


def test_unknown_that_settles_clean_merges(tmp_path, monkeypatch):
    forge = FakeForge("UNKNOWN")
    states = iter(["UNKNOWN", "CLEAN"])
    forge.merge_state = lambda pr, *, repo=None: (forge.calls.append("merge_state"), next(states))[1]
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 0
    assert forge.calls == ["wait_checks", "merge_state", "merge_state", "merge"]


def test_pr_number_prefers_the_wait_checks_pr_then_the_url():
    assert cli._pr_number(STEPS, 1, "") == 7
    assert cli._pr_number([{"kind": "merge"}], 0, "https://github.com/o/r/pull/12") == 12
    assert cli._pr_number([{"kind": "merge"}], 0, "") is None


BEHIND_REFUSAL = "Pull request is not mergeable: the head branch is not up to date with the base branch"


class FakeLease:
    """A repo lease store that grants every renewal but the ones scripted to be refused, and counts them."""

    def __init__(self, refuse_on=()):
        self.refuse_on = set(refuse_on)
        self.renewals = 0

    def renew(self):
        self.renewals += 1
        if self.renewals in self.refuse_on:
            return store_cli.LeaseRefused(epoch=4, holder="other@host:99")
        return store_cli.LeaseGranted(epoch=3, holder="me@host:1")


def _leased_walk(tmp_path, monkeypatch, forge, lease, attempts=None):
    monkeypatch.setattr(cli, "_fetch_branch", lambda repo, branch: None)
    monkeypatch.setattr(cli, "_merge_updating", functools.partial(cli._merge_updating, attempts=attempts))
    guard = cli._repo_lease_guard(str(tmp_path), lease.renew, lambda: 0.0, 1200, [])
    return cli._land_walk(tmp_path, STEPS, STEPS, None, None, "full", False, forge, [], guard=guard)


def test_merge_refused_as_behind_updates_waits_again_then_merges(tmp_path, monkeypatch):
    forge = FakeForge("CLEAN", [(True, "green"), (True, "green")], merges=[(False, BEHIND_REFUSAL), (True, "merged")])
    rc, _, _ = _leased_walk(tmp_path, monkeypatch, forge, FakeLease())
    assert rc == 0
    assert forge.calls == ["wait_checks", "merge_state", "merge", "update_branch", "wait_checks", "merge"]


def test_behind_past_the_bound_stops_once_at_the_merge(tmp_path, monkeypatch, capsys):
    forge = FakeForge("CLEAN", [(True, "green")] * 3, merges=[(False, BEHIND_REFUSAL)] * 3)
    rc, _, _ = _leased_walk(tmp_path, monkeypatch, forge, FakeLease(), attempts=2)
    out = capsys.readouterr().out
    reason = land.behind_stop(2, BEHIND_REFUSAL)
    assert rc == 1
    assert forge.calls.count("update_branch") == 2
    assert f"land: stopped at merge: {reason}" in out
    assert out.count(reason) == 1
    assert out.count(BEHIND_REFUSAL) == 1
    assert "after 2 branch updates" in reason


def test_non_behind_merge_failure_does_not_update(tmp_path, monkeypatch, capsys):
    forge = FakeForge("CLEAN", merges=[(False, "required reviews are missing")])
    rc, _, _ = _leased_walk(tmp_path, monkeypatch, forge, FakeLease())
    assert rc == 1
    assert "update_branch" not in forge.calls
    assert "land: stopped at merge: required reviews are missing" in capsys.readouterr().out


def test_lease_is_renewed_once_per_pass(tmp_path, monkeypatch):
    forge = FakeForge("CLEAN", [(True, "green")] * 3, merges=[(False, BEHIND_REFUSAL)] * 2 + [(True, "merged")])
    lease = FakeLease()
    rc, _, _ = _leased_walk(tmp_path, monkeypatch, forge, lease)
    assert rc == 0
    assert forge.calls.count("update_branch") == 2
    assert lease.renewals == 2 + 2  # the walk's guard renews before wait_checks and merge, the loop before each update


def test_lease_lost_at_the_top_of_a_pass_stops_before_the_update(tmp_path, monkeypatch, capsys):
    forge = FakeForge("CLEAN", merges=[(False, BEHIND_REFUSAL)])
    rc, _, _ = _leased_walk(tmp_path, monkeypatch, forge, FakeLease(refuse_on=[3]))
    assert rc == 1
    assert "update_branch" not in forge.calls
    assert "other@host:99" in capsys.readouterr().out


def test_is_behind_refusal_classifies_by_message():
    assert land.is_behind_refusal(BEHIND_REFUSAL) is True
    assert land.is_behind_refusal("required reviews are missing") is False


class FakeLockStore:
    """In-memory lease store; one mutex makes acquire and release atomic across threads. Events go to `log`."""

    def __init__(self, log=None):
        self.leases = {}
        self.epoch = 0
        self.log = [] if log is None else log
        self.mutex = threading.Lock()

    def acquire(self, runs_dir, name, holder, ttl):
        with self.mutex:
            current = self.leases.get(name)
            if current is not None:
                return store_cli.LeaseRefused(current[1], current[0])
            self.epoch += 1
            self.leases[name] = (holder, self.epoch)
            self.log.append("lock")
            return store_cli.LeaseGranted(self.epoch, holder)

    def release(self, runs_dir, name, holder, epoch):
        with self.mutex:
            self.log.append("unlock")
            self.leases.pop(name, None)
            return store_cli.LeaseReleased()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class LogForge(FakeForge):
    """A forge that appends each call to a shared `log`, and may fail the update or hold the merge for a while."""

    def __init__(self, log, *args, update_error=None, merge_seconds=0.0, gauge=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = log
        self.update_error = update_error
        self.merge_seconds = merge_seconds
        self.gauge = gauge

    def merge_state(self, pr, *, repo=None):
        self.log.append("merge_state")
        return super().merge_state(pr, repo=repo)

    def update_branch(self, pr, *, repo=None):
        self.log.append("update_branch")
        if self.update_error is not None:
            raise ForgeError(self.update_error)

    def wait_checks(self, repo, timeout_s, ref="HEAD"):
        self.log.append("wait_checks")
        return super().wait_checks(repo, timeout_s, ref)

    def merge(self, repo, step):
        self.log.append("merge")
        if self.gauge is None:
            return super().merge(repo, step)
        self.gauge.enter()
        try:
            time.sleep(self.merge_seconds)
            return super().merge(repo, step)
        finally:
            self.gauge.leave()


class Gauge:
    """Counts how many threads are inside the merge step at once, and the most there ever were."""

    def __init__(self):
        self.inside = 0
        self.peak = 0
        self.mutex = threading.Lock()

    def enter(self):
        with self.mutex:
            self.inside += 1
            self.peak = max(self.peak, self.inside)

    def leave(self):
        with self.mutex:
            self.inside -= 1


def _lock(store, repo, holder="me", timeout=5, clock=None):
    clock = clock or FakeClock()
    return lambda: land_merge_lock.hold("runs", str(repo), holder, 30, timeout, poll=0.001, sleep=clock.sleep,
                                        clock=clock.clock, acquire=store.acquire, release=store.release)


def _merge(tmp_path, monkeypatch, forge, lock, attempts=None):
    monkeypatch.setattr(cli, "_fetch_branch", lambda repo, branch: None)
    return cli._merge_updating(tmp_path, STEPS, 1, "", forge, None, attempts=attempts, lock=lock)


def test_two_lands_of_one_repo_are_never_in_the_merge_step_together(tmp_path):
    store, gauge = FakeLockStore(), Gauge()

    def land_one(results):
        forge = LogForge([], "CLEAN", merge_seconds=0.02, gauge=gauge)
        lock = lambda: land_merge_lock.hold("runs", str(tmp_path), "me", 30, 10, poll=0.001,  # noqa: E731
                                            acquire=store.acquire, release=store.release)
        results.append(cli._merge_updating(tmp_path, STEPS, 1, "", forge, None, lock=lock))

    results: list = []
    threads = [threading.Thread(target=land_one, args=(results,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert gauge.peak == 1
    assert results == [(True, "merged", None)] * 2


def test_a_different_repo_is_not_blocked(tmp_path):
    log = []
    store = FakeLockStore(log)
    store.acquire("runs", land_merge_lock.merge_lock_name(str(tmp_path / "one")), "other", 30)
    log.clear()
    forge = LogForge(log, "CLEAN")
    got = cli._merge_updating(tmp_path, STEPS, 1, "", forge, None, lock=_lock(store, tmp_path / "two", timeout=0))
    assert got == (True, "merged", None)
    assert log == ["lock", "merge_state", "merge", "unlock"]


def test_behind_state_releases_the_lock_before_the_update_and_retakes_it_after(tmp_path, monkeypatch):
    log = []
    forge = LogForge(log, "BEHIND", [(True, "green")])
    store = FakeLockStore(log)
    got = _merge(tmp_path, monkeypatch, forge, _lock(store, tmp_path))
    assert got == (True, "merged", None)
    assert log == ["lock", "merge_state", "unlock", "update_branch", "wait_checks", "lock", "merge", "unlock"]


def test_merge_refused_as_behind_releases_the_lock_before_the_update(tmp_path, monkeypatch):
    log = []
    forge = LogForge(log, "CLEAN", [(True, "green")], merges=[(False, BEHIND_REFUSAL), (True, "merged")])
    got = _merge(tmp_path, monkeypatch, forge, _lock(FakeLockStore(log), tmp_path))
    assert got == (True, "merged", None)
    assert log == ["lock", "merge_state", "merge", "unlock", "update_branch", "wait_checks", "lock", "merge", "unlock"]


def test_behind_past_the_bound_still_stops_with_the_behind_text_under_the_lock(tmp_path, monkeypatch):
    forge = LogForge([], "CLEAN", [(True, "green")] * 2, merges=[(False, BEHIND_REFUSAL)] * 3)
    store = FakeLockStore()
    got = _merge(tmp_path, monkeypatch, forge, _lock(store, tmp_path), attempts=2)
    assert got == (False, "", land.behind_stop(2, BEHIND_REFUSAL))
    assert store.leases == {}


def test_a_conflict_on_update_stops_with_the_sibling_reason(tmp_path, monkeypatch, capsys):
    detail = "Cannot update PR branch due to conflicts"
    forge = LogForge([], "BEHIND", update_error=detail)
    store = FakeLockStore()
    monkeypatch.setattr(cli, "_fetch_branch", lambda repo, branch: None)
    rc, _, _ = cli._land_walk(tmp_path, STEPS, STEPS, None, None, "full", False, forge, [], lock=_lock(store, tmp_path))
    reason = land.conflict_stop(7, detail)
    assert rc == 1
    assert f"land: stopped at merge: {reason}" in capsys.readouterr().out
    assert "base moved under a merged sibling" in reason and "#7" in reason and detail in reason
    assert store.leases == {}


def test_another_update_failure_keeps_the_cannot_update_text(tmp_path, monkeypatch):
    forge = LogForge([], "BEHIND", update_error="gh: HTTP 502")
    got = _merge(tmp_path, monkeypatch, forge, _lock(FakeLockStore(), tmp_path))
    assert got == (False, "", "merge: cannot update pull request #7: gh: HTTP 502")


def test_a_lock_timeout_stops_naming_the_holder(tmp_path, monkeypatch):
    store, log = FakeLockStore(), []
    store.acquire("runs", land_merge_lock.merge_lock_name(str(tmp_path)), "other@host:9", 30)
    forge = LogForge(log, "CLEAN")
    ok, detail, halt = _merge(tmp_path, monkeypatch, forge, _lock(store, tmp_path, timeout=2))
    assert (ok, detail) == (False, "")
    assert halt == land.lock_stop("other@host:9", "timeout")
    assert "other@host:9" in halt
    assert log == []  # no state read and no merge without the lock


def test_a_lock_timeout_after_an_update_stops_too(tmp_path, monkeypatch):
    store = FakeLockStore()
    forge = LogForge([], "BEHIND", [(True, "green")])
    clock = FakeClock()
    lock = _lock(store, tmp_path, timeout=1, clock=clock)

    def update_then_sibling_takes_the_lock(pr, *, repo=None):
        store.acquire("runs", land_merge_lock.merge_lock_name(str(tmp_path)), "sibling", 30)

    forge.update_branch = update_then_sibling_takes_the_lock
    ok, _, halt = _merge(tmp_path, monkeypatch, forge, lock)
    assert not ok
    assert "sibling" in halt


def test_without_a_lock_the_merge_runs_as_before(tmp_path, monkeypatch):
    forge = FakeForge("CLEAN")
    assert _merge(tmp_path, monkeypatch, forge, None) == (True, "merged", None)
    assert forge.calls == ["merge_state", "merge"]


def test_conflict_and_lock_wording_is_pure():
    assert land.is_conflict_error("Cannot update PR branch due to conflicts") is True
    assert land.is_conflict_error("merge conflict between base and head") is True
    assert land.is_conflict_error("gh: HTTP 502") is False
    assert land.conflict_stop(7, "merge conflict") == (
        "the base moved under a merged sibling; pull request #7 now conflicts with it: merge conflict")
    assert land.lock_stop("a@h:1", "timeout") == "the merge lock for this repository is held by a@h:1; retrying on the next tick"
    assert land.lock_stop(None, "store outage: down") == "cannot take the merge lock (store outage: down); retrying on the next tick"
