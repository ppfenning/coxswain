"""The dash release binary wired through `cox install` and `cox upgrade`: facts in, binary step run, towpath row out."""

import shutil

import pytest

from agent_tools import cli

ASSET = ("https://github.com/ppfenning/coxswain-dash/releases/download/v2026.11.1/"
         "towpath-v2026.11.1-aarch64-apple-darwin.tar.gz")
MANIFEST = (
    '[coxswain]\nversion = "2026.11.1"\n\n'
    '[components.dash]\nrepo = "ppfenning/coxswain-dash"\ntag = "v2026.11.1"\nflag = "dash"\nprovides = "towpath"\n\n'
    '[providers.claude-code]\nstatus = "supported"\n'
)


def _host(monkeypatch, tmp_path, *, system="Darwin", machine="arm64", before="towpath 2026.11.0",
          after="towpath 2026.11.1", on_path=True, cargo=False):
    """Fake run and platform. `towpath --version` prints `before` until a `cp` runs, then `after`; None means it fails.
    `cargo` puts the Rust build tool on PATH."""
    bin_dir = tmp_path / "bin"
    state, calls = {"copied": False}, []

    def run(argv, cwd):
        calls.append(argv)
        if argv[-1] == "--version":
            shown = after if state["copied"] else before
            return (0, shown) if shown is not None else (127, "not found")
        state["copied"] = state["copied"] or argv[0] == "cp"
        return (0, "abc\trefs/tags/v2026.11.1\n") if "ls-remote" in argv else (0, "")

    monkeypatch.setattr(cli, "_real_run", run)
    monkeypatch.setattr(cli, "_load_lazy_modules", lambda: None)
    monkeypatch.setattr(cli.platform, "system", lambda: system)
    monkeypatch.setattr(cli.platform, "machine", lambda: machine)
    found = {"towpath": on_path, "cargo": cargo}
    monkeypatch.setattr(shutil, "which", lambda name: str(bin_dir / name) if found.get(name) else None)
    monkeypatch.setattr(cli, "_checkout_facts", lambda path, **_: {"present": True, "tag": "v2026.11.1", "dirty": False})
    return calls, bin_dir


def _install(tmp_path, capsys):
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(MANIFEST)
    rc = cli.main(["install", "--root", str(tmp_path / "root"), "--manifest", str(manifest), "--provider", "claude-code",
                   "--with", "dash", "--team", "example-team", "--workspace", str(tmp_path / "ws")])
    return rc, capsys.readouterr().out


def _towpath_row(out):
    return next(line for line in out.splitlines() if line.startswith("towpath")).split()


def test_install_with_dash_on_aarch64_macos_runs_the_asset_into_the_existing_bin_dir_and_towpath_is_ok(monkeypatch, tmp_path, capsys):
    calls, bin_dir = _host(monkeypatch, tmp_path)
    rc, out = _install(tmp_path, capsys)
    assert rc == 0
    assert any(argv[0] == "curl" and argv[-1] == ASSET for argv in calls)
    assert any(argv[0] == "cp" and argv[-1] == str(bin_dir) for argv in calls)
    assert "ok: binary dash" in out
    assert _towpath_row(out) == ["towpath", "v2026.11.1", "2026.11.1", "ok"]


def test_install_leaves_towpath_not_ok_and_exits_2_when_the_binary_after_install_is_off_the_pin(monkeypatch, tmp_path, capsys):
    _host(monkeypatch, tmp_path, after="towpath 2026.11.0")
    rc, out = _install(tmp_path, capsys)
    assert rc == 2
    assert _towpath_row(out) == ["towpath", "v2026.11.1", "2026.11.0", "drift"]


def test_install_on_a_host_with_no_asset_prints_the_detail_runs_no_download_and_towpath_is_missing(monkeypatch, tmp_path, capsys):
    calls, _ = _host(monkeypatch, tmp_path, system="Linux", machine="aarch64", before=None, after=None, on_path=False)
    rc, out = _install(tmp_path, capsys)
    assert rc == 2
    assert "skipped: skip dash" in out
    assert "no release asset for this machine; build towpath from source with cargo install --git" in out
    assert not any(argv[0] in ("curl", "tar", "cp") for argv in calls)
    assert _towpath_row(out)[-1] == "missing"


def _plain_upgrade(monkeypatch, tmp_path, capsys, dash_present=True, **host):
    calls, bin_dir = _host(monkeypatch, tmp_path, **host)
    if not dash_present:
        monkeypatch.setattr(cli, "_checkout_facts", lambda path, **_: {"present": path.name != "coxswain-dash", "tag": "v2026.11.1", "dirty": False})
    flag_manifest = tmp_path / "manifest.toml"
    flag_manifest.write_text(MANIFEST.replace("v2026.11.1", "v2026.11.0"))
    root = tmp_path / "root"
    (root / "coxswain").mkdir(parents=True)
    (root / "coxswain" / "manifest.toml").write_text(MANIFEST)
    rc = cli.main(["upgrade", "--root", str(root), "--manifest", str(flag_manifest), "--provider", "claude-code",
                   "--to", "2026.11.1"])
    return rc, capsys.readouterr().out, calls, bin_dir


def test_plain_upgrade_with_dash_present_runs_the_binary_step_ahead_of_the_doctor(monkeypatch, tmp_path, capsys):
    rc, out, calls, bin_dir = _plain_upgrade(monkeypatch, tmp_path, capsys)
    curl = next(at for at, argv in enumerate(calls) if argv[0] == "curl" and argv[-1] == ASSET)
    assert curl < calls.index(["cox", "setup", "doctor"])
    assert ["cp", "-f", f"{bin_dir}/.towpath-aarch64-apple-darwin.tmp/towpath",
            f"{bin_dir}/.towpath-aarch64-apple-darwin.tmp/coxtop", str(bin_dir)] in calls
    assert rc == 0
    assert _towpath_row(out) == ["towpath", "v2026.11.1", "2026.11.1", "ok"]


