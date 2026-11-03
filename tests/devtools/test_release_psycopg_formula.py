import pytest
from test_release import _TAP_FORMULA, V0

from devtools import release

_FORMULA = _TAP_FORMULA.replace("class Cox < Formula\n", 'class Cox < Formula\n  depends_on "python@3.13"\n', 1)
_REQUIREMENTS = ("mmh3==5.3.1 --hash=sha256:aa\n"
                 "psycopg==3.3.6 --hash=sha256:bb  # extras: postgres\n"
                 "psycopg-binary==3.3.6 ; implementation_name != 'pypy' --hash=sha256:cc\n"
                 "psycopg2-binary==2.9.13 --hash=sha256:dd\n")
_SDIST = "https://files/psycopg-3.3.6.tar.gz"
_TOWPATH = {release.TOWPATH_MACOS_ARM: ("https://t/mac.tar.gz", "c" * 64), release.TOWPATH_LINUX_INTEL: ("https://t/lin.tar.gz", "d" * 64)}
_WHEEL = ('  on_macos do\n    on_arm do\n      resource "psycopg-binary" do\n        url "https://files/psycopg_binary-3.3.5-arm64.whl", using: :nounzip\n'
          '        sha256 "' + "7" * 64 + '"\n      end\n    end\n  end\n\n'
          '  on_linux do\n    on_intel do\n      resource "psycopg-binary" do\n        url "https://files/psycopg_binary-3.3.5-x86_64.whl", using: :nounzip\n'
          '        sha256 "' + "8" * 64 + '"\n      end\n    end\n  end\n\n')
_STAGE = ('    if resources.map(&:name).include?("psycopg-binary")\n      resource("psycopg-binary").stage do\n'
          '        system Formula["python@3.13"].opt_bin/"python3.13", "-m", "pip", "--python=#{libexec}/bin/python",\n'
          '               "install", "--no-deps", "--no-index", Dir["*.whl"].first\n      end\n    end\n')
_OLD_SHAPE = (_FORMULA.replace("\n  def install\n", '\n  resource "psycopg" do\n    url "https://files/psycopg-3.3.5.tar.gz"\n'
                               '    sha256 "' + "6" * 64 + '"\n  end\n\n' + _WHEEL + "  def install\n", 1)
              .replace("    virtualenv_install_with_resources\n",
                       '    virtualenv_install_with_resources without: resources.map(&:name) & ["psycopg-binary"]\n' + _STAGE, 1))


def _sdist(url: str = _SDIST, sha: str = "1" * 64) -> dict:
    files = [{"packagetype": "bdist_wheel", "url": "https://files/psycopg-3.3.6-py3-none-any.whl", "digests": {"sha256": "9" * 64}},
             {"packagetype": "sdist", "url": url, "digests": {"sha256": sha}}]
    return {"urls": files}


def _assets(**kw) -> dict:
    return release.psycopg_assets(_sdist(**kw))


def _bump(text: str = _FORMULA, **kw) -> str:
    return release.bumped_formula_text(text, V0, f"https://x/coxswain-{V0}.tar.gz", "b" * 64, **kw)


def test_psycopg_pin_reads_the_psycopg_version_and_ignores_the_binary_and_psycopg2_lines():
    assert release.psycopg_pin(_REQUIREMENTS) == "3.3.6"
    assert release.psycopg_pin("psycopg-binary==3.3.7\npsycopg==3.3.6\n") == "3.3.6"


@pytest.mark.parametrize("text", ["", "psycopg-binary==3.3.6\npsycopg2-binary==2.9.13\n"])
def test_psycopg_pin_raises_naming_the_missing_pin(text):
    with pytest.raises(ValueError, match="pin no psycopg"):
        release.psycopg_pin(text)


def test_psycopg_assets_holds_only_the_sdist_pair():
    assert _assets() == {release.PSYCOPG_SDIST: (_SDIST, "1" * 64)}


def test_psycopg_assets_refuses_a_payload_without_an_sdist():
    with pytest.raises(ValueError, match="no sdist"):
        release.psycopg_assets({"urls": []})


