from test_release import _TAP_FORMULA, V0, _dash_text, _fake_git_run

from devtools import cli, release

_INDEX = {"urls": [{"packagetype": "sdist", "url": f"https://x/coxswain-{V0}.tar.gz", "digests": {"sha256": "b" * 64}}]}
_URLS = release.towpath_asset_urls(V0)
_MACOS = _URLS[release.TOWPATH_MACOS_ARM]
_LINUX = _URLS[release.TOWPATH_LINUX_INTEL]


def test_towpath_asset_urls_name_both_dash_release_tarballs():
    assert f"https://github.com/ppfenning/coxswain-dash/releases/download/v{V0}/towpath-v{V0}-aarch64-apple-darwin.tar.gz" == _MACOS
    assert _LINUX.endswith(f"/towpath-v{V0}-x86_64-unknown-linux-gnu.tar.gz")


def test_sha256_from_asset_takes_the_first_token_only_when_it_is_64_hex():
    assert release.sha256_from_asset("a" * 64 + "  file.tar.gz\n") == "a" * 64
    assert release.sha256_from_asset("") is None
    assert release.sha256_from_asset("a" * 63 + "\n") is None
    assert release.sha256_from_asset("z" * 64) is None


def _tap_steps(dash_tag=None, dash_commits=0):
    """The plan's tap step for a release of V0, with dash pinned at `dash_tag` (no dash at all when None)."""
    components = {"dash": {"repo": "ppfenning/coxswain-dash", "tag": dash_tag}} if dash_tag else {}
    manifest = {"coxswain": {"version": V0}, "components": components}
    tags = {"coxswain": [], **{name: [] for name in components}}
    return release.release_plan(manifest, V0, tags, pinned_commits={"dash": dash_commits})[-1:]


def _run(tmp_path, formula_text=_TAP_FORMULA, fetch_text=_dash_text):
    """The tap clone starts on `feature/x`, so a restore to it is observable."""
    directory = tmp_path / "homebrew-coxswain"
    formula = directory / "Formula" / "cox.rb"
    formula.parent.mkdir(parents=True)
    formula.write_text(formula_text)
    calls, fake_run = _fake_git_run(off_branch={str(directory)})
    rc = cli._release_execute(_tap_steps(), V0, str(tmp_path), {}, str(tmp_path), fake_run, {}, "",
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__,
                              fetch_index=lambda url: _INDEX, fetch_text=fetch_text)
    return rc, calls, fake_run, formula, str(directory)


def test_a_full_asset_set_writes_both_towpath_urls_and_digests_beside_the_sdist(tmp_path):
    rc, _, _, formula, _ = _run(tmp_path, fetch_text=lambda url: ("1" if "darwin" in url else "2") * 64 + "  asset\n")
    text = formula.read_text()
    assert rc == 0
    assert f'url "{_MACOS}"\n        sha256 "{"1" * 64}"' in text
    assert f'url "{_LINUX}"\n        sha256 "{"2" * 64}"' in text
    assert f'url "https://x/coxswain-{V0}.tar.gz"' in text and "b" * 64 in text


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
    assert release.checkout_branch_argv(directory, f"release/{V0}", "origin/main") in calls
    assert fake_run.current_branch[directory] == "feature/x"
    assert formula.read_text() == unanchored
    assert [c for c in calls if c[0] == "gh" or c[3:4] in (["push"], ["commit"])] == []


def test_a_pinned_dash_points_the_tap_step_at_its_own_tag():
    assert _tap_steps("v0.42.0")[-1]["towpath_version"] == "0.42.0"


def test_a_dash_tagged_by_this_release_points_the_tap_step_at_the_release():
    assert _tap_steps("v0.42.0", dash_commits=2)[-1]["towpath_version"] == V0


def test_a_manifest_without_dash_points_the_tap_step_at_the_release():
    assert _tap_steps()[-1]["towpath_version"] == V0


def test_a_pinned_dash_fetches_and_writes_the_tarballs_of_its_own_release(tmp_path):
    pinned = release.towpath_asset_urls("0.42.0")
    fetched = []

    def fetch(url):
        fetched.append(url)
        return ("1" if "darwin" in url else "2") * 64 + "  asset\n"

    directory = tmp_path / "homebrew-coxswain"
    formula = directory / "Formula" / "cox.rb"
    formula.parent.mkdir(parents=True)
    formula.write_text(_TAP_FORMULA)
    _, fake_run = _fake_git_run(off_branch={str(directory)})
    rc = cli._release_execute(_tap_steps("v0.42.0"), V0, str(tmp_path), {}, str(tmp_path), fake_run, {}, "",
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__,
                              fetch_index=lambda url: _INDEX, fetch_text=fetch)
    assert rc == 0
    assert sorted(fetched) == sorted(f"{url}.sha256" for url in pinned.values())
    assert f'url "{pinned[release.TOWPATH_MACOS_ARM]}"' in formula.read_text()
