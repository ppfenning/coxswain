from agent_tools import land_repo_lease, store_cli


def test_lease_name_is_land_prefixed_with_the_repo():
    assert land_repo_lease.lease_name("/repo") == "land:/repo"


def test_refusal_message_is_the_exact_string():
    assert land_repo_lease.refusal_message("/repo", "pid 1 on host") == "land: refusing, pid 1 on host is landing in /repo"


def test_acquire_calls_store_cli_lease_acquire_with_the_repo_lease_name(monkeypatch, tmp_path):
    seen = []

    def fake_acquire(runs_dir, name, holder, ttl):
        seen.append((runs_dir, name, holder, ttl))
        return store_cli.LeaseGranted(1, "me")

    monkeypatch.setattr(store_cli, "lease_acquire", fake_acquire)
    result = land_repo_lease.acquire(tmp_path, "/repo", "me", 60)

    assert seen == [(tmp_path, "land:/repo", "me", 60)]
    assert result == store_cli.LeaseGranted(1, "me")


def test_acquire_returns_a_refusal_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(store_cli, "lease_acquire", lambda runs_dir, name, holder, ttl: store_cli.LeaseRefused(2, "other"))
    result = land_repo_lease.acquire(tmp_path, "/repo", "me", 60)
    assert result == store_cli.LeaseRefused(2, "other")


def test_renew_calls_store_cli_lease_renew_with_the_repo_lease_name_and_epoch(monkeypatch, tmp_path):
    seen = []

    def fake_renew(runs_dir, name, holder, epoch, ttl):
        seen.append((runs_dir, name, holder, epoch, ttl))
        return store_cli.LeaseGranted(4, "me")

    monkeypatch.setattr(store_cli, "lease_renew", fake_renew)
    result = land_repo_lease.renew(tmp_path, "/repo", "me", 4, 60)

    assert seen == [(tmp_path, "land:/repo", "me", 4, 60)]
    assert result == store_cli.LeaseGranted(4, "me")


def test_release_calls_store_cli_lease_release_with_the_repo_lease_name_and_epoch(monkeypatch, tmp_path):
    seen = []

    def fake_release(runs_dir, name, holder, epoch):
        seen.append((runs_dir, name, holder, epoch))
        return store_cli.LeaseReleased()

    monkeypatch.setattr(store_cli, "lease_release", fake_release)
    result = land_repo_lease.release(tmp_path, "/repo", "me", 4)

    assert seen == [(tmp_path, "land:/repo", "me", 4)]
    assert result == store_cli.LeaseReleased()


def test_holder_host_pid_reads_the_host_and_pid_after_the_last_at_and_colon():
    assert land_repo_lease.holder_host_pid("chair-x@host-a:4242") == ("host-a", 4242)
    assert land_repo_lease.holder_host_pid("chair@x@host-a:7") == ("host-a", 7)
    assert land_repo_lease.holder_host_pid("another land") is None
    assert land_repo_lease.holder_host_pid("chair-x@host-a:0") is None
    assert land_repo_lease.holder_host_pid(None) is None


def test_pid_gone_is_true_only_for_a_dead_pid_on_this_host():
    assert land_repo_lease.pid_gone("chair-x@host-a:4242", "host-a", lambda _pid: False)
    assert not land_repo_lease.pid_gone("chair-x@host-a:4242", "host-a", lambda _pid: True)
    assert not land_repo_lease.pid_gone("chair-x@host-b:4242", "host-a", lambda _pid: False)
    assert not land_repo_lease.pid_gone("another land", "host-a", lambda _pid: False)


def _acquire_script(monkeypatch, first):
    calls = []

    def fake_acquire(runs_dir, name, holder, ttl, steal=False):
        calls.append(steal)
        return store_cli.LeaseGranted(5, holder) if steal else first

    monkeypatch.setattr(store_cli, "lease_acquire", fake_acquire)
    return calls


def test_a_lease_held_by_a_dead_local_pid_is_taken_over(monkeypatch, tmp_path, capsys):
    calls = _acquire_script(monkeypatch, store_cli.LeaseRefused(4, "chair-x@host-a:4242"))
    got = land_repo_lease.acquire_or_take_over(tmp_path, "/repo", "me@host-a:9", 60, "host-a", lambda _pid: False)
    assert got == store_cli.LeaseGranted(5, "me@host-a:9")
    assert calls == [False, True]
    assert capsys.readouterr().out == "land: taking over the repo lease from chair-x@host-a:4242 (pid gone)\n"