def test_plain_upgrade_exits_2_when_the_binary_stays_off_the_new_pin(monkeypatch, tmp_path, capsys):
    rc, out, _, _ = _plain_upgrade(monkeypatch, tmp_path, capsys, after="towpath 2026.11.0")
    assert rc == 2
    assert _towpath_row(out)[-1] == "drift"


STALE_LINE = "upgrade: towpath is at 2026.11.0 but the manifest pins v2026.11.1"
UNREADABLE_LINE = "upgrade: towpath is at unreadable but the manifest pins v2026.11.1"


def test_plain_upgrade_that_leaves_towpath_at_the_old_version_names_it_and_shows_a_drift_row(monkeypatch, tmp_path, capsys):
    rc, out, _, _ = _plain_upgrade(monkeypatch, tmp_path, capsys, after="towpath 2026.11.0")
    assert rc == 2
    assert STALE_LINE in out.splitlines()
    assert _towpath_row(out) == ["towpath", "v2026.11.1", "2026.11.0", "drift"]


def test_plain_upgrade_with_an_unreadable_towpath_version_still_prints_a_missing_row_and_exits_2(monkeypatch, tmp_path, capsys):
    rc, out, _, _ = _plain_upgrade(monkeypatch, tmp_path, capsys, before=None, after=None)
    assert rc == 2
    assert UNREADABLE_LINE in out.splitlines()
    assert _towpath_row(out) == ["towpath", "v2026.11.1", "missing"]


def test_plain_upgrade_that_matches_the_pin_exits_0_prints_no_naming_line_and_shows_an_ok_row(monkeypatch, tmp_path, capsys):
    rc, out, _, _ = _plain_upgrade(monkeypatch, tmp_path, capsys)
    assert rc == 0
    assert "upgrade: towpath is at" not in out
    assert _towpath_row(out) == ["towpath", "v2026.11.1", "2026.11.1", "ok"]


def test_plain_upgrade_without_the_optional_dash_component_exits_0_and_names_no_binary(monkeypatch, tmp_path, capsys):
    rc, out, calls, _ = _plain_upgrade(monkeypatch, tmp_path, capsys, dash_present=False, before=None, after=None, on_path=False)
    assert rc == 0
    assert "upgrade: towpath is at" not in out
    assert not any(argv[0] in ("curl", "tar", "cp") for argv in calls)


def test_binary_facts_take_bin_dir_from_the_command_on_path_and_version_from_its_output():
    manifest = {"components": {"dash": {"provides": "towpath"}}}

    def run(argv, cwd):
        return 0, "towpath v2026.11.1\n"

    facts = cli._binary_facts(manifest, run, "Darwin", "arm64", lambda name: "/opt/bin/towpath", "/users/dev")
    assert facts == {"target": "aarch64-apple-darwin", "bin_dir": "/opt/bin", "binaries": {"towpath": "2026.11.1"}}


@pytest.mark.parametrize("code, output", [(127, "towpath: not found"), (1, "boom")])
def test_binary_facts_fall_back_to_the_local_bin_and_report_none_for_an_absent_or_failing_command(code, output):
    manifest = {"components": {"dash": {"provides": "towpath"}}}
    ran = []
    facts = cli._binary_facts(manifest, lambda argv, cwd: (ran.append(argv), (code, output))[1], "Plan9", "mips",
                              lambda name: None, "/users/dev")
    assert facts == {"target": None, "bin_dir": "/users/dev/.local/bin", "binaries": {"towpath": None}}
    assert ran == [["/users/dev/.local/bin/towpath", "--version"]]


def _edge_dry_run(tmp_path, capsys):
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(MANIFEST)
    rc = cli.main(["install", "--edge", "--dry-run", "--root", str(tmp_path / "root"), "--manifest", str(manifest),
                   "--provider", "claude-code", "--with", "dash", "--team", "example-team",
                   "--workspace", str(tmp_path / "ws")])
    return rc, capsys.readouterr().out.splitlines()


def test_edge_dry_run_with_the_dash_checkout_and_cargo_prints_dev_build_and_dev_link(monkeypatch, tmp_path, capsys):
    _host(monkeypatch, tmp_path, cargo=True)
    rc, lines = _edge_dry_run(tmp_path, capsys)
    assert rc == 0
    assert any(line.startswith("dev_build dash:") for line in lines)
    assert any(line.startswith("dev_link dash:") for line in lines)
    assert not any(line.startswith("binary dash:") for line in lines)


def test_edge_dry_run_without_cargo_prints_the_binary_line_instead(monkeypatch, tmp_path, capsys):
    _host(monkeypatch, tmp_path, cargo=False)
    rc, lines = _edge_dry_run(tmp_path, capsys)
    assert rc == 0
    assert any(line.startswith("binary dash:") for line in lines)
    assert not any(line.startswith(("dev_build", "dev_link")) for line in lines)


def test_a_commit_suffixed_dev_version_never_fails_an_edge_install_but_does_fail_a_release_one():
    manifest = {"components": {"dash": {"provides": "towpath"}}}
    steps = [{"kind": "binary", "component": "dash"}]
    table = [("towpath", "v2026.11.1", "2026.11.1-3-gabc1234", "drift")]
    assert cli._binary_rows_not_ok(steps, manifest, {"channel": "edge"}, table) is False
    assert cli._binary_rows_not_ok(steps, manifest, {"channel": "release"}, table) is True
