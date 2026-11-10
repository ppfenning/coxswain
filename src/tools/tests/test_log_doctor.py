import errno
import os

import pytest

from agent_tools import log_doctor
from agent_tools.jsonl_log import read_log
from agent_tools.log_doctor import LOG_NAMES, check_logs, fix_logs, log_rows, plan_fix

GOOD = '{"a": 1}'
STRIPPABLE = '{"a"\x00: 1\x00}'
BROKEN = '{"a": \x00'


def test_all_null_line_is_moved_and_left_out():
    assert plan_fix(f"{GOOD}\n\x00\x00\x00\n") == (f"{GOOD}\n", ("\x00\x00\x00",))


def test_embedded_nuls_that_parse_stripped_stay_in_stripped_form():
    assert plan_fix(f"{GOOD}\n{STRIPPABLE}\n") == (f'{GOOD}\n{{"a": 1}}\n', (STRIPPABLE,))


def test_line_that_stays_unparseable_is_left_out():
    assert plan_fix(f"{GOOD}\n{BROKEN}\n{GOOD}\n") == (f"{GOOD}\n{GOOD}\n", (BROKEN,))


def test_clean_text_is_returned_unchanged():
    text = f'{GOOD}\r\n\n{{"b": 2}}'
    assert plan_fix(text) == (text, ())


def test_good_lines_keep_order_and_content_around_damage():
    assert plan_fix('{"z": 1}\n{"a": 2}\n[1]\n') == ('{"z": 1}\n{"a": 2}\n', ("[1]",))


def test_log_rows_one_row_per_log_in_order():
    rows = log_rows({"land.jsonl": 2, "chair.actions.jsonl": 1})
    assert [row["check"] for row in rows] == [f"log {name}" for name in LOG_NAMES]
    assert rows[0] == {"check": "log courier.jsonl", "ok": True, "detail": "no damaged lines"}
    assert rows[1] == {
        "check": "log land.jsonl",
        "ok": True,
        "warn": True,
        "detail": "2 damaged lines; run cox setup doctor --fix",
    }
    assert rows[2]["detail"] == "1 damaged line; run cox setup doctor --fix"


@pytest.fixture
def dirs(tmp_path):
    work, runs = tmp_path / "work", tmp_path / "runs"
    work.mkdir()
    runs.mkdir()
    return work, runs


def test_missing_logs_are_ok(dirs):
    assert all(row["ok"] and "warn" not in row for row in check_logs(*dirs))
    assert fix_logs(*dirs) == dict.fromkeys(LOG_NAMES, 0)


def test_check_reports_damage_per_log(dirs):
    work, runs = dirs
    (work / "courier.jsonl").write_bytes(f"{GOOD}\n{STRIPPABLE}\n".encode())
    rows = {row["check"]: row for row in check_logs(work, runs)}
    assert rows["log courier.jsonl"]["detail"] == "1 damaged line; run cox setup doctor --fix"
    assert "warn" not in rows["log land.jsonl"]


def test_clean_log_is_byte_for_byte_unchanged_with_no_damaged_file(dirs):
    work, runs = dirs
    clean = f'{GOOD}\r\n\n{{"b": 2}}'.encode()
    (runs / "land.jsonl").write_bytes(clean)
    assert fix_logs(work, runs)["land.jsonl"] == 0
    assert (runs / "land.jsonl").read_bytes() == clean
    assert not (runs / "land.jsonl.damaged").exists()


def test_damaged_file_holds_the_original_raw_lines_and_second_fix_adds_nothing(dirs):
    work, runs = dirs
    log = runs / "chair.actions.jsonl"
    log.write_bytes(f"{GOOD}\n\x00\x00\n{STRIPPABLE}\n{BROKEN}\n".encode())
    assert fix_logs(work, runs)["chair.actions.jsonl"] == 3
    damaged = runs / "chair.actions.jsonl.damaged"
    assert damaged.read_bytes() == f"\x00\x00\n{STRIPPABLE}\n{BROKEN}\n".encode()
    assert log.read_bytes() == f'{GOOD}\n{{"a": 1}}\n'.encode()
    before = damaged.read_bytes()
    assert fix_logs(work, runs) == dict.fromkeys(LOG_NAMES, 0)
    assert damaged.read_bytes() == before
    assert all("warn" not in row for row in check_logs(work, runs))


def test_invalid_utf8_in_a_good_line_survives_the_fix(dirs):
    work, runs = dirs
    log = work / "courier.jsonl"
    body = b'{"a": "\xff"}\n'
    log.write_bytes(body + b"[1]\n")
    assert fix_logs(work, runs)["courier.jsonl"] == 1
    assert log.read_bytes() == body


