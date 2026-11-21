"""A worker's machine: the takeover rule, what is said of it, and how the machine is found."""
from pathlib import Path

import pytest

from harness import worker_identity as wi

NOW = "2026-11-21T10:00:00Z"
FRESH = "2026-11-21T09:59:30Z"
OLD = "2026-11-21T09:00:00Z"  # an hour: past STALE_S


def row(machine="laptop", state="up", beat=FRESH):
    return {"name": "devbox", "machine_id": machine, "state": state, "beat_at": beat}


@pytest.mark.parametrize(("found", "machine", "verdict"), [
    (None, "desk", "new"),
    (row(), "laptop", "same"),
    (row(beat=OLD), "laptop", "same"),
    (row(), "desk", "refused"),
    (row(machine=""), "desk", "refused"),  # an older build's row: maybe another machine, live
    (row(machine=""), "", "refused"),  # an unknown machine is never "the same" as one
    # An older build beats its row only when it registers: an hours-old beat on a '' row may be a live worker.
    (row(machine="", beat=OLD), "desk", "refused"),
    (row(machine="", beat="2000-01-01T00:00:00Z"), "desk", "refused"),
    (row(machine="", beat=""), "desk", "refused"),
    (row(machine="", state="stopping", beat=OLD), "desk", "stopped"),  # its own clean stop frees the name
    (row(state="stopping"), "desk", "stopped"),
    (row(beat=OLD), "desk", "stale"),
    (row(beat=""), "desk", "stale"),
])
def test_another_machine_s_live_row_is_refused_and_a_stopped_or_long_lapsed_one_is_taken(found, machine, verdict):
    assert wi.takeover(found, machine, NOW) == verdict


def test_a_row_is_stale_only_once_its_beat_is_stale_s_old():
    assert wi.takeover(row(beat="2026-11-21T09:45:01Z"), "desk", NOW) == "refused"
    assert wi.takeover(row(beat="2026-11-21T09:45:00Z"), "desk", NOW) == "stale"
    assert wi.STALE_S >= 10 * wi.BEAT_S, "a live worker misses many beats before it can look stale"


def test_what_is_said_names_the_other_machine_and_how_to_fix_it():
    refused = wi.takeover_note("devbox", "refused", row(), "desk")
    assert "devbox is registered and live from machine laptop" in refused and "--name" in refused
    assert wi.takeover_note("devbox", "stale", row(beat=OLD), "desk") == (
        f"worker: devbox takes over the row machine laptop left (its last beat was {OLD})")
    assert "it was stopped" in wi.takeover_note("devbox", "stopped", row(state="stopping"), "desk")
    assert "an unrecorded machine" in wi.takeover_note("devbox", "stopped", row(machine="", state="stopping"), "desk")
    legacy = wi.takeover_note("devbox", "refused", row(machine="", beat=OLD), "desk")
    assert "older build that records no machine" in legacy and "stop it cleanly" in legacy and "--name" in legacy
    assert wi.takeover_note("devbox", "same", row(), "laptop") == wi.takeover_note("devbox", "new", None, "desk") == ""


def test_the_macos_platform_uuid_is_read_from_ioreg_output():
    text = ('+-o J314sAP  <class IOPlatformExpertDevice>\n    {\n      "IOPlatformSerialNumber" = "C02X"\n'
            '      "IOPlatformUUID" = "4C4C4544-0042-3310-8051-B4C04F4E4A32"\n    }\n')
    assert wi.parse_ioreg(text) == "4C4C4544-0042-3310-8051-B4C04F4E4A32"
    assert wi.parse_ioreg("no such key") == ""


def test_the_first_id_that_says_anything_is_the_machine():
    assert wi.pick(["", "  \n", "abc123\n", "later"]) == "abc123"
    assert wi.pick([]) == ""


def test_with_no_machine_id_the_workspace_keeps_one_of_its_own(tmp_path: Path):
    first = wi.machine_id(tmp_path, platform="plan9")
    assert first.startswith("generated-") and (tmp_path / wi.FALLBACK_FILE).read_text().strip() == first
    assert wi.machine_id(tmp_path, platform="plan9") == first, "stable across restarts"
    assert wi.machine_id(tmp_path / "elsewhere", platform="plan9") != first


@pytest.mark.skipif(not Path("/etc/machine-id").is_file(), reason="this machine has no /etc/machine-id")
def test_on_linux_the_machine_id_is_systemd_s(tmp_path: Path):
    assert wi.machine_id(tmp_path, platform="linux") == Path("/etc/machine-id").read_text().strip()
    assert not (tmp_path / wi.FALLBACK_FILE).exists()
