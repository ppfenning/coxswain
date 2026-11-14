from graphs.delivery.clean_gate import clean_checks, clean_finding, clean_gate_finding

CLEAN = "python .github/check_clean.py . --require-names"
PYTEST = "cd src/graphs && pytest -q -n auto"
HOME = "/" + "home" + "/" + "someone"


def test_clean_checks_picks_only_the_clean_entry() -> None:
    checks = [{"name": PYTEST, "cmd": PYTEST}, {"name": CLEAN, "cmd": CLEAN}]
    assert clean_checks(checks) == ({"name": CLEAN, "cmd": CLEAN},)


def test_clean_checks_without_a_clean_entry_is_empty() -> None:
    no_flag = "python .github/check_clean.py ."
    assert clean_checks([{"name": PYTEST, "cmd": PYTEST}, {"name": no_flag, "cmd": no_flag}]) == ()


def test_exit_zero_is_no_finding() -> None:
    assert clean_finding(0, "clean: no personal reference or vendor named") is None


def test_exit_one_quotes_the_hit_and_states_the_fix() -> None:
    hit = f"src/graphs/tests/test_x.py:12: home-path: '{HOME}'"
    finding = clean_finding(1, f"names rule skipped\n{hit}\n")
    assert finding is not None
    assert hit in finding
    assert "names rule skipped" not in finding
    assert "tmp_path or a neutral constant" in finding


def test_exit_two_is_a_gate_that_did_not_run() -> None:
    assert clean_finding(2, "COXSWAIN_CLEAN_DENY is not set and --require-names was given") is None


def test_no_clean_entry_or_no_repo_is_no_finding() -> None:
    assert clean_gate_finding("diff", [{"name": PYTEST, "cmd": PYTEST}], "/nonexistent") is None
    assert clean_gate_finding("diff", [{"name": CLEAN, "cmd": CLEAN}], None) is None
