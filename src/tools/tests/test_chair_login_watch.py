import json

from agent_tools.chair_login_watch import (
    BACK_WINDOW_S,
    due_for_login_check,
    due_for_probe,
    host_back_needs_chair,
    host_unreachable_needs_chair,
    is_unreachable_reply,
    login_blocked,
    login_needs_chair,
    login_url_from_output,
    merge_login_versions,
)

NOW = "2026-09-27T12:00:00Z"


def _host(name: str, state: str = "active", versions_json: dict | None = None) -> dict:
    return {"name": name, "state": state, "versions_json": versions_json or {}}


def test_a_host_checked_thirty_one_minutes_ago_is_due():
    host = _host("h", versions_json={"login_checked_at": "2026-09-27T11:29:00Z"})
    assert due_for_login_check([host], NOW) == ["h"]


def test_a_host_checked_ten_minutes_ago_is_not_due():
    host = _host("h", versions_json={"login_checked_at": "2026-09-27T11:50:00Z"})
    assert due_for_login_check([host], NOW) == []


def test_a_draining_host_past_threshold_is_not_due():
    host = _host("h", state="draining", versions_json={"login_checked_at": "2026-09-27T11:29:00Z"})
    assert due_for_login_check([host], NOW) == []


def test_a_host_that_has_never_been_checked_is_due():
    host = _host("h", versions_json={})
    assert due_for_login_check([host], NOW) == ["h"]


def test_a_host_with_login_ok_false_is_blocked_with_one_needs_chair_action():
    host = _host("h", versions_json={"login_ok": False})
    assert login_blocked([host]) == {"h"}
    assert login_needs_chair([host]) == [{"kind": "needs_chair", "host": "h", "cause": "login_lapsed"}]


def test_a_host_with_login_ok_true_is_in_neither():
    host = _host("h", versions_json={"login_ok": True})
    assert login_blocked([host]) == set()
    assert login_needs_chair([host]) == []


def test_a_never_checked_host_with_login_ok_absent_is_not_blocked():
    host = _host("h", versions_json={})
    assert login_blocked([host]) == set()
    assert login_needs_chair([host]) == []


def test_merge_login_versions_adds_keys_without_mutating_input():
    versions = {"cox": "0.20.0"}
    merged = merge_login_versions(versions, True, "t")
    assert merged == {"cox": "0.20.0", "login_ok": True, "login_checked_at": "t", "check": "claude_auth"}
    assert versions == {"cox": "0.20.0"}


def test_merge_login_versions_carries_check_and_reason_without_mutating_input():
    versions = {"cox": "0.20.0"}
    merged = merge_login_versions(versions, False, "t", check="env_vars", reason="missing MY_API_KEY")
    assert merged == {
        "cox": "0.20.0",
        "login_ok": False,
        "login_checked_at": "t",
        "check": "env_vars",
        "reason": "missing MY_API_KEY",
    }
    assert versions == {"cox": "0.20.0"}


def test_versions_json_as_a_sqlite_json_string_is_decoded():
    stale = {"name": "s", "state": "active", "versions_json": json.dumps({"login_ok": False, "login_checked_at": "2026-09-27T11:29:00Z"})}
    fresh = {"name": "f", "state": "active", "versions_json": json.dumps({"login_ok": True, "login_checked_at": "2026-09-27T11:50:00Z"})}
    assert due_for_login_check([stale, fresh], NOW) == ["s"]
    assert login_blocked([stale, fresh]) == {"s"}


def test_login_url_from_output_returns_the_first_https_url():
    output = "To authenticate, visit: https://login.tailscale.com/a/abc123. Then retry.\nhttps://other.example/x"
    assert login_url_from_output(output) == "https://login.tailscale.com/a/abc123"


def test_login_url_from_output_is_none_without_a_url():
    assert login_url_from_output("ssh: connect to host shed port 22: Connection refused") is None


def test_merge_login_versions_keeps_a_login_url_without_mutating_input():
    versions = {"cox": "0.20.0"}
    merged = merge_login_versions(versions, False, "t", login_url="https://login.tailscale.com/a/abc123")
    assert merged == {
        "cox": "0.20.0",
        "login_ok": False,
        "login_checked_at": "t",
        "check": "claude_auth",
        "login_url": "https://login.tailscale.com/a/abc123",
    }
    assert versions == {"cox": "0.20.0"}