def test_a_live_local_pid_or_a_holder_on_another_host_still_refuses(monkeypatch, tmp_path):
    for holder, alive in (("chair-x@host-a:4242", True), ("chair-x@host-b:4242", False)):
        refused = store_cli.LeaseRefused(4, holder)
        calls = _acquire_script(monkeypatch, refused)
        got = land_repo_lease.acquire_or_take_over(tmp_path, "/repo", "me@host-a:9", 60, "host-a", lambda _pid, a=alive: a)
        assert (got, calls) == (refused, [False])


def test_a_takeover_granted_past_the_refused_epoch_took_a_live_land_and_is_released(monkeypatch, tmp_path, capsys):
    steals = []
    released = []
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *a, steal=False: (
        store_cli.LeaseGranted(6, a[2]) if steal else store_cli.LeaseRefused(4, "chair-x@host-a:4242")))
    monkeypatch.setattr(store_cli, "lease_release", lambda *a: (released.append(a), store_cli.LeaseReleased())[1])
    got = land_repo_lease.acquire_or_take_over(tmp_path, "/repo", "me@host-a:9", 60, "host-a", lambda _pid: steals.append(1) or False)
    assert got == store_cli.LeaseRefused(4, None)
    assert released == [(tmp_path, "land:/repo", "me@host-a:9", 6)]
    assert "taking over" not in capsys.readouterr().out


def test_a_lost_steal_is_returned_without_a_takeover_line(monkeypatch, tmp_path, capsys):
    lost = store_cli.LeaseRefused(5, "other@host-a:1")
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *a, steal=False: lost if steal else store_cli.LeaseRefused(4, "chair-x@host-a:4242"))
    got = land_repo_lease.acquire_or_take_over(tmp_path, "/repo", "me@host-a:9", 60, "host-a", lambda _pid: False)
    assert got == lost
    assert capsys.readouterr().out == ""


def test_a_granted_acquire_is_returned_without_a_probe(monkeypatch, tmp_path):
    granted = store_cli.LeaseGranted(1, "me")
    calls = _acquire_script(monkeypatch, granted)
    assert land_repo_lease.acquire_or_take_over(tmp_path, "/repo", "me", 60, "host-a", lambda _pid: 1 / 0) == granted
    assert calls == [False]


def test_slot_names_keep_slot_zero_as_today_and_suffix_the_rest():
    assert land_repo_lease.slot_lease_name("/repo", 0) == land_repo_lease.lease_name("/repo") == "land:/repo"
    assert land_repo_lease.slot_lease_name("/repo", 1) == "land:/repo#1"
    assert land_repo_lease.slot_lease_name("/repo", 2) == "land:/repo#2"


def test_slot_order_reads_a_limit_below_one_as_one():
    assert [list(land_repo_lease.slot_order(n)) for n in (-1, 0, 1, 2, 3)] == [[0], [0], [0], [0, 1], [0, 1, 2]]


def _slot_store(monkeypatch, state):
    """Fake lease_acquire over `state`, a name to refusal-or-error map; names absent are granted. Returns the call log."""
    calls = []

    def fake_acquire(runs_dir, name, holder, ttl, steal=False):
        calls.append((name, steal))
        if steal:
            return store_cli.LeaseGranted(state[name].epoch + 1, holder)
        return state.get(name, store_cli.LeaseGranted(1, holder))

    monkeypatch.setattr(store_cli, "lease_acquire", fake_acquire)
    return calls


def test_limit_one_returns_slot_zero_with_what_acquire_or_take_over_returns(monkeypatch, tmp_path, capsys):
    for first in (store_cli.LeaseGranted(1, "me@host-a:9"), store_cli.LeaseRefused(4, "chair-x@host-a:4242"),
                  store_cli.LeaseError("down")):
        calls = _slot_store(monkeypatch, {"land:/repo": first})
        got = land_repo_lease.acquire_in_slots(tmp_path, "/repo", "me@host-a:9", 60, "host-a", 1, lambda _pid: True)
        assert got == (0, first)
        assert calls == [("land:/repo", False)]
    calls = _slot_store(monkeypatch, {"land:/repo": store_cli.LeaseRefused(4, "chair-x@host-a:4242")})
    got = land_repo_lease.acquire_in_slots(tmp_path, "/repo", "me@host-a:9", 60, "host-a", 0, lambda _pid: False)
    assert got == (0, store_cli.LeaseGranted(5, "me@host-a:9"))
    assert calls == [("land:/repo", False), ("land:/repo", True)]
    assert "taking over the repo lease from chair-x@host-a:4242" in capsys.readouterr().out


