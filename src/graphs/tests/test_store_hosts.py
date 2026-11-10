import pytest

from harness import store_hosts
from harness.store_hosts import _merge_login, host_beat, host_list, host_set_state, host_upsert, update_host_login

NOW1 = "2026-10-12T00:00:00Z"
NOW2 = "2026-10-12T01:00:00Z"


@pytest.fixture
def conn(store_conn):
    host_upsert(store_conn, "alpha", "ssh://alpha", 2, "active", "operator", NOW1)
    host_beat(store_conn, "alpha", {"cox": "1.2.3"}, NOW1)
    return store_conn


def test_merge_login_keeps_other_keys_overwrites_shared_ones_and_mutates_nothing():
    versions = {"cox": "1.2.3", "login_ok": False}
    login = {"login_ok": True, "check": "ssh true"}
    assert _merge_login(versions, login) == {"cox": "1.2.3", "login_ok": True, "check": "ssh true"}
    assert versions == {"cox": "1.2.3", "login_ok": False}
    assert login == {"login_ok": True, "check": "ssh true"}


def test_update_host_login_merges_into_versions_and_leaves_every_other_column(conn):
    before = host_list(conn)[0]
    login = {"login_ok": True, "login_checked_at": NOW2, "check": "ssh true"}
    assert update_host_login(conn, "alpha", login) is True
    after = host_list(conn)[0]
    assert after["versions_json"] == {"cox": "1.2.3", **login}
    assert {k: v for k, v in after.items() if k != "versions_json"} == {
        k: v for k, v in before.items() if k != "versions_json"
    }
    assert after["beat_at"] == NOW1


def test_a_second_login_update_overwrites_the_earlier_values(conn):
    update_host_login(conn, "alpha", {"login_ok": True, "login_checked_at": NOW1, "check": "first"})
    assert update_host_login(conn, "alpha", {"login_ok": False, "login_checked_at": NOW2, "check": "second"}) is True
    assert host_list(conn)[0]["versions_json"] == {
        "cox": "1.2.3",
        "login_ok": False,
        "login_checked_at": NOW2,
        "check": "second",
    }


def test_beat_on_an_unreachable_host_sets_it_active_by_beat_with_host_back_at(conn):
    host_set_state(conn, "alpha", "unreachable", "chair", NOW1)
    row = host_beat(conn, "alpha", {"cox": "1.2.4"}, NOW2)
    assert row["state"] == "active"
    assert row["updated_by"] == "beat"
    assert row["updated_at"] == NOW2
    assert row["versions_json"] == {"cox": "1.2.4", "host_back_at": NOW2}


@pytest.mark.parametrize("state", ["draining", "offline", "deploy_only"])
def test_beat_on_a_host_in_another_state_leaves_it_unchanged(conn, state):
    host_set_state(conn, "alpha", state, "operator", NOW1)
    row = host_beat(conn, "alpha", {"cox": "1.2.4"}, NOW2)
    assert (row["state"], row["updated_by"], row["updated_at"]) == (state, "operator", NOW1)
    assert "host_back_at" not in row["versions_json"]


def test_beat_does_not_override_a_state_set_after_its_read(conn, monkeypatch):
    host_set_state(conn, "alpha", "unreachable", "chair", NOW1)
    stale = store_hosts._find(conn, "alpha")
    host_set_state(conn, "alpha", "draining", "operator", NOW1)
    reads = [stale]
    real = store_hosts._find
    monkeypatch.setattr(store_hosts, "_find", lambda c, n: reads.pop() if reads else real(c, n))
    row = host_beat(conn, "alpha", {"cox": "1.2.4"}, NOW2)
    assert (row["state"], row["updated_by"]) == ("draining", "operator")
    assert "host_back_at" not in row["versions_json"]
    assert row["beat_at"] == NOW2


def test_a_second_return_overwrites_the_earlier_host_back_at(conn):
    host_set_state(conn, "alpha", "unreachable", "chair", NOW1)
    host_beat(conn, "alpha", {"cox": "1.2.4"}, NOW1)
    host_set_state(conn, "alpha", "unreachable", "chair", NOW1)
    row = host_beat(conn, "alpha", {"cox": "1.2.4"}, NOW2)
    assert (row["state"], row["versions_json"]["host_back_at"]) == ("active", NOW2)


def test_host_back_at_survives_a_second_beat(conn):
    host_set_state(conn, "alpha", "unreachable", "chair", NOW1)
    host_beat(conn, "alpha", {"cox": "1.2.4"}, NOW1)
    row = host_beat(conn, "alpha", {"cox": "1.2.4"}, NOW2)
    assert row["state"] == "active"
    assert row["versions_json"]["host_back_at"] == NOW1


def test_set_state_accepts_unreachable(conn):
    assert host_set_state(conn, "alpha", "unreachable", "chair", NOW2)["state"] == "unreachable"


def test_an_unknown_host_returns_false_and_creates_no_row(store_conn):
    assert update_host_login(store_conn, "ghost", {"login_ok": True}) is False
    assert store_conn.query_all("SELECT COUNT(*) FROM hosts") == [(0,)]
