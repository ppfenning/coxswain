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
