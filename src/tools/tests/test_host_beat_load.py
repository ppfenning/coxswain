import os

from agent_tools import host_cmd

MEMINFO = "MemTotal:        1000 kB\nMemFree:          100 kB\nMemAvailable:     250 kB\n"


def test_beat_load_record_carries_raw_load_and_cores(monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO, encoding="utf-8")
    monkeypatch.setattr(os, "getloadavg", lambda: (88.0, 70.0, 50.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 20)
    assert host_cmd._load_record(str(meminfo)) == {"cpu": 4.4, "mem": 0.75, "load_1m": 88.0, "cores": 20}


def test_beat_load_record_without_getloadavg_is_all_zeros(monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO, encoding="utf-8")

    def no_loadavg():
        raise OSError("unavailable")

    monkeypatch.setattr(os, "getloadavg", no_loadavg)
    assert host_cmd._load_record(str(meminfo)) == {"cpu": 0.0, "mem": 0.0, "load_1m": 0.0, "cores": 0}


def test_beat_load_record_without_meminfo_is_all_zeros(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "getloadavg", lambda: (88.0, 70.0, 50.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 20)
    assert host_cmd._load_record(str(tmp_path / "missing")) == {"cpu": 0.0, "mem": 0.0, "load_1m": 0.0, "cores": 0}
