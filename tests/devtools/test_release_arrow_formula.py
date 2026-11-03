import pytest

from devtools import release_arrow_formula as arrow

URL = "https://x/pyarrow-26.0.0.tar.gz"
SHA = "p" * 64
MISSING_CLIENT = "needs a client library that is not installed"

HEAD = f'''class CoxTools < Formula
  include Language::Python::Virtualenv

  url "https://x/cox-tools-0.1.0.tar.gz"
  sha256 "{"0" * 64}"

  resource "pyyaml" do
    url "https://x/pyyaml-6.0.tar.gz"
    sha256 "{"a" * 64}"
  end
'''

TAIL = '''
  def install
    virtualenv_install_with_resources
  end

  test do
    system bin/"cox", "--version"
  end
end
'''

PLAIN = HEAD + TAIL
RESOURCE = arrow.pyarrow_resource(URL, SHA)


def _rendered() -> str:
    return arrow.with_doctor_test(arrow.with_arrow_block(PLAIN, RESOURCE))


def test_pyarrow_pin_reads_the_pinned_version():
    assert arrow.pyarrow_pin("pyyaml==6.0\npyarrow==26.0.0\n") == "26.0.0"


def test_pyarrow_pin_names_the_missing_pin():
    with pytest.raises(ValueError, match="pyarrow"):
        arrow.pyarrow_pin("pyyaml==6.0\n")


def test_pyarrow_resource_is_a_two_space_indented_stanza():
    assert f'  resource "pyarrow" do\n    url "{URL}"\n    sha256 "{SHA}"\n  end\n' == RESOURCE


def test_arrow_block_carries_the_resource_dependencies_and_install_env():
    out = arrow.with_arrow_block(PLAIN, RESOURCE)
    assert RESOURCE in out
    assert '  depends_on "apache-arrow"\n' in out
    assert '  depends_on "cmake" => :build\n' in out
    assert '    ENV["ARROW_HOME"] = Formula["apache-arrow"].opt_prefix\n' in out
    assert out.index('ENV["ARROW_HOME"]') < out.index("    virtualenv_install_with_resources")
    assert out.index(RESOURCE) < out.index("  def install")


def test_arrow_block_second_pass_changes_nothing():
    once = arrow.with_arrow_block(PLAIN, RESOURCE)
    assert arrow.with_arrow_block(once, RESOURCE) == once
    assert once.count('resource "pyarrow"') == 1
    assert once.count('depends_on "apache-arrow"') == 1


def test_arrow_block_keeps_an_existing_depends_on_group_together():
    text = PLAIN.replace("  url ", '  depends_on "python@3.13"\n\n  url ', 1)
    assert '  depends_on "python@3.13"\n  depends_on "apache-arrow"\n' in arrow.with_arrow_block(text, RESOURCE)


def test_arrow_block_accepts_a_venv_line_with_a_without_clause():
    text = PLAIN.replace("virtualenv_install_with_resources\n", 'virtualenv_install_with_resources without: ["x"]\n')
    out = arrow.with_arrow_block(text, RESOURCE)
    assert 'virtualenv_install_with_resources without: ["x"]\n' in out


def test_arrow_block_without_def_install_raises():
    with pytest.raises(ValueError, match="def install"):
        arrow.with_arrow_block(HEAD + "end\n", RESOURCE)


def test_arrow_block_without_the_venv_anchor_raises():
    with pytest.raises(ValueError, match="virtualenv_install_with_resources"):
        arrow.with_arrow_block(PLAIN.replace("virtualenv_install_with_resources", "pip_install"), RESOURCE)


def test_doctor_test_asserts_the_object_store_row_and_the_client():
    out = _rendered()
    assert '    assert_match "object_store", doctor\n' in out
    assert f'    refute_match "{MISSING_CLIENT}", doctor\n' in out
    assert "s3://cox-doctor-probe/traces" in out
    assert "mode: shared" in out
    assert "not that the bucket is reachable" in out


def test_doctor_test_lands_inside_the_test_block():
    out = _rendered()
    assert out.index('system bin/"cox", "--version"') < out.index("assert_match") < out.rindex("  end\nend")


def test_doctor_test_second_pass_changes_nothing():
    once = _rendered()
    assert arrow.with_doctor_test(once) == once
    assert once.count("assert_match") == 1


def test_doctor_test_leaves_an_existing_identical_line_alone():
    seeded = PLAIN.replace('  end\nend\n', '    assert_match "object_store", doctor\n  end\nend\n')
    assert arrow.with_doctor_test(seeded).count('assert_match "object_store", doctor') == 1


def test_doctor_test_without_a_test_block_raises():
    with pytest.raises(ValueError, match="test do"):
        arrow.with_doctor_test(HEAD + "end\n")
