"""`running install`: the doctor's advisory row for an interpreter editable from outside the install checkout (H1)."""

import json

from agent_tools import doctor

CHECKOUT = "/home/user/.local/share/coxswain/installs/coxswain"


def _record(url: str, editable: bool | None) -> str:
    return json.dumps({"url": url, "dir_info": {} if editable is None else {"editable": editable}})


def test_an_editable_record_names_its_checkout():
    assert doctor.editable_source(_record(f"file://{CHECKOUT}", True)) == CHECKOUT


def test_a_record_that_is_not_editable_or_not_readable_names_none():
    assert doctor.editable_source(_record(f"file://{CHECKOUT}", False)) is None
    assert doctor.editable_source(_record("https://example.invalid/coxswain.whl", None)) is None
    assert doctor.editable_source("not json") is None
    assert doctor.editable_source(None) is None


def test_an_install_editable_from_the_install_checkout_is_ok():
    row = doctor.running_install_row(CHECKOUT, "/usr/bin/python3", f"{CHECKOUT}/")
    assert row == {"check": "running install", "ok": True, "detail": "/usr/bin/python3: editable from the install checkout"}


def test_an_install_that_is_not_editable_is_ok():
    assert doctor.running_install_row(CHECKOUT, "/usr/bin/python3", None)["ok"] is True


def test_an_install_editable_from_another_checkout_warns_and_never_fails_the_doctor():
    row = doctor.running_install_row(CHECKOUT, "/home/user/repos/other/.venv/bin/python", "/home/user/repos/other")
    assert (row["ok"], row["warn"]) == (True, True)
    assert "editable from /home/user/repos/other, outside the install checkout" in row["detail"]
    assert doctor.row_tier(row) == "advisory" and doctor.exit_code([row]) == 0


def test_the_extras_row_is_ok_when_nothing_is_missing():
    assert doctor.extras_row("/usr/bin/python3", []) == {
        "check": "extras", "ok": True, "detail": "/usr/bin/python3: postgres, parquet",
    }


def test_a_missing_extra_warns_by_name_with_the_fix_and_never_fails_the_doctor():
    row = doctor.extras_row("/usr/bin/python3", ["postgres"])
    assert (row["ok"], row["warn"]) == (True, True)
    assert "lacks the postgres extra(s)" in row["detail"] and "cox setup install" in row["detail"]
    assert doctor.row_tier(row) == "advisory"
