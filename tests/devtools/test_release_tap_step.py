from test_release import _TAP_FORMULA, V0, _dash_text, _fake_git_run

from devtools import cli, release

_INDEX = {"urls": [{"packagetype": "sdist", "url": f"https://x/coxswain-{V0}.tar.gz", "digests": {"sha256": "b" * 64}}]}
_URLS = release.towpath_asset_urls(V0)
_PY_FORMULA = _TAP_FORMULA.replace("class Cox < Formula\n", 'class Cox < Formula\n  depends_on "python@3.13"\n', 1)
_PINS = ("psycopg==3.3.6 --hash=sha256:aa\npsycopg-binary==3.3.6 ; implementation_name != 'pypy' --hash=sha256:bb\n"
         "pyarrow==24.0.0 --hash=sha256:cc\n")
_ARROW_URL = "https://pypi.org/pypi/pyarrow/24.0.0/json"
_ARROW_SDIST = "https://files/pyarrow-24.0.0.tar.gz"
_PYARROW = {"urls": [{"packagetype": "sdist", "url": _ARROW_SDIST, "digests": {"sha256": "6" * 64}}]}
_PSYCOPG_URL = "https://pypi.org/pypi/psycopg/3.3.6/json"
_BINARY_URL = "https://pypi.org/pypi/psycopg-binary/3.3.6/json"
_SDIST = "https://files/psycopg-3.3.6.tar.gz"
_PSYCOPG = {"urls": [{"packagetype": "sdist", "url": _SDIST, "digests": {"sha256": "3" * 64}}]}
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


def _index(url, pyarrow=_PYARROW, psycopg=_PSYCOPG):
    """The coxswain index, or the psycopg and pyarrow release documents at the pinned versions."""
    return {_PSYCOPG_URL: psycopg, _ARROW_URL: pyarrow}.get(url, _INDEX)


def _tap_clone(tmp_path, formula_text=_PY_FORMULA, pins_text=_PINS):
    """The tap clone with its formula, and the umbrella's extras pins."""
    formula = tmp_path / "homebrew-coxswain" / "Formula" / "cox.rb"
    formula.parent.mkdir(parents=True)
    formula.write_text(formula_text)
    pins = tmp_path / "src" / "tools" / "agent_tools" / "extras-requirements.txt"
    pins.parent.mkdir(parents=True)
    pins.write_text(pins_text)
    return formula


def _run(tmp_path, formula_text=_PY_FORMULA, fetch_text=_dash_text, fetch_index=_index, pins=_PINS):
    """The tap clone starts on `feature/x`, so a restore to it is observable."""
    formula = _tap_clone(tmp_path, formula_text, pins)
    directory = str(formula.parents[1])
    calls, fake_run = _fake_git_run(off_branch={directory})
    rc = cli._release_execute(_tap_steps(), V0, str(tmp_path), {}, str(tmp_path), fake_run, {}, "",
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__,
                              fetch_index=fetch_index, fetch_text=fetch_text)
    return rc, calls, fake_run, formula, directory


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
    unanchored = _PY_FORMULA.replace("  def install", "  def setup")
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

    formula = _tap_clone(tmp_path)
    _, fake_run = _fake_git_run(off_branch={str(formula.parents[1])})
    rc = cli._release_execute(_tap_steps("v0.42.0"), V0, str(tmp_path), {}, str(tmp_path), fake_run, {}, "",
                              sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__,
                              fetch_index=_index, fetch_text=fetch)
    assert rc == 0
    assert sorted(fetched) == sorted(f"{url}.sha256" for url in pinned.values())
    assert f'url "{pinned[release.TOWPATH_MACOS_ARM]}"' in formula.read_text()


def test_a_full_run_writes_pure_psycopg_on_libpq_with_the_fetched_sdist_and_never_asks_for_the_binary_document(tmp_path):
    asked = []
    rc, _, _, formula, _ = _run(tmp_path, fetch_index=lambda url: asked.append(url) or _index(url))
    text = formula.read_text()
    assert rc == 0
    assert f'resource "psycopg" do\n    url "{_SDIST}"\n    sha256 "{"3" * 64}"' in text
    assert 'depends_on "libpq"' in text and "psycopg-binary" not in text and "psycopg.pq.version()" in text
    assert all(f'(bin/"{name}").write_env_script' in text for name in ("cox", "agent-tools", "cartridge"))
    assert "DYLD_FALLBACK_LIBRARY_PATH" in text and "LD_LIBRARY_PATH" in text
    assert _PSYCOPG_URL in asked and not [url for url in asked if "psycopg-binary" in url]


