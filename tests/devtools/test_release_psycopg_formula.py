import pytest
from test_release import _TAP_FORMULA, V0

from devtools import release

_FORMULA = _TAP_FORMULA.replace("class Cox < Formula\n", 'class Cox < Formula\n  depends_on "python@3.13"\n', 1)
_REQUIREMENTS = ("mmh3==5.3.1 --hash=sha256:aa\n"
                 "psycopg==3.3.6 --hash=sha256:bb  # extras: postgres\n"
                 "psycopg-binary==3.3.6 ; implementation_name != 'pypy' --hash=sha256:cc\n"
                 "psycopg2-binary==2.9.13 --hash=sha256:dd\n")
_ARM = "https://files/psycopg_binary-3.3.6-cp313-cp313-macosx_11_0_arm64.whl"
_LINUX = "https://files/psycopg_binary-3.3.6-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
_MUSL = "https://files/psycopg_binary-3.3.6-cp313-cp313-musllinux_1_2_x86_64.whl"


def _file(kind: str, url: str, sha: str, tag: str = "cp313") -> dict:
    return {"packagetype": kind, "python_version": tag, "url": url, "digests": {"sha256": sha}}


def _sdist(url: str = "https://files/psycopg-3.3.6.tar.gz", sha: str = "1" * 64) -> dict:
    return {"urls": [_file("bdist_wheel", "https://files/psycopg-3.3.6-py3-none-any.whl", "9" * 64, "py3"),
                     _file("sdist", url, sha, "source")]}


def _binary(arm: str = _ARM, linux: str = _LINUX, tag: str = "cp313", shas: str = "23") -> dict:
    return {"urls": [_file("bdist_wheel", arm, shas[0] * 64, tag), _file("bdist_wheel", _MUSL, "8" * 64, tag),
                     _file("bdist_wheel", linux, shas[1] * 64, tag), _file("bdist_wheel", arm, "7" * 64, "cp312")]}


def _assets(**kw) -> dict:
    return release.psycopg_assets(_sdist(), _binary(**kw), "13")


def _bump(text: str = _FORMULA, **kw) -> str:
    return release.bumped_formula_text(text, V0, f"https://x/coxswain-{V0}.tar.gz", "b" * 64, **kw)


def test_psycopg_pins_reads_both_versions_and_ignores_psycopg2():
    assert release.psycopg_pins(_REQUIREMENTS) == ("3.3.6", "3.3.6")


@pytest.mark.parametrize("name", ["psycopg", "psycopg-binary"])
def test_psycopg_pins_raises_naming_the_missing_pin(name):
    text = "".join(line for line in _REQUIREMENTS.splitlines(keepends=True) if not line.startswith(f"{name}=="))
    with pytest.raises(ValueError, match=name):
        release.psycopg_pins(text)


def test_psycopg_assets_picks_the_sdist_and_the_cp_tagged_wheel_per_platform():
    assert _assets() == {release.PSYCOPG_SDIST: ("https://files/psycopg-3.3.6.tar.gz", "1" * 64),
                         release.TOWPATH_MACOS_ARM: (_ARM, "2" * 64), release.TOWPATH_LINUX_INTEL: (_LINUX, "3" * 64)}


@pytest.mark.parametrize("binary", [_binary(tag="cp312"), {"urls": []}, {"urls": _binary()["urls"][:1]},
                                    {"urls": [_file("bdist_wheel", _MUSL, "8" * 64), _file("bdist_wheel", _ARM, "2" * 64)]}])
def test_psycopg_assets_refuses_a_payload_without_a_matching_wheel(binary):
    with pytest.raises(ValueError, match="no cp313 wheel"):
        release.psycopg_assets(_sdist(), binary, "13")


def test_psycopg_assets_refuses_a_payload_without_an_sdist():
    with pytest.raises(ValueError, match="no sdist"):
        release.psycopg_assets({"urls": []}, _binary(), "13")


