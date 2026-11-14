from agent_tools import cli, doctor

SET_DETAIL = "retired; chair.steward is set and ignored (the lander lands: cox lander run)"
UNSET_DETAIL = "retired; the lander merges phases and lands initiatives (cox lander run)"


def test_steward_row_for_a_profile_that_still_sets_it_says_retired_and_ignored():
    assert doctor.steward_row(True) == {"check": "steward", "ok": True, "detail": SET_DETAIL}


def test_steward_row_for_a_profile_without_it_says_retired():
    assert doctor.steward_row(False) == {"check": "steward", "ok": True, "detail": UNSET_DETAIL}


def test_the_row_never_changes_the_exit_code():
    good = [{"check": "git", "ok": True, "detail": "x"}]
    assert doctor.exit_code([*good, doctor.steward_row(False)]) == 0
    assert doctor.exit_code([*good, doctor.steward_row(True)]) == 0


def test_helper_reports_on_for_a_profile_with_the_steward_set():
    assert cli._doctor_steward_rows({"chair": {"steward": True}}) == [doctor.steward_row(True)]


def test_helper_reports_off_for_an_absent_key():
    assert cli._doctor_steward_rows({}) == [doctor.steward_row(False)]
    assert cli._doctor_steward_rows({"chair": {}}) == [doctor.steward_row(False)]
    assert cli._doctor_steward_rows({"chair": "yes"}) == [doctor.steward_row(False)]


def test_helper_skips_the_row_with_no_profile():
    assert cli._doctor_steward_rows(None) == [{"check": "steward", "ok": False, "detail": "skipped: no profile"}]