def test_limit_one_goes_through_the_module_acquire(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(land_repo_lease, "acquire", lambda *a: (seen.append(a), store_cli.LeaseGranted(3, "me"))[1])
    assert land_repo_lease.acquire_in_slots(tmp_path, "/repo", "me", 60, "host-a", 1) == (0, store_cli.LeaseGranted(3, "me"))
    assert seen == [(tmp_path, "/repo", "me", 60)]


def test_a_second_holder_gets_slot_one_while_slot_zero_is_held_live(monkeypatch, tmp_path):
    calls = _slot_store(monkeypatch, {"land:/repo": store_cli.LeaseRefused(4, "chair-x@host-a:1")})
    got = land_repo_lease.acquire_in_slots(tmp_path, "/repo", "me@host-a:9", 60, "host-a", 2, lambda _pid: True)
    assert got == (1, store_cli.LeaseGranted(1, "me@host-a:9"))
    assert calls == [("land:/repo", False), ("land:/repo#1", False)]


def test_a_third_holder_is_refused_with_slot_zeros_refusal(monkeypatch, tmp_path):
    state = {"land:/repo": store_cli.LeaseRefused(4, "chair-x@host-a:1"), "land:/repo#1": store_cli.LeaseRefused(7, "chair-y@host-a:2")}
    calls = _slot_store(monkeypatch, state)
    got = land_repo_lease.acquire_in_slots(tmp_path, "/repo", "me@host-a:9", 60, "host-a", 2, lambda _pid: True)
    assert got == (0, store_cli.LeaseRefused(4, "chair-x@host-a:1"))
    assert calls == [("land:/repo", False), ("land:/repo#1", False)]


def test_a_dead_pid_in_slot_one_is_taken_over(monkeypatch, tmp_path, capsys):
    state = {"land:/repo": store_cli.LeaseRefused(4, "chair-x@host-a:1"), "land:/repo#1": store_cli.LeaseRefused(7, "chair-y@host-a:2")}
    calls = _slot_store(monkeypatch, state)
    got = land_repo_lease.acquire_in_slots(tmp_path, "/repo", "me@host-a:9", 60, "host-a", 2, lambda pid: pid == 1)
    assert got == (1, store_cli.LeaseGranted(8, "me@host-a:9"))
    assert calls == [("land:/repo", False), ("land:/repo#1", False), ("land:/repo#1", True)]
    assert "chair-y@host-a:2 (pid gone)" in capsys.readouterr().out


def test_a_slot_one_takeover_past_the_refused_epoch_is_released_and_refused(monkeypatch, tmp_path):
    released = []
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *a, steal=False: (
        store_cli.LeaseGranted(9, a[2]) if steal else store_cli.LeaseRefused(7, "chair-y@host-a:2")))
    monkeypatch.setattr(store_cli, "lease_release", lambda *a: (released.append(a), store_cli.LeaseReleased())[1])
    got = land_repo_lease._take_over_in_slot(tmp_path, "/repo", 1, "me@host-a:9", 60, "host-a", lambda _pid: False)
    assert got == store_cli.LeaseRefused(7, None)
    assert released == [(tmp_path, "land:/repo#1", "me@host-a:9", 9)]


def test_a_store_outage_returns_at_once_without_trying_the_next_slot(monkeypatch, tmp_path):
    calls = _slot_store(monkeypatch, {"land:/repo": store_cli.LeaseError("down")})
    got = land_repo_lease.acquire_in_slots(tmp_path, "/repo", "me", 60, "host-a", 3)
    assert got == (0, store_cli.LeaseError("down"))
    assert calls == [("land:/repo", False)]
    calls = _slot_store(monkeypatch, {"land:/repo": store_cli.LeaseRefused(4, "chair-x@host-a:1"), "land:/repo#1": store_cli.LeaseError("down")})
    got = land_repo_lease.acquire_in_slots(tmp_path, "/repo", "me", 60, "host-a", 3, lambda _pid: True)
    assert got == (1, store_cli.LeaseError("down"))
    assert calls == [("land:/repo", False), ("land:/repo#1", False)]


