import pytest

from devtools import release_arrow_formula as arrow

LINE = "Traces are kept as JSON. Install the traces extra with pip for Parquet."
CAVEATS = f"  def caveats\n    <<~EOS\n      {LINE}\n    EOS\n  end\n\n"

PLAIN = f'''class CoxTools < Formula
  include Language::Python::Virtualenv

  url "https://x/cox-tools-0.1.0.tar.gz"
  sha256 "{"0" * 64}"

  resource "pyyaml" do
    url "https://x/pyyaml-6.0.tar.gz"
    sha256 "{"a" * 64}"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    system bin/"cox", "--version"
  end
end
'''
EXPECTED = PLAIN.replace("  test do\n", CAVEATS + "  test do\n")

DOCTOR = "".join(f"    {line}\n" for line in (
    "# A bucket cannot be reached inside the Homebrew test sandbox, so this asserts that the object store client is",
    "# importable (pyarrow), not that the bucket is reachable.",
    r'(testpath/"provider.yaml").write("storage_url: postgresql://doctor@127.0.0.1:1/doctor\n'
    r'traces_url: s3://cox-doctor-probe/traces\nobject_store:\n  endpoint: https://127.0.0.1:9\n")',
    r'(testpath/"profile.yaml").write("provider_profile: #{testpath}/provider.yaml\nstore:\n  mode: shared\n")',
    'doctor = shell_output("#{bin}/cox setup doctor --profile #{testpath}/profile.yaml 2>&1", nil)',
    'assert_match "object_store", doctor',
    'refute_match "needs a client library that is not installed", doctor',
))
OLD = (PLAIN
       .replace("\n  url ", '\n  depends_on "apache-arrow"\n  depends_on "cmake" => :build\n\n  url ', 1)
       .replace("\n  def install\n", f'\n  resource "pyarrow" do\n    url "https://x/pyarrow-23.0.0.tar.gz"\n    sha256 "{"7" * 64}"\n  end\n\n  def install\n', 1)
       .replace("    virtualenv_install_with_resources\n",
                "    # pyarrow builds from its sdist against Homebrew's apache-arrow; a wheel would bundle libraries Homebrew cannot relink.\n"
                '    ENV["ARROW_HOME"] = Formula["apache-arrow"].opt_prefix\n'
                '    ENV["PYARROW_CMAKE_OPTIONS"] = "-DCMAKE_PREFIX_PATH=#{Formula["apache-arrow"].opt_prefix}"\n'
                "    virtualenv_install_with_resources\n", 1)
       .replace('    system bin/"cox", "--version"\n', '    system bin/"cox", "--version"\n' + DOCTOR, 1))


def test_pyarrow_pin_reads_the_pinned_version():
    assert arrow.pyarrow_pin("pyyaml==6.0\npyarrow==26.0.0\n") == "26.0.0"


def test_pyarrow_pin_names_the_missing_pin():
    with pytest.raises(ValueError, match="pyarrow"):
        arrow.pyarrow_pin("pyyaml==6.0\n")


def test_the_old_pyarrow_formula_loses_every_piece_and_gains_the_caveats():
    assert "apache-arrow" in OLD and "doctor" in OLD
    out = arrow.without_pyarrow(OLD)
    assert out == EXPECTED
    assert "cmake" not in out and 'resource "pyarrow"' not in out and "ARROW_HOME" not in out


def test_cmake_without_a_build_suffix_is_removed_too():
    out = arrow.without_pyarrow(OLD.replace('"cmake" => :build', '"cmake"'))
    assert out == EXPECTED


def test_a_formula_that_never_had_pyarrow_only_gains_the_caveats():
    out = arrow.without_pyarrow(PLAIN)
    assert out == EXPECTED
    assert "JSON" in out and "traces extra" in out and out.index("def caveats") < out.index("test do")


def test_a_second_pass_changes_nothing_and_mentions_the_caveats_once():
    once = arrow.without_pyarrow(OLD)
    assert arrow.without_pyarrow(once) == once
    assert once.count(LINE) == 1 and once.count("def caveats") == 1


def test_an_existing_caveats_block_gains_the_line_once():
    seeded = PLAIN.replace("  test do\n", "  def caveats\n    <<~EOS\n      Run cox setup first.\n    EOS\n  end\n\n  test do\n")
    out = arrow.without_pyarrow(seeded)
    assert out.count("def caveats") == 1 and out.count(LINE) == 1
    assert "      Run cox setup first.\n      " + LINE + "\n    EOS\n" in out
    assert arrow.without_pyarrow(out) == out


def test_a_user_assertion_in_the_test_block_stays():
    out = arrow.without_pyarrow(OLD.replace('    system bin/"cox", "--version"\n', '    system bin/"cox", "--version"\n    assert_match "x", "x"\n', 1))
    assert '    assert_match "x", "x"\n' in out and 'assert_match "object_store"' not in out


@pytest.mark.parametrize("text", [PLAIN.replace("  test do\n    system bin/\"cox\", \"--version\"\n  end\n", ""), "not a formula\n"])
def test_a_formula_with_no_test_block_raises(text):
    with pytest.raises(ValueError, match="test do"):
        arrow.without_pyarrow(text)


def test_a_caveats_block_without_a_heredoc_raises():
    seeded = PLAIN.replace("  test do\n", '  def caveats\n    "Run cox setup first."\n  end\n\n  test do\n')
    with pytest.raises(ValueError, match="heredoc"):
        arrow.without_pyarrow(seeded)


def test_an_unclosed_pyarrow_resource_raises():
    with pytest.raises(ValueError, match="closing"):
        arrow.without_pyarrow('class C < Formula\n  resource "pyarrow" do\n    url "u"\n')
