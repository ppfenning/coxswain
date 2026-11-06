import math

from agent_tools import route


def _parse(text: str) -> dict:
    return route._parse_profile(text)[0]


def test_the_chair_block_takes_max_shared_file_initiatives_as_a_float():
    assert _parse("chair:\n  max_shared_file_initiatives: 3\n")["chair"] == {"max_shared_file_initiatives": 3.0}


def test_inf_and_nan_survive_parsing_so_the_resolver_must_reject_them():
    assert _parse("chair:\n  max_shared_file_initiatives: inf\n")["chair"] == {"max_shared_file_initiatives": math.inf}
    assert math.isnan(_parse("chair:\n  max_shared_file_initiatives: nan\n")["chair"]["max_shared_file_initiatives"])


def test_serial_files_parses_to_a_list_of_strings():
    assert _parse("serial_files: [Cargo.lock, src/app.rs]\n")["serial_files"] == ["Cargo.lock", "src/app.rs"]


def test_a_profile_without_serial_files_has_no_such_key():
    assert "serial_files" not in _parse("chair:\n  stale_days: 7\n")