def test_failed_damaged_write_leaves_the_log_untouched(dirs):
    work, runs = dirs
    log = work / "courier.jsonl"
    original = f"{GOOD}\n{BROKEN}\n".encode()
    log.write_bytes(original)
    (work / "courier.jsonl.damaged").mkdir()
    with pytest.raises(OSError):
        fix_logs(work, runs)
    assert log.read_bytes() == original
    assert not (work / "courier.jsonl.fix.tmp").exists()


def _short_writes(monkeypatch, *, then_fail):
    real, calls = os.write, []

    def write(fd, data):
        calls.append(len(data))
        if then_fail and len(calls) > 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, bytes(data)[:3])

    monkeypatch.setattr(os, "write", write)
    return calls


def test_short_writes_still_put_every_damaged_line_in_the_damaged_file(dirs, monkeypatch):
    work, runs = dirs
    log = work / "courier.jsonl"
    log.write_bytes(f"{GOOD}\n{BROKEN}\n[1]\n".encode())
    calls = _short_writes(monkeypatch, then_fail=False)
    assert fix_logs(work, runs)["courier.jsonl"] == 2
    assert len(calls) > 1
    assert (work / "courier.jsonl.damaged").read_bytes() == f"{BROKEN}\n[1]\n".encode()
    assert log.read_bytes() == f"{GOOD}\n".encode()


def test_short_write_then_full_disk_leaves_the_log_untouched(dirs, monkeypatch):
    work, runs = dirs
    log = work / "courier.jsonl"
    original = f"{GOOD}\n{BROKEN}\n[1]\n".encode()
    log.write_bytes(original)
    _short_writes(monkeypatch, then_fail=True)
    with pytest.raises(OSError):
        fix_logs(work, runs)
    assert log.read_bytes() == original


def test_write_of_zero_bytes_is_an_error_and_the_log_is_untouched(dirs, monkeypatch):
    work, runs = dirs
    log = work / "courier.jsonl"
    original = f"{GOOD}\n[1]\n".encode()
    log.write_bytes(original)
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        fix_logs(work, runs)
    assert log.read_bytes() == original


def test_replacement_keeps_the_log_mode(dirs):
    work, runs = dirs
    log = work / "courier.jsonl"
    log.write_bytes(f"{GOOD}\n[1]\n".encode())
    log.chmod(0o600)
    fix_logs(work, runs)
    assert log.stat().st_mode & 0o777 == 0o600


def test_replacement_is_synced_before_the_rename(dirs, monkeypatch):
    work, runs = dirs
    (work / "courier.jsonl").write_bytes(f"{GOOD}\n[1]\n".encode())
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    fix_logs(work, runs)
    assert events == ["fsync", "fsync", "replace"]


def test_a_line_appended_during_the_fix_is_not_dropped(dirs, monkeypatch):
    work, runs = dirs
    log = work / "courier.jsonl"
    log.write_bytes(f"{GOOD}\n[1]\n".encode())
    real = log_doctor._append_damaged

    def append_then_race(path, damaged):
        real(path, damaged)
        with log.open("ab") as handle:
            handle.write(b'{"late": 1}\n')

    monkeypatch.setattr(log_doctor, "_append_damaged", append_then_race)
    with pytest.raises(OSError):
        fix_logs(work, runs)
    assert log.read_bytes() == f'{GOOD}\n[1]\n{{"late": 1}}\n'.encode()
    assert not (work / "courier.jsonl.fix.tmp").exists()


def test_a_crash_between_append_and_replace_duplicates_and_never_loses(dirs, monkeypatch):
    work, runs = dirs
    log = work / "courier.jsonl"
    log.write_bytes(f"{GOOD}\n[1]\n".encode())
    monkeypatch.setattr(log_doctor, "_replace", lambda *args: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        fix_logs(work, runs)
    monkeypatch.undo()
    assert fix_logs(work, runs)["courier.jsonl"] == 1
    assert (work / "courier.jsonl.damaged").read_bytes() == b"[1]\n[1]\n"


@pytest.mark.parametrize("body", [b'{"a": "\xff"}\n', b'{"a": \xff}\n', b"\xff\xfe\n", b'{"a": 1}\n[1]\n'])
def test_check_agrees_with_read_log_when_no_nul_is_present(dirs, body):
    work, runs = dirs
    (work / "courier.jsonl").write_bytes(body)
    skipped = read_log(work / "courier.jsonl").skipped
    assert check_logs(work, runs)[0]["ok"] is True
    assert ("warn" in check_logs(work, runs)[0]) == (skipped > 0)
