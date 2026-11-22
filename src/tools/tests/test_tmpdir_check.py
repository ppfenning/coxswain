"""The doctor's `lane TMPDIR` rows, judged over mount tables in /proc/self/mountinfo's own format."""

from __future__ import annotations

from agent_tools import tmpdir_check
from agent_tools.tmpdir_check import UnitTmpdir

# A host like the E1 one: / on ext4, /tmp a tmpfs with user quotas, /var/tmp on the root disk, a big quota'd /srv.
MOUNTINFO = """\
22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
30 22 0:27 / /tmp rw,nosuid,nodev shared:5 - tmpfs tmpfs rw,size=8G,inode64,usrquota,usrquota_block_hardlimit=1073741824
31 22 259:3 / /srv rw,relatime shared:6 - xfs /dev/nvme0n1p3 rw,usrquota
32 22 259:4 / /data rw,relatime shared:7 - ext4 /dev/sdb1 rw,usrjquota=aquota.user,jqfmt=vfsv1
33 22 0:28 / /mnt/with\\040space rw shared:8 - tmpfs tmpfs rw
garbage line
"""
MOUNTS = tmpdir_check.parse_mountinfo(MOUNTINFO)
DEFAULT = "/var/tmp/coxswain-alice"


def _row(tmpdir: str, source: str | None = "unit", unit: str = "coxswain-lander.service") -> dict:
    [row] = tmpdir_check.rows([UnitTmpdir(unit, tmpdir, tmpdir, source)], MOUNTS, DEFAULT)
    return row


def test_the_mount_table_is_read_with_both_option_sets_and_escaped_points():
    assert [m.point for m in MOUNTS] == ["/", "/tmp", "/srv", "/data", "/mnt/with space"]
    assert MOUNTS[1].fstype == "tmpfs" and "usrquota" in MOUNTS[1].options and "nosuid" in MOUNTS[1].options
    assert tmpdir_check.mount_of("/var/tmp/coxswain-alice", MOUNTS).point == "/"
    assert tmpdir_check.mount_of("/tmp/x/y", MOUNTS).point == "/tmp"
    assert tmpdir_check.mount_of("/tmpfoo", MOUNTS).point == "/"
    assert tmpdir_check.quota_limit(MOUNTS[1].options) == 1024**3


def test_a_unit_with_no_tmpdir_on_a_quotad_tmpfs_warns_with_the_unit_and_the_upgrade_fix():
    row = _row("/tmp", source=None)
    assert row["ok"] and row["warn"] and row["check"] == "lane TMPDIR"
    assert row["detail"].startswith("coxswain-lander.service: /tmp is on /tmp (tmpfs, user quota 1.0 GB), which is RAM")
    assert f"fix: run `cox upgrade`, which sets TMPDIR={DEFAULT} in the unit" in row["detail"]


def test_any_tmpfs_warns_even_without_a_quota():
    row = _row("/mnt/with space/x", source="tmpdir.conf")
    assert row.get("warn") and "(tmpfs), which is RAM" in row["detail"]
    assert "fix: change or remove the drop-in tmpdir.conf" in row["detail"]


def test_a_disk_quota_under_the_floor_warns_and_one_whose_limit_is_unknown_says_how_to_check():
    small = tmpdir_check.parse_mountinfo("40 1 8:1 / /q rw - ext4 /dev/sdc1 rw,usrquota_block_hardlimit=5G\n")
    [row] = tmpdir_check.rows([UnitTmpdir("coxswain-chair.service", "/q/t", "/q/t", "unit")], small, DEFAULT)
    assert row.get("warn") and "user quota of 5.0 GB, under 20.0 GB" in row["detail"]
    assert "fix: set lane_tmpdir in the profile to a directory on disk" in row["detail"]
    assert "`quota -s`" in _row("/srv/scratch")["detail"] and "`quota -s`" in _row("/data/t")["detail"]


def test_an_on_disk_tmpdir_passes_and_names_where_it_came_from():
    assert _row(DEFAULT) == {"check": "lane TMPDIR", "ok": True,
                             "detail": f"coxswain-lander.service: {DEFAULT} (TMPDIR from unit)"}
    big = tmpdir_check.parse_mountinfo("40 1 8:1 / /q rw - ext4 /dev/sdc1 rw,usrquota_block_hardlimit=50G\n")
    [row] = tmpdir_check.rows([UnitTmpdir("u", "/q/t", "/q/t", None)], big, DEFAULT)
    assert "warn" not in row


def test_the_resolved_path_is_judged_so_a_symlink_into_tmp_is_caught():
    [row] = tmpdir_check.rows([UnitTmpdir("u", "/var/tmp/link", "/tmp/real", "unit")], MOUNTS, DEFAULT)
    assert row.get("warn") and "/var/tmp/link is on /tmp (tmpfs" in row["detail"]


def test_no_unit_or_no_mount_table_gives_one_passing_row():
    assert tmpdir_check.rows([], MOUNTS, DEFAULT) == [{"check": "lane TMPDIR", "ok": True, "detail": "no unit installed"}]
    [row] = tmpdir_check.rows([UnitTmpdir("u", "/tmp", "/tmp", None)], None, DEFAULT)
    assert row["ok"] and "warn" not in row and row["detail"].startswith("not checked")


def test_the_doctor_reads_each_installed_unit_and_its_drop_in_against_this_machines_mount_table(tmp_path, monkeypatch):
    """The edge over real files and this machine's /proc/self/mountinfo: the drop-in's TMPDIR is the one judged."""
    from agent_tools import cli_doctor

    monkeypatch.setenv("HOME", str(tmp_path))
    units = tmp_path / ".config/systemd/user"
    (units / "coxswain-lander.service.d").mkdir(parents=True)
    (units / "coxswain-lander.service").write_text("[Service]\nExecStart=/c lander run\n")
    (units / "coxswain-lander.service.d/tmpdir.conf").write_text(f"[Service]\nEnvironment=TMPDIR={tmp_path}\n")
    [row] = cli_doctor._doctor_tmpdir_rows({})
    assert row["check"] == "lane TMPDIR" and row["ok"]
    assert row["detail"].startswith("not checked") or (
        row["detail"].startswith(f"coxswain-lander.service: {tmp_path}") and "tmpdir.conf" in row["detail"])
