from agent_tools import cli, doctor

OFF_DETAIL = "off; turn it on with chair.steward set to true in the profile"


def test_steward_row_on():
    assert doctor.steward_row(True) == {"check": "steward", "ok": True, "detail": "on"}


def test_steward_row_off_says_how_to_turn_it_on():
    assert doctor.steward_row(False) == {"check": "steward", "ok": True, "detail": OFF_DETAIL}


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
