"""The release refuses a component whose pinned tree names a person, a host or a home path.

The fixture trees are real git repositories, exported by the gate itself; only the
remote-facing git calls (tags, counts, fetch) are faked. Every personal string is
built at run time, so this file stays clean under the checker it exercises."""
import subprocess

import pytest

from devtools import cli, release_clean

_MANIFEST_TOML = """
[coxswain]
version = "0.1.0"

[components.harness]
repo = "org/harness"
tag = "v0.1.0"

[components.cartridges]
repo = "org/cartridges"
tag = "v0.1.0"
"""

HOME = "/" + "home" + "/" + "someone" + "/repos"
NAME = "Zebulon" + "quist"


@pytest.fixture(autouse=True)
def _clean_trees():
    """Overrides the suite-wide stub: the gate runs for real in this module."""


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(cli, "_maintainer_remote_url", lambda directory: "git@github.com:ppfenning/coxswain.git")
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [])
    monkeypatch.delenv("COXSWAIN_CLEAN_DENY", raising=False)


def _git(*argv, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *argv], cwd=cwd,
                   check=True, capture_output=True)


def _commit(path, files):
    for name, text in files.items():
        target = path / name
        if text is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "fixture", cwd=path)
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=path)


def _repo(path, files, tag=None):
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    _commit(path, files)
    if tag:
        _git("tag", tag, cwd=path)
    return path


def _fake_run(pinned=()):
    """Every component level with origin/main; one past its tag unless named in `pinned`."""
    def run(argv, cwd):
        if "--left-right" in argv:
            return (0, "0\t0\n")
        if "symbolic-ref" in argv:
            return (0, "refs/remotes/origin/main\n")
        if "rev-parse" in argv:
            return (0, "main\n")
        if "rev-list" in argv:
            return (0, "0\n" if any(argv[2].endswith(name) for name in pinned) else "2\n")
        return (0, "")
    return run


def _checkouts(tmp_path, harness, cartridges=None):
    _repo(tmp_path / "harness", harness, tag="v0.1.0")
    _repo(tmp_path / "cartridges", cartridges or {"README.md": "cartridges\n"}, tag="v0.1.0")
    _repo(tmp_path / "coxswain", {"README.md": "umbrella\n"})
    (tmp_path / "manifest.toml").write_text(_MANIFEST_TOML)


def _dry_run(tmp_path, monkeypatch, pinned=()):
    run = _fake_run(pinned)
    monkeypatch.setattr(cli, "_real_run", run)
    monkeypatch.setattr(cli, "_stdout_run", run)
    return cli.main(["release", "0.2.0", "--dry-run", "--manifest", str(tmp_path / "manifest.toml"),
                     "--root", str(tmp_path), "--umbrella", str(tmp_path / "coxswain")])


def test_a_dry_run_passes_clean_components(tmp_path, monkeypatch, capsys):
    _checkouts(tmp_path, {"README.md": "harness\n"})
    rc = _dry_run(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "refuse" not in out
    assert "names rule skipped: COXSWAIN_CLEAN_DENY is not set" in out


def test_a_dry_run_refuses_a_component_holding_a_home_path_naming_file_line_and_rule(tmp_path, monkeypatch, capsys):
    _checkouts(tmp_path, {"README.md": "harness\n", "src/setup.sh": f"cd ~\ncd {HOME}/harness\n"})
    rc = _dry_run(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 2
    assert "refuse harness: personal reference at origin/main: src/setup.sh:2 home-path" in out
    assert "refuse cartridges" not in out and "refuse coxswain" not in out


def test_a_pinned_component_is_checked_at_its_tag_not_at_main(tmp_path, monkeypatch, capsys):
    _checkouts(tmp_path, {"README.md": "harness\n"}, cartridges={"notes.md": f"see {HOME}\n"})
    _commit(tmp_path / "cartridges", {"notes.md": None, "README.md": "cleaned on main\n"})
    rc = _dry_run(tmp_path, monkeypatch, pinned=("cartridges",))
    out = capsys.readouterr().out
    assert rc == 2
    assert "pinned cartridges: v0.1.0" in out
    assert "refuse cartridges: personal reference at v0.1.0: notes.md:1 home-path" in out


def test_the_umbrella_itself_is_checked(tmp_path, monkeypatch, capsys):
    _checkouts(tmp_path, {"README.md": "harness\n"})
    _commit(tmp_path / "coxswain", {"docs/hosts.md": "store at host-a" + ".lan\n"})
    rc = _dry_run(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 2
    assert "refuse coxswain: personal reference at origin/main: docs/hosts.md:1 lan-host" in out


def test_the_names_rule_refuses_with_an_env_supplied_regex_and_never_prints_the_name(tmp_path, monkeypatch, capsys):
    _checkouts(tmp_path, {"README.md": f"thanks to {NAME}\n"})
    monkeypatch.setenv("COXSWAIN_CLEAN_DENY", NAME[:7] + ".*")
    rc = _dry_run(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 2
    assert "refuse harness: personal reference at origin/main: README.md:1 names" in out
    assert NAME not in out
    assert "names rule skipped" not in out


def test_unset_env_skips_the_names_rule(tmp_path, monkeypatch, capsys):
    _checkouts(tmp_path, {"README.md": f"thanks to {NAME}\n"})
    rc = _dry_run(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "names rule skipped: COXSWAIN_CLEAN_DENY is not set" in out


def test_an_unusable_regex_refuses(tmp_path, monkeypatch, capsys):
    _checkouts(tmp_path, {"README.md": "harness\n"})
    monkeypatch.setenv("COXSWAIN_CLEAN_DENY", "(")
    rc = _dry_run(tmp_path, monkeypatch)
    assert rc == 2
    assert "refuse clean: COXSWAIN_CLEAN_DENY is not a usable regex" in capsys.readouterr().out


def test_a_tree_git_cannot_export_is_refused_not_passed():
    steps = release_clean.refusals({"harness": ("v9.9.9", None), "cartridges": ("v0.1.0", [])})
    assert [s["component"] for s in steps] == ["harness"]
    assert "could not export v9.9.9" in steps[0]["detail"]


def test_targets_follow_the_plan_pinned_at_its_tag_tagged_and_umbrella_at_the_tip():
    steps = [{"kind": "pinned", "component": "crew", "tag": "v0.6.0"},
             {"kind": "tag", "component": "harness", "repo": "org/harness", "tag": "v0.2.0"},
             {"kind": "github_release", "component": "harness"},
             {"kind": "tag_self", "component": "coxswain", "tag": "v0.2.0"}]
    assert release_clean.clean_targets(steps) == [("crew", "v0.6.0"), ("harness", None), ("coxswain", None)]


def test_a_hit_location_drops_the_matched_text():
    assert release_clean.hit_location("a/b.md:12: names: 'Someone: x'") == "a/b.md:12 names"
