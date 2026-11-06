from agent_tools.release_deploy import HostResult, deploy_events, deploy_host, result_line

V = "2026.11.6"
GOOD_COX = (0, f"cox {V}")
GOOD_TOW = (0, f"towpath {V}")
OLD_TOW = (0, "towpath 2026.11.5")
OK = (0, "ok")

UPGRADE = ["cox", "upgrade", "--to", V]
COX_VERSION = ["cox", "--version"]
TOW_VERSION = ["towpath", "--version"]
DOCTOR = ["cox", "setup", "doctor"]
CHECKOUT_MAIN = ["git", "checkout", "main"]
PULL_MAIN = ["git", "pull", "--ff-only", "origin", "main"]


def stub(script):
    """A run that answers from `script` in order and records every argv; running past the script raises."""
    calls = []
    pending = list(script)

    def run(argv):
        calls.append(list(argv))
        return pending.pop(0)

    return run, calls, pending


def test_clean_deploy_runs_the_whole_sequence_in_order():
    run, calls, pending = stub([OK, GOOD_COX, GOOD_TOW, OK])
    result = deploy_host("h1", V, run, False)
    assert result == HostResult("h1", V, True, "none", "")
    assert calls == [UPGRADE, COX_VERSION, TOW_VERSION, DOCTOR]
    assert pending == []


def test_towpath_mismatch_fixed_by_retry():
    run, calls, pending = stub([OK, GOOD_COX, OLD_TOW, OK, GOOD_COX, GOOD_TOW, OK])
    result = deploy_host("h1", V, run, False)
    assert result.ok and result.failing_step == "none"
    assert calls == [UPGRADE, COX_VERSION, TOW_VERSION, UPGRADE, COX_VERSION, TOW_VERSION, DOCTOR]
    assert pending == []


def test_persistent_mismatch_fails_at_version_towpath():
    run, calls, pending = stub([OK, GOOD_COX, OLD_TOW, OK, GOOD_COX, OLD_TOW])
    result = deploy_host("h1", V, run, False)
    assert not result.ok
    assert result.failing_step == "version_towpath"
    assert calls == [UPGRADE, COX_VERSION, TOW_VERSION, UPGRADE, COX_VERSION, TOW_VERSION]
    assert pending == []


def test_cox_mismatch_fails_at_version_cox_without_checking_towpath():
    run, calls, _ = stub([OK, (0, "cox 2026.11.5"), OK, (0, "cox 2026.11.5")])
    result = deploy_host("h1", V, run, False)
    assert result.failing_step == "version_cox"
    assert calls == [UPGRADE, COX_VERSION, UPGRADE, COX_VERSION]


def test_version_must_match_whole_token():
    for reported in ("towpath 2026.11.60", "towpath 2026.11.6+14df4b9", "towpath 2026.11.16"):
        run, _, _ = stub([OK, GOOD_COX, (0, reported), OK, GOOD_COX, (0, reported)])
        assert deploy_host("h1", V, run, False).failing_step == "version_towpath"


def test_leading_v_on_either_side_still_matches():
    run, _, _ = stub([OK, (0, f"cox v{V}"), GOOD_TOW, OK])
    assert deploy_host("h1", V, run, False).ok
    run, _, _ = stub([OK, GOOD_COX, GOOD_TOW, OK])
    assert deploy_host("h1", f"v{V}", run, False).ok


def test_upgrade_failure_stops_the_sequence_without_retry():
    run, calls, _ = stub([(1, "boom")])
    result = deploy_host("h1", V, run, False)
    assert result.failing_step == "upgrade"
    assert calls == [UPGRADE]


def test_failed_retry_upgrade_fails_at_upgrade():
    run, calls, _ = stub([OK, GOOD_COX, OLD_TOW, (1, "boom")])
    result = deploy_host("h1", V, run, False)
    assert result.failing_step == "upgrade"
    assert calls == [UPGRADE, COX_VERSION, TOW_VERSION, UPGRADE]


def test_development_host_is_refreshed_on_main_never_upgraded():
    run, calls, pending = stub([OK, OK, OK])
    result = deploy_host("dev", V, run, True)
    assert result == HostResult("dev", V, True, "none", "")
    assert calls == [CHECKOUT_MAIN, PULL_MAIN, DOCTOR]
    assert pending == []


def test_development_host_needs_no_version_match_and_gets_no_retry():
    run, calls, _ = stub([OK, OK, (1, "unhealthy")])
    result = deploy_host("dev", V, run, True)
    assert result.failing_step == "doctor"
    assert calls == [CHECKOUT_MAIN, PULL_MAIN, DOCTOR]


def test_development_refresh_failure_stops_before_doctor():
    run, calls, _ = stub([OK, (1, "not a fast-forward")])
    result = deploy_host("dev", V, run, True)
    assert result.failing_step == "refresh"
    assert calls == [CHECKOUT_MAIN, PULL_MAIN]


def test_doctor_failure_gives_deploy_failed_event():
    run, calls, _ = stub([OK, GOOD_COX, GOOD_TOW, (2, "unhealthy")])
    result = deploy_host("h1", V, run, False)
    assert result.failing_step == "doctor"
    assert calls[-1] == DOCTOR
    events = deploy_events(V, [result])
    assert [e.kind for e in events] == ["deploy_failed"]
    assert events[0].detail == {"host": "h1", "step": "doctor"}
    assert events[0].run == f"release-{V}"


def test_released_only_when_every_host_ok():
    good = HostResult("a", V, True, "none", "")
    bad = HostResult("b", V, False, "upgrade", "boom")
    mixed = deploy_events(V, [good, bad])
    assert [e.kind for e in mixed] == ["deployed", "deploy_failed"]
    clean = deploy_events(V, [good, HostResult("c", V, True, "none", "")])
    assert [e.kind for e in clean] == ["deployed", "deployed", "released"]
    assert sum(e.kind == "released" for e in clean) == 1


def test_no_results_emit_no_released():
    assert deploy_events(V, []) == []


def test_result_line():
    assert result_line(HostResult("h1", V, True, "none", "")) == f"h1 {V} ok"
    assert result_line(HostResult("h1", V, False, "doctor", "x")) == f"h1 {V} failed at doctor"