def test_a_psycopg_payload_with_no_sdist_refuses_before_any_git_call(tmp_path, capsys):
    rc, calls, _, formula, _ = _run(tmp_path, fetch_index=lambda url: _index(url, psycopg={"urls": []}))
    assert rc == 2
    assert "FAILED tap_formula_pr tap: psycopg release lists no sdist" in capsys.readouterr().out
    assert calls == []
    assert formula.read_text() == _PY_FORMULA


def test_a_missing_psycopg_pin_refuses_before_any_git_call(tmp_path, capsys):
    rc, calls, _, _, _ = _run(tmp_path, pins="psycopg-binary==3.3.6\npyarrow==24.0.0\n")
    assert rc == 2
    assert "FAILED tap_formula_pr tap: psycopg: extras requirements pin no psycopg" in capsys.readouterr().out
    assert calls == []


def test_an_unreachable_index_for_psycopg_refuses_cleanly_before_any_git_call(tmp_path, capsys):
    def down(url):
        if url == _PSYCOPG_URL:
            raise OSError("HTTP Error 503")
        return _index(url)

    rc, calls, _, _, _ = _run(tmp_path, fetch_index=down)
    assert rc == 2
    assert "FAILED tap_formula_pr tap: psycopg: HTTP Error 503" in capsys.readouterr().out
    assert calls == []


def test_a_full_run_writes_the_pyarrow_resource_the_apache_arrow_dependency_and_the_object_store_assertion(tmp_path):
    rc, _, _, formula, _ = _run(tmp_path)
    text = formula.read_text()
    assert rc == 0
    assert f'resource "pyarrow" do\n    url "{_ARROW_SDIST}"\n    sha256 "{"6" * 64}"' in text
    assert 'depends_on "apache-arrow"' in text
    test_block = text.split("  test do\n", 1)[1]
    assert 'assert_match "object_store", doctor' in test_block.split("\n  end\n", 1)[0]


def test_a_pyarrow_payload_with_no_sdist_refuses_before_any_git_call(tmp_path, capsys):
    rc, calls, _, formula, _ = _run(tmp_path, fetch_index=lambda url: _index(url, pyarrow={"urls": []}))
    assert rc == 2
    assert "FAILED tap_formula_pr tap: pyarrow release lists no sdist" in capsys.readouterr().out
    assert calls == []
    assert formula.read_text() == _PY_FORMULA


def test_an_unreachable_index_for_pyarrow_refuses_cleanly_before_any_git_call(tmp_path, capsys):
    def down(url):
        if url == _ARROW_URL:
            raise OSError("HTTP Error 503")
        return _index(url)

    rc, calls, _, _, _ = _run(tmp_path, fetch_index=down)
    assert rc == 2
    assert "FAILED tap_formula_pr tap: pyarrow: HTTP Error 503" in capsys.readouterr().out
    assert calls == []


def test_a_missing_pyarrow_pin_refuses_before_any_git_call(tmp_path, capsys):
    rc, calls, _, _, _ = _run(tmp_path, pins=_PINS.split("pyarrow==")[0])
    assert rc == 2
    assert "FAILED tap_formula_pr tap: extras requirements pin no pyarrow" in capsys.readouterr().out
    assert calls == []


def test_a_base_formula_in_the_old_wheel_shape_is_migrated_by_the_run(tmp_path):
    towpath = {key: (url, "e" * 64) for key, url in _URLS.items()}
    bumped = release.bumped_formula_text(_PY_FORMULA, V0, f"https://x/coxswain-{V0}.tar.gz", "b" * 64, towpath=towpath)
    old = bumped.replace('["towpath"]', '["towpath", "psycopg-binary"]', 1)
    assert "psycopg-binary" in old
    rc, _, _, formula, _ = _run(tmp_path, formula_text=old)
    assert rc == 0
    assert "psycopg-binary" not in formula.read_text() and formula.read_text().count('depends_on "libpq"') == 1
