import pytest

from agent_tools.log_doctor import (
    CHAIR_ACTIONS,
    COURIER,
    LAND,
    check_logs,
    fix_logs,
    log_rows,
    plan_fix,
)


def test_all_null_line_is_moved_and_left_out():
    assert plan_fix('{"a": 1}\n\x00\x00\x00\n{"b": 2}\n') == ('{"a": 1}\n{"b": 2}\n', ("\x00\x00\x00",))


def test_embedded_nuls_that_parse_stripped_stay_stripped_and_are_returned_raw():
    assert plan_fix('{"a"\x00: 1\x00}\n') == ('{"a": 1}\n', ('{"a"\x00: 1\x00}',))


def test_line_that_stays_unparseable_is_left_out_and_returned_raw():
    assert plan_fix('{"a": 1}\n{"a": \x00\n') == ('{"a": 1}\n', ('{"a": \x00',))


def test_unparseable_line_without_nul_is_damaged():
    assert plan_fix('{"a": 1}\nnot json\n') == ('{"a": 1}\n', ("not json",))


def test_damaged_unterminated_tail_leaves_a_closed_log():
    assert plan_fix('{"a": 1}\nnot json') == ('{"a": 1}\n', ("not json",))


@pytest.mark.parametrize("text", ["", '{"a": 1}\n', '{"a": 1}', '{"a": 1}\r\n\n{"b": 2}\n'])
def test_clean_text_is_returned_unchanged(text):
    assert plan_fix(text) == (text, ())


def test_log_rows_ok_at_zero_and_warning_otherwise():
    assert log_rows({"land.jsonl": 0, "courier.jsonl": 3}) == [
        {"check": "land.jsonl", "ok": True, "detail": "ok"},
        {
            "check": "courier.jsonl",
            "ok": True,
            "warn": True,
            "detail": "3 damaged line(s): run cox setup doctor --fix",
        },
    ]


@pytest.fixture
def dirs(tmp_path):
    work, runs = tmp_path / "work", tmp_path / "runs"
    work.mkdir()
    runs.mkdir()
    return work, runs


def test_missing_logs_are_ok(dirs):
    assert [row["detail"] for row in check_logs(*dirs)] == ["ok", "ok", "ok"]
    assert fix_logs(*dirs) == {COURIER: 0, LAND: 0, CHAIR_ACTIONS: 0}
    assert sorted(p.name for d in dirs for p in d.iterdir()) == []


def test_check_counts_nul_lines_that_parse_stripped(dirs):
    work, runs = dirs
    (runs / LAND).write_bytes(b'{"a"\x00: 1}\n\x00\n')
    rows = {row["check"]: row for row in check_logs(work, runs)}
    assert rows[LAND]["detail"] == "2 damaged line(s): run cox setup doctor --fix"
    assert rows[COURIER]["detail"] == rows[CHAIR_ACTIONS]["detail"] == "ok"


def test_clean_log_is_byte_for_byte_unchanged_with_no_damaged_file(dirs):
    work, runs = dirs
    clean = b'{"a": 1}\r\n\n{"b": 2}\n'
    (work / COURIER).write_bytes(clean)
    assert fix_logs(work, runs) == {COURIER: 0, LAND: 0, CHAIR_ACTIONS: 0}
    assert (work / COURIER).read_bytes() == clean
    assert not (work / (COURIER + ".damaged")).exists()
    assert not (work / (COURIER + ".tmp")).exists()


def test_fix_moves_raw_lines_aside_and_a_second_fix_adds_nothing(dirs):
    work, runs = dirs
    (runs / CHAIR_ACTIONS).write_bytes(b'{"a": 1}\n\x00\x00\n{"b"\x00: 2}\n{"c": \x00\n{"d": 4}\n')
    assert fix_logs(work, runs) == {COURIER: 0, LAND: 0, CHAIR_ACTIONS: 3}
    assert (runs / CHAIR_ACTIONS).read_bytes() == b'{"a": 1}\n{"b": 2}\n{"d": 4}\n'
    aside = runs / (CHAIR_ACTIONS + ".damaged")
    original = b'\x00\x00\n{"b"\x00: 2}\n{"c": \x00\n'
    assert aside.read_bytes() == original
    assert fix_logs(work, runs) == {COURIER: 0, LAND: 0, CHAIR_ACTIONS: 0}
    assert aside.read_bytes() == original
    assert (runs / CHAIR_ACTIONS).read_bytes() == b'{"a": 1}\n{"b": 2}\n{"d": 4}\n'


def test_later_damage_appends_to_the_damaged_file(dirs):
    work, runs = dirs
    (work / COURIER).write_bytes(b"one bad\n")
    fix_logs(work, runs)
    (work / COURIER).write_bytes(b'{"a": 1}\ntwo bad\n')
    fix_logs(work, runs)
    assert (work / (COURIER + ".damaged")).read_bytes() == b"one bad\ntwo bad\n"


def test_failure_writing_damaged_file_leaves_the_log_untouched(dirs):
    work, runs = dirs
    original = b'{"a": 1}\nnot json\n'
    (work / COURIER).write_bytes(original)
    (work / (COURIER + ".damaged")).mkdir()
    with pytest.raises(OSError):
        fix_logs(work, runs)
    assert (work / COURIER).read_bytes() == original
    assert not (work / (COURIER + ".tmp")).exists()


def test_non_utf8_bytes_in_good_lines_survive_a_fix(dirs):
    work, runs = dirs
    (work / COURIER).write_bytes(b'{"a": "\xff"}\nnot json\n')
    fix_logs(work, runs)
    assert (work / COURIER).read_bytes() == b'{"a": "\xff"}\n'
