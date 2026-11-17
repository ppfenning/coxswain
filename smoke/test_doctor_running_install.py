"""`cox setup doctor` reports where the interpreter running `cox` imports coxswain from (H1 item 3).

The smoke install is a plain (not editable) install of this tree, so the row is ok and says so. The row is advisory: it
never sets the doctor's exit code by itself (its judgement has unit tests in test_doctor_running_install.py).
"""

import json


def test_the_doctor_reports_the_running_install(sandbox):
    done = sandbox.cox("setup", "doctor", "--json", timeout=300)
    rows = {row["check"]: row for row in json.loads(done.stdout)["rows"]}
    assert rows["running install"] == {
        "check": "running install",
        "ok": True,
        "detail": f"{sandbox.install.python}: not an editable install",
    }
