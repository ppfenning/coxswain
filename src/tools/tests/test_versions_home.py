from pathlib import Path

from agent_tools import cli
from agent_tools.home import resolve_here, write_home_record


def _manifest_text(tag: str) -> str:
    return (f'[coxswain]\nversion = "{tag}"\n\n'
            f'[components.dash]\nrepo = "org/dash"\ntag = "{tag}"\nrequired = false\nflag = "dash"\n\n'
            '[providers.claude-code]\nstatus = "supported"\n')


def _write_manifest(checkout: Path, tag: str) -> Path:
    checkout.mkdir(parents=True)
    path = checkout / "manifest.toml"
    path.write_text(_manifest_text(tag))
    return path


def _record_home(tmp_path, monkeypatch) -> Path:
    """Point HOME at tmp_path, record `tmp_path/h` as the coxswain home, and return its installs folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("COXSWAIN_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "AGENT_TOOLS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    locs = resolve_here(flag=str(tmp_path / "h"))
    write_home_record(locs)
    return Path(locs.installs)


def test_versions_from_an_unrelated_directory_reads_the_recorded_homes_checkout(tmp_path, monkeypatch, capsys):
    installs = _record_home(tmp_path, monkeypatch)
    _write_manifest(installs / "coxswain", "v2026.10.3")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rc = cli.main(["versions"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v2026.10.3" in out
    assert "dash" in out


def test_versions_manifest_flag_wins_over_the_home_checkout(tmp_path, monkeypatch, capsys):
    installs = _record_home(tmp_path, monkeypatch)
    _write_manifest(installs / "coxswain", "v2026.10.3")
    other = _write_manifest(tmp_path / "other" / "coxswain", "v1.2.3")
    rc = cli.main(["versions", "--manifest", str(other)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v1.2.3" in out and "v2026.10.3" not in out


def test_versions_root_flag_selects_the_manifest_under_that_root(tmp_path, monkeypatch, capsys):
    installs = _record_home(tmp_path, monkeypatch)
    _write_manifest(installs / "coxswain", "v2026.10.3")
    _write_manifest(tmp_path / "root" / "coxswain", "v1.2.3")
    rc = cli.main(["versions", "--root", str(tmp_path / "root")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v1.2.3" in out and "v2026.10.3" not in out


def test_versions_without_a_checkout_under_the_home_refuses_naming_that_path(tmp_path, monkeypatch, capsys):
    installs = _record_home(tmp_path, monkeypatch)
    rc = cli.main(["versions"])
    out = capsys.readouterr().out
    assert rc == 2
    assert f"no manifest at {installs}/coxswain/manifest.toml" in out