def test_a_formula_without_psycopg_gains_both_resources_the_stage_step_and_the_pip_exclusion():
    after = _bump(psycopg=_assets())
    assert after.count('resource "psycopg" do') == 1
    assert after.count('resource "psycopg-binary" do') == 2
    assert f'url "{_ARM}", using: :nounzip' in after and f'url "{_LINUX}", using: :nounzip' in after
    assert 'url "https://files/psycopg-3.3.6.tar.gz"' in after
    assert "    virtualenv_install_with_resources without: resources.map(&:name) & [\"psycopg-binary\"]\n" in after
    venv, stage = after.index("virtualenv_install_with_resources"), after.index('resource("psycopg-binary").stage')
    assert after.index("def install") < venv < stage < after.index("test do")
    assert 'Formula["python@3.13"].opt_bin/"python3.13"' in after
    assert after.index('resource "psycopg" do') > after.index('resource "dep" do')


def test_a_formula_with_psycopg_gets_new_urls_and_digests_without_duplicates():
    older = _bump(psycopg=_assets())
    newer_arm = _ARM.replace("3.3.6", "3.3.7")
    newer = _bump(older, psycopg=release.psycopg_assets(
        _sdist("https://files/psycopg-3.3.7.tar.gz", "4" * 64), _binary(arm=newer_arm, shas="56"), "13"))
    assert 'url "https://files/psycopg-3.3.7.tar.gz"' in newer and 'sha256 "' + "4" * 64 + '"' in newer
    assert f'url "{newer_arm}", using: :nounzip' in newer and 'sha256 "' + "5" * 64 + '"' in newer
    assert 'sha256 "' + "6" * 64 + '"' in newer
    assert _ARM not in newer and "psycopg-3.3.6.tar.gz" not in newer and "2" * 64 not in newer
    assert newer.count('resource "psycopg" do') == 1 and newer.count('resource "psycopg-binary" do') == 2
    assert newer.count("virtualenv_install_with_resources") == 1 and newer.count('.stage do') == 1


def test_a_second_run_over_its_own_output_changes_nothing():
    once = _bump(psycopg=_assets())
    assert _bump(once, psycopg=_assets()) == once


def test_without_psycopg_the_output_is_the_old_output():
    assert _bump() == release.bumped_formula_text(_FORMULA, V0, f"https://x/coxswain-{V0}.tar.gz", "b" * 64)


def test_a_formula_without_a_python_depends_on_raises():
    with pytest.raises(ValueError, match="python@3"):
        _bump(_TAP_FORMULA, psycopg=_assets())
    with pytest.raises(ValueError, match="python@3"):
        release._formula_python_minor(_TAP_FORMULA)


def test_the_python_minor_is_read_from_the_depends_on_line():
    assert release._formula_python_minor(_TAP_FORMULA.replace("class Cox", 'depends_on "python@3.12"\nclass Cox')) == "12"


@pytest.mark.parametrize(("old", "match"), [("  def install\n    virtualenv_install_with_resources\n  end\n", "def install"),
                                            ("virtualenv_install_with_resources", "virtualenv_install_with_resources")])
def test_a_formula_missing_an_anchor_raises(old, match):
    with pytest.raises(ValueError, match=match):
        _bump(_FORMULA.replace(old, "" if old.startswith("  def") else "pip_install_resources"), psycopg=_assets())


def test_assets_missing_a_platform_raise():
    with pytest.raises(ValueError, match=release.TOWPATH_LINUX_INTEL):
        _bump(psycopg={k: v for k, v in _assets().items() if k != release.TOWPATH_LINUX_INTEL})


def test_a_psycopg_binary_resource_under_unrecognised_scopes_raises():
    odd = _bump(psycopg=_assets()).replace("on_arm do", "on_intel do", 1)
    with pytest.raises(ValueError, match="unrecognised scopes"):
        _bump(odd, psycopg=_assets())


def test_towpath_and_psycopg_share_one_without_clause_and_stay_idempotent():
    towpath = {release.TOWPATH_MACOS_ARM: ("https://t/mac.tar.gz", "c" * 64), release.TOWPATH_LINUX_INTEL: ("https://t/lin.tar.gz", "d" * 64)}
    once = _bump(towpath=towpath, psycopg=_assets())
    assert once.count("virtualenv_install_with_resources") == 1
    assert 'virtualenv_install_with_resources without: resources.map(&:name) & ["towpath", "psycopg-binary"]\n' in once
    assert once.count('resource "towpath" do') == 2 and once.count('resource "psycopg-binary" do') == 2
    assert _bump(once, towpath=towpath, psycopg=_assets()) == once


def test_the_output_names_no_psycopg2():
    assert "psycopg2" not in _bump(psycopg=_assets())
