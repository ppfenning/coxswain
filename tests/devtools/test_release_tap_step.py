from test_release import _TAP_FORMULA, _dash_text, _fake_git_run, _tap_kinds, _tools_manifest

from devtools import cli, release

_INDEX = {"urls": [{"packagetype": "sdist", "url": "https://x/cox-0.2.0.tar.gz", "digests": {"sha256": "b" * 64}}]}
_URLS = release.towpath_asset_urls("0.2.0")
_MACOS = _URLS[release.TOWPATH_MACOS_ARM]
_LINUX = _URLS[release.TOWPATH_LINUX_INTEL]


def test_towpath_asset_urls_name_both_dash_release_tarballs():
    assert _MACOS == "https://github.com/ppfenning/coxswain-dash/releases/download/v0.2.0/towpath-v0.2.0-aarch64-apple-darwin.tar.gz"
    assert _LINUX.endswith("/towpath-v0.2.0-x86_64-unknown-linux-gnu.tar.gz")


def test_sha256_from_asset_takes_the_first_token_only_when_it_is_64_hex():
    assert release.sha256_from_asset("a" * 64 + "  file.tar.gz\n") == "a" * 64
    assert release.sha256_from_asset("") is None
    assert release.sha256_from_asset("a" * 63 + "\n") is None
    assert release.sha256_from_asset("z" * 64) is None


def _run(tmp_path, formula_text=_TAP_FORMULA, fetch_text=_dash_text):
    """The tap clone starts on `feature/x`, so a restore to it is observable."""
    directory = tmp_path / "homebrew-coxswain"
    formula = directory / "Formula" / "cox.rb"
    formula.parent.mkdir(parents=True)
    formula.write_text(formula_text)
    steps, _ = _tap_kinds(_tools_manifest(), pinned_commits={"tools": 1})
    calls, fake_run = _fake_git_run(off_branch={str(directory)})
    rc = cli._release_execute(steps[-1:], "0.2.0", str(tmp_path), {}, str(tmp_path), fake_run, {}, "",
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__,
                              fetch_index=lambda url: _INDEX, fetch_text=fetch_text)
    return rc, calls, fake_run, formula, str(directory)


def test_a_full_asset_set_writes_both_towpath_urls_and_digests_beside_the_sdist(tmp_path):
    rc, _, _, formula, _ = _run(tmp_path, fetch_text=lambda url: ("1" if "darwin" in url else "2") * 64 + "  asset\n")
    text = formula.read_text()
    assert rc == 0
    assert f'url "{_MACOS}"\n        sha256 "{"1" * 64}"' in text
    assert f'url "{_LINUX}"\n        sha256 "{"2" * 64}"' in text
    assert 'url "https://x/cox-0.2.0.tar.gz"' in text and "b" * 64 in text


def test_a_missing_sha256_asset_refuses_naming_it_before_any_git_call(tmp_path, capsys):
    def fetch(url):
        if url == f"{_LINUX}.sha256":
            raise OSError("HTTP Error 404")
        return _dash_text(url)

    rc, calls, _, _, _ = _run(tmp_path, fetch_text=fetch)
    assert rc == 2
    assert f"FAILED tap_formula_pr tap: {_LINUX}.sha256 is missing" in capsys.readouterr().out
    assert calls == []


def test_an_empty_sha256_asset_refuses_naming_it_before_any_git_call(tmp_path, capsys):
    rc, calls, _, _, _ = _run(tmp_path, fetch_text=lambda url: "" if url == f"{_MACOS}.sha256" else _dash_text(url))
    assert rc == 2
    assert f"FAILED tap_formula_pr tap: {_MACOS}.sha256 is missing, empty or not a 64-hex digest" in capsys.readouterr().out
    assert calls == []


def test_a_formula_without_the_towpath_anchors_fails_cleanly_and_restores_the_branch(tmp_path, capsys):
    unanchored = _TAP_FORMULA.replace("  def install", "  def setup")
    rc, calls, fake_run, formula, directory = _run(tmp_path, formula_text=unanchored)
    out = capsys.readouterr().out
    assert rc == 2
    assert "FAILED tap_formula_pr tap: the formula could not be bumped: " in out
    assert release.checkout_branch_argv(directory, "release/0.2.0", "origin/main") in calls
    assert fake_run.current_branch[directory] == "feature/x"
    assert formula.read_text() == unanchored
    assert [c for c in calls if c[0] == "gh" or c[3:4] in (["push"], ["commit"])] == []