def test_renew_and_release_address_the_named_slot(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(store_cli, "lease_renew", lambda *a: (seen.append(("renew",) + a), store_cli.LeaseGranted(a[3], a[2]))[1])
    monkeypatch.setattr(store_cli, "lease_release", lambda *a: (seen.append(("release",) + a), store_cli.LeaseReleased())[1])
    for slot in (0, 1):
        assert land_repo_lease.renew_slot(tmp_path, "/repo", slot, "me", 4, 60) == store_cli.LeaseGranted(4, "me")
        assert land_repo_lease.release_slot(tmp_path, "/repo", slot, "me", 4) == store_cli.LeaseReleased()
    assert seen == [
        ("renew", tmp_path, "land:/repo", "me", 4, 60), ("release", tmp_path, "land:/repo", "me", 4),
        ("renew", tmp_path, "land:/repo#1", "me", 4, 60), ("release", tmp_path, "land:/repo#1", "me", 4),
    ]


def test_waiting_message_names_the_holder_or_another_land():
    assert land_repo_lease.waiting_message("/repo", "pid 1 on host") == "land: waiting, pid 1 on host is landing in /repo"
    assert land_repo_lease.waiting_message("/repo", None) == "land: waiting, another land is landing in /repo"


def _waiting(monkeypatch, tmp_path, results, timeout_s, poll_s=2):
    """Run acquire_in_slots_waiting over a scripted acquire and a fake clock; returns (got, attempts, sleeps)."""
    queue = list(results)
    attempts = []
    sleeps = []
    clock = [100.0]

    def fake_acquire_in_slots(*args):
        attempts.append(args)
        return 0, (queue.pop(0) if len(queue) > 1 else queue[0])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(land_repo_lease, "acquire_in_slots", fake_acquire_in_slots)
    got = land_repo_lease.acquire_in_slots_waiting(
        tmp_path, "/repo", "me", 60, "host-a", 1, timeout_s, poll_s, sleep=fake_sleep, now=lambda: clock[0])
    return got, attempts, sleeps


def test_a_land_refused_twice_then_granted_returns_the_grant_after_two_sleeps(monkeypatch, tmp_path):
    refused = store_cli.LeaseRefused(4, "other@host-b:1")
    granted = store_cli.LeaseGranted(5, "me")
    got, attempts, sleeps = _waiting(monkeypatch, tmp_path, [refused, refused, granted], timeout_s=60)
    assert got == (0, granted)
    assert len(attempts) == 3
    assert sleeps == [2, 2]


def test_a_land_refused_until_the_timeout_returns_the_refusal_and_never_sleeps_past_it(monkeypatch, tmp_path):
    refused = store_cli.LeaseRefused(4, "other@host-b:1")
    got, attempts, sleeps = _waiting(monkeypatch, tmp_path, [refused], timeout_s=5, poll_s=2)
    assert got == (0, refused)
    assert sleeps == [2, 2, 1]
    assert len(attempts) == 4


def test_a_store_outage_or_lease_error_returns_on_the_first_attempt_without_sleeping(monkeypatch, tmp_path):
    for result in (store_cli.NotAvailable("no store"), store_cli.LeaseError("down")):
        got, attempts, sleeps = _waiting(monkeypatch, tmp_path, [result], timeout_s=60)
        assert (got, len(attempts), sleeps) == ((0, result), 1, [])


def test_a_timeout_of_zero_makes_one_attempt(monkeypatch, tmp_path):
    refused = store_cli.LeaseRefused(4, "other@host-b:1")
    got, attempts, sleeps = _waiting(monkeypatch, tmp_path, [refused], timeout_s=0)
    assert (got, len(attempts), sleeps) == ((0, refused), 1, [])


def test_waiting_passes_its_arguments_through_to_acquire_in_slots(monkeypatch, tmp_path):
    def alive(_pid):
        return True

    seen = []

    def fake_acquire_in_slots(*args):
        seen.append(args)
        return 0, store_cli.LeaseGranted(1, "me")

    monkeypatch.setattr(land_repo_lease, "acquire_in_slots", fake_acquire_in_slots)
    land_repo_lease.acquire_in_slots_waiting(tmp_path, "/repo", "me", 60, "host-a", 3, 0, 1, alive)
    assert seen == [(tmp_path, "/repo", "me", 60, "host-a", 3, alive)]


def test_the_real_acquire_in_slots_is_retried_and_still_takes_over_a_dead_pid_each_attempt(monkeypatch, tmp_path, capsys):
    replies = [store_cli.LeaseRefused(4, "chair-x@host-b:1"), store_cli.LeaseRefused(4, "chair-x@host-a:4242")]
    calls = []

    def fake_acquire(runs_dir, name, holder, ttl, steal=False):
        calls.append(steal)
        return store_cli.LeaseGranted(5, holder) if steal else replies.pop(0)

    monkeypatch.setattr(store_cli, "lease_acquire", fake_acquire)
    sleeps = []
    clock = iter([0, 0, 3])
    got = land_repo_lease.acquire_in_slots_waiting(
        tmp_path, "/repo", "me@host-a:9", 60, "host-a", 1, 10, 3, lambda _pid: False, sleeps.append, lambda: next(clock))
    assert got == (0, store_cli.LeaseGranted(5, "me@host-a:9"))
    assert calls == [False, False, True]
    assert sleeps == [3]
    assert "taking over" in capsys.readouterr().out