def test_a_release_formula_has_libpq_env_scripts_for_all_three_entry_points_and_the_test_line():
    after = _bump(psycopg=_assets())
    assert after.count('depends_on "libpq"') == 1
    assert after.count('resource "psycopg" do') == 1 and f'url "{_SDIST}"' in after
    assert "psycopg-binary" not in after and "psycopg2" not in after
    for name in ("cox", "agent-tools", "cartridge"):
        assert f'(bin/"{name}").write_env_script libexec/"bin/{name}", libpq_env\n' in after
    assert "DYLD_FALLBACK_LIBRARY_PATH: " in after and "LD_LIBRARY_PATH: " in after
    assert "psycopg.pq.version()" in after.split("  test do\n", 1)[1]
    venv = after.index("virtualenv_install_with_resources")
    assert after.index("def install") < venv < after.index("write_env_script") < after.index("test do")
    assert after.index('resource "psycopg" do') > after.index('resource "dep" do')


def test_an_old_shape_formula_is_migrated_with_nothing_of_it_left_behind():
    assert "psycopg-binary" in _OLD_SHAPE
    after = _bump(_OLD_SHAPE, psycopg=_assets())
    assert "psycopg-binary" not in after and "on_arm" not in after and ".stage do" not in after and "pip" not in after
    assert "    virtualenv_install_with_resources\n" in after
    assert after.count('resource "psycopg" do') == 1 and "psycopg-3.3.5" not in after and "6" * 64 not in after
    assert after.count('depends_on "libpq"') == 1 and after.count("write_env_script") == 3
    assert "\n\n\n" not in after
    assert after == _bump(_FORMULA, psycopg=_assets())


def test_a_formula_with_psycopg_gets_a_new_url_and_digest_without_duplicates():
    newer = _bump(_bump(psycopg=_assets()), psycopg=_assets(url="https://files/psycopg-3.3.7.tar.gz", sha="4" * 64))
    assert 'url "https://files/psycopg-3.3.7.tar.gz"' in newer and 'sha256 "' + "4" * 64 + '"' in newer
    assert "psycopg-3.3.6.tar.gz" not in newer and "1" * 64 not in newer
    assert newer.count('resource "psycopg" do') == 1 and newer.count("depends_on") == 2
    assert newer.count("virtualenv_install_with_resources") == 1 and newer.count("write_env_script") == 3
    assert newer.count("psycopg.pq.version()") == 1


def test_a_second_run_over_its_own_output_changes_nothing():
    once = _bump(psycopg=_assets())
    assert _bump(once, psycopg=_assets()) == once
    assert _bump(_bump(_OLD_SHAPE, psycopg=_assets()), psycopg=_assets()) == _bump(_OLD_SHAPE, psycopg=_assets())


def test_without_psycopg_the_output_is_the_old_output():
    assert _bump() == release.bumped_formula_text(_FORMULA, V0, f"https://x/coxswain-{V0}.tar.gz", "b" * 64)


def test_a_formula_without_a_python_depends_on_still_renders():
    assert 'depends_on "libpq"' in _bump(_TAP_FORMULA, psycopg=_assets())


@pytest.mark.parametrize(("old", "new", "match"), [
    ("  def install\n    virtualenv_install_with_resources\n  end\n", "", "def install"),
    ("virtualenv_install_with_resources", "pip_install_resources", "virtualenv_install_with_resources"),
    ("  test do\n", "  check do\n", "test do")])
def test_a_formula_missing_an_anchor_raises_naming_it(old, new, match):
    with pytest.raises(ValueError, match=match):
        _bump(_FORMULA.replace(old, new), psycopg=_assets())


def test_assets_without_the_sdist_raise():
    with pytest.raises(ValueError, match=release.PSYCOPG_SDIST):
        _bump(psycopg={})


def test_towpath_keeps_its_without_clause_alone_and_the_run_stays_idempotent():
    once = _bump(towpath=_TOWPATH, psycopg=_assets())
    assert once.count("virtualenv_install_with_resources") == 1
    assert 'virtualenv_install_with_resources without: resources.map(&:name) & ["towpath"]\n' in once
    assert once.count('resource "towpath" do') == 2 and "psycopg-binary" not in once
    assert _bump(once, towpath=_TOWPATH, psycopg=_assets()) == once


def test_migrating_beside_towpath_leaves_only_towpath_in_the_without_clause():
    with_towpath = _bump(towpath=_TOWPATH)
    old = (with_towpath.replace("\n  def install\n", "\n" + _WHEEL + "  def install\n", 1)
           .replace('["towpath"]\n', '["towpath", "psycopg-binary"]\n' + _STAGE, 1))
    assert "psycopg-binary" in old
    after = _bump(old, towpath=_TOWPATH, psycopg=_assets())
    assert 'virtualenv_install_with_resources without: resources.map(&:name) & ["towpath"]\n' in after
    assert "psycopg-binary" not in after and after.count('resource "towpath" do') == 2