def test_merge_login_versions_omits_a_none_login_url_and_clears_a_stored_one():
    merged = merge_login_versions({"login_url": "https://old.example/a"}, True, "t", login_url=None)
    assert merged == {"login_ok": True, "login_checked_at": "t", "check": "claude_auth"}


def test_a_blocked_host_holding_a_login_url_has_one_needs_chair_action_carrying_url():
    host = _host("h", versions_json={"login_ok": False, "login_url": "https://login.tailscale.com/a/abc123"})
    assert login_needs_chair([host]) == [
        {"kind": "needs_chair", "host": "h", "cause": "login_lapsed", "url": "https://login.tailscale.com/a/abc123"}
    ]


def test_a_timed_out_exit_124_reply_is_unreachable():
    assert is_unreachable_reply(124, "exit 124: ssh: timed out after 30s") is True


def test_a_connection_refused_255_reply_is_unreachable():
    assert is_unreachable_reply(255, "ssh: connect to host h port 22: Connection refused") is True


def test_a_permission_denied_255_reply_is_not_unreachable():
    assert is_unreachable_reply(255, "h: Permission denied (publickey).") is False


def test_a_timed_out_reply_with_exit_zero_is_not_unreachable():
    assert is_unreachable_reply(0, "timed out") is False


def test_a_never_probed_unreachable_host_is_due_for_probe():
    assert due_for_probe([_host("h", state="unreachable")], NOW) == ["h"]


def test_an_unreachable_host_probed_ten_minutes_ago_is_not_due_for_probe():
    host = _host("h", state="unreachable", versions_json={"probe_checked_at": "2026-09-27T11:50:00Z"})
    assert due_for_probe([host], NOW) == []


def test_an_unreachable_host_probed_thirty_one_minutes_ago_is_due_for_probe():
    host = _host("h", state="unreachable", versions_json={"probe_checked_at": "2026-09-27T11:29:00Z"})
    assert due_for_probe([host], NOW) == ["h"]


def test_an_active_host_is_never_due_for_probe():
    assert due_for_probe([_host("h")], NOW) == []


def test_an_unreachable_host_is_absent_from_the_login_rules():
    host = _host("h", state="unreachable", versions_json={"login_ok": False})
    assert due_for_login_check([host], NOW) == []
    assert login_blocked([host]) == set()
    assert login_needs_chair([host]) == []


def test_host_unreachable_needs_chair_is_sorted_and_omits_an_absent_since():
    hosts = [
        _host("b", state="unreachable"),
        _host("a", state="unreachable", versions_json={"unreachable_since": "2026-09-27T10:00:00Z"}),
        _host("c"),
    ]
    assert host_unreachable_needs_chair(hosts) == [
        {"kind": "needs_chair", "host": "a", "cause": "host_unreachable", "since": "2026-09-27T10:00:00Z"},
        {"kind": "needs_chair", "host": "b", "cause": "host_unreachable"},
    ]


def test_host_back_needs_chair_names_active_hosts_back_within_the_window():
    assert BACK_WINDOW_S == 3600
    hosts = [
        _host("b", versions_json={"host_back_at": "2026-09-27T11:30:00Z"}),
        _host("a", versions_json={"host_back_at": "2026-09-27T11:59:00Z"}),
        _host("old", versions_json={"host_back_at": "2026-09-27T10:59:00Z"}),
        _host("bad", versions_json={"host_back_at": "not a time"}),
        _host("none"),
        _host("down", state="unreachable", versions_json={"host_back_at": "2026-09-27T11:59:00Z"}),
    ]
    assert host_back_needs_chair(hosts, NOW) == [
        {"kind": "needs_chair", "host": "a", "cause": "host_back", "back_at": "2026-09-27T11:59:00Z"},
        {"kind": "needs_chair", "host": "b", "cause": "host_back", "back_at": "2026-09-27T11:30:00Z"},
    ]
