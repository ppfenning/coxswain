import pytest
from test_release import _TAP_FORMULA, V0

from devtools import release

_PY_FORMULA = _TAP_FORMULA.replace("class Cox < Formula\n", 'class Cox < Formula\n  depends_on "python@3.13"\n', 1)
_SDIST_URL = f"https://x/coxswain-{V0}.tar.gz"
_SDIST_SHA = "b" * 64
_LINE = "Traces are kept as JSON. Install the traces extra with pip for Parquet."
_PSYCOPG = {release.PSYCOPG_SDIST: ("https://files/psycopg-3.3.6.tar.gz", "3" * 64)}
_TOWPATH = {release.TOWPATH_MACOS_ARM: ("https://files/towpath-arm.tar.gz", "8" * 64),
            release.TOWPATH_LINUX_INTEL: ("https://files/towpath-x86.tar.gz", "9" * 64)}
_WITH_PYARROW = (_PY_FORMULA
                 .replace('  depends_on "python@3.13"\n', '  depends_on "apache-arrow"\n  depends_on "cmake" => :build\n  depends_on "python@3.13"\n', 1)
                 .replace("\n  def install\n", '\n  resource "pyarrow" do\n    url "https://files/pyarrow-23.0.0.tar.gz"\n'
                          f'    sha256 "{"7" * 64}"\n  end\n\n  def install\n', 1)
                 .replace("    virtualenv_install_with_resources\n",
                          '    ENV["ARROW_HOME"] = Formula["apache-arrow"].opt_prefix\n'
                          '    ENV["PYARROW_CMAKE_OPTIONS"] = "-DCMAKE_PREFIX_PATH=#{Formula["apache-arrow"].opt_prefix}"\n'
                          "    virtualenv_install_with_resources\n", 1)
                 .replace('"--version"\n  end\nend\n', '"--version"\n    assert_match "object_store", doctor\n  end\nend\n', 1))


def _bump(text=_PY_FORMULA, **assets):
    return release.bumped_formula_text(text, V0, _SDIST_URL, _SDIST_SHA, **assets)


def test_a_formula_with_the_old_pyarrow_pieces_comes_out_without_any_of_them():
    assert 'resource "pyarrow"' in _WITH_PYARROW and "cmake" in _WITH_PYARROW
    out = _bump(_WITH_PYARROW)
    assert "cmake" not in out and "pyarrow" not in out and "apache-arrow" not in out and "ARROW_HOME" not in out
    assert "doctor" not in out
    assert out == _bump()


def test_a_formula_that_never_had_pyarrow_gains_the_caveats_naming_json_traces_and_the_extra():
    out = _bump()
    assert out.count(_LINE) == 1 and out.count("def caveats") == 1
    assert out.index("def caveats") < out.index("  test do")


def test_a_second_run_over_its_own_output_changes_nothing():
    once = _bump(psycopg=_PSYCOPG, towpath=_TOWPATH)
    assert _bump(once, psycopg=_PSYCOPG, towpath=_TOWPATH) == once
    assert once.count(_LINE) == 1


def test_the_psycopg_and_towpath_rewrites_are_unchanged_apart_from_the_caveats():
    out = _bump(_WITH_PYARROW, psycopg=_PSYCOPG, towpath=_TOWPATH)
    plain = _bump(psycopg=_PSYCOPG, towpath=_TOWPATH)
    assert out == plain
    assert 'depends_on "libpq"' in out and out.count('resource "towpath"') == 2 and 'resource "psycopg" do' in out


def test_the_pyarrow_argument_is_gone():
    with pytest.raises(TypeError):
        _bump(pyarrow=("https://files/pyarrow-24.0.0.tar.gz", "6" * 64))


def test_a_formula_without_a_test_block_raises():
    with pytest.raises(ValueError, match="test do"):
        _bump(_PY_FORMULA.split("  test do")[0] + "end\n")
