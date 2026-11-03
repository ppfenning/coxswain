import pytest
from test_release import _TAP_FORMULA, V0

from devtools import release

_PY_FORMULA = _TAP_FORMULA.replace("class Cox < Formula\n", 'class Cox < Formula\n  depends_on "python@3.13"\n', 1)
_SDIST_URL = f"https://x/coxswain-{V0}.tar.gz"
_SDIST_SHA = "b" * 64
_OLD = ("https://files/pyarrow-23.0.0.tar.gz", "7" * 64)
_NEW = ("https://files/pyarrow-24.0.0.tar.gz", "6" * 64)
_PSYCOPG = {release.PSYCOPG_SDIST: ("https://files/psycopg-3.3.6.tar.gz", "3" * 64),
            release.TOWPATH_MACOS_ARM: ("https://files/psycopg_binary-arm.whl", "4" * 64),
            release.TOWPATH_LINUX_INTEL: ("https://files/psycopg_binary-x86.whl", "5" * 64)}
_TOWPATH = {release.TOWPATH_MACOS_ARM: ("https://files/towpath-arm.tar.gz", "8" * 64),
            release.TOWPATH_LINUX_INTEL: ("https://files/towpath-x86.tar.gz", "9" * 64)}


def _bump(text=_PY_FORMULA, **assets):
    return release.bumped_formula_text(text, V0, _SDIST_URL, _SDIST_SHA, **assets)


def _is_subsequence(small, large):
    rest = iter(large)
    return all(line in rest for line in small)


def test_a_formula_lacking_the_block_gains_the_resource_dependency_install_env_and_doctor_test():
    text = _bump(pyarrow=_NEW)
    assert f'resource "pyarrow" do\n    url "{_NEW[0]}"\n    sha256 "{_NEW[1]}"\n  end\n' in text
    assert '  depends_on "apache-arrow"\n' in text
    assert 'ENV["ARROW_HOME"]' in text
    assert text.index('ENV["ARROW_HOME"]') < text.index("virtualenv_install_with_resources")
    assert 'assert_match "object_store", doctor' in text.split("  test do\n", 1)[1].split("\n  end\n", 1)[0]


def test_a_formula_already_carrying_the_block_is_rewritten_in_place_with_one_resource():
    first = _bump(pyarrow=_OLD)
    second = _bump(first, pyarrow=_NEW)
    assert second.count('resource "pyarrow" do') == 1
    assert _NEW[0] in second and _NEW[1] in second
    assert _OLD[0] not in second and _OLD[1] not in second
    assert second == first.replace(_OLD[0], _NEW[0]).replace(_OLD[1], _NEW[1])


def test_a_hand_edited_resource_keeps_its_extra_lines_and_changes_only_url_and_sha256():
    edited = _bump(pyarrow=_OLD).replace(
        f'    url "{_OLD[0]}"\n', f'    # pinned by hand\n    url "{_OLD[0]}", using: :nounzip\n    patch :DATA\n', 1)
    assert '    patch :DATA\n' in edited
    assert _bump(edited, pyarrow=_NEW) == edited.replace(_OLD[0], _NEW[0]).replace(_OLD[1], _NEW[1])


def test_the_psycopg_and_towpath_blocks_are_untouched():
    without = _bump(psycopg=_PSYCOPG, towpath=_TOWPATH).splitlines()
    with_arrow = _bump(psycopg=_PSYCOPG, towpath=_TOWPATH, pyarrow=_NEW).splitlines()
    assert _is_subsequence(without, with_arrow)
    added = [line for line in with_arrow if line not in without]
    assert added and not any("psycopg" in line or "towpath" in line for line in added)


def test_no_pyarrow_argument_leaves_the_formula_as_it_was():
    assert "pyarrow" not in _bump(psycopg=_PSYCOPG, towpath=_TOWPATH)


@pytest.mark.parametrize("pair", [("", "6" * 64), (_NEW[0], ""), (_NEW[0],)])
def test_a_malformed_pair_raises_instead_of_writing(pair):
    with pytest.raises(ValueError):
        _bump(pyarrow=pair)


def test_a_formula_without_def_install_raises():
    with pytest.raises(ValueError, match="def install"):
        _bump("class Cox < Formula\n  url \"u\"\n  sha256 \"s\"\nend\n", pyarrow=_NEW)


def test_a_formula_without_a_test_block_raises():
    with pytest.raises(ValueError, match="test do"):
        _bump(_PY_FORMULA.split("  test do")[0] + "end\n", pyarrow=_NEW)


def test_two_pyarrow_resources_raise_instead_of_rewriting_one():
    doubled = _bump(pyarrow=_OLD).replace("  def install", '  resource "pyarrow" do\n    url "u"\n    sha256 "s"\n  end\n\n  def install', 1)
    with pytest.raises(ValueError, match="2 pyarrow resources"):
        _bump(doubled, pyarrow=_NEW)
